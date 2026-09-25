// The compact timeline's layout rules under Node (no browser). Run by tests/test_board.py.
import { createRequire } from "node:module";
import assert from "node:assert/strict";
const require = createRequire(import.meta.url);
const T = require("../studyplan/static/timeline.js");

const M = s => { const [h, m] = s.split(":"); return +h * 60 + +m; };
const rows = (r) => r.rows.map(x => `${x.kind} ${T.hhmm(x.from)}-${T.hhmm(x.to)}`);

// the sample day (plans/2026-09-08): a 09:00 call, blocks 17:00–21:10, now at 16:47
const sample = [{ start: M("09:00"), end: M("09:25") }, { start: M("17:00"), end: M("18:30") }, { start: M("18:30"), end: M("18:40") },
                { start: M("18:40"), end: M("19:00") }, { start: M("19:00"), end: M("20:30") }, { start: M("20:30"), end: M("20:50") },
                { start: M("20:50"), end: M("21:10") }];
const day = T.layout(sample, M("16:47"));
assert.deepEqual(rows(day), ["strip 00:00-08:30", "open 08:30-09:30", "strip 09:30-16:30", "open 16:30-22:00", "strip 22:00-24:00"]);
assert.deepEqual(day.window, [M("08:30"), M("22:00")]);
assert.equal(T.hours(8.5 * 60), "8.5 h");
assert.equal(T.hours(7 * 60), "7 h");

// not today: no `now`, so nothing splits the empty afternoon
assert.deepEqual(rows(T.layout(sample, null)), ["strip 00:00-08:30", "open 08:30-09:30", "strip 09:30-16:30", "open 16:30-22:00", "strip 22:00-24:00"]);

// now in the middle of an empty run keeps its own slot open
assert.deepEqual(rows(T.layout(sample, M("12:10"))),
  ["strip 00:00-08:30", "open 08:30-09:30", "strip 09:30-12:00", "open 12:00-12:30", "strip 12:30-16:30", "open 16:30-22:00", "strip 22:00-24:00"]);

// a run shorter than two hours stays open; the window's end strip merges with the day's
assert.deepEqual(rows(T.layout([{ start: M("09:00"), end: M("09:25") }, { start: M("11:00"), end: M("12:00") }], null)),
  ["strip 00:00-08:30", "open 08:30-12:00", "strip 12:00-24:00"]);

// an undone past block keeps its hour open; the window never ends before 21:30
assert.deepEqual(rows(T.layout([{ start: M("07:00"), end: M("08:00") }], M("15:00"))),
  ["strip 00:00-06:30", "open 06:30-08:00", "strip 08:00-15:00", "open 15:00-15:30", "strip 15:30-24:00"]);

// an empty day still has an open window to plan into
assert.deepEqual(rows(T.layout([], null)), ["strip 00:00-08:30", "open 08:30-21:30", "strip 21:30-24:00"]);

// expanding a strip, and showing all hours
assert.deepEqual(rows(T.layout(sample, null, { expanded: [[M("09:30"), M("16:30")]] })), ["strip 00:00-08:30", "open 08:30-22:00", "strip 22:00-24:00"]);
assert.deepEqual(rows(T.layout(sample, null, { showAll: true })), ["open 00:00-24:00"]);

// geometry: open minutes scale, strips are a fixed height, and the mapping round-trips
const geo = T.geometry(day.rows, 0.8, 22);
assert.equal(geo.height, 22 + 60 * 0.8 + 22 + 330 * 0.8 + 22);
assert.equal(T.yOf(geo, M("08:30")), 22);
assert.equal(T.yOf(geo, M("17:00")), 22 + 48 + 22 + 30 * 0.8);
assert.equal(T.minuteAt(geo, T.yOf(geo, M("19:05"))).minute, M("19:05"));
assert.equal(T.minuteAt(geo, 22 + 48 + 10).row.kind, "strip");                    // a drop on the strip names it
assert.equal(T.minuteAt(geo, 22 + 48 + 10).minute, M("09:30"));

// overlapping chips share the width, others keep it
const cols = T.columns([{ top: 0, bottom: 28 }, { top: 8, bottom: 36 }, { top: 100, bottom: 120 }]);
assert.deepEqual(cols, [{ col: 0, cols: 2 }, { col: 1, cols: 2 }, { col: 0, cols: 1 }]);

console.log("timeline: ok");
