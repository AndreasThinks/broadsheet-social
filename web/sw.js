// Service worker for Broadsheet in the browser.
//
// The app runs as Python inside a Web Worker owned by the page at /web. The app's own pages are shown in a frame
// on that page, and every request the frame makes arrives here: we pass it to the page, which asks the worker, and
// hand the answer back as if a server had sent it. Nothing reaches this site's server except /web and its files.
// Everything else on the site (the home page, the feed directory) is left alone.
const FILES = "/web-files/";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

function isSiteOwn(url) {
  return url.pathname === "/web" || url.pathname.startsWith("/web/") || url.pathname.startsWith(FILES);
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (url.origin !== location.origin || isSiteOwn(url)) return;
  if (req.mode === "navigate") {
    // Only the app's frame is ours; a top-level visit goes to the real site.
    if (req.destination === "iframe" || req.destination === "frame") event.respondWith(toApp(req, true));
    return;
  }
  if (!event.clientId) return;
  event.respondWith(self.clients.get(event.clientId).then((client) =>
    client && client.frameType === "nested" ? toApp(req, false) : fetch(req)));
});

async function shells() {
  const wins = await self.clients.matchAll({ type: "window" });
  return wins.filter((c) => c.frameType === "top-level" && /^\/web(\/|$)/.test(new URL(c.url).pathname));
}

function ask(client, msg) {
  return new Promise((resolve, reject) => {
    const ch = new MessageChannel();
    const timer = setTimeout(() => reject(new Error("timeout")), 120000);
    ch.port1.onmessage = (e) => {
      clearTimeout(timer);
      e.data && e.data.status ? resolve(e.data) : reject(new Error("busy"));
    };
    client.postMessage({ type: "broadsheet-request", ...msg }, [ch.port2]);
  });
}

function escapeTo(target) {
  // The sign-in pages of Bluesky and Mastodon refuse to load inside a frame, so leave the frame for them.
  const html = `<!doctype html><meta charset="utf-8"><script>top.location.replace(${JSON.stringify(target)});</script>`;
  return new Response(html, { headers: { "Content-Type": "text/html; charset=utf-8" } });
}

async function toApp(req, isNavigation) {
  const pages = await shells();
  if (!pages.length) return isNavigation ? fetch(req) : new Response("Broadsheet isn't running.", { status: 503 });
  const body = req.method === "GET" || req.method === "HEAD" ? null : await req.arrayBuffer();
  const msg = { method: req.method, url: req.url, headers: [...req.headers], body };
  let res;
  try {
    res = await Promise.any(pages.map((c) => ask(c, msg)));
  } catch (e) {
    return new Response("Broadsheet isn't running in another tab.", { status: 503 });
  }
  const headers = new Headers(res.headers);
  const location = headers.get("location");
  if (res.status >= 300 && res.status < 400 && location) {
    const to = new URL(location, req.url);
    if (to.origin !== self.location.origin && isNavigation) return escapeTo(to.href);
    return Response.redirect(to.href, [301, 302, 303, 307, 308].includes(res.status) ? res.status : 303);
  }
  const hx = headers.get("hx-redirect");
  if (hx && new URL(hx, req.url).origin !== self.location.origin) {
    headers.set("hx-redirect", FILES + "out.html?to=" + encodeURIComponent(new URL(hx, req.url).href));
  }
  const empty = res.status === 204 || res.status === 304 || req.method === "HEAD";
  return new Response(empty ? null : res.body, { status: res.status, headers });
}

// Mastodon pushes notifications straight to this browser (see the push routes in the app).
self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = { title: "Broadsheet", body: event.data.text() }; }
  const n = data.notification || data;
  event.waitUntil(self.registration.showNotification(n.title || "Broadsheet", {
    body: n.body || "",
    icon: n.icon || "/static/icons/icon-192.png",
    tag: n.notification_id ? `mastodon-${n.notification_id}` : n.tag,
    data: { url: "/web/notifications" },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/web";
  event.waitUntil(shells().then((pages) => {
    if (pages.length) return pages[0].navigate(target).then((c) => c && c.focus());
    return self.clients.openWindow(target);
  }));
});
