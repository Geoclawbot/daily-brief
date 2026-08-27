// Cache the shell so the app opens instantly and works with no signal.
// Data is always network-first: a stale brief that looks live is the one
// failure mode this whole project exists to avoid.
const SHELL = 'brief-shell-v2';
const FILES = ['./', './index.html', './manifest.json'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== SHELL).map(k => caches.delete(k)))
  ).then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (url.pathname.endsWith('brief.json')) {
    // Network first. Fall back to cache only if offline, and the app will
    // show the real age of whatever it renders.
    e.respondWith(
      fetch(e.request).then(r => {
        const copy = r.clone();
        caches.open(SHELL).then(c => c.put(e.request, copy));
        return r;
      }).catch(() => caches.match(e.request))
    );
    return;
  }
  e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
});
