// Live Assist v2: modern view over obplayer's existing live-assist API (same server, /v2/).
// Uses only the endpoints the classic UI uses; no obplayer changes required.
(function () {
  'use strict';

  var state = {
    offset: 0,            // client clock minus player clock (seconds)
    showEnd: 0,           // player-clock timestamp
    showName: '',
    playlist: [],
    groups: [],
    status: null,         // last /info/play_status response
    statusAt: 0,          // client time the status was received
    connected: true,
    lastTrack: -1,        // last playlist position seen (kept while a cart plays)
    peak: -120,
    peakAt: 0,
    userScrollAt: 0,      // client ms the operator last scrolled/touched the playlist
    factsPick: -1,        // playlist row picked for Song facts (-1 = follow what's on air)
    factsKey: ''          // what the facts pane is currently showing
  };

  // TheAudioDB API key. '123' is the public free key (no biographies or descriptions);
  // a premium key fills those in. It is visible to anyone who can load this page.
  var AUDIODB_KEY = '123';
  var AUDIODB = 'https://www.theaudiodb.com/api/v1/json/' + AUDIODB_KEY + '/';

  var $ = function (id) { return document.getElementById(id); };

  // ---- API ---------------------------------------------------------------

  function post(path, args) {
    var body = new URLSearchParams(args || {});
    return fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: body,
      cache: 'no-store'
    }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }).then(function (data) {
      setConnected(true);
      return data;
    }, function (err) {
      setConnected(false);
      throw err;
    });
  }

  function setConnected(ok) {
    if (state.connected === ok) return;
    state.connected = ok;
    $('offline').hidden = ok;
    if (!ok) setPill('offline', 'Offline');
  }

  // ---- formatting --------------------------------------------------------

  function dur(secs) {
    if (!isFinite(secs) || secs < 0) secs = 0;
    secs = Math.floor(secs);
    var h = Math.floor(secs / 3600), m = Math.floor(secs / 60) % 60, s = secs % 60;
    var mm = h ? String(m).padStart(2, '0') : String(m);
    return (h ? h + ':' : '') + mm + ':' + String(s).padStart(2, '0');
  }

  // 12-hour time, e.g. "9:40 PM" (built by hand so every browser formats it the same).
  function clockParts(ts, withSeconds) {
    var d = new Date(ts * 1000), h = d.getHours();
    var pad = function (n) { return String(n).padStart(2, '0'); };
    return {
      time: (h % 12 || 12) + ':' + pad(d.getMinutes()) + (withSeconds ? ':' + pad(d.getSeconds()) : ''),
      ampm: h < 12 ? 'AM' : 'PM'
    };
  }

  function clock(ts, withSeconds) {
    var c = clockParts(ts, withSeconds);
    return c.time + ' ' + c.ampm;
  }

  function playerNow() { return Date.now() / 1000 - state.offset; }

  // ---- data loading ------------------------------------------------------

  function loadTime() {
    return post('/info/current_time').then(function (r) {
      state.offset = Date.now() / 1000 - parseFloat(r.value);
    });
  }

  function loadShow() {
    return Promise.all([
      post('/info/show_name'),
      post('/info/show_end'),
      post('/info/playlist'),
      post('/info/liveassist_groups')
    ]).then(function (res) {
      if ((res[0].value || '') !== state.showName) state.factsPick = -1;
      state.showName = res[0].value || '';
      state.showEnd = parseFloat(res[1].value) || 0;
      state.playlist = Array.isArray(res[2]) ? res[2] : [];
      state.groups = Array.isArray(res[3]) ? res[3] : [];
      $('show-name').textContent = state.showName || '(no show)';
      $('show-end').textContent = state.showEnd ? clock(state.showEnd) : '--:--';
      renderPlaylist();
      renderGroups();
      renderFacts();
      loadNextShow();
    });
  }

  // "Next: <show> · <time>". Players without /info/next_show answer null, which hides it.
  function loadNextShow() {
    return post('/info/next_show').then(function (n) {
      var start = n && n.name ? parseFloat(n.start_time) : 0;
      $('next-show').hidden = !start;
      if (!start) return;
      $('next-name').textContent = n.name;
      $('next-time').textContent = clock(start);
    }).catch(noop);
  }

  function loadStatus() {
    return post('/info/play_status').then(function (s) {
      state.status = s;
      state.statusAt = Date.now() / 1000;
      if (s.mode === 'playlist' && s.track >= 0) state.lastTrack = s.track;
      renderStatus();
      renderPlaylistState();
      renderFacts();
    });
  }

  function loadLevels() {
    return post('/info/levels').then(renderLevels);
  }

  // ---- rendering ---------------------------------------------------------

  function setPill(kind, text) {
    var p = $('status');
    p.dataset.status = kind;
    p.textContent = text;
  }

  // Stopped on a playlist breakpoint. The player has already advanced its position past
  // the breakpoint, and the break request's position counts up from when it started.
  function inBreak() {
    var s = state.status;
    return !!s && s.status === 'stopped' && s.title === 'live assist breakpoint';
  }

  // Playlist row to highlight: the breakpoint itself while in a break.
  function currentTrack() {
    var s = state.status;
    if (!s || s.mode !== 'playlist') return -1;
    return inBreak() ? s.track - 1 : s.track;
  }

  function nextBreakpoint() {
    var from = currentTrack();
    if (from < 0) from = state.lastTrack;
    if (from < 0) return -1;
    for (var i = from + 1; i < state.playlist.length; i++) {
      if (state.playlist[i].media_type === 'breakpoint') return i;
    }
    return -1;
  }

  function currentPosition() {
    var s = state.status;
    if (!s) return 0;
    var pos = parseFloat(s.position) || 0;
    if (s.status === 'playing' || s.status === 'override' || inBreak()) pos += Date.now() / 1000 - state.statusAt;
    return pos;
  }

  function currentDuration() {
    var s = state.status;
    if (!s || inBreak()) return 0;
    if (s.mode === 'playlist' && s.track >= 0 && state.playlist[s.track]) return parseFloat(state.playlist[s.track].duration) || 0;
    if (s.mode === 'group' && state.groups[s.group_num] && state.groups[s.group_num].items[s.group_item_num])
      return parseFloat(state.groups[s.group_num].items[s.group_item_num].duration) || 0;
    return parseFloat(s.duration) || 0;
  }

  function renderStatus() {
    var s = state.status;
    if (!s) return;
    var playing = s.status === 'playing' || s.status === 'override';
    var brk = inBreak();
    if (brk) setPill('break', 'Break');
    else setPill(s.status === 'override' ? 'override' : (playing ? 'playing' : 'paused'),
                 s.status === 'override' ? 'Override' : (playing ? 'Playing' : 'Paused'));
    $('btn-play').innerHTML = playing ? '&#10073;&#10073;' : '&#9654;';
    $('btn-play').title = playing ? 'Pause' : 'Play';

    var title = s.title, artist = s.artist, mode = '';
    if (brk) {
      var up = state.playlist[s.track];
      title = 'Breakpoint';
      artist = up ? 'Play starts: ' + (up.title || '(untitled)') + (up.artist ? ' – ' + up.artist : '') : 'End of playlist';
      mode = 'Talk';
    } else if (s.mode === 'playlist') {
      mode = 'Track ' + (s.track + 1) + ' of ' + state.playlist.length;
      if (state.playlist[s.track]) { title = state.playlist[s.track].title; artist = state.playlist[s.track].artist; }
    } else if (s.mode === 'group') {
      mode = 'Cart';
    }
    $('mode').textContent = mode;
    $('now-title').textContent = title || ' ';
    $('now-artist').textContent = artist || ' ';
  }

  // Players with the stop-after command report stop_after in play_status. Older players
  // don't, and the button falls back to jumping to the next breakpoint row.
  function stopAfterSupported() {
    return !!state.status && 'stop_after' in state.status;
  }

  function renderBreakButton() {
    var b = $('btn-break');
    if (b.classList.contains('confirm')) return;

    if (stopAfterSupported()) {
      var s = state.status, armed = !!s.stop_after;
      var why = s.show_type !== 'live_assist' ? 'Only available on live assist shows'
        : inBreak() ? 'Already in a break'
        : s.mode !== 'playlist' ? 'Not while a cart is playing'
        : s.status !== 'playing' ? 'Only while a track is playing' : '';
      b.classList.toggle('armed', armed);
      b.disabled = !armed && !!why;
      if (armed) {
        var ends = Date.now() / 1000 + Math.max(0, currentDuration() - currentPosition());
        b.textContent = 'Stops at ' + clock(ends);
        b.title = 'Holds for talk when this track ends. Click to cancel.';
      } else {
        b.textContent = 'Stop after track';
        b.title = why || 'Hold for talk when this track ends; Play starts the next track';
      }
      return;
    }

    b.classList.remove('armed');
    b.textContent = 'Breakpoint';
    var i = nextBreakpoint();
    b.disabled = i < 0;
    b.title = i < 0 ? 'No breakpoint after the current track' : 'Jump to breakpoint (row ' + (i + 1) + ')';
  }

  function trackRow(track, i) {
    var li = document.createElement('li');
    li.className = 'track';
    li.dataset.index = i;

    var num = document.createElement('span');
    num.className = 'num mono';
    num.textContent = i + 1;

    var meta = document.createElement('div');
    meta.className = 'meta';
    var t = document.createElement('div');
    t.className = 't';
    var a = document.createElement('div');
    a.className = 'a';
    if (track.media_type === 'breakpoint') {
      li.classList.add('breakpoint');
      t.textContent = 'Breakpoint';
    } else {
      t.textContent = track.title || '(untitled)';
      a.textContent = track.artist || '';
    }
    meta.appendChild(t);
    meta.appendChild(a);

    var when = document.createElement('div');
    when.className = 'when mono';

    var go = document.createElement('button');
    go.className = 'go';
    go.type = 'button';
    go.textContent = 'Play';
    armConfirm(go, 'Play', function () {
      return post('/command/playlist_seek', { track_num: i, position: 0 });
    });

    li.appendChild(num);
    li.appendChild(meta);
    li.appendChild(when);
    li.appendChild(go);
    li.addEventListener('dblclick', function () { go.click(); });
    li.addEventListener('click', function (e) {
      if (track.media_type === 'breakpoint' || e.target.closest('.go')) return;
      pickFacts(i);
    });
    return li;
  }

  function renderPlaylist() {
    var ol = $('playlist');
    ol.textContent = '';
    state.playlist.forEach(function (track, i) { ol.appendChild(trackRow(track, i)); });
    var total = state.playlist.reduce(function (n, t) { return n + (parseFloat(t.duration) || 0); }, 0);
    $('list-summary').textContent = state.playlist.length + ' tracks · ' + dur(total);
    renderPlaylistState();
  }

  // Marks played/current rows and fills in each upcoming track's projected start time.
  function renderPlaylistState() {
    var cur = currentTrack();
    var rows = $('playlist').children;
    var now = Date.now() / 1000;
    var t = now + Math.max(0, currentDuration() - currentPosition());
    var showEndLocal = state.showEnd ? state.showEnd + state.offset : 0;
    // A picked row that comes on air is just "now playing" again.
    if (state.factsPick >= 0 && state.factsPick === cur) state.factsPick = -1;
    // Held after a track (stop-after) rather than on a breakpoint row: that track is done.
    var heldAfter = inBreak() && cur >= 0 && (state.playlist[cur] || {}).media_type !== 'breakpoint';

    for (var i = 0; i < rows.length; i++) {
      var row = rows[i], track = state.playlist[i] || {}, when = row.querySelector('.when');
      row.classList.toggle('played', cur >= 0 && (i < cur || (heldAfter && i === cur)));
      row.classList.toggle('current', i === cur && !heldAfter);
      row.classList.toggle('picked', i === state.factsPick);
      row.classList.remove('past-end');
      if (cur >= 0 && i > cur) {
        when.textContent = track.media_type === 'breakpoint' ? '' : dur(track.duration);
        // Starts after the show ends: won't get played.
        if (showEndLocal && t >= showEndLocal) row.classList.add('past-end');
        t += parseFloat(track.duration) || 0;
      } else if (i === cur && !heldAfter) {
        when.textContent = track.media_type === 'breakpoint' ? 'now' : 'now · ' + dur(track.duration);
      } else {
        when.textContent = track.media_type === 'breakpoint' ? '' : dur(track.duration);
      }
    }
    renderBreakButton();
  }

  function renderGroups() {
    var box = $('groups');
    box.textContent = '';
    var any = state.groups.some(function (g) { return g.items && g.items.length; });
    $('no-carts').hidden = any;
    state.groups.forEach(function (g, gi) {
      if (!g.items || !g.items.length) return;
      var sec = document.createElement('div');
      var name = document.createElement('div');
      name.className = 'group-name';
      name.textContent = g.name;
      var carts = document.createElement('div');
      carts.className = 'carts';
      g.items.forEach(function (item, ii) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'cart';
        b.dataset.group = gi;
        b.dataset.item = ii;
        var label = document.createElement('span');
        label.textContent = (item.artist ? item.artist + ' – ' : '') + (item.title || '');
        var d = document.createElement('span');
        d.className = 'dur mono';
        d.textContent = dur(item.duration);
        b.appendChild(label);
        b.appendChild(d);
        armConfirm(b, null, function () {
          return post('/command/play_group_item', { group_num: gi, group_item_num: ii, position: 0 });
        });
        carts.appendChild(b);
      });
      sec.appendChild(name);
      sec.appendChild(carts);
      box.appendChild(sec);
    });
  }

  function renderLevels(levels) {
    if (!Array.isArray(levels)) return;
    var now = Date.now();
    // One meter: the louder of the two channels.
    var db = Math.max.apply(null, levels.map(function (v) { v = parseFloat(v); return isFinite(v) ? v : -120; }).concat(-120));
    var pct = Math.max(0, Math.min(100, (db + 60) / 60 * 100));
    $('meter').style.height = (100 - pct) + '%';
    $('db').textContent = db <= -60 ? '-∞' : db.toFixed(0);
    // Peak hold for 2 s, then fall.
    if (db >= state.peak || now - state.peakAt > 2000) { state.peak = db; state.peakAt = now; }
    var pk = Math.max(0, Math.min(100, (state.peak + 60) / 60 * 100));
    $('peak').style.bottom = 'calc(' + pk + '% - 2px)';
  }

  // Ticks every 250 ms: clocks, progress bar, countdowns. No network.
  function tick() {
    var now = Date.now() / 1000;
    var c = clockParts(now, true);
    $('clock').textContent = c.time;
    $('clock-ampm').textContent = c.ampm;

    // Show left glows like Track left: amber for the last 5 min, red for the last minute.
    var showTile = $('show-left').parentNode, left = Infinity;
    if (state.showEnd) {
      left = state.showEnd - playerNow();
      $('show-left').textContent = left > 0 ? dur(left) : '0:00';
      if (left < -2) { state.showEnd = 0; setTimeout(refreshShow, 1500); }
    } else {
      $('show-left').textContent = '--:--';
    }
    $('show-left').classList.toggle('attention', left > 0 && left < 300);
    showTile.classList.toggle('flash', left > 60 && left < 300);
    showTile.classList.toggle('flash-urgent', left > 0 && left <= 60);

    var d = currentDuration(), p = Math.min(currentPosition(), d || Infinity), r = Math.max(0, d - p);
    var brk = inBreak();
    $('elapsed').textContent = dur(p);
    $('duration').textContent = dur(d);
    $('track-label').textContent = brk ? 'Talk time' : 'Track left';
    $('remaining').textContent = brk ? dur(p) : '-' + dur(r);
    $('remaining').classList.toggle('talk', brk);
    var ending = d > 20 && (r < 20 || r / d < 0.05);
    $('remaining').classList.toggle('attention', ending);
    $('progress-bar').classList.toggle('ending', ending);
    // The whole Track left tile flashes: amber while ending, red for the last 10 s.
    var tile = $('remaining').parentNode;
    tile.classList.toggle('flash', ending && r > 10);
    tile.classList.toggle('flash-urgent', ending && r <= 10);
    $('progress-bar').style.width = (d ? (p / d * 100) : 0) + '%';

    // Track should have ended: ask the player what is on now.
    var s = state.status;
    if (s && (s.status === 'playing' || s.status === 'override') && d && p >= d + 0.5 && now - state.statusAt > 1.5) {
      state.statusAt = now; // throttle
      loadStatus().catch(noop);
    }

    autoScroll();
    syncSeek();
  }

  // ---- seeking -----------------------------------------------------------
  // Drag/click the progress bar to pick a point; nothing happens on air until Confirm.
  // Live assist only: on clock-locked (standard) shows the player would loop the song when it
  // snaps back to the schedule. The show type only comes from players with the newer API.

  var seek = { drag: null, armed: null, timer: null };

  function seekTarget() {
    var s = state.status;
    if (!s || s.show_type !== 'live_assist' || s.status !== 'playing' || inBreak() || !currentDuration()) return null;
    if (s.mode === 'playlist' && s.track >= 0) return { key: 'p' + s.track, mode: 'playlist', track: s.track };
    if (s.mode === 'group' && s.group_num >= 0) return { key: 'g' + s.group_num + '.' + s.group_item_num, mode: 'group', group: s.group_num, item: s.group_item_num };
    return null;
  }

  function seekFraction(e) {
    var r = $('seekbar').getBoundingClientRect();
    return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
  }

  function showSeek(frac, armed) {
    var mark = $('seek-mark');
    mark.hidden = false;
    mark.style.left = (frac * 100) + '%';
    $('seek-confirm').hidden = false;
    $('seek-go').textContent = 'Seek to ' + dur(frac * currentDuration());
    $('seek-go').disabled = !armed;
  }

  function cancelSeek() {
    clearTimeout(seek.timer);
    seek.drag = seek.armed = null;
    $('seek-mark').hidden = true;
    $('seek-confirm').hidden = true;
    $('seekbar').classList.remove('dragging');
  }

  // Keeps the bar's state honest: disabled when seeking isn't safe, and cancels a pending
  // seek if the track it was aimed at is no longer the one playing.
  function syncSeek() {
    var t = seekTarget();
    $('seekbar').classList.toggle('enabled', !!t);
    $('seekbar').title = t ? 'Drag or click to seek' : '';
    var pending = seek.drag || seek.armed;
    if (pending && (!t || t.key !== pending.target.key)) cancelSeek();
  }

  $('seekbar').addEventListener('pointerdown', function (e) {
    var t = seekTarget();
    if (!t) return;
    cancelSeek();
    seek.drag = { target: t, frac: seekFraction(e) };
    $('seekbar').setPointerCapture(e.pointerId);
    $('seekbar').classList.add('dragging');
    showSeek(seek.drag.frac, false);
  });
  $('seekbar').addEventListener('pointermove', function (e) {
    if (!seek.drag) return;
    seek.drag.frac = seekFraction(e);
    showSeek(seek.drag.frac, false);
  });
  $('seekbar').addEventListener('pointerup', function () {
    if (!seek.drag) return;
    seek.armed = seek.drag;
    seek.drag = null;
    $('seekbar').classList.remove('dragging');
    showSeek(seek.armed.frac, true);
    seek.timer = setTimeout(cancelSeek, 5000);
  });
  $('seekbar').addEventListener('pointercancel', cancelSeek);
  $('seek-cancel').addEventListener('click', cancelSeek);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') cancelSeek(); });

  $('seek-go').addEventListener('click', function () {
    var a = seek.armed, t = seekTarget();
    cancelSeek();
    if (!a || !t || t.key !== a.target.key) return;
    var pct = a.frac * 100; // the player takes position as a percentage of the track
    var req = t.mode === 'playlist'
      ? post('/command/playlist_seek', { track_num: t.track, position: pct })
      : post('/command/play_group_item', { group_num: t.group, group_item_num: t.item, position: pct });
    req.then(function () { setTimeout(function () { loadStatus().catch(noop); }, 300); }).catch(noop);
  });

  // Keeps the current track near the top of the playlist with two played rows above it.
  // Backs off for 15 s whenever the operator scrolls or touches the list, and never moves
  // the list while a Play/Confirm is armed.
  function autoScroll() {
    var list = $('playlist');
    var cur = currentTrack();
    if (cur < 0) cur = state.lastTrack;
    if (cur < 0 || Date.now() - state.userScrollAt < 15000 || list.querySelector('.confirm')) return;
    var row = list.children[Math.max(0, cur - 2)];
    if (!row) return;
    var target = Math.min(row.offsetTop - 6, list.scrollHeight - list.clientHeight);
    if (Math.abs(list.scrollTop - target) > 2) list.scrollTo({ top: target, behavior: 'smooth' });
  }

  // ---- song facts (TheAudioDB) -------------------------------------------

  var audiodbCache = {};

  // Cached per URL for the life of the page; failed requests are retried next time.
  function audiodb(endpoint, params) {
    var url = AUDIODB + endpoint + '?' + new URLSearchParams(params);
    if (!audiodbCache[url]) {
      audiodbCache[url] = fetch(url).then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      });
      audiodbCache[url].catch(function () { delete audiodbCache[url]; });
    }
    return audiodbCache[url];
  }

  // Library names carry extras TheAudioDB doesn't: "Gary Numan, Tubeway Army",
  // "The Robots - Live", "Computer God - 2008 Remaster", "(Remastered 2011)".
  function artistVariants(a) {
    var first = a.split(/,\s*|\s+(?:feat|ft)\.?\s+/i)[0].trim();
    return first && first !== a ? [a, first] : [a];
  }
  function titleVariants(t) {
    var clean = t.replace(/\s+-\s+.*$/, '')
      .replace(/\s*[(\[][^)\]]*\b(remaster\w*|live|mix|version|edit|mono|stereo)\b[^)\]]*[)\]]/ig, '').trim();
    return clean && clean !== t ? [t, clean] : [t];
  }

  // Tries each artist/title spelling in turn; resolves to the first match or null.
  function findTrack(artist, title) {
    var tries = [];
    artistVariants(artist).forEach(function (a) {
      titleVariants(title).forEach(function (t) { tries.push({ s: a, t: t }); });
    });
    return tries.reduce(function (p, q) {
      return p.then(function (found) {
        return found || audiodb('searchtrack.php', q).then(function (d) { return (d && d.track && d.track[0]) || null; });
      });
    }, Promise.resolve(null));
  }

  function findArtist(names) {
    return names.reduce(function (p, name) {
      return p.then(function (found) {
        return found || audiodb('search.php', { s: name }).then(function (d) { return (d && d.artists && d.artists[0]) || null; });
      });
    }, Promise.resolve(null));
  }

  // What the facts pane should describe: the picked row, else what's on air
  // (or, during a break, the track that starts when it ends).
  function factsTarget() {
    var p = state.factsPick >= 0 && state.playlist[state.factsPick];
    if (p) return { artist: p.artist, title: p.title, media: p.media_type, label: 'Playlist row ' + (state.factsPick + 1), picked: true };
    var s = state.status;
    if (!s) return null;
    if (inBreak()) {
      var up = state.playlist[s.track];
      return up ? { artist: up.artist, title: up.title, media: up.media_type, label: 'Up after the break' } : null;
    }
    var t = s.mode === 'playlist' && state.playlist[s.track];
    if (t) return { artist: t.artist, title: t.title, media: t.media_type, label: 'Now playing' };
    return { artist: s.artist, title: s.title, media: 'audio', label: s.mode === 'group' ? 'Now playing (cart)' : 'Now playing' };
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function link(href, text) {
    var a = el('a', null, text);
    a.href = /^https?:\/\//i.test(href) ? href : 'https://' + href;
    a.target = '_blank';
    a.rel = 'noopener';
    return a;
  }

  function renderFacts() {
    var target = factsTarget();
    var key = target ? [target.label, target.artist, target.title].join('\n') : '';
    if (key === state.factsKey) return;
    state.factsKey = key;

    var box = $('facts');
    box.textContent = '';
    if (!target) { box.appendChild(el('p', 'muted empty', 'Nothing on air.')); return; }

    var head = el('div', 'facts-head');
    head.appendChild(el('span', 'label', target.label));
    if (target.picked) {
      var back = el('button', 'facts-back', 'Back to now playing');
      back.type = 'button';
      back.addEventListener('click', function () { state.factsPick = -1; renderPlaylistState(); renderFacts(); });
      head.appendChild(back);
    }
    box.appendChild(head);

    var body = el('div');
    box.appendChild(body);
    if (!target.artist || !target.title || (target.media && target.media !== 'audio')) {
      body.appendChild(el('p', 'muted empty', 'No song facts for this item.'));
      return;
    }
    body.appendChild(el('p', 'muted empty', 'Looking up “' + target.title + '”…'));

    findTrack(target.artist, target.title).then(function (track) {
      var names = track && track.strArtist ? [track.strArtist] : artistVariants(target.artist);
      return Promise.all([
        track,
        track && track.idAlbum ? audiodb('album.php', { m: track.idAlbum }).then(function (d) { return d && d.album && d.album[0]; }).catch(noop) : null,
        findArtist(names).catch(noop)
      ]);
    }).then(function (r) {
      if (state.factsKey !== key) return; // moved on while loading
      fillFacts(body, target, r[0], r[1], r[2]);
    }).catch(function () {
      if (state.factsKey !== key) return;
      body.textContent = '';
      body.appendChild(el('p', 'muted empty', 'Couldn’t reach TheAudioDB.'));
      state.factsKey = ''; // retry on the next status poll
    });
  }

  function fillFacts(body, target, track, album, artist) {
    body.textContent = '';
    if (!track && !artist) {
      body.appendChild(el('p', 'muted empty', 'No TheAudioDB match for “' + target.title + '” by ' + target.artist + '.'));
      return;
    }
    track = track || {}; album = album || {}; artist = artist || {};

    var card = el('div', 'facts-card');
    var art = album.strAlbumThumb || track.strTrackThumb || artist.strArtistThumb;
    if (art) {
      var img = el('img', 'facts-art');
      img.src = art + '/small';
      img.alt = '';
      img.addEventListener('error', function () { img.remove(); });
      card.appendChild(img);
    }
    var names = el('div');
    names.appendChild(el('div', 'facts-title', track.strTrack || target.title));
    names.appendChild(el('div', 'facts-artist', artist.strArtist || track.strArtist || target.artist));
    card.appendChild(names);
    body.appendChild(card);

    var dl = el('dl', 'facts-list');
    function row(label, value) {
      if (!value) return;
      dl.appendChild(el('dt', null, label));
      var dd = el('dd');
      if (typeof value === 'string') dd.textContent = value; else dd.appendChild(value);
      dl.appendChild(dd);
    }
    if (!track.strTrack) body.appendChild(el('p', 'muted empty', 'Track not found; showing the artist only.'));
    row('Album', (album.strAlbum || track.strAlbum || '') + (track.intTrackNumber && (album.strAlbum || track.strAlbum) ? ' (track ' + track.intTrackNumber + ')' : ''));
    row('Released', album.intYearReleased);
    row('Label', album.strLabel);
    row('Genre', [track.strGenre || album.strGenre, track.strStyle || album.strStyle]
      .filter(function (v, i, a) { return v && a.indexOf(v) === i; }).join(' · '));
    row('Mood', track.strMood || album.strMood);
    row('From', artist.strCountry);
    // Solo artists carry born/died years; bands carry formed and (reused) died = split.
    if (artist.intMembers === '1') {
      row('Born', artist.intBornYear);
      row('Died', artist.intDiedYear);
    } else {
      row('Formed', artist.intFormedYear);
      row('Split', artist.intDiedYear);
      row('Members', artist.intMembers);
    }
    row('Website', artist.strWebsite ? link(artist.strWebsite, artist.strWebsite.replace(/^https?:\/\//i, '')) : '');
    row('Video', track.strMusicVid ? link(track.strMusicVid, 'Watch the music video') : '');
    body.appendChild(dl);

    var about = track.strDescriptionEN || album.strDescriptionEN;
    if (about) { body.appendChild(el('h3', null, 'About the song')); body.appendChild(el('p', 'facts-text', about)); }
    if (artist.strBiographyEN) { body.appendChild(el('h3', null, 'About the artist')); body.appendChild(el('p', 'facts-text', artist.strBiographyEN)); }
    body.appendChild(el('div', 'facts-src muted', 'Data: TheAudioDB'));
  }

  function pickFacts(i) {
    state.factsPick = i === currentTrack() ? -1 : i;
    renderPlaylistState();
    showTab('facts');
    setSideOpen(true);
    renderFacts();
  }

  // ---- side panel --------------------------------------------------------

  function showTab(name) {
    document.querySelectorAll('.tab').forEach(function (t) { t.setAttribute('aria-selected', String(t.dataset.tab === name)); });
    $('pane-carts').hidden = name !== 'carts';
    $('pane-facts').hidden = name !== 'facts';
    try { localStorage.setItem('la2.sideTab', name); } catch (e) {}
  }

  function setSideOpen(open) {
    $('side').classList.toggle('collapsed', !open);
    $('side-toggle').setAttribute('aria-expanded', String(open));
    $('side-toggle').title = open ? 'Collapse panel' : 'Expand panel';
    try { localStorage.setItem('la2.cartsOpen', open ? '1' : '0'); } catch (e) {}
  }

  // ---- interaction -------------------------------------------------------

  // First click arms the button ("Confirm"), second click within 3 s fires.
  function armConfirm(btn, label, action, enabled) {
    var timer = null;
    btn.addEventListener('click', function () {
      if (enabled && !enabled()) return;
      if (!btn.classList.contains('confirm')) {
        document.querySelectorAll('.confirm').forEach(function (other) { if (other !== btn) other.dispatchEvent(new Event('disarm')); });
        btn.classList.add('confirm');
        if (label) btn.textContent = 'Confirm';
        timer = setTimeout(disarm, 3000);
        return;
      }
      disarm();
      action().then(function () { setTimeout(function () { loadStatus().catch(noop); }, 500); }).catch(noop);
    });
    btn.addEventListener('disarm', disarm);
    function disarm() {
      clearTimeout(timer);
      btn.classList.remove('confirm');
      if (label) btn.textContent = label;
    }
  }

  function command(path) {
    return post(path).then(function () { setTimeout(function () { loadStatus().catch(noop); }, 500); }).catch(noop);
  }

  function noop() {}

  $('btn-play').addEventListener('click', function () {
    var s = state.status;
    var playing = s && (s.status === 'playing' || s.status === 'override');
    command(playing ? '/command/pause' : '/command/play');
  });
  $('btn-next').addEventListener('click', function () { command('/command/next'); });
  $('btn-prev').addEventListener('click', function () { command('/command/prev'); });
  // Stop after this track: one click arms, another cancels. Every screen sees the state via
  // play_status, and nothing changes on air until the track ends, so there's no confirm step.
  $('btn-break').addEventListener('click', function () {
    if (!stopAfterSupported()) return;
    var enable = !state.status.stop_after;
    state.status.stop_after = enable; // show it straight away; the next poll confirms
    renderBreakButton();
    post('/command/stop_after_current', { enable: enable ? 1 : 0 })
      .then(function () { setTimeout(function () { loadStatus().catch(noop); }, 300); })
      .catch(function () { loadStatus().catch(noop); });
  });
  // Older players: seeking onto a breakpoint row makes the player stop there and wait for Play.
  armConfirm($('btn-break'), 'Breakpoint', function () {
    var i = nextBreakpoint();
    if (i < 0) return Promise.resolve();
    return post('/command/playlist_seek', { track_num: i, position: 0 });
  }, function () { return !stopAfterSupported(); });

  // Side panel tab and open/collapsed state are remembered per browser.
  document.querySelectorAll('.tab').forEach(function (t) {
    t.addEventListener('click', function () { showTab(t.dataset.tab); });
  });
  $('side-toggle').addEventListener('click', function () {
    setSideOpen($('side').classList.contains('collapsed'));
  });
  var savedTab = 'carts', savedOpen = true;
  try {
    savedTab = localStorage.getItem('la2.sideTab') === 'facts' ? 'facts' : 'carts';
    savedOpen = localStorage.getItem('la2.cartsOpen') !== '0';
  } catch (e) {}
  showTab(savedTab);
  setSideOpen(savedOpen);

  // Any hands-on scrolling of the playlist pauses auto-scroll for a while.
  ['wheel', 'touchstart', 'pointerdown', 'keydown'].forEach(function (ev) {
    $('playlist').addEventListener(ev, function () { state.userScrollAt = Date.now(); }, { passive: true });
  });

  // ---- startup & polling -------------------------------------------------

  function refreshShow() {
    return loadTime().then(loadShow).then(loadStatus).catch(noop);
  }

  refreshShow();
  setInterval(tick, 250);
  setInterval(function () { loadStatus().catch(noop); }, 3000);
  setInterval(function () { loadLevels().catch(noop); }, 500);
  setInterval(function () { loadShow().catch(noop); }, 30000);
  setInterval(function () { loadTime().catch(noop); }, 60000);
})();
