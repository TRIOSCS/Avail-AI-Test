/**
 * Service Worker — self-destruct mode.
 *
 * Unregisters itself and clears all caches on activation.
 * This ensures all clients get fresh assets from the server.
 */

self.addEventListener('install', function() {
  self.skipWaiting();
});

self.addEventListener('activate', function(event) {
  event.waitUntil(
    caches.keys().then(function(names) {
      return Promise.all(names.map(function(name) { return caches.delete(name); }));
    }).then(function() {
      return self.registration.unregister();
    }).then(function() {
      return self.clients.claim();
    })
  );
});
