// Cross-poster client: service worker, video, composer counters and @mentions, push sign-up.
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
  var REMOTE_MENTION_RE = /(^|[^\w@\/])(@[A-Za-z0-9_]+)@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+/g;
  function lengthFor(network, text) {
    if (network === "mastodon" || network === "x") text = text.replace(URL_RE, "x".repeat(23));
    if (network === "mastodon") text = text.replace(REMOTE_MENTION_RE, "$1$2");  // counted as @user
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

  // ---- Composer: @mention autocomplete ----
  var NETWORK_NAMES = { bluesky: "Bluesky", mastodon: "Mastodon", x: "X" };
  var menu = null, menuItems = [], menuIndex = 0, menuFor = null, menuStart = 0, menuTimer = null, menuSeq = 0;
  function mentionQuery(ta) {
    var before = ta.value.slice(0, ta.selectionStart);
    var m = /(^|[^\w@\/])@([\w.-]*(?:@[\w.-]*)?)$/.exec(before);
    return m ? { q: m[2], start: before.length - m[2].length - 1 } : null;
  }
  function chosenNetworks() {
    var nets = [];
    document.querySelectorAll("#compose-form .acct-box:checked").forEach(function (b) {
      if (nets.indexOf(b.dataset.network) < 0) nets.push(b.dataset.network);
    });
    return nets;
  }
  function closeMenu() { if (menu) menu.hidden = true; menuItems = []; menuFor = null; }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text) e.textContent = text;
    return e;
  }
  function showMenu(ta, items) {
    if (!menu) {
      menu = el("ul", "mention-menu");
      menu.setAttribute("role", "listbox");
      document.body.appendChild(menu);
      // mousedown, not click, so the textarea keeps focus
      menu.addEventListener("mousedown", function (e) {
        var li = e.target.closest("li");
        if (li) { e.preventDefault(); pickMention(parseInt(li.dataset.i, 10)); }
      });
    }
    menuItems = items; menuIndex = 0; menuFor = ta;
    menu.innerHTML = "";
    items.forEach(function (it, i) {
      var li = el("li", i === 0 ? "active" : "");
      li.dataset.i = i;
      li.setAttribute("role", "option");
      if (it.avatar) { var img = el("img", "avatar avatar-xs"); img.src = it.avatar; img.alt = ""; li.appendChild(img); }
      else li.appendChild(el("span", "avatar avatar-xs avatar-blank", (it.name || "?").slice(0, 1).toUpperCase()));
      var who = el("span", "who");
      who.appendChild(el("strong", "", it.name));
      var hs = el("small", "handles");
      it.handles.forEach(function (h) {
        hs.appendChild(el("span", "badge badge-" + h.network, NETWORK_NAMES[h.network] || h.network));
        hs.appendChild(document.createTextNode(" @" + h.handle + " "));
      });
      if (it.linked && it.handles.length > 1) hs.appendChild(el("span", "linked", "linked"));
      who.appendChild(hs);
      li.appendChild(who);
      menu.appendChild(li);
    });
    var r = ta.getBoundingClientRect();
    menu.style.left = (r.left + window.scrollX) + "px";
    menu.style.top = (r.bottom + window.scrollY + 4) + "px";
    menu.style.width = r.width + "px";
    menu.hidden = items.length === 0;
  }
  function moveMenu(step) {
    if (!menuItems.length) return;
    menuIndex = (menuIndex + step + menuItems.length) % menuItems.length;
    Array.from(menu.children).forEach(function (li, i) { li.classList.toggle("active", i === menuIndex); });
    menu.children[menuIndex].scrollIntoView({ block: "nearest" });
  }
  function pickMention(i) {
    var ta = menuFor, it = menuItems[i];
    if (!ta || !it) return;
    var end = ta.selectionStart, insert = it.insert + " ";
    ta.value = ta.value.slice(0, menuStart) + insert + ta.value.slice(end);
    ta.selectionStart = ta.selectionEnd = menuStart + insert.length;
    closeMenu();
    ta.focus();
    ta.dispatchEvent(new Event("input", { bubbles: true }));
  }
  function onMentionInput(ta) {
    var m = mentionQuery(ta);
    clearTimeout(menuTimer);
    if (!m || !m.q) { closeMenu(); return; }
    menuStart = m.start;
    var seq = ++menuSeq;
    menuTimer = setTimeout(function () {
      var url = "/people/suggest?q=" + encodeURIComponent(m.q) + "&networks=" + encodeURIComponent(chosenNetworks().join(","));
      fetch(url).then(function (r) { return r.json(); }).then(function (items) {
        if (seq === menuSeq && document.activeElement === ta) showMenu(ta, items);
      }).catch(function () {});
    }, 150);
  }
  function onMentionKey(e) {
    if (!menu || menu.hidden || e.target !== menuFor) return;
    if (e.key === "ArrowDown") { e.preventDefault(); moveMenu(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); moveMenu(-1); }
    else if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); pickMention(menuIndex); }
    else if (e.key === "Escape") { e.preventDefault(); closeMenu(); }
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
      if (e.target.classList && e.target.classList.contains("mentionable")) onMentionInput(e.target);
    });
    document.addEventListener("keydown", onMentionKey);
    document.addEventListener("focusout", function (e) {
      if (e.target === menuFor) setTimeout(function () { if (document.activeElement !== menuFor) closeMenu(); }, 150);
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
