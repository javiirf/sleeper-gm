/* Caches the app shell, and keeps the last good JSON so the board still opens offline. */
var SHELL = 'gm-shell-v1';
var DATA = 'gm-data-v1';
var FONTS = 'gm-fonts-v1';
var DATA_MAX_AGE = 7 * 86400000;
var FILES = ['./', 'index.html', 'app.css', 'app.js', 'manifest.webmanifest',
  'icons/icon.svg', 'icons/icon-192.png', 'icons/apple-touch-icon.png'];

self.addEventListener('install', function (e) {
  e.waitUntil(caches.open(SHELL).then(function (c) { return c.addAll(FILES); }).then(function () { return self.skipWaiting(); }));
});

self.addEventListener('activate', function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k.indexOf('gm-') === 0 && [SHELL, DATA, FONTS].indexOf(k) === -1; })
      .map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

function withHeader(res, name, value) {
  var h = new Headers(res.headers);
  h.set(name, value);
  return res.blob().then(function (b) { return new Response(b, { status: res.status, statusText: res.statusText, headers: h }); });
}

self.addEventListener('fetch', function (e) {
  var req = e.request;
  if (req.method !== 'GET') return;
  var url = new URL(req.url);

  if (url.hostname === 'fonts.googleapis.com' || url.hostname === 'fonts.gstatic.com') {
    e.respondWith(caches.open(FONTS).then(function (c) {
      return c.match(req).then(function (hit) {
        var net = fetch(req).then(function (res) { if (res.ok || res.type === 'opaque') c.put(req, res.clone()); return res; });
        return hit || net;
      });
    }));
    return;
  }
  if (url.origin !== self.location.origin) return;

  var isData = /\/api\//.test(url.pathname) || /\/demo\/[^/]+\.json$/.test(url.pathname);
  if (isData) {
    // network first; on failure hand back the last good copy, marked so the page can say it is old
    e.respondWith(fetch(req).then(function (res) {
      if (res.ok) {
        return withHeader(res.clone(), 'x-gm-cached-at', String(Date.now())).then(function (copy) {
          return caches.open(DATA).then(function (c) { return c.put(req, copy); });
        }).then(function () { return res; }, function () { return res; });
      }
      return res;
    }).catch(function () {
      return caches.open(DATA).then(function (c) {
        return c.match(req).then(function (hit) {
          var t = hit && Number(hit.headers.get('x-gm-cached-at'));
          if (t > 0 && Date.now() - t <= DATA_MAX_AGE) return withHeader(hit, 'x-gm-cache', '1');
          if (hit) return c.delete(req).then(function () { return null; });
          return null;
        });
      }).then(function (hit) {
        if (hit) return hit;
        return new Response('{"error":"offline"}', { status: 503, headers: { 'content-type': 'application/json', 'x-gm-offline': '1' } });
      });
    }));
    return;
  }

  // shell: network first so updates land, cache when offline
  e.respondWith(fetch(req).then(function (res) {
    if (res.ok) { var copy = res.clone(); caches.open(SHELL).then(function (c) { c.put(req, copy); }); }
    return res;
  }).catch(function () {
    return caches.open(SHELL).then(function (c) {
      return c.match(req, { ignoreSearch: true }).then(function (hit) {
        return hit || (req.mode === 'navigate' ? c.match('index.html') : Response.error());
      });
    });
  }));
});
