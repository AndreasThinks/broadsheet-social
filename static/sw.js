// Service worker: shows push notifications and keeps a small offline page.
const CACHE = "crossposter-v3";
const SHELL = ["/static/app.css", "/static/fonts/inter-latin.woff2", "/static/fonts/newsreader-latin.woff2", "/static/app.js", "/static/icons/icon-192.png", "/static/offline.html"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  if (url.pathname.startsWith("/static/")) {
    // Serve from cache straight away but refresh it in the background, so an app update's
    // new stylesheet and script are picked up instead of the first copy being kept forever.
    event.respondWith(caches.open(CACHE).then((c) => c.match(req).then((hit) => {
      const fresh = fetch(req).then((res) => { if (res.ok) c.put(req, res.clone()); return res; });
      if (hit) { fresh.catch(() => {}); event.waitUntil(fresh.then(() => {}, () => {})); return hit; }
      return fresh;
    })));
    return;
  }
  if (req.mode === "navigate") {
    event.respondWith(fetch(req).catch(() => caches.match("/static/offline.html")));
  }
});

// The server sends the declarative Web Push format. Safari 18.4+ displays it without waking us;
// every other browser delivers it here and we show it ourselves.
self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = { notification: { title: "Broadsheet", body: event.data.text() } }; }
  const n = data.notification || data;
  const options = {
    body: n.body || "",
    icon: n.icon || "/static/icons/icon-192.png",
    badge: "/static/icons/badge-96.png",
    tag: n.tag,
    data: { url: n.navigate || "/notifications" },
  };
  event.waitUntil(Promise.all([
    self.registration.showNotification(n.title || "Broadsheet", options),
    self.navigator && self.navigator.setAppBadge ? self.navigator.setAppBadge().catch(() => {}) : Promise.resolve(),
  ]));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      for (const w of wins) {
        if ("focus" in w) { w.navigate(target); return w.focus(); }
      }
      return self.clients.openWindow(target);
    })
  );
});
