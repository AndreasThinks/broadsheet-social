"""Checks for feeds shared with the community directory. Standard library only: the directory server uses this too.

Nothing here runs the feed. It reads the source with `ast`, so a feed is never executed before someone has read it.
Errors block a submission; warnings are shown to the author before they send it, and to the reviewer.
These checks are a first pass, not a sandbox: a maintainer still reads every feed before it is listed.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

MAX_BYTES = 20_000

ALLOWED_IMPORTS = {"crossposter.feeds", "math", "collections", "re", "statistics", "datetime", "itertools",
                   "functools", "typing", "dataclasses", "__future__", "time", "string", "operator", "bisect", "heapq"}
BANNED_NAMES = {"open", "eval", "exec", "compile", "__import__", "globals", "locals", "vars", "getattr", "setattr",
                "delattr", "input", "breakpoint", "exit", "quit", "memoryview", "__builtins__", "help"}

_DID = re.compile(r"did:(plc|web):[A-Za-z0-9._:%-]+")
_HANDLE = re.compile(r"(?<![\w.])@?[a-z0-9-]+(\.[a-z0-9-]+)*\.(bsky\.social|bsky\.team)\b|"
                     r"(?<![\w.])@[A-Za-z0-9_]+@[a-z0-9-]+(\.[a-z0-9-]+)+\b", re.I)


@dataclass
class Item:
    level: str          # "error" or "warning"
    text: str
    line: int = 0


@dataclass
class Result:
    items: list[Item] = field(default_factory=list)
    name: str = ""
    description: str = ""
    sources: list[str] = field(default_factory=list)
    lines: int = 0

    @property
    def ok(self) -> bool:
        return not any(i.level == "error" for i in self.items)

    @property
    def errors(self) -> list[Item]:
        return [i for i in self.items if i.level == "error"]

    @property
    def warnings(self) -> list[Item]:
        return [i for i in self.items if i.level == "warning"]

    def as_dict(self) -> dict:
        return {"ok": self.ok, "name": self.name, "description": self.description, "sources": self.sources,
                "lines": self.lines, "items": [i.__dict__ for i in self.items]}


def _literal(node):
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


def check(source: str) -> Result:
    r = Result(lines=len(source.splitlines()))
    if len(source.encode()) > MAX_BYTES:
        r.items.append(Item("error", f"The file is over {MAX_BYTES // 1000} KB. Feeds should be short."))
        return r
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        r.items.append(Item("error", f"It doesn't parse: {e.msg}", e.lineno or 0))
        return r

    has_score = False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "score":
            has_score = True
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "feed" for t in node.targets):
            call = node.value
            if isinstance(call, ast.Call) and getattr(call.func, "id", "") == "Feed":
                for kw in call.keywords:
                    v = _literal(kw.value)
                    if kw.arg == "name" and isinstance(v, str):
                        r.name = v
                    elif kw.arg == "description" and isinstance(v, str):
                        r.description = v
                    elif kw.arg == "sources" and isinstance(v, list):
                        r.sources = [s for s in v if isinstance(s, str)]
    if not r.name:
        r.items.append(Item("error", 'It needs feed = Feed(name="...", sources=[...]) with a plain text name.'))
    if not has_score:
        r.items.append(Item("error", "It needs a score(post, ctx) function."))

    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in ALLOWED_IMPORTS:
                    r.items.append(Item("error", f"Imports {a.name}, which shared feeds can't use.", line))
        elif isinstance(node, ast.ImportFrom):
            mod = ("." * node.level) + (node.module or "")
            if mod not in ALLOWED_IMPORTS:
                r.items.append(Item("error", f"Imports from {mod}, which shared feeds can't use.", line))
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            r.items.append(Item("error", f"Uses {node.id}(), which shared feeds can't use.", line))
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            r.items.append(Item("error", f"Reaches into {node.attr}, which shared feeds can't do.", line))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            s = node.value
            if s.startswith(("bluesky:list:", "mastodon:list:")):
                r.items.append(Item("warning", "Points at one of your own lists. Others won't have it, and it shows "
                                               "people what's on it. Swap it for Following or Home.", line))
            elif _DID.search(s) and "app.bsky.feed.generator" not in s:
                r.items.append(Item("warning", "Contains an account ID (did:...). Remove it unless it's meant to be public.", line))
            elif _HANDLE.search(s):
                r.items.append(Item("warning", "Contains what looks like someone's handle. Remove it unless it's meant to be public.",
                                    line))
            if s == "x:home":
                r.items.append(Item("warning", "Reads X Home, which costs money for anyone who has X switched on.", line))
    # one message per problem and line is plenty
    seen, items = set(), []
    for i in r.items:
        if (i.text, i.line) not in seen:
            seen.add((i.text, i.line))
            items.append(i)
    r.items = items
    return r
