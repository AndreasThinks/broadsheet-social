// Broadsheet client: service worker, video, composer counters and @mentions, push sign-up, arranging tabs,
// pull to refresh.
(function () {
  "use strict";

  // In the browser version the page's own service worker runs the app, so don't replace it with ours.
  var inBrowserBuild = !!document.querySelector('meta[name="broadsheet-browser"]');
  if ("serviceWorker" in navigator && !inBrowserBuild) {
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
    var keyInfo = await (await fetch("/push/key")).json();
    if (!keyInfo.publicKey) { status(keyInfo.error || "Push isn't available."); return; }
    var key = b64ToBytes(keyInfo.publicKey);
    var sub = await reg.pushManager.getSubscription();
    if (sub && sub.options.applicationServerKey &&
        new Uint8Array(sub.options.applicationServerKey).join() !== key.join()) {
      await sub.unsubscribe();  // made with a different server's key
      sub = null;
    }
    if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
    var r = await fetch("/push/subscribe", { method: "POST", headers: { "Content-Type": "application/json" },
                                             body: JSON.stringify(sub) });
    var j = await r.json().catch(function () { return {}; });
    status(r.ok ? (j.message || "Notifications are on for this device.") : (j.error || "Couldn't save the subscription."));
  }
  async function testPush() {
    var r = await fetch("/push/test", { method: "POST" });
    var j = await r.json();
    status(j.sent ? "Sent to " + j.sent + " device(s)." : "No devices are subscribed yet.");
  }

  // ---- Arrange tabs: drag a row by its handle (mouse, pen or finger), or nudge it with the arrows ----
  function setupArrange() {
    var list = document.getElementById("arrange");
    if (!list) return;
    var statusEl = document.getElementById("arrange-status"), saveTimer = null, saveSeq = 0;
    function save() {
      clearTimeout(saveTimer);
      statusEl.textContent = "Saving…";
      saveTimer = setTimeout(function () {
        var seq = ++saveSeq;
        var tabs = Array.from(list.querySelectorAll(".arrange-item")).map(function (li) {
          return { key: li.dataset.key, visible: li.querySelector(".arrange-show").checked };
        });
        fetch("/feeds/arrange", { method: "POST", headers: { "Content-Type": "application/json" },
                                  body: JSON.stringify({ tabs: tabs }) })
          .then(function (r) { if (seq === saveSeq) statusEl.textContent = r.ok ? "Saved" : "Couldn't save. Try again."; })
          .catch(function () { if (seq === saveSeq) statusEl.textContent = "Couldn't save. Are you offline?"; });
      }, 250);
    }

    list.addEventListener("change", function (e) {
      if (!e.target.classList.contains("arrange-show")) return;
      e.target.closest(".arrange-item").classList.toggle("off", !e.target.checked);
      save();
    });
    list.addEventListener("click", function (e) {
      var b = e.target.closest(".nudge");
      if (!b) return;
      var li = b.closest(".arrange-item");
      if (b.dataset.step === "-1" && li.previousElementSibling) list.insertBefore(li, li.previousElementSibling);
      else if (b.dataset.step === "1" && li.nextElementSibling) list.insertBefore(li.nextElementSibling, li);
      else return;
      b.focus();
      flash(li);
      save();
    });
    function flash(li) { li.classList.remove("moved"); void li.offsetWidth; li.classList.add("moved"); }

    var drag = null;
    list.addEventListener("pointerdown", function (e) {
      var grip = e.target.closest(".grip");
      if (!grip || e.button > 0) return;
      e.preventDefault();
      var li = grip.closest(".arrange-item");
      grip.setPointerCapture(e.pointerId);
      drag = { li: li, grab: e.clientY - li.getBoundingClientRect().top, y: e.clientY,
               before: li.nextElementSibling, frame: 0 };
      li.classList.add("dragging");
      list.classList.add("sorting");
      drag.frame = requestAnimationFrame(follow);
    });
    document.addEventListener("pointermove", function (e) { if (drag) drag.y = e.clientY; });
    function follow() {
      if (!drag) return;
      var li = drag.li, y = drag.y;
      // keep going when the finger is near the top or bottom of the screen
      if (y < 72) window.scrollBy(0, -Math.ceil((72 - y) / 6));
      else if (y > window.innerHeight - 72) window.scrollBy(0, Math.ceil((y - window.innerHeight + 72) / 6));
      var others = Array.from(list.children).filter(function (o) { return o !== li; });
      var next = null;
      for (var i = 0; i < others.length; i++) {
        var r = others[i].getBoundingClientRect();
        if (y < r.top + r.height / 2) { next = others[i]; break; }
      }
      if (li.nextElementSibling !== next) place(li, next);
      li.style.transform = "";
      var top = li.getBoundingClientRect().top;
      li.style.transform = "translateY(" + (y - drag.grab - top) + "px)";
      drag.frame = requestAnimationFrame(follow);
    }
    // Put li before next (null = at the end) by moving its neighbours round it: moving the dragged row itself
    // would make the browser drop the pointer mid-drag.
    function place(li, next) {
      var items = Array.from(list.children), from = items.indexOf(li), to = next ? items.indexOf(next) : items.length;
      if (to > from) items.slice(from + 1, to).forEach(function (o) { list.insertBefore(o, li); });
      else items.slice(to, from).forEach(function (o) { list.insertBefore(o, li.nextElementSibling); });
    }
    function drop() {
      if (!drag) return;
      var d = drag;
      drag = null;
      cancelAnimationFrame(d.frame);
      d.li.style.transform = "";
      d.li.classList.remove("dragging");
      list.classList.remove("sorting");
      if (d.li.nextElementSibling !== d.before) { flash(d.li); save(); }
    }
    document.addEventListener("pointerup", drop);
    document.addEventListener("pointercancel", drop);
  }

  // ---- Pull to refresh: at the top of a timeline, pull down and let go to fetch the newest posts ----
  var PULL_READY = 64, PULL_MAX = 110;
  var ARROW = '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
              'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5v14M6 13l6 6 6-6"/></svg>';
  function setupPull() {
    var target = document.querySelector("[data-refresh]");
    if (!target || !("ontouchstart" in window || navigator.maxTouchPoints > 0)) return;
    document.documentElement.classList.add("pull-page");  // stops the browser's own pull-to-reload competing
    var bar = el("div", "ptr");
    bar.setAttribute("aria-live", "polite");
    bar.innerHTML = '<div class="ptr-inner"><span class="ptr-arrow">' + ARROW + '</span><span class="spinner"></span>' +
                    '<span class="ptr-label">Pull to refresh</span></div>';
    target.parentNode.insertBefore(bar, target);
    var label = bar.querySelector(".ptr-label");
    var start = null, pulling = false, dist = 0, busy = false;

    function set(h) { dist = h; bar.style.height = h + "px"; }
    function reset() { bar.classList.remove("pulling", "ready", "refreshing"); set(0); }

    document.addEventListener("touchstart", function (e) {
      if (busy || e.touches.length !== 1 || window.scrollY > 0) { start = null; return; }
      if (e.target.closest && e.target.closest("input, textarea, select, video, .mention-menu")) { start = null; return; }
      start = { x: e.touches[0].clientX, y: e.touches[0].clientY };
      pulling = false;
    }, { passive: true });
    document.addEventListener("touchmove", function (e) {
      if (!start) return;
      var dx = e.touches[0].clientX - start.x, dy = e.touches[0].clientY - start.y;
      if (!pulling) {
        if (dy > 8 && dy > Math.abs(dx) * 1.5 && window.scrollY <= 0) { pulling = true; bar.classList.add("pulling"); }
        else if (Math.abs(dx) > 8 || dy < -4) { start = null; return; }
        else return;
      }
      e.preventDefault();
      set(Math.min(PULL_MAX, Math.max(0, dy - 8) * 0.55));
      var ready = dist >= PULL_READY;
      bar.classList.toggle("ready", ready);
      label.textContent = ready ? "Let go to refresh" : "Pull to refresh";
    }, { passive: false });
    function end() {
      if (!start) return;
      start = null;
      if (!pulling) return;
      pulling = false;
      bar.classList.remove("pulling");
      if (dist < PULL_READY) { reset(); return; }
      busy = true;
      bar.classList.add("refreshing");
      label.textContent = "Fetching the latest";
      set(52);
      var url = target.dataset.refresh;
      if (url === "reload") { window.location.reload(); return; }
      var done = function () { busy = false; reset(); label.textContent = "Pull to refresh"; };
      var safety = setTimeout(done, 30000);
      Promise.resolve(window.htmx ? htmx.ajax("GET", url, { target: target, swap: "innerHTML" }) : null)
        .catch(function () {}).then(function () { clearTimeout(safety); done(); });
    }
    document.addEventListener("touchend", end);
    document.addEventListener("touchcancel", end);
  }

  function init(root) {
    setupVideos(root);
    updateCounts();
  }
  document.addEventListener("DOMContentLoaded", function () {
    init(document);
    setupArrange();
    setupPull();
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
    // Tapping a post anywhere that isn't a link, button or picture opens its conversation.
    document.addEventListener("click", function (e) {
      var card = e.target.closest && e.target.closest(".post[data-href]");
      if (!card || e.defaultPrevented || e.button !== 0) return;
      if (e.target.closest("a, button, input, textarea, select, label, summary, video, audio, img, .media, .why")) return;
      var sel = window.getSelection && window.getSelection();
      if (sel && String(sel).length) return;
      if (e.metaKey || e.ctrlKey) window.open(card.dataset.href, "_blank");
      else location.href = card.dataset.href;
    });
    // Copy buttons: data-copy names the field to copy.
    document.addEventListener("click", function (e) {
      var b = e.target.closest && e.target.closest("[data-copy]");
      if (!b) return;
      var field = document.querySelector(b.dataset.copy);
      if (!field) return;
      field.select();
      var done = function () { b.textContent = "Copied"; setTimeout(function () { b.textContent = "Copy"; }, 1500); };
      if (navigator.clipboard) navigator.clipboard.writeText(field.value).then(done, function () { document.execCommand("copy"); done(); });
      else { document.execCommand("copy"); done(); }
    });
    var on = document.getElementById("push-on");
    if (on) on.addEventListener("click", function () { enablePush().catch(function (e) { status(String(e)); }); });
    var test = document.getElementById("push-test");
    if (test) test.addEventListener("click", function () { testPush().catch(function (e) { status(String(e)); }); });
  });
  document.addEventListener("htmx:afterSwap", function (e) { init(e.target); });
})();
