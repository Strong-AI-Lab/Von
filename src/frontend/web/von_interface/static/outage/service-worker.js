// Cache only this public, dependency-free outage shell. Never cache application HTML,
// API responses, navigation history, health payloads, credentials or uploaded files.
const CACHE = 'von-outage-v1';
const STATIC_ROOT = '/static/';
const ASSETS = ['/static/outage/offline.html', '/static/outage/offline.js', '/static/outage/status.js', '/static/outage/outage.css'];
self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    for (const path of ASSETS) {
      const response = await fetch(path.replace('/static/', STATIC_ROOT), { cache: 'reload', credentials: 'same-origin', redirect: 'error' });
      const contentType = response.headers.get('content-type') || '';
      const expectedType = path.endsWith('.js') ? 'javascript' : path.endsWith('.css') ? 'text/css' : 'text/html';
      if (!response.ok || !contentType.includes(expectedType)) throw new Error('Outage shell could not be installed');
      // Static assets may sit behind Access. Send same-origin authentication to
      // fetch them, but retain no response headers that could carry credentials.
      await cache.put(path, new Response(await response.arrayBuffer(), {
        status: 200, headers: { 'Content-Type': contentType }
      }));
    }
    await self.skipWaiting();
  })());
});
self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    await self.clients.claim();
    for (const name of await caches.keys()) {
      if (name.startsWith('von-outage-v1-') && name !== CACHE) await caches.delete(name);
    }
  })());
});
self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (ASSETS.includes(url.pathname) && !url.search) {
    event.respondWith(caches.open(CACHE).then(async cache => (await cache.match(url.pathname)) || fetch(request)));
    return;
  }
  // Confine navigation recovery to the home app; do not intercept OAuth or API routes.
  if (request.mode !== 'navigate' || !['/von/', '/von'].includes(url.pathname)) return;
  event.respondWith((async () => {
    try {
      const response = await fetch(request);
      if (response.status < 500) return response;
    } catch { /* Device offline or backend unavailable. */ }
    const cache = await caches.open(CACHE);
    return (await cache.match('/static/outage/offline.html')) || Response.error();
  })());
});
