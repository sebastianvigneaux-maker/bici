'use strict';

const CACHE_NAME = 'ftms-bike-shell-v1';
const APP_FILES = ['./', './index.html', './supabase-config.js'];
const SUPABASE_CLIENT = 'https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.117.3';

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    await cache.addAll(APP_FILES);
    try {
      await cache.add(SUPABASE_CLIENT);
    } catch (error) {
      // La app y su historial local siguen disponibles aunque no se pueda almacenar el cliente remoto.
    }
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(key => key.startsWith('ftms-bike-shell-') && key !== CACHE_NAME).map(key => caches.delete(key)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', event => {
  if (event.request.method !== 'GET') return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin && event.request.url !== SUPABASE_CLIENT) return;
  event.respondWith((async () => {
    const cache = await caches.open(CACHE_NAME);
    const cached = await cache.match(event.request);
    const refreshFirst = url.origin === self.location.origin && (event.request.mode === 'navigate' || url.pathname.endsWith('/supabase-config.js'));
    if (cached && !refreshFirst) return cached;
    try {
      const response = await fetch(event.request);
      if (response.ok) {
        try {
          await cache.put(event.request, response.clone());
        } catch (error) {
          // A cache write failure must not turn a successful network response into an offline error.
        }
      }
      return response;
    } catch (error) {
      if (cached) return cached;
      if (event.request.mode === 'navigate') return (await cache.match('./index.html')) || Response.error();
      return Response.error();
    }
  })());
});
