const CACHE_NAME = "notestream-static-v1";
const STATIC_ASSETS = [
  "/static/notestream-logo.png",
  "/static/notestream-icon-180.png",
  "/static/notestream-icon-192.png",
  "/static/notestream-icon-512.png",
  "/static/notestream-icon-maskable-512.png"
];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))
    ))
  );
  self.clients.claim();
});

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);

  if (request.method !== "GET" || url.origin !== self.location.origin) return;

  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(request).then(cached => cached || fetch(request))
    );
  }
});
