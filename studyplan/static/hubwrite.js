/* The hosted page's write path: GitHub as the backend (study hub design, option B).
 *
 * Every write is GET file + sha -> modify -> PUT with the sha through the GitHub Contents API, retried once on a
 * 409 / 422 sha conflict. Targets: study-plan/data/log.jsonl (append a row), study-plan/plans/<date>.json (block
 * edits), study-plan/profile/notes.md (a dated debrief section, newest first like the file says). Commit messages
 * are `hub: <date> <what>`. The token is whatever the page hands in (localStorage under the site's origin); this
 * module never stores or logs it. Plain script: the board page inlines it, Node loads it for the tests.
 */
(function (root) {
  "use strict";

  function utf8ToB64(s) {
    var bytes = new TextEncoder().encode(s), bin = "";
    for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin);
  }
  function b64ToUtf8(b) {
    var bin = atob(String(b).replace(/\s/g, "")), bytes = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new TextDecoder().decode(bytes);
  }
  function localIso(d) {
    d = d || new Date();
    var p = function (n) { return String(n).padStart(2, "0"); };
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + "T" + p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }

  function HubWriter(opts) {
    this.repo = opts.repo;                                   // "owner/name"
    this.branch = opts.branch || "main";
    this.token = opts.token;
    this.api = (opts.api || "https://api.github.com").replace(/\/$/, "");
    this.fetch = opts.fetch || (typeof fetch !== "undefined" ? fetch.bind(root) : null);
    this.now = opts.now || localIso;
  }

  HubWriter.prototype.headers = function () {
    return { "Authorization": "Bearer " + this.token, "Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json" };
  };

  HubWriter.prototype.url = function (path) {
    return this.api + "/repos/" + this.repo + "/contents/" + path.split("/").map(encodeURIComponent).join("/");
  };

  /* The file's text and blob sha on the branch; a missing file is {text: "", sha: null}. */
  HubWriter.prototype.get = async function (path) {
    var r = await this.fetch(this.url(path) + "?ref=" + encodeURIComponent(this.branch), { headers: this.headers() });
    if (r.status === 404) return { text: "", sha: null };
    if (!r.ok) throw new Error("GET " + path + ": HTTP " + r.status + " " + (await errorText(r)));
    var body = await r.json();
    return { text: b64ToUtf8(body.content || ""), sha: body.sha };
  };

  HubWriter.prototype.put = async function (path, text, sha, message) {
    var payload = { message: message, content: utf8ToB64(text), branch: this.branch };
    if (sha) payload.sha = sha;
    var r = await this.fetch(this.url(path), { method: "PUT", headers: this.headers(), body: JSON.stringify(payload) });
    if (!r.ok) { var e = new Error("PUT " + path + ": HTTP " + r.status + " " + (await errorText(r))); e.status = r.status; throw e; }
    return r.json();
  };

  /* GET, apply `change(text) -> newText`, PUT with the sha; one retry when someone else wrote in between. */
  HubWriter.prototype.modify = async function (path, change, message) {
    for (var attempt = 0; attempt < 2; attempt++) {
      var cur = await this.get(path);
      try {
        var res = await this.put(path, change(cur.text), cur.sha, message);
        return { commit: res.commit && res.commit.sha, path: path, message: message };
      } catch (e) {
        if (attempt === 0 && (e.status === 409 || e.status === 422)) continue;
        throw e;
      }
    }
  };

  HubWriter.prototype.appendLine = function (path, line, message) {
    return this.modify(path, function (text) { return text + (text && !text.endsWith("\n") ? "\n" : "") + line + "\n"; }, message);
  };

  HubWriter.prototype.editJson = function (path, change, message) {
    return this.modify(path, function (text) { return JSON.stringify(change(JSON.parse(text)), null, 2) + "\n"; }, message);
  };

  /* ---- the three writes ---- */

  /* Tick or untick an item or a block: one row appended to the log, the shape the local board writes. */
  HubWriter.prototype.tick = async function (o) {
    var target = o.item || o.block;
    var row = { ts: this.now(), date: o.date, block: o.block, minutes: 0, note: "", target: target };
    if (o.undo) { row.note = "undo " + target; row.undo = true; }
    else if (o.item) { row.note = ("item: " + (o.text || "")).slice(0, 200); row.item = o.item; }
    else { row.minutes = o.minutes == null ? 0 : o.minutes; row.note = o.note || ""; }
    row.source = "hub";
    var line = JSON.stringify(row);
    var res = await this.appendLine("study-plan/data/log.jsonl", line, "hub: " + o.date + " " + (o.undo ? "undo " : "tick ") + target);
    res.row = row; res.line = line;
    return res;
  };

  HubWriter.prototype.itemDecision = function (o) {
    var reason = (o.reason || "").trim(), ts = this.now();
    if (!["applicable", "not_applicable"].includes(o.state) || !reason || reason.length > 1000) throw new Error("A reason of 1–1000 characters is required.");
    return this.editJson("study-plan/plans/" + o.date + ".json", function (plan) {
      var block = plan.blocks.find(b => b.id === o.block);
      var item = block && block.items.find(i => i.id === o.item);
      if (!item) throw new Error("Unknown item");
      (item.decisions ||= []).push({state: o.state, reason: reason, ts: ts, source: "hub"});
      return plan;
    }, "hub: " + o.date + " item decision " + o.item);
  };

  /* Move / resize a block in the day's plan JSON; the day becomes arranged by hand. */
  HubWriter.prototype.moveBlock = function (o) {
    var what = [];
    if (o.start != null) what.push("to " + o.start);
    if (o.minutes != null) what.push(o.minutes + " min");
    return this.editJson("study-plan/plans/" + o.date + ".json", function (plan) {
      var block = (plan.blocks || []).find(function (b) { return b.id === o.block; });
      if (!block) throw new Error("no block " + o.block + " in the plan for " + o.date);
      if (o.start != null) block.start = o.start;
      if (o.minutes != null) block.minutes = o.minutes;
      if (o.title) block.title = o.title;
      plan.arranged = true;
      return plan;
    }, "hub: " + o.date + " move " + o.block + (what.length ? " " + what.join(", ") : ""));
  };

  /* A dated debrief section in notes.md, inserted after the preamble because the file keeps newest first. */
  HubWriter.prototype.debrief = function (o) {
    var title = "## " + o.date + " — debrief" + (o.event ? ": " + o.event : "");
    var section = title + "\n\n**What did you do?** " + (o.did || "").trim() + "\n\n**What did you learn?** " + (o.learned || "").trim()
      + "\n\n**What belongs in a story?** " + (o.story || "").trim() + "\n";
    return this.modify("study-plan/profile/notes.md", function (text) {
      if (!text) return "# Coach notes\n\n" + section;
      var at = text.search(/^## /m);
      if (at < 0) return text.replace(/\n*$/, "\n\n") + section;
      return text.slice(0, at) + section + "\n" + text.slice(at);
    }, "hub: " + o.date + " debrief" + (o.event ? " " + o.event : ""));
  };

  async function errorText(r) {
    try { var j = await r.json(); return j.message || ""; } catch (e) { return ""; }
  }

  HubWriter.utf8ToB64 = utf8ToB64;
  HubWriter.b64ToUtf8 = b64ToUtf8;
  root.HubWriter = HubWriter;
  if (typeof module !== "undefined" && module.exports) module.exports = HubWriter;
})(typeof globalThis !== "undefined" ? globalThis : this);
