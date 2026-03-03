/**
 * Service Worker for AvailAI PWA
 *
 * What it does:
 *   - Caches static assets (cache-first) for fast repeat loads
 *   - Proxies API calls network-first so data is always fresh
 *   - Shows an offline fallback page when the network is down
 *   - Self-updates: skipWaiting + clients.claim for instant activation
 *   - Cleans up old caches on version bump
 *
 * Depends on: /static/offline.html (offline fallback)
 * Called by: navigator.serviceWorker.register() in index.html
 */

const CACHE_VERSION = 'availai-v1';
const STATIC_CACHE = CACHE_VERSION + '-static';
const API_CACHE    = CACHE_VERSION + '-api';

const PRECACHE_URLS = [
  '/',
  '/static/offline.html'
];

/* ------------------------------------------------------------------ */
/*  INSTALL — precache shell assets                                    */
/* ------------------------------------------------------------------ */
self.addEventListener('install', function(event) {
  event.waitUntil(
    caches.open(STATIC_CACHE).then(function(cache) {
      return cache.addAll(PRECACHE_URLS);
    }).then(function() {
      return self.skipWaiting();
    })
  );
});

/* ------------------------------------------------------------------ */
/*  ACTIVATE — clean old caches, claim clients immediately             */
/* ------------------------------------------------------------------ */
self.addEventListener('activate', function(event) {
  event.waitUntil(
    caches.keys().then(function(cacheNames) {
      return Promise.all(
        cacheNames.filter(function(name) {
          // Delete any cache that doesn't match the current version
          return name.indexOf(CACHE_VERSION) !== 0;
        }).map(function(name) {
          return caches.delete(name);
        })
      );
    }).then(function() {
      return self.clients.claim();
    })
  );
});

/* ------------------------------------------------------------------ */
/*  FETCH — strategy router                                            */
/* ------------------------------------------------------------------ */
self.addEventListener('fetch', function(event) {
  var request = event.request;
  var url = new URL(request.url);

  // Only handle same-origin requests
  if (url.origin !== self.location.origin) {
    return;
  }

  // API calls: network-first, never serve stale data silently
  if (url.pathname.indexOf('/api/') === 0) {
    event.respondWith(networkFirst(request, API_CACHE));
    return;
  }

  // Static assets: cache-first for speed
  if (url.pathname.indexOf('/static/') === 0) {
    event.respondWith(cacheFirst(request, STATIC_CACHE));
    return;
  }

  // HTML navigation: network-first with offline fallback
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(function() {
        return caches.match('/static/offline.html');
      })
    );
    return;
  }
});

/* ------------------------------------------------------------------ */
/*  Strategies                                                         */
/* ------------------------------------------------------------------ */

/**
 * Cache-first: return cached copy if available, otherwise fetch and cache.
 */
function cacheFirst(request, cacheName) {
  return caches.match(request).then(function(cached) {
    if (cached) {
      return cached;
    }
    return fetch(request).then(function(response) {
      if (response.ok) {
        var clone = response.clone();
        caches.open(cacheName).then(function(cache) {
          cache.put(request, clone);
        });
      }
      return response;
    });
  });
}

/**
 * Network-first: try network, fall back to cache.
 */
function networkFirst(request, cacheName) {
  return fetch(request).then(function(response) {
    if (response.ok) {
      var clone = response.clone();
      caches.open(cacheName).then(function(cache) {
        cache.put(request, clone);
      });
    }
    return response;
  }).catch(function() {
    return caches.match(request);
  });
}
