/* Service worker: keeps the app usable offline.
   - App shell (HTML/CSS/JS/Plotly/icons): served from cache, refreshed in the background (stale-while-revalidate).
   - data/*.json: network first (always the newest published data); if offline, the last copy viewed on this device,
     marked with the header x-sw-offline: 1 so the page can say it is showing a saved copy.
   - Cross-origin requests (live prices from CNBC) are not touched: they always go to the network.
   BUILD is replaced by the deploy workflow with the commit id, so each deploy gets a fresh shell cache. */
const BUILD = "dev";
const SHELL = "shell-" + BUILD;
const DATA = "data-v1";
const SHELL_FILES = ["./", "index.html", "assets/style.css", "assets/app.js", "assets/plotly-basic.min.js",
  "manifest.webmanifest", "icons/icon-192.png", "icons/icon-512.png", "icons/apple-touch-icon.png", "data/markets.json"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(SHELL_FILES.map(u => new Request(u, { cache: "reload" })))).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k.startsWith("shell-") && k !== SHELL).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

async function networkFirst(req) {
  const cache = await caches.open(DATA);
  const key = req.url.split("?")[0];
  try {
    const res = await fetch(req, { cache: "no-cache" });
    if (res.ok) await cache.put(key, res.clone());
    return res;
  } catch (err) {
    const hit = await cache.match(key) || await caches.match(key);
    if (!hit) throw err;
    const h = new Headers(hit.headers); h.set("x-sw-offline", "1");
    return new Response(await hit.blob(), { status: 200, headers: h });
  }
}
async function staleWhileRevalidate(req, event) {
  const cache = await caches.open(SHELL);
  const hit = await cache.match(req, { ignoreSearch: true });
  const net = fetch(req).then(res => { if (res.ok) cache.put(req, res.clone()); return res; });
  if (hit) { event.waitUntil(net.catch(() => null)); return hit; }
  try { return await net; }
  catch (err) { const idx = req.mode === "navigate" ? await cache.match("index.html") : null; if (idx) return idx; throw err; }
}

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;            // live prices etc.: straight to the network
  if (/\/data\/[^/]+\.json$/.test(url.pathname)) { e.respondWith(networkFirst(req)); return; }
  e.respondWith(staleWhileRevalidate(req, e));
});
