/* The compact timeline's layout: which stretches of the day are drawn as hours and which collapse to a strip.
 *
 * Pure functions on minutes, no DOM. The board page inlines this file; Node runs it for the tests
 * (tests/timeline.test.mjs). The rules come from docs/superpowers/specs/2026-09-08-board-ui-design.md:
 *
 *   - the active window runs from min(first block, first event, now) − 30 min to max(last end, 21:00) + 30 min,
 *     on a 30-minute grid; everything outside is one collapsed strip at each end;
 *   - inside the window a run of two or more empty hours (past or future) collapses too; a block or an event
 *     keeps its own slots and the half hour before it open, so nothing planned can ever disappear;
 *   - `now` keeps its own slot open (the now-line is always drawn inside the window);
 *   - `expanded` ranges (a strip the reader clicked or dragged onto) stay open; `showAll` disables collapsing.
 */
(function (root) {
  "use strict";

  var SLOT = 30, DAY = 24 * 60, DAY_END = 21 * 60, MIN_GAP = 120, DEFAULT_START = 9 * 60;

  function floorSlot(m) { return Math.floor(m / SLOT) * SLOT; }
  function ceilSlot(m) { return Math.ceil(m / SLOT) * SLOT; }
  function clamp(m, lo, hi) { return Math.max(lo, Math.min(hi, m)); }

  /* items: [{start, end}] in minutes; now: minutes or null; opts: {showAll, expanded: [[from, to], ...]}.
     Returns {rows: [{kind: "open" | "strip", from, to}], window: [from, to]} with every boundary on the grid. */
  function layout(items, now, opts) {
    opts = opts || {};
    if (opts.showAll) return { rows: [{ kind: "open", from: 0, to: DAY }], window: [0, DAY] };
    var keep = [];
    for (var i = 0; i < DAY / SLOT; i++) keep.push(false);
    function mark(a, b) {
      for (var m = floorSlot(clamp(a, 0, DAY)); m < clamp(b, 0, DAY); m += SLOT) keep[m / SLOT] = true;
    }
    var lastEnd = 0;
    (items || []).forEach(function (it) {
      var s = Number(it.start), e = Math.max(Number(it.end), s + 1);
      mark(s - SLOT, e);
      lastEnd = Math.max(lastEnd, e);
    });
    if (now != null) { mark(now, now + 1); lastEnd = Math.max(lastEnd, now + 1); }
    var first = keep.indexOf(true);
    var w0 = first < 0 ? DEFAULT_START - SLOT : first * SLOT;
    var w1 = clamp(ceilSlot(Math.max(lastEnd, DAY_END)) + SLOT, w0 + SLOT, DAY);
    if (first < 0) mark(w0, w1);                       // nothing planned: the default day stays open to plan into
    (opts.expanded || []).forEach(function (r) { mark(r[0], r[1]); });

    var rows = [];
    if (w0 > 0) rows.push({ kind: "strip", from: 0, to: w0 });
    var m = w0;
    while (m < w1) {
      var start = m;
      if (keep[m / SLOT]) {
        while (m < w1 && keep[m / SLOT]) m += SLOT;
        push(rows, "open", start, m);
      } else {
        while (m < w1 && !keep[m / SLOT]) m += SLOT;
        push(rows, m - start >= MIN_GAP ? "strip" : "open", start, m);
      }
    }
    if (w1 < DAY) push(rows, "strip", w1, DAY);
    return { rows: rows, window: [w0, w1] };
  }

  /* Adjacent rows of one kind merge: two strips in a row read as one gap, two open runs as one stretch. */
  function push(rows, kind, from, to) {
    var last = rows[rows.length - 1];
    if (last && last.kind === kind && last.to === from) last.to = to;
    else rows.push({ kind: kind, from: from, to: to });
  }

  /* Pixel geometry over the rows: an open minute is `ppm` pixels tall, a strip is `stripH` pixels whatever it spans. */
  function geometry(rows, ppm, stripH) {
    var y = 0, out = [];
    rows.forEach(function (r) {
      var h = r.kind === "open" ? (r.to - r.from) * ppm : stripH;
      out.push({ kind: r.kind, from: r.from, to: r.to, y: y, h: h });
      y += h;
    });
    return { rows: out, height: y };
  }

  function yOf(geo, minute) {
    var rows = geo.rows;
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      if (minute >= r.from && (minute < r.to || i === rows.length - 1)) {
        return r.y + (r.kind === "open" ? (minute - r.from) * (r.h / (r.to - r.from)) : (minute - r.from) / (r.to - r.from) * r.h);
      }
    }
    return minute < rows[0].from ? rows[0].y : geo.height;
  }

  /* The minute under a pixel, and the row it falls in (a strip returns its first minute so a drop expands it). */
  function minuteAt(geo, y) {
    var rows = geo.rows;
    if (y <= 0) return { minute: rows[0].from, row: rows[0] };
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      if (y < r.y + r.h || i === rows.length - 1) {
        if (r.kind === "strip") return { minute: r.from, row: r };
        return { minute: clamp(r.from + (y - r.y) / (r.h / (r.to - r.from)), r.from, r.to), row: r };
      }
    }
    return { minute: rows[rows.length - 1].to, row: rows[rows.length - 1] };
  }

  /* Side-by-side columns for chips whose pixel boxes overlap (a 10-minute chip has a 28 px minimum, so it can
     run into the next one). chips: [{top, bottom}] -> [{col, cols}] in the same order. */
  function columns(chips) {
    var order = chips.map(function (c, i) { return { i: i, top: c.top, bottom: c.bottom }; })
                     .sort(function (a, b) { return a.top - b.top || a.bottom - b.bottom; });
    var out = chips.map(function () { return { col: 0, cols: 1 }; });
    var cluster = [], ends = [], clusterEnd = -Infinity;
    function flush() {
      var n = ends.length || 1;
      cluster.forEach(function (c) { out[c.i].cols = n; });
      cluster = []; ends = [];
    }
    order.forEach(function (c) {
      if (c.top >= clusterEnd) flush();
      var col = 0;
      while (col < ends.length && ends[col] > c.top) col++;
      ends[col] = c.bottom;
      out[c.i].col = col;
      cluster.push(c);
      clusterEnd = Math.max(clusterEnd, c.bottom);
    });
    flush();
    return out;
  }

  function hhmm(m) { m = Math.round(m); return String(Math.floor(m / 60)).padStart(2, "0") + ":" + String(m % 60).padStart(2, "0"); }
  function hours(minutes) { var h = minutes / 60; return (Math.round(h * 10) / 10).toString().replace(/\.0$/, "") + " h"; }

  var TimelineLayout = { SLOT: SLOT, DAY: DAY, layout: layout, geometry: geometry, yOf: yOf, minuteAt: minuteAt,
                         columns: columns, hhmm: hhmm, hours: hours };
  root.TimelineLayout = TimelineLayout;
  if (typeof module !== "undefined" && module.exports) module.exports = TimelineLayout;
})(typeof globalThis !== "undefined" ? globalThis : this);
