import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { startMockCanvas } from "./mock-canvas.mjs";
import { syncCanvas, upcoming, NotLoggedInError, parseCanvasJson, nextLink, fileIdsInHtml, zipPlan, fileKey, classKey, isVisible } from "../canvas.js";
import { buildZip, crc32 } from "../zip.js";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

let canvas;
before(async () => { canvas = await startMockCanvas(); });
after(() => canvas.close());

const getWith = (cookie) => async (url) => {
  const r = await fetch(url, { headers: { Cookie: cookie, Accept: "application/json" } });
  return { status: r.status, link: r.headers.get("link"), text: await r.text() };
};

test("parses while(1) prefix and Link headers", () => {
  assert.deepEqual(parseCanvasJson('while(1);[{"a":1}]'), [{ a: 1 }]);
  assert.equal(nextLink('<https://c/x?page=1>; rel="current",<https://c/x?page=2>; rel="next"'), "https://c/x?page=2");
  assert.equal(nextLink('<https://c/x?page=1>; rel="current"'), null);
});

test("logged-out session raises NotLoggedInError", async () => {
  await assert.rejects(syncCanvas({ baseUrl: canvas.url, get: getWith("") }), NotLoggedInError);
});

test("full sync against mock Canvas", async () => {
  const snap = await syncCanvas({ baseUrl: canvas.url, get: getWith("canvas_session=valid"), now: canvas.NOW });

  assert.equal(snap.user.name, "Traveler Test");
  assert.deepEqual(snap.courses.map((c) => c.course_code).sort(),
    ["CS 1110", "CS 1110-102", "MATH 1320", "PLACEMENT"], "date-restricted course dropped");
  const byId = (id) => snap.courses.find((c) => c.id === id);
  assert.equal(isVisible(byId("104")), false, "off-dashboard course hidden by default");
  assert.equal(isVisible(byId("104"), { 104: true }), true, "user override wins");
  assert.equal(byId("105").class_key, byId("102").class_key, "same-name sections merge");
  assert.notEqual(byId("101").class_key, byId("102").class_key);

  const calc = snap.courses.find((c) => c.id === "101");
  assert.equal(calc.assignments.length, 8, "pagination followed across 4 pages");
  assert.equal(calc.files.length, 3);
  assert.equal(calc.modules.length, 1, "retried after rate limit");
  const ps1 = calc.assignments.find((a) => a.name === "PS1");
  assert.equal(ps1.submission.comments[0].comment, "Show your work on 3b");
  assert.equal(ps1.submission.attachments[0].name, "ps1.pdf");
  assert.equal(calc.grade.current_score, 91.4);
  assert.equal(calc.assignment_groups.find((g) => g.id === "11").drop_lowest, 1);
  assert.equal(calc.announcements[0].title, "Exam room change");

  const status = Object.fromEntries(calc.assignments.map((a) => [a.name, a.status]));
  assert.deepEqual(status, {
    PS1: "graded", PS2: "missing", PS3: "upcoming", "Midterm 1": "no_submission", PS0: "submitted_late",
    PA03: "submitted", "Knowledge Checkpoint 1": "no_submission", "Gallery Walk": "past_due",
  });

  const cs = snap.courses.find((c) => c.id === "102");
  assert.equal(cs.files_tab_hidden, true);
  // Files tab hidden, yet the module file and the embedded link both resolve; 79 is forbidden.
  assert.deepEqual(cs.files.map((f) => [f.name, f.sources[0]]).sort(),
    [["lab1.py", "embedded"], ["week1-slides.pdf", "module"]]);
  assert.equal(cs.pages[0].title, "Week 1 notes");
  assert.match(cs.pages[0].body_html, /answers/, "page body fetched even though Pages list is hidden");
  assert.equal(cs.assignments[0].rubric[0].description, "Correctness");
  assert.equal(cs.quizzes, null, "a hidden tab is 'unknown' (null), never an empty list the server would treat as deletions");
  assert.equal(calc.discussions, null, "logged in but not authorized (401 unauthorized) is not treated as logged out");
  assert.deepEqual(calc.roster_ids, ["7", "8"]);
  assert.equal(cs.pages_partial, true, "Pages list hidden: only module pages were sent");
  assert.deepEqual(snap.restricted.map((r) => `${r.status} ${r.endpoint}`).sort(),
    ["401 /courses/101/discussion_topics", "403 /courses/102/files", "403 /files/79", "404 /courses/102/pages",
     "404 /courses/102/quizzes"]);
  assert.deepEqual(snap.errors, []);

  assert.equal(snap.missing[0].name, "PS2");
  assert.equal(snap.planner[0].html_url, `${canvas.url}/courses/101/assignments/3`);
  assert.equal(snap.calendar_events[0].location, "Kerchof 317");

  const due = upcoming(snap, 14, canvas.NOW).map((a) => a.name);
  // Past in-class items and already-submitted work drop out; unflagged past-due stays.
  assert.deepEqual(due, ["Gallery Walk", "PS2", "Lab 1", "PS3", "Midterm 1"]);
});

