/* Only public static files are cached. Conversations, authentication, and API
   responses must always go through the network and the hub's access checks. */
const STATIC_CACHE = "codex-anywhere-static-v1";
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(STATIC_CACHE)
      .then((cache) =>
        cache.addAll(["/icons/codex-anywhere.svg", "/manifest.webmanifest"]),
      ),
  );
  self.skipWaiting();
});
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter(
              (key) =>
                key.startsWith("codex-anywhere-static-") &&
                key !== STATIC_CACHE,
            )
            .map((key) => caches.delete(key)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin)
    return;
  if (
    !url.pathname.startsWith("/assets/") &&
    !url.pathname.startsWith("/icons/") &&
    url.pathname !== "/manifest.webmanifest"
  )
    return;
  event.respondWith(
    caches.open(STATIC_CACHE).then(async (cache) => {
      const stored = await cache.match(event.request);
      if (stored) return stored;
      const response = await fetch(event.request);
      if (
        response.ok &&
        !response.headers.get("content-type")?.includes("text/html")
      )
        cache.put(event.request, response.clone());
      return response;
    }),
  );
});
