// Broadsheet's Python, running in the browser with Pyodide. Started by shell.js; answers the app's requests.
// /data (accounts, tokens, cached posts, feeds) lives in this browser's IndexedDB and nowhere else.
"use strict";

let py = null;
let handle = null;
let syncing = false;
let syncAgain = false;
let syncTimer = null;

function progress(text, pct) { self.postMessage({ type: "progress", text, pct }); }

function syncfs(populate) {
  return new Promise((resolve, reject) => py.FS.syncfs(populate, (err) => (err ? reject(err) : resolve())));
}

// Write /data back to IndexedDB shortly after the app touches it, one write at a time.
function persistSoon() {
  clearTimeout(syncTimer);
  syncTimer = setTimeout(async () => {
    if (syncing) { syncAgain = true; return; }
    syncing = true;
    try { await syncfs(false); } catch (e) { console.warn("saving to IndexedDB failed", e); }
    syncing = false;
    if (syncAgain) { syncAgain = false; persistSoon(); }
  }, 400);
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
