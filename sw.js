// Network first, so the app always shows the newest data; the last copy is used when offline.
const CACHE = 'solchain-v1';
const SHELL = ['./', 'index.html', 'copy.html', 'early.html', 'wallets.html', 'assets.css', 'icons/icon-192.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;  // live prices and fonts go straight to the network
  const key = url.origin + url.pathname;  // data files carry ?v=… cache-busters; store one copy per file
  e.respondWith(
    fetch(e.request)
      .then((res) => { if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(key, copy)); } return res; })
      .catch(() => caches.match(key).then((hit) => hit || caches.match(e.request)))
  );
});
