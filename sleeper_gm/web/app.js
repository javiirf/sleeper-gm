/* sleeper-gm phone app: read-only views over the manager's JSON. No framework, no build step. */
(function () {
  'use strict';

  var qs = new URLSearchParams(location.search);
  var S = {
    demo: qs.get('demo') === '1',
    fallback: false,          // the API 404'd, showing the demo week instead
    leagues: [], league: null,
    now: null, runs: [], season: [],
    staleFrom: null,          // ms of the snapshot we are showing when the network failed
    error: null,
    open: {},                 // expanded row keys
    pin: null,                // selected lock pin on the win line
    showStatus: false,
    lastLoad: 0, loading: false
  };
  var HOUR = 3600000, DAY = 86400000;
  var CACHE_MAX_AGE = 7 * DAY;
  var $ = function (id) { return document.getElementById(id); };

  /* ---------- small helpers ---------- */
  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function num(x) {
    if (typeof x === 'string' && x.trim() !== '') x = Number(x);
    return typeof x === 'number' && isFinite(x) ? x : null;
  }
  function f1(x) { x = num(x); return x == null ? '' : x.toFixed(1); }
  function pct(p) { p = num(p); return p == null ? '' : Math.round(p * 100) + '%'; }
  function arr(x) { return Array.isArray(x) ? x : []; }
  function obj(x) { return x && typeof x === 'object' && !Array.isArray(x) ? x : {}; }
  function cap(s) { s = String(s || ''); return s.charAt(0).toUpperCase() + s.slice(1); }
  function ord(n) {
    n = Math.round(n);
    var s = ['th', 'st', 'nd', 'rd'], v = n % 100;
    return n + (s[(v - 20) % 10] || s[v] || s[0]);
  }
  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : (many || one + 's')); }

  var fTime = new Intl.DateTimeFormat('en-US', { hour: 'numeric', minute: '2-digit' });
  var fWd = new Intl.DateTimeFormat('en-US', { weekday: 'short' });
  var fLong = new Intl.DateTimeFormat('en-US', { weekday: 'long', month: 'short', day: 'numeric' });
  var fMd = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric' });
  function tm(ms) {
    return fTime.format(new Date(ms)).replace(/[\s ]*([AP])M/, function (_, a) { return ' ' + a.toLowerCase() + 'm'; });
  }
  function wd(ms) { return fWd.format(new Date(ms)); }
  function ymd(s) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(s || ''));
    return m ? new Date(+m[1], +m[2] - 1, +m[3], 12) : null;
  }
  function shortDate(s) { var d = ymd(s); return d ? fMd.format(d) : String(s || ''); }
  // A "board day" runs 4 am to 4 am, so a 1:40 am run belongs to the night before.
  function bday(ms) { var d = new Date(ms - 4 * HOUR); return new Date(d.getFullYear(), d.getMonth(), d.getDate()); }
  function dkey(d) { return d.getFullYear() + '-' + (d.getMonth() + 1) + '-' + d.getDate(); }
  function dayDiff(a, b) { return Math.round((b - a) / DAY); }
  function clock() {
    var t = num(obj(S.now).t);
    return (S.demo || S.fallback) && t ? t : Date.now();
  }
  function when(ms) {
    // "10:10 pm" today, "Tue 10:10 pm" this week, "Nov 24, 10:10 pm" older
    if (!num(ms)) return '';
    var d = dayDiff(bday(ms), bday(clock()));
    if (d === 0) return tm(ms);
    if (d > 0 && d < 7) return wd(ms) + ' ' + tm(ms);
    return fMd.format(new Date(ms)) + ', ' + tm(ms);
  }
  function tipLabel(ms) {
    var d = dayDiff(bday(clock()), bday(ms));
    if (d === 0) return new Date(ms).getHours() >= 17 || new Date(ms).getHours() < 4 ? 'Tonight' : 'Today';
    if (d === 1) return 'Tomorrow';
    return wd(ms);
  }
  function nextGame(p) {
    var n = obj(obj(p).next);
    var tip = num(n.tip_ms);
    var when_ = tip ? tipLabel(tip) : (ymd(n.date) ? wd(ymd(n.date).getTime()) : '');
    if (!when_ && !n.opp) return null;
    var where = n.opp ? (n.home === false ? 'at ' : 'vs ') + n.opp : '';
    return { short: (when_ === 'Tomorrow' && tip ? wd(tip) : when_) + (where ? ' ' + where : ''), day: when_, where: where, tip: tip };
  }
  function lockSvg(open) { return '<svg aria-hidden="true" viewBox="0 0 16 18"><use href="#i-' + (open ? 'open' : 'lock') + '"/></svg>'; }

  /* ---------- data ---------- */
  function cacheGet(url) {
    var key = 'gm.cache:' + url;
    try {
      var v = JSON.parse(localStorage.getItem(key) || 'null');
      if (v && 'data' in v && num(v.t) > 0 && Date.now() - v.t <= CACHE_MAX_AGE) return v;
      localStorage.removeItem(key);
    } catch (e) { try { localStorage.removeItem(key); } catch (ignored) { /* storage blocked */ } }
    return null;
  }
  function cachePut(url, data) {
    try { localStorage.setItem('gm.cache:' + url, JSON.stringify({ t: Date.now(), data: data })); } catch (e) { /* storage full or blocked */ }
  }
  function getJSON(name, params) {
    var demo = S.demo || S.fallback;
    var url = demo ? 'demo/' + name + '.json'
      : 'api/' + name + (params ? '?' + new URLSearchParams(params).toString() : '');
    function offline() {
      var c = cacheGet(url);
      if (c) return { data: c.data, stale: true };
      var e = new Error('offline'); e.kind = 'offline'; throw e;
    }
    return fetch(url, { cache: 'no-store', headers: { accept: 'application/json' } }).then(function (res) {
      if (res.headers.get('x-gm-offline')) return offline();
      if (res.status === 404 && !demo) { var e = new Error('missing'); e.kind = 'missing'; throw e; }
      if (!res.ok) { var c = cacheGet(url); if (c) return { data: c.data, stale: true }; var e2 = new Error('http ' + res.status); e2.kind = 'http'; e2.status = res.status; throw e2; }
      var fromSw = res.headers.get('x-gm-cache');
      var cachedAt = Number(res.headers.get('x-gm-cached-at'));
      if (fromSw && (!(cachedAt > 0) || Date.now() - cachedAt > CACHE_MAX_AGE)) return offline();
      return res.json().then(function (data) {
        if (!fromSw) cachePut(url, data);
        return { data: data, stale: !!fromSw };
      }, function () { var e3 = new Error('bad json'); e3.kind = 'http'; throw e3; });
    }, offline);
  }

  function load() {
    if (S.loading) return;
    S.loading = true;
    var stale = false;
    function mark(r) { if (r && r.stale) stale = true; return r ? r.data : null; }
    getJSON('leagues').then(function (r) {
      var list = arr(mark(r)).filter(function (l) { return l && l.id != null; });
      if (!list.length && !S.demo && !S.fallback) { var e = new Error('missing'); e.kind = 'missing'; throw e; }
      S.leagues = list;
      var want = qs.get('league');
      if (!want) { try { want = localStorage.getItem('gm.league'); } catch (e2) { want = null; } }
      var pick = list.filter(function (l) { return String(l.id) === String(want); })[0] || list[0] || null;
      S.league = pick ? String(pick.id) : null;
      return getJSON('now', S.league ? { league: S.league } : null);
    }).then(function (r) {
      var now = mark(r);
      if (!now || typeof now !== 'object') { var e = new Error('missing'); e.kind = 'missing'; throw e; }
      S.now = now;
      var wk = num(obj(now.league).week);
      var p = { league: S.league || obj(now.league).id || '' };
      var pr = wk != null ? { league: p.league, week: wk } : p;
      return Promise.all([
        getJSON('runs', pr).then(mark, function () { return null; }),
        getJSON('season', p).then(mark, function () { return null; })
      ]);
    }).then(function (rs) {
      var isObj = function (x) { return x && typeof x === 'object' && !Array.isArray(x); };
      if (rs[0] != null) S.runs = arr(rs[0]).filter(isObj);
      if (rs[1] != null) S.season = arr(rs[1]).filter(isObj);
      S.error = null;
      S.staleFrom = stale ? (num(obj(S.now).t) || Date.now()) : null;
    }).catch(function (e) {
      if (e && e.kind === 'missing' && !S.demo && !S.fallback) {
        S.fallback = true; S.loading = false; load(); return 'again';
      }
      S.error = (e && e.kind) || 'http';
    }).then(function (again) {
      if (again === 'again') return;
      S.loading = false;
      S.lastLoad = Date.now();
      render();
    });
  }

  /* ---------- header + notices ---------- */
  function renderHeader() {
    var now = obj(S.now), lg = obj(now.league);
    var wk = num(lg.week);
    $('week').textContent = wk != null ? 'Week ' + wk : 'Sleeper GM';
    document.title = wk != null ? 'Week ' + wk + ' | Sleeper GM' : 'Sleeper GM';
    var L = $('league');
    if (S.leagues.length > 1) {
      L.innerHTML = '<label class="sr" for="league-pick">League</label><select id="league-pick">' +
        S.leagues.map(function (l) {
          return '<option value="' + esc(l.id) + '"' + (String(l.id) === S.league ? ' selected' : '') + '>' + esc(l.name || 'League ' + l.id) + '</option>';
        }).join('') + '</select>';
    } else {
      L.textContent = lg.name || (S.leagues[0] && S.leagues[0].name) || '';
    }
    var st = $('status');
    if (!S.now) { st.hidden = true; return; }
    st.hidden = false;
    var live = now.live === true;
    st.className = 'status' + (live ? '' : ' paused');
    var t = num(now.t);
    st.innerHTML = '<button type="button" class="icon-btn" style="width:auto;height:auto;border-radius:8px;padding:4px 6px;text-align:right;display:block" aria-expanded="' + S.showStatus + '" aria-controls="notice" id="status-btn">' +
      '<b><i class="led" aria-hidden="true"></i>' + (live ? 'Live' : 'Paused') + '</b>' +
      (t ? '<small>Updated ' + esc(when(t)) + '</small>' : '') + '</button>';
  }

  function renderNotice() {
    var n = $('notice'), html = '', bad = false;
    var t = num(obj(S.now).t);
    if (S.error && !S.now) {
      bad = true;
      html = S.error === 'offline'
        ? "Couldn't reach the manager, and this phone has no saved update yet. Check that the server is running and you are on its network."
        : "The manager's server answered with an error, and there is no saved update to show. Try again in a minute.";
    } else if (S.error || S.staleFrom) {
      bad = true;
      html = "Couldn't reach the manager. Showing the last update from " + esc(t ? when(t) : 'earlier') + '.';
    } else if (S.fallback) {
      html = 'Sample week. The manager has no data on this server yet, so this is a made-up league. Your league shows here after its first run.';
    } else if (S.showStatus && S.now) {
      html = S.now.live === true
        ? 'Live: the manager is making changes in Sleeper on its own. Every move shows up in Moves.'
        : 'Paused: the manager only reports. It works out every call but sends nothing to Sleeper.';
    }
    n.hidden = !html;
    n.className = 'notice' + (bad ? ' bad' : '');
    n.innerHTML = html;
  }

  /* ---------- the win line ---------- */
  function noise(seed) {
    // smooth 1D value noise in [-1, 1], deterministic so the stroke doesn't wobble on refresh
    function h(i) { var x = Math.sin(i * 127.1 + seed * 311.7) * 43758.5453; return (x - Math.floor(x)) * 2 - 1; }
    return function (x) { var i = Math.floor(x), f = x - i, u = f * f * (3 - 2 * f); return h(i) * (1 - u) + h(i + 1) * u; };
  }
  function monotone(P) {
    // Fritsch-Carlson monotone cubic through pixel points: smooth, never overshoots 0-100%
    var n = P.length, d = [], m = [], i;
    for (i = 0; i < n - 1; i++) d[i] = (P[i + 1][1] - P[i][1]) / ((P[i + 1][0] - P[i][0]) || 1);
    m[0] = d[0]; m[n - 1] = d[n - 2];
    for (i = 1; i < n - 1; i++) m[i] = d[i - 1] * d[i] <= 0 ? 0 : (d[i - 1] + d[i]) / 2;
    for (i = 0; i < n - 1; i++) {
      if (d[i] === 0) { m[i] = 0; m[i + 1] = 0; continue; }
      var a = m[i] / d[i], b = m[i + 1] / d[i], s = a * a + b * b;
      if (s > 9) { var k = 3 / Math.sqrt(s); m[i] = k * a * d[i]; m[i + 1] = k * b * d[i]; }
    }
    var out = [P[0]];
    for (i = 0; i < n - 1; i++) {
      var x0 = P[i][0], y0 = P[i][1], x1 = P[i + 1][0], y1 = P[i + 1][1], dx = x1 - x0;
      var c1x = x0 + dx / 3, c1y = y0 + m[i] * dx / 3, c2x = x1 - dx / 3, c2y = y1 - m[i + 1] * dx / 3;
      var steps = Math.max(2, Math.ceil(dx / 4));
      for (var j = 1; j <= steps; j++) {
        var t = j / steps, u = 1 - t;
        out.push([u * u * u * x0 + 3 * u * u * t * c1x + 3 * u * t * t * c2x + t * t * t * x1,
                  u * u * u * y0 + 3 * u * u * t * c1y + 3 * u * t * t * c2y + t * t * t * y1]);
      }
    }
    return out;
  }
  function inkPath(pts, seed, amp, dx, dy) {
    var nz = noise(seed), s = '';
    for (var i = 0; i < pts.length; i++) {
      var x = pts[i][0] + dx, y = pts[i][1] + dy + amp * nz(pts[i][0] / 9);
      s += (i ? 'L' : 'M') + x.toFixed(1) + ' ' + y.toFixed(1);
    }
    return s;
  }
  function lockEvents(run) {
    var acted = arr(run.acted).map(String).filter(function (a) { return /^Locked\s/.test(a); });
    return acted.map(function (a) { return a.replace(/^Locked\s+/, '').trim(); }).filter(Boolean);
  }

  function weekStart() {
    var now = obj(S.now), ref = num(now.t);
    if (!ref) { var r0 = S.runs[S.runs.length - 1]; ref = r0 && num(r0.t) ? num(r0.t) * 1000 : clock(); }
    var d = bday(ref), back = (d.getDay() + 6) % 7;
    return new Date(d.getFullYear(), d.getMonth(), d.getDate() - back);
  }

  function drawWin() {
    var el = $('winline');
    if (!el) return;
    var now = obj(S.now);
    var W = Math.max(280, Math.round(el.clientWidth || 358));
    var H = Math.round(Math.min(270, Math.max(212, W * 0.6)));
    var padL = 36, padR = 6, padT = 16, padB = 32;
    var pw = W - padL - padR, ph = H - padT - padB;
    var mon = weekStart();
    function fx(ms) {
      var d = bday(ms), idx = dayDiff(mon, d);
      var h = (ms - 4 * HOUR - d.getTime()) / HOUR;              // hours since 4 am
      var f = Math.min(1, Math.max(0, (h - 5) / 17)) * 0.92 + 0.04; // 9 am .. 2 am fills the column
      return Math.min(1, Math.max(0, (idx + f) / 7));
    }
    var ps = S.runs.map(function (r) { return num(obj(r).p_win); }).concat([num(now.p_win)]).filter(function (p) { return p != null; });
    var lo = Math.max(0, Math.floor((Math.min.apply(null, ps.concat([0.5])) - 0.1) * 10) / 10);
    var hi = Math.min(1, Math.ceil((Math.max.apply(null, ps.concat([0.5])) + 0.1) * 10) / 10);
    if (hi - lo < 0.4) { var padd = (0.4 - (hi - lo)) / 2; lo = Math.max(0, lo - padd); hi = Math.min(1, hi + padd); }
    var X = function (f) { return padL + f * pw; }, Y = function (p) { return padT + (1 - (Math.min(hi, Math.max(lo, p)) - lo) / (hi - lo)) * ph; };

    var pts = [];
    S.runs.slice().sort(function (a, b) { return (num(a.t) || 0) - (num(b.t) || 0); }).forEach(function (r) {
      var t = num(r.t), p = num(r.p_win);
      if (t == null || p == null) return;
      pts.push({ x: X(fx(t * 1000)), y: Y(p), p: p, t: t * 1000, run: r, locks: lockEvents(r) });
    });
    var nowP = num(now.p_win), nowT = num(now.t);
    if (nowP != null && nowT && (!pts.length || nowT > pts[pts.length - 1].t + 5 * 60000)) {
      pts.push({ x: X(fx(nowT)), y: Y(nowP), p: nowP, t: nowT, run: null, locks: [] });
    }
    var finalP = nowP != null ? nowP : (pts.length ? pts[pts.length - 1].p : null);
    if (pts.length && nowP != null) { pts[pts.length - 1].p = nowP; pts[pts.length - 1].y = Y(nowP); }

    var today = bday(clock()), todayIdx = dayDiff(mon, today);
    var left = {};
    arr(now.days_left).forEach(function (s) { var d = ymd(s); if (d) left[dayDiff(mon, bday(d.getTime() + 4 * HOUR))] = true; });
    var names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
    var g = [];
    g.push('<svg viewBox="0 0 ' + W + ' ' + H + '" width="' + W + '" height="' + H + '" role="group" aria-labelledby="wl-title">');
    var first = pts[0], last = pts[pts.length - 1];
    g.push('<title id="wl-title">' + esc(first ? 'Win chance this week, from ' + pct(first.p) + ' at the first run to ' + pct(finalP) + ' now' : 'Win chance this week') + '</title>');
    if (todayIdx >= 0 && todayIdx < 7) g.push('<rect class="wl-today" x="' + X(todayIdx / 7).toFixed(1) + '" y="' + padT + '" width="' + (pw / 7).toFixed(1) + '" height="' + ph + '"/>');
    for (var i = 1; i < 7; i++) g.push('<line class="wl-col" x1="' + X(i / 7).toFixed(1) + '" x2="' + X(i / 7).toFixed(1) + '" y1="' + padT + '" y2="' + (padT + ph) + '"/>');
    g.push('<line class="wl-grid" x1="' + padL + '" x2="' + (W - padR) + '" y1="' + Y(hi) + '" y2="' + Y(hi) + '"/>');
    g.push('<line class="wl-half" x1="' + padL + '" x2="' + (W - padR) + '" y1="' + Y(0.5) + '" y2="' + Y(0.5) + '"/>');
    g.push('<line class="wl-grid" style="stroke-width:2" x1="' + padL + '" x2="' + (W - padR) + '" y1="' + Y(lo) + '" y2="' + Y(lo) + '"/>');
    [[hi, Math.round(hi * 100) + '%'], [0.5, '50%'], [lo, Math.round(lo * 100) + '%']].forEach(function (a) {
      g.push('<text class="wl-ylab" x="' + (padL - 7) + '" y="' + (Y(a[0]) + 4) + '" text-anchor="end">' + a[1] + '</text>');
    });
    names.forEach(function (n, k) {
      var cx = X((k + 0.5) / 7), cls = 'wl-day' + (k === todayIdx ? ' today' : (left[k] ? ' left' : ''));
      g.push('<text class="' + cls + '" x="' + cx.toFixed(1) + '" y="' + (H - 9) + '" text-anchor="middle">' + n + '</text>');
      if (k === todayIdx) g.push('<line class="wl-ul" x1="' + (cx - 11).toFixed(1) + '" x2="' + (cx + 11).toFixed(1) + '" y1="' + (H - 2) + '" y2="' + (H - 3) + '"/>');
    });

    if (!pts.length) {
      g.push('<text class="wl-empty" x="' + (padL + pw / 2) + '" y="' + (padT + ph / 2 - 8) + '" text-anchor="middle">No win chance yet.</text>');
      g.push('<text class="wl-empty" x="' + (padL + pw / 2) + '" y="' + (padT + ph / 2 + 12) + '" text-anchor="middle">The line starts with the first run of the week.</text>');
    } else {
      var P = [];
      pts.forEach(function (q) {
        if (P.length && q.x - P[P.length - 1][0] < 0.6) P[P.length - 1] = [P[P.length - 1][0], q.y];
        else P.push([q.x, q.y]);
      });
      var curve = P.length > 1 ? monotone(P) : [[P[0][0] - 0.5, P[0][1]], [P[0][0] + 0.5, P[0][1]]];
      g.push('<path class="wl-ink ghost" d="' + inkPath(curve, 3, 0.9, 0.7, -0.9) + '"/>');
      g.push('<path class="wl-ink main" d="' + inkPath(curve, 1, 0.55, 0, 0) + '"/>');
      g.push('<circle class="wl-end" cx="' + last.x.toFixed(1) + '" cy="' + last.y.toFixed(1) + '" r="5"/>');

      // padlocks pinned where locks happened
      var lastPinX = -99, lastPinUp = true;
      pts.forEach(function (q, k) {
        if (!q.locks.length) return;
        var up = q.y - padT > 34;
        if (Math.abs(q.x - lastPinX) < 20 && up === lastPinUp) up = !up;
        lastPinX = q.x; lastPinUp = up;
        var dir = up ? -1 : 1, cy = q.y + dir * 24;
        var dry = q.run && q.run.live === false;
        var label = (dry ? 'Would have locked ' : 'Locked ') + q.locks.join(', ') + ', ' + when(q.t);
        g.push('<g class="wl-pin' + (S.pin === k ? ' on' : '') + '" data-pin="' + k + '" tabindex="0" role="button" aria-pressed="' + (S.pin === k) + '" aria-label="' + esc(label) + '">' +
          '<circle class="hit" cx="' + q.x.toFixed(1) + '" cy="' + cy.toFixed(1) + '" r="17"/>' +
          '<line class="stem" x1="' + q.x.toFixed(1) + '" x2="' + q.x.toFixed(1) + '" y1="' + (q.y + dir * 5).toFixed(1) + '" y2="' + (cy - dir * 9).toFixed(1) + '"/>' +
          '<circle class="ring" cx="' + q.x.toFixed(1) + '" cy="' + cy.toFixed(1) + '" r="14"/>' +
          '<use href="#i-' + (dry ? 'open' : 'lock') + '" x="' + (q.x - 7.5).toFixed(1) + '" y="' + (cy - 9).toFixed(1) + '" width="15" height="17"/></g>');
      });

      // the current number, written at the line's end
      var fs = Math.round(Math.min(80, Math.max(56, W * 0.18)));
      g.push('<text class="wl-pct" id="wl-pct" font-size="' + fs + '">' + esc(pct(finalP)) + '</text>');
      g.push('<text class="wl-pct-sub" id="wl-sub">to win</text>');
    }
    g.push('</svg>');
    el.innerHTML = g.join('');

    var T = $('wl-pct');
    if (T && last) {
      var sub = $('wl-sub'), tw = T.getComputedTextLength ? T.getComputedTextLength() : fs * 1.6;
      var subW = sub.getComputedTextLength ? sub.getComputedTextLength() : 40;
      var bw = Math.max(tw, subW), x, base, anchor = 'start';
      if (last.x + 14 + bw <= W - padR) {
        x = last.x + 14;
        base = Math.min(padT + ph - 22, Math.max(padT + fs * 0.72, last.y + fs * 0.34));
      } else {
        x = W - padR; anchor = 'end';
        base = last.y - padT > fs + 28 ? last.y - 26 : Math.min(padT + ph - 22, last.y + fs + 14);
      }
      T.setAttribute('x', x.toFixed(1)); T.setAttribute('y', base.toFixed(1)); T.setAttribute('text-anchor', anchor);
      sub.setAttribute('x', (anchor === 'end' ? x : x + 2).toFixed(1)); sub.setAttribute('y', (base + 18).toFixed(1)); sub.setAttribute('text-anchor', anchor);
    }
    S._pts = pts;
    renderCaption();
  }

  function renderCaption() {
    var c = $('caption');
    if (!c) return;
    var pts = S._pts || [];
    var idx = S.pin;
    if (idx == null || !pts[idx]) {
      for (var i = pts.length - 1; i >= 0; i--) if (pts[i].locks.length) { idx = i; break; }
    }
    var q = pts[idx];
    if (q && q.locks.length) {
      var prev = idx > 0 ? pts[idx - 1].p : null;
      var dryRun = q.run && q.run.live === false;
      c.innerHTML = lockSvg(dryRun) + esc(when(q.t)) + (dryRun ? ': would have locked ' : ': locked ') + esc(q.locks.join(', ')) + (dryRun ? ' (paused, not sent). ' : '. ') +
        (prev != null && Math.round(prev * 100) !== Math.round(q.p * 100)
          ? 'Win chance ' + pct(prev) + ' to ' + pct(q.p) + '.'
          : 'Win chance ' + pct(q.p) + '.');
    } else if (pts.length) {
      c.textContent = 'It started the week at ' + pct(pts[0].p) + '. No locks yet.';
    } else {
      c.textContent = '';
    }
  }

  /* ---------- board ---------- */
  function tag(p) {
    var inj = obj(p).injury;
    if (!inj || inj === 'null' || inj === 'None') return '';
    var soft = /^(DTD|GTD|Q|Questionable|Probable|P)$/i.test(inj);
    return '<span class="tag' + (soft ? ' soft' : '') + '">' + esc(inj) + '</span>';
  }
  function facts(list) {
    list = list.filter(function (f) { return f && f[1] !== '' && f[1] != null; });
    if (!list.length) return '';
    return '<dl class="facts">' + list.map(function (f) {
      return '<div' + (f[2] ? ' class="wide"' : '') + '><dt>' + esc(f[0]) + '</dt><dd>' + esc(f[1]) + '</dd></div>';
    }).join('') + '</dl>';
  }
  function playerFacts(p, extra, skipNext) {
    p = obj(p);
    var n = nextGame(p), gl = num(p.games_left), pp = num(p.p_play), pr = num(p.proj);
    var list = [
      ['Games left this week', gl == null ? '' : gl === 0 ? 'None' : String(gl)],
      ['Chance he plays', pp == null ? '' : pct(pp)],
      ['Projection', pr == null ? '' : f1(pr) + ' next game'],
      ['Next game', skipNext ? '' : n && n.tip ? wd(n.tip) + ' ' + tm(n.tip) + (n.where ? ' ' + n.where : '') : (n ? n.short : '')],
      ['Back from injury', p['return'] ? 'About ' + shortDate(p['return']) : ''],
      ['Plays', arr(p.pos).join(', ')]
    ];
    return facts((extra || []).concat(list));
  }
  function rowWrap(key, cls, btnInner, detailHtml) {
    var open = !!S.open[key], id = 'd-' + key.replace(/[^a-z0-9]/gi, '-');
    if (!detailHtml) return '<li class="' + cls + '"><div class="row-btn" style="cursor:default">' + btnInner + '</div></li>';
    return '<li class="' + cls + (open ? ' open' : '') + '" data-key="' + esc(key) + '">' +
      '<button type="button" class="row-btn" data-toggle="' + esc(key) + '" aria-expanded="' + open + '" aria-controls="' + id + '">' + btnInner + '</button>' +
      '<div class="detail" id="' + id + '"><div>' + detailHtml + '</div></div></li>';
  }
  function barHtml(o, scale) {
    function w(v) { return Math.max(0, Math.min(100, v / scale * 100)).toFixed(1) + '%'; }
    var s = '<span class="bar" aria-hidden="true">';
    if (o.proj != null) s += '<i class="proj" style="width:' + w(o.proj) + '"></i>';
    if (o.fill != null) s += '<i class="fill" style="width:' + w(o.fill) + '"></i>';
    if (o.tick != null) s += '<i class="tick" style="left:' + w(o.tick) + '"></i>';
    return s + '</span>';
  }

  function crowdLine(signal) {
    var c = obj(signal);
    if (!(num(c.n) > 0)) return '';
    if (num(c.lock_share) == null) return 'Sleeper chat: no lock/pass votes';
    return 'Sleeper chat: ' + pct(c.lock_share) + ' lock (' + plural((num(c.lock) || 0) + (num(c.pass) || 0), 'vote') + ')';
  }

  function planHtml(plan) {
    if (!plan || plan.locking) return '';
    var looks = arr(plan.looks);
    var time = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', weekday: 'short', hour: 'numeric', minute: '2-digit' });
    var h = '<span class="slot-plan"><span class="plan-label">Next looks <span>' + esc(f1(plan.expected_value)) + ' expected</span></span>';
    if (!looks.length) return h + '<span class="plan-empty">No further looks; hold this slot.</span></span>';
    h += '<span class="looks">';
    looks.forEach(function (look, i) {
      var name = String(look.name || 'Unknown player').split(' ').slice(-1)[0];
      var when = num(look.tip_ms) != null ? time.format(new Date(look.tip_ms)).replace(':00', '').replace(', ', ' ').toLowerCase()
        : String(look.date || '');
      when = when.charAt(0).toUpperCase() + when.slice(1);
      if (i) h += '<span class="look-arrow" aria-label="then">→</span>';
      h += '<span class="look"><b>' + esc(name) + '</b> <span class="look-time">' + esc(when) + '</span>' +
        '<span class="lock-bar">' + (look.terminal ? 'final game' : 'lock at ' + esc(f1(look.lock_above)) + '+') + '</span></span>';
    });
    return h + '</span><span class="plan-note">If you pass · ET' +
      (looks.some(function (x) { return num(x.handoff_p) != null && x.handoff_p < 1; }) ? ' · same-day swap needs a final box' : '') + '</span></span>';
  }

  function slotRow(s, i, scale, ctx) {
    s = obj(s);
    var p = s.player && typeof s.player === 'object' ? s.player : null;
    var st = String(s.state || (p ? 'open' : 'empty'));
    var dec = s.decision === 'lock' || s.decision === 'pass' ? s.decision : null;
    var sc = num(s.score), ov = num(s.open_value), sv = num(s.slot_value);
    var worth = ov != null ? ov : sv;
    var pausedLock = false;
    if (st === 'locked' && s.just_locked && ctx.paused) { st = 'pending'; dec = 'lock'; pausedLock = true; }
    if (!p) st = 'empty';
    else if (!/^(locked|pending|playing|open)$/.test(st)) st = 'open';
    var key = 'slot-' + i + '-' + (p && p.id != null ? p.id : 'x');
    var slot = esc(s.slot || '');
    var who = p
      ? '<span class="who-line"><span class="pname">' + esc(p.name || 'Unknown player') + '</span>' + (p.team ? '<span class="team">' + esc(p.team) + '</span>' : '') + tag(p) + '</span>'
      : '<span class="who-line"><span class="empty-name">Empty slot</span></span>';
    var bar = {}, call = '', reason = '', extra = [];
    var n = p ? nextGame(p) : null;
    var proj = p ? num(p.proj) : null;

    if (st === 'locked') {
      bar.fill = sc;
      call = '<span class="call locked"><span class="big">' + lockSvg() + esc(f1(sc) || 'Locked') + '</span>' +
        (s.just_locked ? '<span class="sub">Just locked</span>' : '<span class="sub">Locked</span>') + '</span>';
      extra.push(['Locked score', f1(sc)]);
    } else if (st === 'pending') {
      bar.fill = sc; bar.tick = worth;
      var word = dec === 'lock' ? 'Lock' : dec === 'pass' ? 'Wait' : 'Open';
      call = '<span class="call ' + (dec === 'lock' ? 'lock' : 'wait') + '"><span class="circled">' + word + '</span></span>';
      var behind = num(ctx.pwin) != null && ctx.pwin < 0.5;
      if (sc != null && worth != null) {
        if (dec === 'lock') reason = sc >= worth ? f1(sc) + ' beats the ' + f1(worth) + ' his slot is worth.'
          : f1(sc) + ' is under the ' + f1(worth) + ' his slot is worth, but ' + (behind ? 'locking it' : 'the sure points') + ' help you win.';
        else if (dec === 'pass') reason = sc < worth ? f1(sc) + ' is under the ' + f1(worth) + ' his slot is worth.'
          : f1(sc) + ' beats the ' + f1(worth) + ' his slot is worth, but ' + (behind ? 'from behind it wants a bigger game.' : 'waiting gives a better shot at the win.');
        else reason = f1(sc) + ' scored. His slot is worth ' + f1(worth) + '.';
      } else if (sc != null) reason = f1(sc) + ' scored and can still be locked.';
      if (pausedLock) reason += ' Paused, so nothing was sent.';
      if (n && n.tip) {
        var left = n.tip - clock();
        if (left > 0 && left < 3 * HOUR) reason += ' <span class="hot">Lock window closes ' + esc(tm(n.tip)) + '.</span>';
        extra.push(['Lock window open until', wd(n.tip) + ' ' + tm(n.tip), true]);
      }
      if (reason.indexOf('<span') === -1) reason = esc(reason);
      else reason = esc(reason.slice(0, reason.indexOf(' <span'))) + reason.slice(reason.indexOf(' <span'));
    } else if (st === 'playing') {
      bar.proj = proj; bar.tick = worth;
      if (sc != null) bar.fill = sc;
      call = '<span class="call playing"><span class="big"><i class="live-dot" aria-hidden="true"></i>' + (sc != null ? esc(f1(sc)) : 'Playing') + '</span>' +
        (sc != null ? '<span class="sub">Playing now</span>' : '') + '</span>';
      if (worth != null) reason = esc('Worth locking if he tops ' + f1(worth) + '.');
    } else if (st === 'open') {
      bar.proj = proj; bar.tick = worth;
      call = n ? '<span class="call next"><span class="big">' + esc(n.short) + '</span>' + (n.tip ? '<span class="sub">' + esc(tm(n.tip)) + '</span>' : '') + '</span>'
        : '<span class="call empty"><span class="big">No game</span></span>';
      if (num(p.games_left) === 0) reason = 'No games left this week.';
      if (worth != null) extra.push(['His slot is worth', f1(worth)]);
    } else {
      call = '';
      reason = 'Scores 0 unless someone with a game left goes here.';
      if (worth != null) extra.push(['Worth filling', f1(worth) + ' expected']);
    }
    var inner = '<span class="pos">' + slot + '</span>' + who + call + barHtml(bar, scale) +
      (reason ? '<span class="reason' + (st === 'empty' || st === 'open' ? ' quiet' : '') + '">' + reason + '</span>' : '');
    var chat = st === 'pending' ? crowdLine(s.crowd) : '';
    if (chat) inner += '<span class="reason quiet crowd">' + esc(chat) + '</span>';
    if (st !== 'locked') inner += planHtml(s.plan);
    if (st === 'pending') arr(obj(s.crowd).news).forEach(function (text) { extra.push(['Sleeper chat (crowd)', text, true]); });
    var detail = p ? playerFacts(p, extra, st === 'pending') : facts(extra);
    return rowWrap(key, 'slot s-' + st, inner, detail);
  }

  function benchRow(p, kind, i) {
    p = obj(p);
    var key = kind + '-' + i + '-' + (p.id != null ? p.id : 'x');
    var n = nextGame(p), pr = num(p.proj);
    var right;
    if (kind === 'ir') right = p['return'] ? 'Back about ' + esc(shortDate(p['return'])) : 'No return date';
    else right = (n ? esc(n.short) : 'No games left') + (pr != null ? '<small>' + esc(f1(pr)) + ' expected</small>' : '');
    var inner = '<span class="who-line"><span class="pname">' + esc(p.name || 'Unknown player') + '</span>' +
      (p.team ? '<span class="team">' + esc(p.team) + '</span>' : '') + tag(p) + '</span>' +
      '<span class="right">' + right + '</span>';
    if (kind === 'bench' && p.role) inner += '<span class="bench-role">' + esc(p.role) +
      (num(p.games_left) != null ? '<span> · ' + esc(plural(p.games_left, 'game') + ' left') + '</span>' : '') + '</span>';
    return rowWrap(key, 'prow', inner, playerFacts(p));
  }

  function oppRow(s, i) {
    s = obj(s);
    var p = s.player && typeof s.player === 'object' ? s.player : null;
    var st = String(s.state || (p ? 'open' : 'empty')), sc = num(s.score), right = '';
    if (!p) st = 'empty';
    if (st === 'locked') right = '<span class="lk">' + lockSvg() + esc(f1(sc)) + '</span>';
    else if (st === 'pending') right = esc(f1(sc)) + '<small>Not locked</small>';
    else if (st === 'playing') right = '<i class="live-dot" aria-hidden="true"></i>Playing';
    else if (st === 'open') { var n = nextGame(p); right = n ? esc(n.short) : 'No game'; }
    else right = 'Empty';
    var inner = '<span class="pos">' + esc(s.slot || '') + '</span>' +
      '<span class="who-line"><span class="pname">' + (p ? esc(p.name || 'Unknown player') : 'Empty slot') + '</span>' +
      (p && p.team ? '<span class="team">' + esc(p.team) + '</span>' : '') + (p ? tag(p) : '') + '</span>' +
      '<span class="right">' + right + '</span>';
    return rowWrap('opp-' + i + '-' + (p && p.id != null ? p.id : 'x'), 'prow opp', inner, p ? playerFacts(p) : '');
  }

  function renderBoard() {
    var el = $('p-board');
    var now = S.now;
    if (!now) {
      el.innerHTML = S.error ? '<div class="empty-state"><strong>Nothing to show yet</strong>When the manager answers, this week shows up here.</div>' : '<div class="empty-state">Loading the board.</div>';
      return;
    }
    var me = obj(now.me), opp = obj(now.opp);
    var slots = arr(me.slots);
    var vals = [30];
    slots.forEach(function (s) {
      s = obj(s);
      [s.score, s.open_value, s.slot_value, obj(s.player).proj].forEach(function (v) { v = num(v); if (v != null) vals.push(v); });
    });
    var scale = Math.max.apply(null, vals) * 1.08;
    var ctx = { paused: now.live === false, pwin: num(now.p_win) };
    var h = [];
    h.push('<section class="hero"><div class="winline" id="winline"></div><p class="caption" id="caption"></p>');
    h.push('<div class="score" role="table" aria-label="Points">' +
      '<span role="columnheader"></span><span class="h" role="columnheader">Now</span><span class="h" role="columnheader">Projected</span>' +
      '<span class="who" role="rowheader">' + esc(me.name || 'You') + (me.record ? '<small>' + esc(me.record) + '</small>' : '') + '</span>' +
      '<span class="n" role="cell">' + esc(f1(me.points)) + '</span><span class="n p" role="cell">' + esc(f1(me.proj)) + '</span>' +
      '<span class="who" role="rowheader"><span class="vs">vs</span>' + esc(opp.name || 'Opponent') + (opp.record ? '<small>' + esc(opp.record) + '</small>' : '') + '</span>' +
      '<span class="n" role="cell">' + esc(f1(opp.points)) + '</span><span class="n p" role="cell">' + esc(f1(opp.proj)) + '</span></div></section>');
    if (Array.isArray(now.days_left) && !now.days_left.length) {
      h.push('<div class="empty-state"><strong>No games left this week</strong>The board resets Monday.' + (slots.length ? ' Final calls are below.' : '') + '</div>');
    }
    h.push('<h2 class="sr">Starters</h2>');
    if (slots.length) h.push('<div class="key" aria-hidden="true"><span><i class="k-fill"></i>Score</span><span><i class="k-tick"></i>What his slot is worth</span><span><i class="k-proj"></i>Projected</span></div>');
    if (slots.length) h.push('<ul class="slots">' + slots.map(function (s, i) { return slotRow(s, i, scale, ctx); }).join('') + '</ul>');
    else h.push('<div class="empty-state"><strong>No lineup yet</strong>The starting slots show up after the first run of the week.</div>');

    var bench = arr(me.bench), ir = arr(me.ir);
    h.push('<section class="sec"><h2>Bench</h2>' + (bench.length
      ? '<ul class="plist">' + bench.map(function (p, i) { return benchRow(p, 'bench', i); }).join('') + '</ul>'
      : '<p class="sec-note">Nobody on the bench.</p>') + '</section>');
    if (ir.length) h.push('<section class="sec"><h2>Injured reserve</h2><ul class="plist">' + ir.map(function (p, i) { return benchRow(p, 'ir', i); }).join('') + '</ul></section>');

    var os = arr(opp.slots);
    if (os.length) {
      var locked = os.filter(function (s) { return obj(s).state === 'locked'; }).length;
      var ok = !!S.open['opp-lineup'];
      h.push('<section class="sec' + (ok ? ' open' : '') + '" data-key="opp-lineup"><button type="button" class="sec-toggle" data-toggle="opp-lineup" aria-expanded="' + ok + '" aria-controls="opp-lineup">' +
        '<h2>Their lineup</h2><span>' + esc(locked + ' of ' + os.length + ' locked') + '<i class="chev" aria-hidden="true"></i></span></button>' +
        '<div class="detail" id="opp-lineup"><div><ul class="plist">' + os.map(oppRow).join('') + '</ul></div></div></section>');
    }
    el.innerHTML = h.join('');
    drawWin();
  }

  /* ---------- moves ---------- */
  function tidy(a) {
    var s = String(a == null ? '' : a), win = null, kind = 'note', m;
    m = s.match(/,?\s*week win\s*(\d+(?:\.\d+)?)%\s*->\s*(\d+(?:\.\d+)?)%/i);
    if (m) { win = [+m[1], +m[2]]; s = s.replace(m[0], ''); }
    s = s.replace(/\((\d+(?:\.\d+)?)\/g(?: ROS)?\)/g, '($1 a game)');
    s = s.replace(/\b(\d{4}-\d{2}-\d{2})\b/g, function (d) { return shortDate(d); });
    if (/^NEEDS YOU/i.test(s)) {
      kind = 'needs';
      s = s.replace(/^NEEDS YOU\s*(?:\(([^)]*)\))?\s*:?\s*/i, function (_, why) { return why ? cap(why) + '. ' : ''; });
      s = cap(s);
    } else if (/^Locked\s/.test(s)) kind = 'lock';
    else if (/^Added\s/.test(s)) { kind = 'add'; s = s.replace(/^Added open roster spot:\s*/i, 'Added '); }
    else if (/^Swap:?\s/i.test(s)) { kind = 'swap'; s = s.replace(/^Swap:?\s*(.+?)\s+for\s+(.+)$/i, 'Added $1, dropped $2'); }
    else if (/\b(to|off) IR\b/.test(s)) {
      kind = 'ir';
      s = s.replace(/^(.+?) to IR \(([^)]*)\)(?:, frees an active spot)?$/, 'Moved $1 to IR ($2)');
      s = s.replace(/^(.+?) off IR \(([^)]*)\),?\s*/, 'Took $1 off IR ($2). ');
    } else if (/^Lineup:/i.test(s)) {
      kind = 'lineup';
      m = s.match(/^Lineup:\s*in\s+(.*?)(?:\s*\/\s*out\s+(.*))?$/i);
      if (m) {
        var ins = m[1] && m[1] !== 'nobody' ? m[1] : '', outs = m[2] || '';
        s = (ins ? 'Started ' + ins : '') + (ins && outs ? ', benched ' + outs : outs ? 'Benched ' + outs : '');
        if (!s) s = 'Lineup unchanged';
      }
    } else if (/^(Missed|LINEUP WRITE FAILED|WRITE DID NOT STICK|Sleeper refused|Could not add|IR move failed)/i.test(s)) {
      kind = 'problem';
      s = s.replace(/^([A-Z][A-Z ]{5,}):/, function (x) { return x.charAt(0) + x.slice(1).toLowerCase(); });
    }
    return { kind: kind, text: s.replace(/\s+/g, ' ').trim(), win: win };
  }

  function buildFeed() {
    var runs = S.runs.slice().filter(function (r) { return r && num(r.t) != null; })
      .sort(function (a, b) { return num(a.t) - num(b.t); });
    var items = [], seen = {}, prevP = null;
    var nowSlots = arr(obj(S.now).me && S.now.me.slots);
    function pendingNow(name, score) {
      return nowSlots.filter(function (s) {
        s = obj(s);
        return s.state === 'pending' && obj(s.player).name === name && num(s.score) === num(score);
      })[0];
    }
    runs.forEach(function (r, ri) {
      var t = num(r.t) * 1000, p = num(r.p_win), decs = arr(r.decisions).map(obj);
      var lockedNames = {};
      arr(r.acted).forEach(function (a, ai) {
        var x = tidy(a);
        if (!x.text) return;
        var it = { t: t, i: ai, kind: x.kind, text: x.text, win: x.win, p: p, prevP: prevP, live: r.live !== false, raw: String(a) };
        if (x.kind === 'lock') {
          var lm = /^Locked\s+(.+?)\s+(-?\d+(?:\.\d+)?)$/.exec(x.text);
          if (lm) {
            lockedNames[lm[1]] = true;
            var d = decs.filter(function (d) { return d.player === lm[1]; })[0];
            if (!d) {
              for (var k = ri - 1; k >= 0 && !d; k--) {
                d = arr(runs[k].decisions).map(obj).filter(function (d) { return d.player === lm[1] && num(d.score) === num(lm[2]); })[0];
              }
            }
            it.score = num(lm[2]); it.ov = d ? num(d.open_value) : null;
          }
        }
        if (x.kind === 'needs' || x.kind === 'problem' || x.kind === 'note') {
          var sk = 'acted|' + x.text;
          if (seen[sk]) { seen[sk].again++; seen[sk].lastT = t; return; }
          it.again = 0; seen[sk] = it;
        }
        items.push(it);
      });
      decs.forEach(function (d, di) {
        if (!d.player || lockedNames[d.player]) return;
        if (d.decision !== 'pass' && d.decision !== 'lock') return;
        var k = d.decision + '|' + d.player + '|' + f1(d.score);
        if (seen[k]) { seen[k].again++; seen[k].lastT = t; return; }
        var it = {
          t: t, i: 100 + di, kind: d.decision === 'pass' ? 'pass' : 'call', player: d.player, slot: d.slot,
          score: num(d.score), ov: num(d.open_value), p: p, prevP: prevP, again: 0, live: r.live !== false,
          text: d.decision === 'pass' ? 'Passed on ' + d.player + ' ' + f1(d.score) : 'Lock call on ' + d.player + ' ' + f1(d.score),
          still: pendingNow(d.player, d.score)
        };
        seen[k] = it;
        items.push(it);
      });
      if (p != null) prevP = p;
    });
    items.sort(function (a, b) { return b.t - a.t || a.i - b.i; });
    return items;
  }

  function glyph(kind) {
    var g = {
      lock: '<svg viewBox="0 0 16 18"><use href="#i-lock"/></svg>',
      call: '<svg viewBox="0 0 16 18"><use href="#i-open"/></svg>',
      pass: '<svg viewBox="0 0 20 20"><circle cx="10" cy="10" r="7.5" fill="none" stroke="currentColor" stroke-width="2"/><path d="M10 6v4.5l3 2" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
      add: '<svg viewBox="0 0 20 20"><path d="M10 4v12M4 10h12" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg>',
      swap: '<svg viewBox="0 0 20 20"><path d="M4 7h11l-3-3M16 13H5l3 3" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
      ir: '<svg viewBox="0 0 20 20"><rect x="3" y="3" width="14" height="14" rx="3" fill="none" stroke="currentColor" stroke-width="2"/><path d="M10 6.5v7M6.5 10h7" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
      lineup: '<svg viewBox="0 0 20 20"><path d="M6 15V5L3.5 7.5M14 5v10l2.5-2.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
      needs: '<svg viewBox="0 0 20 20"><path d="M10 4v8" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"/><circle cx="10" cy="15.8" r="1.6" fill="currentColor"/></svg>',
      problem: '<svg viewBox="0 0 20 20"><path d="M10 4v8" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"/><circle cx="10" cy="15.8" r="1.6" fill="currentColor"/></svg>',
      note: '<svg viewBox="0 0 20 20"><circle cx="10" cy="10" r="2.6" fill="currentColor"/></svg>'
    };
    return g[kind] || g.note;
  }

  function whyHtml(it) {
    var out = [], behind = it.p != null && it.p < 0.5;
    if (it.kind === 'lock' || it.kind === 'pass' || it.kind === 'call') {
      if (it.score != null && it.ov != null) {
        var beat = it.score >= it.ov;
        if (it.kind === 'pass') out.push(beat
          ? f1(it.score) + ' beat the ' + f1(it.ov) + ' his slot was worth, but ' + (behind ? 'from behind it wanted a bigger game.' : 'waiting gave a better shot at the win.')
          : f1(it.score) + ' was under the ' + f1(it.ov) + ' his slot was worth, so it waited for a better game.');
        else out.push(beat
          ? f1(it.score) + ' beat the ' + f1(it.ov) + ' his slot was worth.'
          : f1(it.score) + ' was under the ' + f1(it.ov) + ' his slot was worth, but the sure points helped the win chance.');
      }
      if (it.kind === 'call') out.push('Not locked yet.' + (it.still && obj(obj(it.still.player).next).tip_ms ? ' The window is open until ' + wd(it.still.player.next.tip_ms) + ' ' + tm(it.still.player.next.tip_ms) + '.' : ''));
      if (it.kind === 'pass' && it.still) out.push('Still waiting' + (obj(obj(it.still.player).next).tip_ms ? ': he can be locked until ' + wd(it.still.player.next.tip_ms) + ' ' + tm(it.still.player.next.tip_ms) + '.' : '.'));
      else if (it.kind === 'pass' && it.again) out.push('It looked again in ' + plural(it.again, 'more run') + ' and kept waiting.');
    }
    if (it.win) out.push('Week win chance ' + it.win[0] + '% to ' + it.win[1] + '% with this move.');
    else if (it.p != null) {
      out.push(it.prevP != null && Math.round(it.prevP * 100) !== Math.round(it.p * 100)
        ? 'Win chance ' + pct(it.prevP) + ' before this run, ' + pct(it.p) + ' after.'
        : 'Win chance ' + pct(it.p) + ' at the time.');
    }
    if (it.again && it.lastT && it.kind !== 'pass') out.push('Repeated in ' + plural(it.again, 'more run') + ', last at ' + when(it.lastT) + '.');
    if (!it.live && /^(lock|add|swap|ir|lineup)$/.test(it.kind)) out.push('Paused at the time, so this was not sent to Sleeper.');
    if (/^(add|swap|ir|needs|problem|note|lineup)$/.test(it.kind)) out.unshift(it.text + (/[.!?]$/.test(it.text) ? '' : '.'));
    return out.map(function (x) { return '<p>' + esc(x) + '</p>'; }).join('');
  }

  function renderMoves() {
    var el = $('p-moves');
    if (!S.now && !S.runs.length) { el.innerHTML = '<div class="empty-state">Loading moves.</div>'; return; }
    var items = buildFeed();
    var nowNeeds = arr(obj(S.now).acted).map(tidy).filter(function (x) { return x.kind === 'needs'; });
    if (!nowNeeds.length && !S.now && S.runs.length) nowNeeds = arr(S.runs[S.runs.length - 1].acted).map(tidy).filter(function (x) { return x.kind === 'needs'; });
    var pinned = {};
    nowNeeds.forEach(function (x) { pinned[x.text] = true; });
    items = items.filter(function (it) { return !(it.kind === 'needs' && pinned[it.text]); });
    var h = [];
    if (nowNeeds.length) {
      h.push('<section class="needs" aria-label="Needs you"><h2>Needs you</h2>' + nowNeeds.map(function (x) { return '<p>' + esc(x.text) + '</p>'; }).join('') + '</section>');
    }
    if (!items.length) {
      h.push('<div class="empty-state"><strong>No moves yet this week</strong>Every lock, pass, pickup and lineup change shows up here as it happens.</div>');
    } else {
      var day = null, open = false;
      items.forEach(function (it, n) {
        var d = dkey(bday(it.t));
        if (d !== day) {
          if (open) h.push('</ul>');
          h.push('<h2 class="day-head">' + esc(fLong.format(bday(it.t))) + '</h2><ul class="feed">');
          day = d; open = true;
        }
        var key = 'mv-' + Math.round(it.t / 1000) + '-' + it.i + '-' + it.kind;
        var inner = '<span class="t">' + esc(tm(it.t)) + '</span><span class="g" aria-hidden="true">' + glyph(it.kind) + '</span>' +
          '<span class="txt">' + esc(it.text) + (!it.live && /^(lock|add|swap|ir|lineup)$/.test(it.kind) ? '<em>not sent</em>' : '') + '</span>';
        h.push(rowWrap(key, 'move k-' + it.kind, inner, '<div class="why">' + whyHtml(it) + '</div>'));
      });
      if (open) h.push('</ul>');
    }
    el.innerHTML = h.join('');
    var flag = document.querySelector('.tab-flag');
    if (flag) flag.hidden = !nowNeeds.length;
  }

  /* ---------- season ---------- */
  function renderSeason() {
    var el = $('p-season');
    var weeks = S.season.map(obj).filter(function (w) { return num(w.week) != null; })
      .sort(function (a, b) { return num(a.week) - num(b.week); });
    if (!weeks.length) {
      el.innerHTML = '<div class="empty-state"><strong>No finished weeks yet</strong>Week 1 shows up here the Monday after it ends.</div>';
      return;
    }
    var W = 0, L = 0, T = 0, effs = [], ranks = [], teams = null, sumP = 0, nP = 0, gotP = 0, briers = [];
    weeks.forEach(function (w) {
      var won = num(w.won);
      if (won === 1) W++; else if (won === 0) L++; else if (won === 0.5) T++;
      if (num(w.efficiency) != null) effs.push(num(w.efficiency));
      if (num(w.efficiency_rank) != null) ranks.push(num(w.efficiency_rank));
      if (num(w.teams) != null) teams = num(w.teams);
      if (num(w.p_win_first) != null && won != null) { sumP += num(w.p_win_first); gotP += won; nP++; }
      if (num(w.brier) != null) briers.push(num(w.brier));
    });
    function mean(a) { return a.reduce(function (x, y) { return x + y; }, 0) / a.length; }
    var rec = W + '-' + L + (T ? '-' + T : '');
    var sum = [rec + ' after ' + plural(weeks.length, 'week') + '.'];
    if (effs.length) sum.push('Lineups averaged ' + pct(mean(effs)) + ' of the best possible' +
      (ranks.length && teams ? ', about ' + ord(mean(ranks)) + ' of ' + teams + ' in the league.' : '.'));
    if (nP) sum.push('Its Monday odds added up to ' + sumP.toFixed(1) + ' wins; you won ' + (gotP % 1 ? gotP.toFixed(1) : gotP) + '.');

    var h = ['<p class="summary"><strong>' + esc(sum[0]) + '</strong> ' + esc(sum.slice(1).join(' ')) + '</p>'];
    h.push('<section class="sec" style="margin-top:22px"><h2>Week by week</h2><ul class="plist">');
    weeks.slice().reverse().forEach(function (w) {
      var won = num(w.won), eff = num(w.efficiency), rank = num(w.efficiency_rank), tm_ = num(w.teams) || teams;
      var res = won === 1 ? '<b class="won">Won</b>' : won === 0 ? '<b class="lost">Lost</b>' : won === 0.5 ? '<b class="tied">Tied</b>' : '';
      var inner = '<span class="wn">Week ' + esc(w.week) + '</span>' +
        '<span class="res">' + res + esc(f1(w.points)) + (num(w.opp_points) != null ? ' <span class="opp">to ' + esc(f1(w.opp_points)) + '</span>' : '') + '</span>' +
        '<span class="eff">' + (eff != null ? '<span class="track" aria-hidden="true"><i style="width:' + Math.max(0, Math.min(100, eff * 100)).toFixed(1) + '%"></i></span>' +
          '<span class="lab">' + esc(pct(eff)) + ' of the best lineup</span>' : '<span class="lab">No lineup check yet</span>') +
          (rank != null ? '<span class="rank">' + esc(ord(rank) + (tm_ ? ' of ' + tm_ : '')) + '</span>' : '') + '</span>';
      var det = facts([
        ['Best possible lineup', f1(w.max_points)],
        ['Monday win chance', pct(w.p_win_first)],
        ['Runs', num(w.runs) != null ? String(w.runs) : ''],
        ['Locks', num(w.locks) != null ? String(w.locks) : ''],
        ['Forecast miss (Brier)', num(w.brier) != null ? num(w.brier).toFixed(2) : '']
      ]);
      h.push(rowWrap('wk-' + w.week, 'wk', inner, det));
    });
    h.push('</ul></section>');

    var cal = weeks.filter(function (w) { return num(w.p_win_first) != null; });
    if (cal.length) {
      h.push('<section class="sec"><h2>How its odds held up</h2><p class="sec-note">Each dot is the win chance it gave on Monday. Filled means you won that week.</p>' +
        '<div class="calib" id="calib"></div><div class="legend"><span><i class="won"></i>Won</span><span><i></i>Lost</span>' +
        (briers.length ? '<span>Average miss ' + esc(mean(briers).toFixed(2)) + ' (0 is perfect, 0.25 is a coin flip)</span>' : '') + '</div></section>');
    }
    el.innerHTML = h.join('');
    drawCalib();
  }

  function drawCalib() {
    var el = $('calib');
    if (!el) return;
    var W = Math.max(280, Math.round(el.clientWidth || 358)), pad = 16;
    var cal = S.season.map(obj).filter(function (w) { return num(w.p_win_first) != null && num(w.week) != null; })
      .sort(function (a, b) { return num(a.p_win_first) - num(b.p_win_first); });
    var X = function (p) { return pad + Math.max(0, Math.min(1, p)) * (W - 2 * pad); };
    var lanes = [], placed = cal.map(function (w) {
      var x = X(num(w.p_win_first)), lane = 0;
      while (lanes[lane] != null && x - lanes[lane] < 22) lane++;
      lanes[lane] = x;
      return { w: w, x: x, lane: lane };
    });
    var top = 26 + 24 * Math.max(0, lanes.length - 1), axisY = top + 14, H = axisY + 28;
    var g = ['<svg viewBox="0 0 ' + W + ' ' + H + '" width="' + W + '" height="' + H + '" role="img" aria-label="' +
      esc(cal.map(function (c) { return 'Week ' + c.week + ': ' + pct(c.p_win_first) + ', ' + (num(c.won) === 1 ? 'won' : num(c.won) === 0.5 ? 'tied' : 'lost'); }).join('. ')) + '">'];
    g.push('<line class="cal-axis" x1="' + pad + '" x2="' + (W - pad) + '" y1="' + axisY + '" y2="' + axisY + '"/>');
    [0, 0.5, 1].forEach(function (p) {
      g.push('<line class="cal-tick" x1="' + X(p) + '" x2="' + X(p) + '" y1="' + (axisY - 5) + '" y2="' + (axisY + 5) + '"/>' +
        '<text class="cal-lab" x="' + X(p) + '" y="' + (axisY + 22) + '" text-anchor="middle">' + Math.round(p * 100) + '%</text>');
    });
    placed.forEach(function (d) {
      var cy = axisY - d.lane * 24;
      g.push('<circle class="cal-dot' + (num(d.w.won) >= 0.5 ? ' won' : '') + '" cx="' + d.x.toFixed(1) + '" cy="' + cy + '" r="7.5"/>' +
        '<text class="cal-wk" x="' + d.x.toFixed(1) + '" y="' + (cy - 13) + '" text-anchor="middle">' + esc(d.w.week) + '</text>');
    });
    g.push('</svg>');
    el.innerHTML = g.join('');
  }

  /* ---------- shell ---------- */
  function currentTab() {
    var t = (location.hash || '').replace('#', '');
    return t === 'moves' || t === 'season' ? t : 'board';
  }
  function showTab() {
    var t = currentTab();
    ['board', 'moves', 'season'].forEach(function (k) { $('p-' + k).hidden = k !== t; });
    document.querySelectorAll('.tab').forEach(function (a) {
      if (a.getAttribute('data-tab') === t) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });
    if (t === 'board') drawWin();
    if (t === 'season') drawCalib();
  }
  function render() {
    var focusKey = document.activeElement && document.activeElement.getAttribute && document.activeElement.getAttribute('data-toggle');
    renderHeader();
    renderNotice();
    renderBoard();
    renderMoves();
    renderSeason();
    showTab();
    if (focusKey) {
      var f = document.querySelector('[data-toggle="' + (window.CSS && CSS.escape ? CSS.escape(focusKey) : focusKey) + '"]');
      if (f) f.focus({ preventScroll: true });
    }
  }

  function effectiveTheme() {
    var a = document.documentElement.getAttribute('data-theme');
    if (a === 'dark' || a === 'light') return a;
    return window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  function syncTheme() {
    var t = effectiveTheme();
    var c = t === 'dark' ? '#1A1D21' : '#F6F7F5';
    document.querySelectorAll('meta[name="theme-color"]').forEach(function (m) { m.setAttribute('content', c); });
    $('theme').setAttribute('aria-label', t === 'dark' ? 'Switch to the light board' : 'Switch to the dark board');
  }

  document.addEventListener('click', function (e) {
    var tg = e.target.closest && e.target.closest('[data-toggle]');
    if (tg) {
      var key = tg.getAttribute('data-toggle');
      S.open[key] = !S.open[key];
      var li = tg.closest('[data-key]');
      if (li) li.classList.toggle('open', !!S.open[key]);
      tg.setAttribute('aria-expanded', String(!!S.open[key]));
      return;
    }
    var pin = e.target.closest && e.target.closest('.wl-pin');
    if (pin) { togglePin(+pin.getAttribute('data-pin')); return; }
    if (e.target.closest && e.target.closest('#status-btn')) {
      S.showStatus = !S.showStatus; renderNotice(); renderHeader(); var b = $('status-btn'); if (b) b.focus(); return;
    }
    if (e.target.closest && e.target.closest('#theme')) {
      var next = effectiveTheme() === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      try { localStorage.setItem('gm.theme', next); } catch (err) { /* private mode */ }
      syncTheme();
    }
  });
  document.addEventListener('keydown', function (e) {
    var pin = e.target.closest && e.target.closest('.wl-pin');
    if (pin && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); togglePin(+pin.getAttribute('data-pin')); }
  });
  function togglePin(k) {
    S.pin = S.pin === k ? null : k;
    document.querySelectorAll('.wl-pin').forEach(function (g) {
      var on = +g.getAttribute('data-pin') === S.pin;
      g.classList.toggle('on', on); g.setAttribute('aria-pressed', String(on));
    });
    renderCaption();
  }
  document.addEventListener('change', function (e) {
    if (e.target && e.target.id === 'league-pick') {
      S.league = e.target.value;
      try { localStorage.setItem('gm.league', S.league); } catch (err) { /* ignore */ }
      var u = new URL(location.href); u.searchParams.set('league', S.league); history.replaceState(null, '', u);
      qs.set('league', S.league);
      S.now = null; S.runs = []; S.season = []; S.open = {}; S.pin = null;
      load();
    }
  });
  window.addEventListener('hashchange', function () { showTab(); window.scrollTo(0, 0); });
  var rz = null, lastW = 0;
  function redrawIfWider() {
    clearTimeout(rz);
    rz = setTimeout(function () {
      var w = document.querySelector('.app').clientWidth;
      if (w !== lastW) { lastW = w; drawWin(); drawCalib(); }
    }, 80);
  }
  if (window.ResizeObserver) {
    new ResizeObserver(function () {
      var w = document.querySelector('.app').clientWidth;
      if (w !== lastW) { lastW = w; drawWin(); drawCalib(); }
    }).observe(document.querySelector('.app'));
  }
  else window.addEventListener('resize', redrawIfWider);
  window.addEventListener('orientationchange', redrawIfWider);
  if (window.matchMedia) {
    var mq = matchMedia('(prefers-color-scheme: dark)');
    if (mq.addEventListener) mq.addEventListener('change', syncTheme);
  }
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { drawWin(); drawCalib(); });

  setInterval(function () { if (document.visibilityState === 'visible') load(); }, 60000);
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible' && Date.now() - S.lastLoad > 20000) load();
  });

  if ('serviceWorker' in navigator && /^https?:$/.test(location.protocol)) {
    window.addEventListener('load', function () { navigator.serviceWorker.register('sw.js').catch(function () { /* no SW, still works */ }); });
  }

  syncTheme();
  showTab();
  load();
})();
