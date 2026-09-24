const CACHE_NAME = 'ades-pwa-v1';
const APP_SHELL = [
  '/',
  '/index.html',
  '/manifest.webmanifest',
  '/icons/ades.svg',
  '/icons/icon-180.png',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
];

async function precacheAppShell() {
  const assetUrls = [];
  try {
    const response = await fetch('/asset-manifest.json', { cache: 'no-store' });
    if (response.ok && response.headers.get('content-type')?.includes('json')) {
      const manifest = await response.json();
      for (const entry of Object.values(manifest)) {
        if (!entry || typeof entry !== 'object') continue;
        const assets = [entry.file, ...(entry.css ?? []), ...(entry.assets ?? [])];
        for (const asset of assets) {
          if (typeof asset === 'string' && asset.startsWith('assets/')) assetUrls.push(`/${asset}`);
        }
      }
    }
  } catch {
    // The shell remains cacheable if an asset manifest is unavailable.
  }

  const cache = await caches.open(CACHE_NAME);
  await cache.addAll([...new Set([...APP_SHELL, ...assetUrls])]);
}

self.addEventListener('install', (event) => {
  event.waitUntil(precacheAppShell().then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const cacheNames = await caches.keys();
    await Promise.all(cacheNames
      .filter((cacheName) => cacheName.startsWith('ades-pwa-') && cacheName !== CACHE_NAME)
      .map((cacheName) => caches.delete(cacheName)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return;

  if (request.mode === 'navigate') {
    event.respondWith((async () => {
      try {
        const response = await fetch(request);
        if (response.ok) {
          const cache = await caches.open(CACHE_NAME);
          await cache.put('/index.html', response.clone());
        }
        return response;
      } catch {
        return await caches.match('/index.html') ?? Response.error();
      }
    })());
    return;
  }

  if (url.pathname.startsWith('/assets/') || url.pathname.startsWith('/icons/') || url.pathname === '/manifest.webmanifest') {
    event.respondWith((async () => {
      const cached = await caches.match(request);
      if (cached) return cached;
      const response = await fetch(request);
      if (response.ok) {
        const cache = await caches.open(CACHE_NAME);
        await cache.put(request, response.clone());
      }
      return response;
    })());
  }
});
