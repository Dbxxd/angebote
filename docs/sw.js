// Service Worker: App funktioniert auch bei schlechtem Netz im Laden.
// App-Dateien: aus dem Cache. Angebotsdaten: erst Netz, bei Fehler der letzte Stand.
const CACHE = "angebote-v1";
const APP = ["./", "index.html", "manifest.json", "icon.svg", "icon-192.png", "icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(APP)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;

  // Angebotsdaten und die Seite selbst: Netz zuerst, damit neue Angebote sofort da sind
  if (url.pathname.includes("/data/") || e.request.mode === "navigate") {
    e.respondWith(
      fetch(e.request)
        .then((r) => {
          const kopie = r.clone();
          caches.open(CACHE).then((c) => c.put(e.request, kopie));
          return r;
        })
        .catch(() => caches.match(e.request, { ignoreSearch: true }))
    );
    return;
  }

  // Alles andere (Icons, Schrift): Cache zuerst
  e.respondWith(
    caches.match(e.request).then((treffer) => treffer || fetch(e.request).then((r) => {
      if (r.ok && (url.origin === location.origin || url.host.includes("fonts."))) {
        const kopie = r.clone();
        caches.open(CACHE).then((c) => c.put(e.request, kopie));
      }
      return r;
    }))
  );
});
