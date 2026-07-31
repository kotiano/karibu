/**
 * Service worker for Karibu POS.
 *
 * Deliberately conservative. This is a point-of-sale app whose entire value is
 * showing the *current* state of orders and payments, so caching API responses
 * would be actively harmful — a waiter seeing a stale order list is worse than
 * seeing an error. Only the app shell (HTML, JS, CSS, icons) is cached.
 *
 * Two rules:
 *   - Anything under /api/ is never cached and never served from cache.
 *   - The shell uses network-first with a cache fallback, so a new deploy is
 *     picked up immediately rather than pinning users to a stale bundle, but
 *     the app still opens on a flaky connection.
 *
 * Bump CACHE_VERSION on any change to this file to evict old entries.
 */
const CACHE_VERSION = "karibu-v1";
const SHELL = ["/", "/index.html", "/manifest.json", "/icon-192.png", "/icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  // Never cache the API. Stale order or payment data is worse than an error.
  if (url.pathname.startsWith("/api/")) return;
  // Don't touch cross-origin requests.
  if (url.origin !== self.location.origin) return;

  event.respondWith(
    fetch(request)
      .then((response) => {
        // Only cache complete, successful, same-origin responses.
        if (response && response.status === 200 && response.type === "basic") {
          const copy = response.clone();
          caches.open(CACHE_VERSION).then((c) => c.put(request, copy));
        }
        return response;
      })
      .catch(async () => {
        const cached = await caches.match(request);
        if (cached) return cached;
        // SPA navigation offline: fall back to the cached shell.
        if (request.mode === "navigate") {
          const shell = await caches.match("/index.html");
          if (shell) return shell;
        }
        throw new Error("offline and not cached");
      })
  );
});
