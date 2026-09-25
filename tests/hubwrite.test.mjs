// Request construction of the hosted write path under Node with a fake fetch. Run by tests/test_hubwrite.py.
import { createRequire } from "node:module";
import assert from "node:assert/strict";
const require = createRequire(import.meta.url);
const HubWriter = require("../studyplan/static/hubwrite.js");

const b64 = s => Buffer.from(s, "utf8").toString("base64");
const calls = [];
let putFailures = 0;
const files = { "study-plan/data/log.jsonl": { text: '{"date":"2026-09-07","block":"news","minutes":0,"target":"news-1"}\n', sha: "sha-log-1" },
                "study-plan/plans/2026-09-07.json": { text: JSON.stringify({ date: "2026-09-07", arranged: false, blocks: [{ id: "design", start: "13:00", minutes: 60 }] }), sha: "sha-plan-1" },
                "study-plan/profile/notes.md": { text: "# Coach notes\n\nNewest first.\n\n## 2026-09-06 — first assessment\n\n- old\n", sha: "sha-notes-1" } };

const res = (status, body) => ({ status, ok: status >= 200 && status < 300, json: async () => body });
async function fakeFetch(url, init = {}) {
  calls.push({ url, method: init.method || "GET", headers: init.headers, body: init.body ? JSON.parse(init.body) : null });
  const path = decodeURIComponent(url.replace(/^https:\/\/api\.example\/repos\/example-user\/job-radar\/contents\//, "").split("?")[0]);
  if ((init.method || "GET") === "GET") {
    const f = files[path];
    return f ? res(200, { content: b64(f.text).replace(/(.{60})/g, "$1\n"), sha: f.sha }) : res(404, { message: "Not Found" });
  }
  if (putFailures > 0) { putFailures--; return res(409, { message: "is at sha-log-2 but expected sha-log-1" }); }
  const f = files[path] || { text: "", sha: null };
  assert.equal(JSON.parse(init.body).sha ?? null, f.sha, "PUT carries the sha it read");
  files[path] = { text: Buffer.from(JSON.parse(init.body).content, "base64").toString("utf8"), sha: f.sha ? f.sha.replace(/\d+$/, n => +n + 1) : "sha-new" };
  return res(200, { commit: { sha: "c" + calls.length }, content: { sha: files[path].sha } });
}

const w = new HubWriter({ repo: "example-user/job-radar", token: "ghp_test", api: "https://api.example", fetch: fakeFetch,
                          now: () => "2026-09-07T20:00:00" });

// 1. tick an item: GET then PUT with sha, base64 of old + new row, message `hub: <date> tick <target>`
let r = await w.tick({ date: "2026-09-07", block: "news", item: "news-2", text: "read the thing" });
assert.equal(calls[0].method, "GET");
assert.equal(calls[0].url, "https://api.example/repos/example-user/job-radar/contents/study-plan/data/log.jsonl?ref=main");
assert.equal(calls[0].headers.Authorization, "Bearer ghp_test");
const put = calls[1];
assert.equal(put.method, "PUT");
assert.equal(put.body.sha, "sha-log-1");
assert.equal(put.body.branch, "main");
assert.equal(put.body.message, "hub: 2026-09-07 tick news-2");
const text = Buffer.from(put.body.content, "base64").toString("utf8");
assert.ok(text.startsWith('{"date":"2026-09-07","block":"news","minutes":0,"target":"news-1"}\n'), "old rows kept");
const row = JSON.parse(text.trim().split("\n").pop());
assert.deepEqual(row, { ts: "2026-09-07T20:00:00", date: "2026-09-07", block: "news", minutes: 0, note: "item: read the thing", target: "news-2", item: "news-2", source: "hub" });
assert.equal(r.commit, "c2");
assert.equal(r.row.target, "news-2");

// 2. a sha conflict is retried once: GET again, PUT again with the new sha
putFailures = 1;
calls.length = 0;
r = await w.tick({ date: "2026-09-07", block: "leetcode", minutes: 40, note: "two DP problems" });
assert.deepEqual(calls.map(c => c.method), ["GET", "PUT", "GET", "PUT"]);
assert.equal(calls[3].body.sha, "sha-log-2");
assert.equal(calls[3].body.message, "hub: 2026-09-07 tick leetcode");
const blockRow = JSON.parse(Buffer.from(calls[3].body.content, "base64").toString("utf8").trim().split("\n").pop());
assert.equal(blockRow.minutes, 40); assert.equal(blockRow.note, "two DP problems"); assert.equal(blockRow.item, undefined);

// 3. two failures in a row surface the API's error text
putFailures = 2;
await assert.rejects(() => w.tick({ date: "2026-09-07", block: "news", item: "news-3", undo: true }), /HTTP 409 is at sha-log-2/);

// 4. undo row
calls.length = 0;
r = await w.tick({ date: "2026-09-07", block: "news", item: "news-2", undo: true });
assert.equal(r.row.undo, true); assert.equal(r.row.note, "undo news-2"); assert.equal(calls[1].body.message, "hub: 2026-09-07 undo news-2");

// 5. move a block: the plan JSON is edited in place and the day becomes arranged
calls.length = 0;
r = await w.moveBlock({ date: "2026-09-07", block: "design", start: "14:05", minutes: 65 });
assert.equal(calls[1].body.message, "hub: 2026-09-07 move design to 14:05, 65 min");
const plan = JSON.parse(Buffer.from(calls[1].body.content, "base64").toString("utf8"));
assert.equal(plan.blocks[0].start, "14:05"); assert.equal(plan.blocks[0].minutes, 65); assert.equal(plan.arranged, true);
await assert.rejects(() => w.moveBlock({ date: "2026-09-07", block: "nope", start: "14:05" }), /no block nope/);

// 6. debrief: a dated section after the preamble, newest first
calls.length = 0;
r = await w.debrief({ date: "2026-09-07", event: "Agents meetup", did: "talked to two founders", learned: "nobody has evals", story: "the BGM quality gate" });
assert.equal(calls[1].body.message, "hub: 2026-09-07 debrief Agents meetup");
const notes = Buffer.from(calls[1].body.content, "base64").toString("utf8");
assert.ok(notes.startsWith("# Coach notes\n\nNewest first.\n\n## 2026-09-07 — debrief: Agents meetup\n\n**What did you do?** talked to two founders\n\n**What did you learn?** nobody has evals\n\n**What belongs in a story?** the BGM quality gate\n\n## 2026-09-06 — first assessment"), notes);

// 7. a missing file is created without a sha; unicode survives the base64 round trip
calls.length = 0;
delete files["study-plan/data/log.jsonl"];
await w.tick({ date: "2026-09-07", block: "stories", minutes: 20, note: "story — «BGM» ✓" });
assert.equal(calls[1].body.sha, undefined);
assert.ok(Buffer.from(calls[1].body.content, "base64").toString("utf8").includes("story — «BGM» ✓"));
assert.equal(HubWriter.b64ToUtf8(HubWriter.utf8ToB64("héllo ✓")), "héllo ✓");

console.log("hubwrite: ok");