test("finds file ids in Canvas rich content", () => {
  const ids = fileIdsInHtml('<a href="/courses/5/files/12/download">x</a><img src="/files/34/preview"><a data-api-endpoint="https://c/api/v1/courses/5/files/12">');
  assert.deepEqual([...ids].sort(), ["12", "34"]);
});

test("zip plan: one folder per class, deduped files, hidden courses and locked files skipped", () => {
  const f = (id, name = `f${id}.pdf`) => ({ id, name, download_url: `https://x/${id}` });
  const snap = { courses: [
    { id: "a", class_key: "t::ethics", name: "Ethics: Lecture/Discussion", on_dashboard: true, files: [f("1"), f("2", "Notes.pdf")] },
    { id: "b", class_key: "t::ethics", name: "Ethics: Lecture/Discussion", on_dashboard: true, files: [f("2", "Notes.pdf"), f("6", "notes.pdf")] },
    { id: "c", class_key: "t::placement", name: "Placement Test", on_dashboard: false, files: [f("3")] },
    { id: "d", class_key: "t::cs", name: "Intro to CS", on_dashboard: true, files: [f("4"), { ...f("5"), locked: true }] },
  ] };
  assert.deepEqual(zipPlan(snap).map((j) => j.path), [
    "Ethics_ Lecture_Discussion/f1.pdf",
    "Ethics_ Lecture_Discussion/Notes.pdf",
    "Ethics_ Lecture_Discussion/notes (2).pdf", // different file, clashing name
    "Intro to CS/f4.pdf",
  ]);
  assert.equal(zipPlan(snap, { c: true }).length, 5, "unhiding a course includes its files");
});

test("zip output opens with standard tools", () => {
  assert.equal(crc32(new TextEncoder().encode("123456789")), 0xcbf43926);
  const bin = new Uint8Array(70000).map((_, i) => (i * 31) & 0xff);
  const parts = buildZip([
    { path: "Calculus I/syllabus.txt", data: new TextEncoder().encode("hello canvas") },
    { path: "Intro to CS/lab1.bin", data: bin, date: new Date(2026, 8, 1, 10, 30) },
    { path: "日本語/ノート.txt", data: new TextEncoder().encode("utf8 names") },
  ]);
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "hatchzip-")), "t.zip");
  fs.writeFileSync(file, Buffer.concat(parts.map((p) => Buffer.from(p))));
  execFileSync("unzip", ["-tq", file]); // throws on any CRC/structure error
  assert.equal(execFileSync("unzip", ["-p", file, "Calculus I/syllabus.txt"]).toString(), "hello canvas");
  assert.deepEqual(new Uint8Array(execFileSync("unzip", ["-p", file, "Intro to CS/lab1.bin"])), bin);
  const listing = execFileSync("python3", ["-c", "import zipfile,sys;print('\\n'.join(zipfile.ZipFile(sys.argv[1]).namelist()))", file]).toString();
  assert.match(listing, /日本語\/ノート\.txt/);
});

test("no school-specific assumptions: varied course naming across schools", () => {
  const term = { id: 3 };
  const k = (name, t = term) => classKey({ name, term: t });
  // High school, community college, and university naming all key on the name alone.
  assert.equal(k("AP Calculus AB"), k("AP Calculus AB"));
  assert.notEqual(k("AP Calculus AB"), k("AP Calculus BC"));
  assert.equal(k("  English 101 "), k("english 101"));
  assert.notEqual(k("Biology"), k("Biology", { id: 4 }), "same name in a different term is a different class");
  assert.equal(k("Chem"), classKey({ name: "Chem" }).replace("none", "3"));
  // Students who never starred courses see everything.
  assert.equal(isVisible({ id: "1", on_dashboard: null }), true);
});

