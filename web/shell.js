// The page at /web: starts Broadsheet's Python in a Web Worker and shows the app in a frame.
// The service worker (sw.js) sends every request the frame makes here, and we pass it to the worker.
(function () {
  "use strict";
  var statusEl = document.getElementById("status");
  var bar = document.getElementById("progress");
  var frame = document.getElementById("app");

  function say(text, pct) {
    statusEl.textContent = text;
    if (pct != null) bar.style.width = pct + "%";
  }
  function fail(text) {
    statusEl.textContent = text;
    statusEl.className = "error";
    document.getElementById("hint").hidden = true;
  }

  // An absolute link inside the app can land the frame on /web itself; send it to the app page it meant instead.
  if (window.top !== window) {
    location.replace((location.pathname.replace(/^\/web/, "") || "/") + location.search + location.hash);
    return;
  }
  if (!("serviceWorker" in navigator) || !window.Worker || !window.indexedDB || !window.WebAssembly) {
    fail("This browser can't run Broadsheet. Try a recent Chrome, Edge, Firefox or Safari, outside private browsing.");
    return;
  }

  var worker = null;
  var ready = false;
  var nextId = 1;
  var waiting = {};

  // Requests from the service worker. A tab that isn't running the app answers with nothing, so the service worker
  // tries the tab that is.
  navigator.serviceWorker.addEventListener("message", function (e) {
    if (!e.data || e.data.type !== "broadsheet-request") return;
    var port = e.ports[0];
    if (!ready) { port.postMessage({}); return; }
    var id = nextId++;
    waiting[id] = port;
    var msg = { type: "request", id: id, method: e.data.method, url: e.data.url, headers: e.data.headers, body: e.data.body };
    worker.postMessage(msg, msg.body ? [msg.body] : []);
  });
  navigator.serviceWorker.startMessages();

  function startWorker(build) {
    return new Promise(function (resolve, reject) {
      worker = new Worker("/web-files/worker.js");
      worker.onmessage = function (e) {
        var m = e.data;
        if (m.type === "progress") say(m.text, m.pct);
        else if (m.type === "ready") resolve();
        else if (m.type === "error") reject(new Error(m.text));
        else if (m.type === "response") {
          var port = waiting[m.id];
          delete waiting[m.id];
          if (port) port.postMessage({ status: m.status, headers: m.headers, body: m.body }, m.body ? [m.body] : []);
        }
      };
      worker.onerror = function (e) { reject(new Error(e.message || "The app stopped unexpectedly.")); };
      worker.postMessage({ type: "start", build: build, base: location.origin + "/web" });
    });
  }

  function appPath() {
    return (location.pathname.replace(/^\/web/, "") || "/") + location.search + location.hash;
  }

  // Keep the address bar on /web/<page>, so reloading or bookmarking returns to the same page.
  frame.addEventListener("load", function () {
    try {
      var loc = frame.contentWindow.location;
      if (loc.origin !== location.origin) return;
      history.replaceState(null, "", "/web" + (loc.pathname === "/" ? "" : loc.pathname) + loc.search + loc.hash);
      document.title = frame.contentDocument.title || "Broadsheet";
    } catch (e) { /* not ours */ }
  });

  async function serviceWorkerReady() {
    await navigator.serviceWorker.register("/web-files/sw.js", { scope: "/", updateViaCache: "none" });
    if (navigator.serviceWorker.controller) return;
    await new Promise(function (resolve) {
      navigator.serviceWorker.addEventListener("controllerchange", resolve, { once: true });
    });
  }

  async function run() {
    say("Starting…", 3);
    await serviceWorkerReady();
    var build = await (await fetch("/web-files/build.json", { cache: "no-cache" })).json();
    if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(function () {});
    await startWorker(build);
    ready = true;
    say("Ready", 100);
    frame.src = appPath();
    frame.hidden = false;
    document.getElementById("boot").hidden = true;
  }

  function guarded() {
    return run().catch(function (e) {
      console.error(e);
      fail("Broadsheet couldn't start: " + e.message + " Reload the page to try again.");
    });
  }

  // One tab at a time: two copies of the app writing to the same storage would overwrite each other.
  if (navigator.locks) {
    navigator.locks.request("broadsheet-browser", { ifAvailable: true }, function (lock) {
      if (!lock) {
        fail("Broadsheet is already open in another tab. Close that tab, then reload this one.");
        return;
      }
      return guarded().then(function () { return new Promise(function () {}); });  // hold the lock while open
    });
  } else {
    guarded();
  }
})();
