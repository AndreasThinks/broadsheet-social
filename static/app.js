// Cross-poster client: service worker, video, composer counters, push sign-up.
(function () {
  "use strict";

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function (e) { console.warn("sw", e); });
  }

  // ---- Bluesky videos are HLS streams; Safari plays them natively, others need hls.js ----
  var hlsLoading = null;
  function loadHls() {
    if (!hlsLoading) {
      hlsLoading = new Promise(function (resolve, reject) {
        var s = document.createElement("script");
        s.src = "https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js";
        s.onload = function () { resolve(window.Hls); };
        s.onerror = reject;
        document.head.appendChild(s);
      });
    }
    return hlsLoading;
  }
  function setupVideos(root) {
    root.querySelectorAll("video.hls:not([data-ready])").forEach(function (v) {
      v.dataset.ready = "1";
      var src = v.dataset.src;
      if (v.canPlayType("application/vnd.apple.mpegurl")) { v.src = src; return; }
      v.addEventListener("play", function once() {
        v.removeEventListener("play", once);
        loadHls().then(function (Hls) {
          if (Hls && Hls.isSupported()) { var h = new Hls(); h.loadSource(src); h.attachMedia(v); v.play(); }
        });
      });
    });
  }

  // ---- Composer: per-network character counts and alt-text fields ----
  var URL_RE = /https?:\/\/[^\s<>"')\]]*[^\s<>"')\].,;:!?]/g;
  function graphemes(s) {
    if (window.Intl && Intl.Segmenter) { var n = 0; for (var _ of new Intl.Segmenter().segment(s)) n++; return n; }
    return Array.from(s).length;
  }
  function lengthFor(network, text) {
    if (network === "mastodon" || network === "x") text = text.replace(URL_RE, "x".repeat(23));
    return graphemes(text);
  }
  function updateCounts() {
    var form = document.getElementById("compose-form");
    if (!form) return;
    var main = form.querySelector("#compose-text").value;
    var threaded = form.querySelector("input[name=thread]").checked;
    form.querySelectorAll(".acct-box").forEach(function (box) {
      var id = box.value, counter = form.querySelector('.counter[data-for="' + CSS.escape(id) + '"]');
      var override = form.querySelector('textarea[data-override="' + CSS.escape(id) + '"]');
      var text = (override && override.value.trim()) ? override.value : main;
      var limit = parseInt(box.dataset.limit, 10), used = lengthFor(box.dataset.network, text);
      if (!counter) return;
      counter.textContent = box.checked ? used + " / " + limit + (used > limit && threaded ? " (thread)" : "") : "";
      counter.classList.toggle("over", box.checked && used > limit && !threaded);
    });
  }
  function updateAltFields(input) {
    var holder = document.getElementById("alt-fields");
    holder.innerHTML = "";
    Array.from(input.files).slice(0, 4).forEach(function (f, i) {
      var label = document.createElement("label");
      label.textContent = "Alt text for " + f.name;
      var field = document.createElement("input");
      field.name = "alt_" + i; field.placeholder = "Describe the image for people who can't see it";
      label.appendChild(field);
      holder.appendChild(label);
    });
  }

  // ---- Push notifications ----
  function b64ToBytes(s) {
    var pad = "=".repeat((4 - (s.length % 4)) % 4);
    var raw = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(raw, function (c) { return c.charCodeAt(0); });
  }
  function status(msg) { var el = document.getElementById("push-status"); if (el) el.textContent = msg; }
  async function enablePush() {
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
      status("This browser can't do push. On iPhone, add the app to your home screen and open it from there.");
      return;
    }
    var perm = await Notification.requestPermission();
    if (perm !== "granted") { status("Notifications were not allowed."); return; }
    var reg = await navigator.serviceWorker.ready;
    var key = (await (await fetch("/push/key")).json()).publicKey;
    var sub = await reg.pushManager.getSubscription();
    if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(key) });
    var r = await fetch("/push/subscribe", { method: "POST", headers: { "Content-Type": "application/json" },
                                             body: JSON.stringify(sub) });
    status(r.ok ? "Notifications are on for this device." : "Couldn't save the subscription.");
  }
  async function testPush() {
    var r = await fetch("/push/test", { method: "POST" });
    var j = await r.json();
    status(j.sent ? "Sent to " + j.sent + " device(s)." : "No devices are subscribed yet.");
  }

  function init(root) {
    setupVideos(root);
    updateCounts();
  }
  document.addEventListener("DOMContentLoaded", function () {
    init(document);
    document.addEventListener("input", function (e) {
      if (e.target.closest && e.target.closest("#compose-form")) updateCounts();
    });
    document.addEventListener("change", function (e) {
      if (e.target.id === "images") updateAltFields(e.target);
      if (e.target.closest && e.target.closest("#compose-form")) updateCounts();
    });
    var on = document.getElementById("push-on");
    if (on) on.addEventListener("click", function () { enablePush().catch(function (e) { status(String(e)); }); });
    var test = document.getElementById("push-test");
    if (test) test.addEventListener("click", function () { testPush().catch(function (e) { status(String(e)); }); });
  });
  document.addEventListener("htmx:afterSwap", function (e) { init(e.target); });
})();