test("uploads straight to storage when the server hands out upload URLs, else through the server", async () => {
  const { uploadSnapshot } = await import("../upload.js");
  const calls = [];
  const fakeFetch = async (url, init = {}) => {
    calls.push({ url, method: init.method, headers: init.headers || {} });
    const json = (body, status = 200) => ({ ok: status < 400, status, json: async () => body });
    if (url.endsWith("/v1/snapshots")) {
      return json({ snapshot_id: "s1", files_needed: ["1", "2", "3"], upload_urls: {
        1: { url: "https://storage.test/one", headers: { "Content-Type": "application/pdf" } },
        2: { url: "https://storage.test/broken", headers: { "Content-Type": "text/plain" } },
      } });
    }
    if (url === "https://storage.test/broken") return json({}, 403);
    return json({ ok: true });
  };
  const plan = ["1", "2", "3"].map((id) => ({ file: { id, updated_at: "2026-01-01T00:00:00Z", name: `${id}.pdf`, content_type: "application/pdf" }, path: `${id}.pdf` }));
  const progress = [];
  const result = await uploadSnapshot({ serverUrl: "https://hatch.test", token: "t", snapshot: {}, plan, fetchImpl: fakeFetch,
    fetchBytes: async () => new Uint8Array([1, 2, 3]), onProgress: (p) => progress.push(p.done), retryDelayMs: 0 });
  assert.equal(result.uploaded, 3);
  assert.deepEqual(result.failed, []);
  const seen = (m, u) => calls.some((c) => c.method === m && c.url.startsWith(u));
  // 1: direct upload with the signed Content-Type, then a confirm; never sent through the server
  assert.ok(seen("PUT", "https://storage.test/one"));
  assert.equal(calls.find((c) => c.url === "https://storage.test/one").headers["Content-Type"], "application/pdf");
  assert.ok(seen("POST", "https://hatch.test/v1/files/1/uploaded?"));
  assert.ok(!seen("PUT", "https://hatch.test/v1/files/1?"));
  // 2: storage refused three times, so it falls back to the server; 3: no URL, straight to the server
  assert.equal(calls.filter((c) => c.url === "https://storage.test/broken").length, 3);
  assert.ok(seen("PUT", "https://hatch.test/v1/files/2?") && !seen("POST", "https://hatch.test/v1/files/2/uploaded"));
  assert.ok(seen("PUT", "https://hatch.test/v1/files/3?"));
  assert.equal(Math.max(...progress), 3);
});

test("zip plan skips files already downloaded and zips copies of one file once", () => {
  const f = (id, name, size, content_type = "application/pdf") => ({ id, name, size, content_type, download_url: `https://x/${id}` });
  const snapshot = { courses: [
    { id: "1", name: "Programming", class_key: "p", on_dashboard: true,
      files: [f("1", "lab02.py", 2116, "text/x-python"), f("2", "lab02.py", 2116, "text/x-python"), f("3", "solution.py", 10, "text/x-python")] },
    { id: "2", name: "Programming lab", class_key: "p2", on_dashboard: true,
      files: [f("4", "solution.py", 99, "text/x-python"), f("5", "Syllabus.pdf", 500)] },
  ] };
  const all = zipPlan(snapshot, {}, { dedupe: true });
  assert.deepEqual(all.map((j) => j.file.id), ["1", "3", "4", "5"], "same name+type+size zipped once; same name, other size kept");
  const skip = new Set(all.slice(0, 3).map((j) => fileKey(j.file)));
  assert.deepEqual(zipPlan(snapshot, {}, { dedupe: true, skipKeys: skip }).map((j) => j.file.id), ["5"], "only the new file");
  assert.equal(zipPlan(snapshot).length, 5, "uploads still announce every Canvas file; the server dedupes");
});

test("a 502 from the server is retried instead of waiting for the next sync", async () => {
  const { uploadSnapshot } = await import("../upload.js");
  let putTries = 0;
  const fakeFetch = async (url, init = {}) => {
    const json = (body, status = 200) => ({ ok: status < 400, status, json: async () => body });
    if (url.endsWith("/v1/snapshots")) return json({ snapshot_id: "s", files_needed: ["1"] });
    if (init.method === "PUT") return ++putTries < 2 ? json({}, 502) : json({ ok: true });
    return json({ ok: true });
  };
  const plan = [{ file: { id: "1", updated_at: null, name: "a.py", content_type: "text/x-python" }, path: "a.py" },
                { file: { id: "2", updated_at: null, name: "b.py", content_type: "text/x-python" }, path: "b.py" }];
  const progress = [];
  const r = await uploadSnapshot({ serverUrl: "https://h.test", token: "t", snapshot: {}, plan, fetchImpl: fakeFetch,
    fetchBytes: async () => new Uint8Array([1]), retryDelayMs: 0, onProgress: (p) => progress.push(p) });
  assert.equal(putTries, 2);
  assert.deepEqual([r.uploaded, r.skipped, r.failed.length], [1, 1, 0]);
  assert.ok(progress.every((p) => p.skipped === 1), "progress says how many were already there");
});
