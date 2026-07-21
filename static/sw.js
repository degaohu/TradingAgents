// Minimal PWA shell. We intentionally do NOT cache /api/* — those must
// always be live. We only precache the static shell so the app opens
// instantly on repeat visits and can degrade gracefully offline (showing
// a "no network" page rather than a chrome error).
//
// The CACHE value below is a placeholder only relevant if this file is
// somehow served as-is from disk. In normal operation, GET /sw.js is
// handled by web/routes.py's service_worker(), which rewrites this line
// to embed the current app version (from pyproject.toml) at request time
// — so the cache key can never silently drift out of sync with a release
// the way a hand-maintained constant here once did.
const CACHE = 'ta-shell-vDEV';
const SHELL = ['/', '/style.css', '/app.js', '/icon.svg', '/manifest.json'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))),
    ).then(() => self.clients.claim())
  );
});
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  // Never intercept API, SSE or POST — those are dynamic.
  if (url.pathname.startsWith('/api/') || e.request.method !== 'GET') return;
  e.respondWith(
    caches.match(e.request).then((hit) => hit || fetch(e.request).then((res) => {
      // Cache only same-origin GETs with a 200 response.
      if (res && res.status === 200 && url.origin === self.location.origin) {
        const clone = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, clone));
      }
      return res;
    }).catch(() => hit))
  );
});
