// BoniBuddy Service Worker (minimal PWA)
const CACHE_NAME = 'bonibuddy-v1';
const ASSETS_TO_CACHE = [
  '/',
  '/static/manifest.webmanifest',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png'
];

// Install: cache core assets
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(ASSETS_TO_CACHE))
  );
  self.skipWaiting();
});

// Activate: cleanup old caches
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))
      )
    )
  );
  self.clients.claim();
});

// Fetch: network first, fallback to cache
self.addEventListener('fetch', event => {
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});

// --- Push notifications ---
self.addEventListener('push', event => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {}

  const title = data.title || 'BoniBuddy';
  const options = {
    body: data.body || 'Našli smo družbo! Odpri app.',
    icon: '/static/icons/icon-192.png',
    badge: '/static/icons/icon-192.png',
    data: {
      url: (() => {
        const raw = data.url || '/';
        try {
          return new URL(raw, self.location.origin).href;
        } catch (e) {
          return new URL('/', self.location.origin).href;
        }
      })(),
    }
  };

  event.waitUntil(
    self.registration.showNotification(title, options)
  );
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const targetUrl = (() => {
    const raw = (event.notification.data && event.notification.data.url) ? event.notification.data.url : '/';
    try {
      return new URL(raw, self.location.origin).href;
    } catch (e) {
      return new URL('/', self.location.origin).href;
    }
  })();

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clientList => {
      const target = new URL(targetUrl);

      // Prefer an existing window on the same origin.
      const sameOrigin = clientList.filter(c => {
        try { return new URL(c.url).origin === target.origin; } catch (e) { return false; }
      });

      // Prefer a window whose URL starts with the target path.
      const preferred = sameOrigin.find(c => {
        try {
          const u = new URL(c.url);
          return u.pathname === target.pathname || u.pathname.startsWith(target.pathname);
        } catch (e) {
          return false;
        }
      }) || sameOrigin[0];

      if (preferred && 'focus' in preferred) {
        return preferred.focus();
      }

      if (clients.openWindow) {
        return clients.openWindow(targetUrl);
      }
    })
  );
});