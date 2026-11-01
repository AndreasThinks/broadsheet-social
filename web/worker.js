// Broadsheet's Python, running in the browser with Pyodide. Started by shell.js; answers the app's requests.
// /data (accounts, tokens, cached posts, feeds) lives in this browser's IndexedDB and nowhere else.
"use strict";

let py = null;
let handle = null;
let syncTimer = null;
let current = Promise.resolve();  // the IndexedDB write in progress, if any

function progress(text, pct) { self.postMessage({ type: "progress", text, pct }); }

function syncfs(populate) {
  return new Promise((resolve, reject) => py.FS.syncfs(populate, (err) => (err ? reject(err) : resolve())));
}

// Write /data back to IndexedDB, one write at a time. Resolves once everything written so far is saved.
function persistNow() {
  clearTimeout(syncTimer);
  current = current.then(() => syncfs(false)).catch((e) => console.warn("saving to IndexedDB failed", e));
  return current;
}

// Write /data back shortly after the app touches it, so a burst of requests is saved once.
function persistSoon() {
  clearTimeout(syncTimer);
  syncTimer = setTimeout(persistNow, 400);
}

// Whether this answer sends the browser away from Broadsheet (to a sign-in page). The tab is unloaded straight after,
// which ends this worker, so anything not yet in IndexedDB (such as the sign-in's state) would be lost.
function leavesApp(url, status, headers) {
  const get = (name) => (headers.find(([k]) => k.toLowerCase() === name) || [])[1];
  const target = status >= 300 && status < 400 ? get("location") : get("hx-redirect");
  if (!target) return false;
  try { return new URL(target, url).origin !== new URL(url).origin; } catch (e) { return false; }
}

async function start(build, base) {
  const indexURL = `https://cdn.jsdelivr.net/pyodide/v${build.pyodide}/full/`;
  progress("Downloading Python…", 8);
  importScripts(indexURL + "pyodide.js");
  py = await loadPyodide({ indexURL });
  progress("Loading libraries…", 30);
  await py.loadPackage(build.packages, { messageCallback: () => {} });
  await py.loadPackage(build.wheels.map((w) => new URL(`/web-files/wheels/${w}`, self.location.href).href),
                       { messageCallback: () => {} });
  progress("Opening your saved data…", 60);
  py.FS.mkdirTree("/data");
  py.FS.mount(py.FS.filesystems.IDBFS, {}, "/data");
  await syncfs(true);
  progress("Loading Broadsheet…", 70);
  const bundle = await fetch(`/web-files/${build.bundle}`);
  if (!bundle.ok) throw new Error(`Couldn't download the app (${bundle.status}).`);
  py.unpackArchive(await bundle.arrayBuffer(), "zip", { extractDir: "/app" });
  progress("Starting Broadsheet…", 85);
  py.runPython(`
import sys
sys.path.insert(0, "/app")
from crossposter import browser
browser.boot(${JSON.stringify(base)})

from js import Object
from pyodide.ffi import to_js

async def _js_handle(method, url, headers, body):
    status, out_headers, out = await browser.handle(
        method, url, [tuple(h) for h in headers.to_py()], body.to_bytes() if body is not None else None)
    return to_js({"status": status, "headers": [list(h) for h in out_headers], "body": out},
                 dict_converter=Object.fromEntries)
`);
  handle = py.globals.get("_js_handle");
  await syncfs(false);  // keep the generated secret and starter feeds
}

async function answer(m) {
  let res;
  try {
    const out = await handle(m.method, m.url, m.headers, m.body ? new Uint8Array(m.body) : undefined);
    const body = out.body.slice().buffer;
    res = { status: out.status, headers: out.headers, body };
    if (leavesApp(m.url, res.status, res.headers)) await persistNow();
  } catch (e) {
    console.error(e);
    const text = `Something went wrong inside Broadsheet.\n\n${e.message || e}`;
    res = { status: 500, headers: [["content-type", "text/plain; charset=utf-8"]], body: new TextEncoder().encode(text).buffer };
  }
  self.postMessage({ type: "response", id: m.id, ...res }, [res.body]);
  persistSoon();
}

self.onmessage = async (e) => {
  const m = e.data;
  if (m.type === "start") {
    try {
      await start(m.build, m.base);
      self.postMessage({ type: "ready" });
    } catch (err) {
      console.error(err);
      self.postMessage({ type: "error", text: String(err.message || err).split("\n").slice(-2).join(" ") });
    }
  } else if (m.type === "request") {
    answer(m);
  }
};
