import { test } from "node:test";
import assert from "node:assert/strict";
import { startMockD2L, ENROLLMENTS, NOW } from "./mock-d2l.mjs";
import {
  diagnoseBrightspace, shapeOf, unionShapes, isBrightspaceUrl, pickVersion, summaryText, summarize,
  ENUM_FIELDS, ENUM_VALUE, D2L_TYPE_VALUE, NOTES, ROUTES,
} from "../d2l.js";
import { uploadDiagnostic } from "../upload.js";

const getWith = (cookie, log = null) => async (url, { signal } = {}) => {
  log?.push(url);
  const r = await fetch(url, { headers: { Cookie: cookie, Accept: "application/json" }, signal });
  return { status: r.status, text: await r.text(), headers: { "retry-after": r.headers.get("retry-after") } };
};
const SIGNED_IN = "d2lSessionVal=valid; d2lSecureSessionVal=valid";

async function run(mockOptions = {}, options = {}, cookie = SIGNED_IN) {
  const mock = await startMockD2L(mockOptions);
  try {
    const report = await diagnoseBrightspace({ baseUrl: `${mock.url}/d2l/home`, get: getWith(cookie), now: NOW,
      extensionVersion: "1.5.0", transport: "worker", ...options });
    return { report, mock };
  } finally {
    await mock.close();
  }
}

// Every string value in a report, with where it sits: inside an "enums" map (and under which
// field), or anywhere else.
function stringValues(value, out = [], where = { enumField: null }) {
  if (typeof value === "string") out.push({ value, enumField: where.enumField });
  else if (Array.isArray(value)) value.forEach((v) => stringValues(v, out, where));
  else if (value && typeof value === "object") {
    for (const [k, v] of Object.entries(value)) {
      if (k === "enums" && v && typeof v === "object") {
        for (const [field, list] of Object.entries(v)) stringValues(list, out, { enumField: field });
      } else {
        stringValues(v, out, { enumField: null });
      }
    }
  }
  return out;
}

const allKeys = (value, out = new Set()) => {
  if (Array.isArray(value)) value.forEach((v) => allKeys(v, out));
  else if (value && typeof value === "object") for (const [k, v] of Object.entries(value)) { out.add(k); allKeys(v, out); }
  return out;
};

test("Brightspace pages are recognised by their /d2l/ path, on any domain", () => {
  assert.equal(isBrightspaceUrl("https://purdue.brightspace.com/d2l/home"), true);
  assert.equal(isBrightspaceUrl("https://learn.uwaterloo.ca/d2l/le/content/123/Home"), true);
  assert.equal(isBrightspaceUrl("https://school.instructure.com/courses/1"), false);
  assert.equal(isBrightspaceUrl("chrome://extensions/d2l/"), false);
  assert.equal(isBrightspaceUrl("not a url"), false);
});

test("version picking takes the newest supported lp and le", () => {
  const list = [{ ProductCode: "lp", LatestVersion: "1.9", SupportedVersions: ["1.10", "1.63", "unstable"] },
    { ProductCode: "le", LatestVersion: "1.99", SupportedVersions: ["1.82"] }];
  assert.equal(pickVersion(list, "lp"), "1.63", "numeric, not string, order; 'unstable' ignored");
  assert.equal(pickVersion(list, "le"), "1.99");
  assert.equal(pickVersion(list, "ep"), null);
  assert.equal(pickVersion({ not: "a list" }, "lp"), null);
});

test("shapeOf keeps types and field names, never values", () => {
  assert.equal(shapeOf("Pat Example"), "str");
  assert.equal(shapeOf("2026-10-03T16:00:00.000Z"), "date");
  assert.equal(shapeOf("2026-10-03"), "date");
  assert.equal(shapeOf("https://school.brightspace.com/d2l/home"), "url");
  assert.equal(shapeOf("<p>Exam moved</p>"), "html");
  assert.equal(shapeOf("2 < 3 and 5 > 4"), "str", "comparison signs aren't tags");
  assert.equal(shapeOf(87.25), "num");
  assert.equal(shapeOf(false), "bool");
  assert.equal(shapeOf(null), "null");
  assert.deepEqual(shapeOf({ b: 1, a: "x", DueDate: null }), { DueDate: "null", a: "str", b: "num" });
  assert.deepEqual(Object.keys(shapeOf({ z: 1, a: 1, M: 1 })), ["M", "a", "z"], "keys sorted");

  // Arrays: the union of the first 5 elements, and the length.
  assert.deepEqual(shapeOf([{ d: "2026-01-01" }, { d: null }, { d: "2026-01-02", e: true }]),
    { "[]": { d: "date|null", e: "bool" }, len: 3 });
  assert.deepEqual(shapeOf([1, 2, 3, 4, 5, "sixth is not sampled"]), { "[]": "num", len: 6 });
  assert.deepEqual(shapeOf([]), { len: 0 });
  // An object that is sometimes null (D2L's Availability): the alternatives go under "{or}".
  assert.deepEqual(shapeOf([null, { StartDate: "2026-01-01" }]), { "[]": { StartDate: "date", "{or}": "null" }, len: 2 });
  assert.deepEqual(unionShapes({ len: 1, "[]": "num" }, { len: 4, "[]": "str" }), { "[]": "num|str", len: 4 });

  // Keys that aren't field names (ids, names) are never written out.
  assert.deepEqual(shapeOf({ 12345: { a: 1 }, "Pat Example": 2, "x.y": 3, Ok_1: 4 }),
    { Ok_1: "num", "{id}": { a: "num" }, "{key}": "num" });

  // At most 60 fields per object, 5 levels deep.
  const wide = Object.fromEntries(Array.from({ length: 70 }, (_, i) => [`F${String(i).padStart(2, "0")}`, i]));
  const flags = {};
  assert.equal(Object.keys(shapeOf(wide, flags)).length, 60);
  assert.equal(flags.keysCapped, true);
  const deep = { a: { b: { c: { d: { e: { f: { g: 1 } } } } } } };
  assert.deepEqual(shapeOf(deep), { a: { b: { c: { d: { e: { f: "obj" } } } } } });
  assert.deepEqual(shapeOf([[[[[[["deep"]]]]]]]), { "[]": { "[]": { "[]": { "[]": { "[]": { "[]": "arr", len: 1 }, len: 1 }, len: 1 }, len: 1 }, len: 1 }, len: 1 });
});

test("full check against the mock Brightspace", async () => {
  const { report, mock } = await run();
  assert.equal(report.lms, "brightspace");
  assert.equal(report.schema, 1);
  assert.equal(report.host, "127.0.0.1");
  assert.equal(report.ran_at, "2026-10-03T16:00:00.000Z");
  assert.deepEqual(report.versions, { lp: "1.63", le: "1.99" });
  assert.equal(report.signed_in, true);
  assert.deepEqual(report.notes, []);

  const names = report.endpoints.map((e) => `${e.course ?? "-"} ${e.name} ${e.status}`);
  assert.deepEqual(names, [
    "- versions 200", "- whoami 200", "- enrollments 200", "- calendar_events 200", "- my_items_due 403",
    "1 dropbox_folders 200", "1 dropbox_mysubmissions 200", "1 quizzes 200", "1 grade_values 200", "1 grade_objects 200",
    "1 grade_categories 200", "1 grade_setup 200", "1 news 200", "1 content_toc 200",
    "2 dropbox_folders 200", "2 quizzes 200", "2 grade_values 200", "2 grade_objects 403", "2 grade_categories 200",
    "2 grade_setup 200", "2 news 200", "2 content_toc 200",
    "3 dropbox_folders 403", "3 quizzes 200", "3 grade_values 200", "3 grade_objects 200", "3 grade_categories 200",
    "3 grade_setup 200", "3 news 200", "3 content_toc 200",
  ], "course 2 has no folders, so no mysubmissions call; 403s are recorded, not fatal");

  const enroll = report.endpoints.find((e) => e.name === "enrollments");
  assert.equal(enroll.count, 5, "all three bookmark pages read");
  assert.equal(enroll.pages, 3);
  assert.equal(enroll.path, "/d2l/api/lp/1.63/enrollments/myenrollments/?orgUnitTypeId=3");
  assert.equal(enroll.shape.Items["[]"].OrgUnit.Name, "str");
  assert.equal(report.endpoints.find((e) => e.name === "quizzes").count, 2, "ObjectListPage counted by Objects");
  assert.equal(report.endpoints.find((e) => e.name === "dropbox_mysubmissions").path,
    "/d2l/api/le/1.99/{ou}/dropbox/folders/{id}/submissions/mysubmissions/", "ids replaced by placeholders");
  assert.ok(report.endpoints.every((e) => typeof e.ms === "number" && e.ms >= 0));
  assert.equal(report.endpoints.find((e) => e.name === "whoami").shape, null, "nothing from whoami is kept");

  // Only GETs, at most 40, to the courses the student can use (6609 is inactive, 6610 locked).
  const api = mock.hits.filter((h) => h.startsWith("/d2l/api/"));
  assert.ok(api.length <= 40);
  assert.ok(!api.some((h) => /\/(6609|6610)\//.test(h)));
  const cal = new URL(api.find((h) => h.includes("myEvents")), "http://x");
  assert.equal(cal.searchParams.get("orgUnitIdsCSV"), "6606,6607,6608", "active courses only");
  assert.equal(cal.searchParams.get("startDateTime"), "2026-09-19T16:00:00.000Z", "now - 14 days, D2L's UTC format");
  assert.equal(cal.searchParams.get("endDateTime"), "2027-01-31T16:00:00.000Z", "now + 120 days");
  assert.ok(api.includes("/d2l/api/le/1.99/6606/dropbox/folders/1001/submissions/mysubmissions/"), "first folder's submissions");

  // Enum values, merged and per route.
  assert.deepEqual(report.enums.SubmissionType, [0, 2]);
  assert.deepEqual(report.enums.AssociatedEntityType, ["D2L.LE.Dropbox.Dropbox"]);
  assert.deepEqual(report.enums.GradingSystem, ["Points", "Weighted"]);
  assert.deepEqual(report.enums.GradeType, ["Numeric", "PassFail"]);
  assert.deepEqual(report.endpoints.find((e) => e.name === "dropbox_folders").enums.CompletionType, [0]);
  assert.deepEqual(report.endpoints.find((e) => e.name === "content_toc").enums.CompletionType, [2]);
  assert.ok(Object.keys(report.enums).every((k) => ENUM_FIELDS.has(k)));

  assert.equal(summaryText(report),
    "27 of 30 checks answered; assignments: partly; quizzes: yes; grades: partly; calendar: yes; content: partly; announcements: yes");
  assert.ok(JSON.parse(JSON.stringify(report)), "JSON-serializable");
});

test("nothing the mock served reaches the report except whitelisted enum values and versions", async () => {
  const { report, mock } = await run();
  const json = JSON.stringify(report);
  const served = mock.served;
  for (const { value, enumField } of stringValues(report)) {
    if (!served.has(value)) continue;
    const allowed = (enumField && ENUM_FIELDS.has(enumField) && (ENUM_VALUE.test(value) || D2L_TYPE_VALUE.test(value)))
      || value === report.versions.lp || value === report.versions.le;
    assert.ok(allowed, `served string leaked: ${JSON.stringify(value)}`);
  }
  for (const s of ["Pat Example", "pexample", "31337", "FAKE Biology 101 - Section 2", "BIOL-101-02", "Course Offering",
    "87.25 %", "B+", "Nice work, Pat - check your units.", "FAKE-quiz-password", "FAKE Hall 101", "Student", "8.725", "13.0875"]) {
    assert.ok(!json.includes(s), `${s} must not appear`);
  }
});

test("signed out: whoami's 403 ends the check after two requests", async () => {
  const { report, mock } = await run({}, {}, "");
  assert.equal(report.signed_in, false);
  assert.deepEqual(report.endpoints.map((e) => [e.name, e.status]), [["versions", 200], ["whoami", 403]]);
  assert.deepEqual(report.notes, ["signed_out"]);
  assert.deepEqual(report.versions, { lp: "1.63", le: "1.99" }, "the public version list still answers");
  assert.equal(mock.hits.length, 2);
  assert.ok(report.notes.every((n) => n in NOTES));
});

test("not Brightspace: no version list, nothing else is tried", async () => {
  const report = await diagnoseBrightspace({ baseUrl: "https://example.test", now: NOW,
    get: async () => ({ status: 404, text: "<html>Not found</html>" }) });
  assert.equal(report.signed_in, false);
  assert.deepEqual(report.endpoints.map((e) => [e.name, e.status]), [["versions", 404]]);
  assert.deepEqual(report.notes, ["versions_unavailable"]);
});

test("a 429 stops the check and records Retry-After", async () => {
  const { report, mock } = await run({ rateLimitAfter: 6 });
  const limited = report.endpoints.filter((e) => e.status === 429);
  // Two requests are in flight at a time, so the one beside the first 429 can be a 429 too;
  // nothing new starts after it.
  assert.ok(limited.length >= 1 && limited.length <= 2, `${limited.length} 429s`);
  assert.ok(limited.every((e) => e.retry_after === 30));
  assert.ok(report.notes.includes("rate_limited"));
  const api = mock.hits.filter((h) => h.startsWith("/d2l/api/") && h !== "/d2l/api/versions/");
  assert.equal(api.length, 6 + limited.length, "six answered, then only the 429s");
  assert.equal(report.endpoints.length, 1 + 6 - 2 + limited.length, "versions + answered (two pages folded) + 429s");
});

test("enrollment paging stops after 5 pages", async () => {
  const many = Array.from({ length: 13 }, (_, i) => ({ ...ENROLLMENTS[0], OrgUnit: { ...ENROLLMENTS[0].OrgUnit, Id: 7000 + i } }));
  const { report, mock } = await run({ enrollments: many, pageSize: 2 }, { maxCourses: 1 });
  const enroll = report.endpoints.find((e) => e.name === "enrollments");
  assert.equal(enroll.count, 10);
  assert.equal(enroll.pages, 5);
  assert.ok(report.notes.includes("paging_capped"));
  assert.equal(mock.hits.filter((h) => h.includes("myenrollments")).length, 5);
  assert.equal(report.endpoints.filter((e) => e.course).every((e) => e.course === 1), true, "maxCourses respected");
});

test("a route that never answers times out and the rest still run", async () => {
  const { report } = await run({ hang: /grades\/setup\/$/ }, { timeoutMs: 150 });
  const setup = report.endpoints.filter((e) => e.name === "grade_setup");
  assert.equal(setup.length, 3);
  assert.ok(setup.every((e) => e.status === 0 && e.error === "timeout"));
  assert.equal(report.endpoints.filter((e) => e.name === "content_toc").length, 3);
});

test("requests are capped and never more than two at a time", async () => {
  const mock = await startMockD2L();
  let inFlight = 0, peak = 0;
  const base = getWith(SIGNED_IN);
  const get = async (url, opts) => {
    peak = Math.max(peak, ++inFlight);
    try { return await base(url, opts); } finally { inFlight--; }
  };
  try {
    const capped = await diagnoseBrightspace({ baseUrl: mock.url, get, now: NOW, maxRequests: 10 });
    assert.equal(capped.endpoints.length, 10 - 2, "two enrollment pages fold into one entry");
    assert.ok(capped.notes.includes("request_cap"));
    assert.equal(mock.hits.length, 10);
    assert.equal(peak, 2);
  } finally {
    await mock.close();
  }
});

// ---------- the "no strings leak" property ----------

function rng(seed) {
  let s = seed >>> 0;
  return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 2 ** 32);
}

function randomString(r) {
  const pick = (list) => list[Math.floor(r() * list.length)];
  const word = () => Array.from({ length: 3 + Math.floor(r() * 7) }, () => pick("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")).join("");
  const n = () => String(Math.floor(r() * 1e9));
  const kinds = [
    () => `${word()} ${word()}`,                                   // looks like an enum value (or a name)
    () => word() + word(),
    () => `20${10 + Math.floor(r() * 30)}-0${1 + Math.floor(r() * 9)}-1${Math.floor(r() * 9)}T0${Math.floor(r() * 9)}:${10 + Math.floor(r() * 49)}:00.${n().slice(0, 3)}Z`,
    () => `https://${word()}.example/${word()}?id=${n()}`,
    () => `<p>${word()} <b>${word()}</b></p>`,
    () => `D2L.${word()}.${word()}`,
    () => `${word()}-${n()} ${word()}!`,
    () => `${n()}${n()}`,
    () => `Ünïcödé ${word()} 🎓 ${n()}`,
    () => `${word()}@${word()}.edu`,
  ];
  return pick(kinds)();
}

function hostileKey(r) {
  const list = [() => `${randomString(r).slice(0, 12)} key`, () => String(Math.floor(r() * 1e9)), () => `x.${Math.floor(r() * 1e6)}`,
    () => `naïve-${Math.floor(r() * 1e6)}`, () => `@odata.${Math.floor(r() * 1e6)}`, () => `${Math.floor(r() * 9)}abc${Math.floor(r() * 1e6)}`];
  return list[Math.floor(r() * list.length)]();
}

// Replaces every string with a random one (and some enum numbers), and adds a field with a
// hostile key to every object. Records each injected string with the field it went under.
function injector(seed) {
  const r = rng(seed);
  const injected = new Map();  // string -> Set of field names it was placed under
  const record = (s, field) => { if (!injected.has(s)) injected.set(s, new Set()); injected.get(s).add(field); return s; };
  const walk = (value, field) => {
    if (typeof value === "string") return record(randomString(r), field);
    if (typeof value === "number" && ENUM_FIELDS.has(field) && r() < 0.5) return record(randomString(r), field);
    if (Array.isArray(value)) return value.map((v) => walk(v, field));
    if (value && typeof value === "object") {
      const out = {};
      for (const [k, v] of Object.entries(value)) out[k] = k === "Bookmark" ? v : walk(v, k);
      const key = record(hostileKey(r), "(key)");
      out[key] = walk(r() < 0.5 ? "x" : { nested: "x" }, key);
      return out;
    }
    return value;
  };
  return { injected, transform: (path, body) => (path === "/d2l/api/versions/" ? body : walk(body, null)) };
}

test("property: random strings injected anywhere in the answers never reach the report", async () => {
  let captured = 0;
  for (let seed = 1; seed <= 25; seed++) {
    const { injected, transform } = injector(seed);
    const { report } = await run({ transform });
    assert.equal(report.signed_in, true, `seed ${seed}`);
    const json = JSON.stringify(report);
    const keys = allKeys(report);
    for (const { value, enumField } of stringValues(report)) {
      if (!injected.has(value)) continue;
      const ok = enumField && ENUM_FIELDS.has(enumField) && injected.get(value).has(enumField)
        && (ENUM_VALUE.test(value) || D2L_TYPE_VALUE.test(value));
      assert.ok(ok, `seed ${seed}: ${JSON.stringify(value)} leaked outside a whitelisted enum`);
      captured++;
    }
    for (const [s, fields] of injected) {
      assert.ok(!keys.has(s), `seed ${seed}: injected key ${JSON.stringify(s)} written out`);
      const enumOk = [...fields].some((f) => ENUM_FIELDS.has(f)) && (ENUM_VALUE.test(s) || D2L_TYPE_VALUE.test(s));
      if (s.length >= 6 && !enumOk) assert.ok(!json.includes(s), `seed ${seed}: ${JSON.stringify(s)} found in the report`);
    }
  }
  assert.ok(captured > 0, "whitelisted enum fields with enum-looking values were exercised");
});

test("Send posts the report with the sync token; a 429 explains the daily limit", async () => {
  const calls = [];
  const reply = (status, body) => ({ ok: status < 300, status, json: async () => body });
  let next = reply(201, { ok: true, id: 7 });
  const fetchImpl = async (url, init) => { calls.push({ url, init }); return next; };
  const report = { lms: "brightspace", schema: 1, endpoints: [] };
  const out = await uploadDiagnostic({ serverUrl: "https://hatch.example/", token: "hh_tok", report, fetchImpl });
  assert.deepEqual(out, { ok: true, id: 7 });
  assert.equal(calls[0].url, "https://hatch.example/v1/diagnostics");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.headers.Authorization, "Bearer hh_tok");
  assert.deepEqual(JSON.parse(calls[0].init.body), report);
  next = reply(429, { error: "at most 10 reports a day" });
  await assert.rejects(uploadDiagnostic({ serverUrl: "https://hatch.example", token: "hh_tok", report, fetchImpl }), /10 reports/);
  next = reply(401, { error: "invalid or revoked token" });
  await assert.rejects(uploadDiagnostic({ serverUrl: "https://hatch.example", token: "hh_tok", report, fetchImpl }), /Link it again/);
});

test("summary wording", () => {
  const report = { endpoints: [{ name: "grade_values", status: 200 }, { name: "grade_setup", status: 403 },
    { name: "calendar_events", status: 500 }] };
  assert.deepEqual(summarize(report).features, { assignments: "not checked", quizzes: "not checked", grades: "partly",
    calendar: "no", content: "not checked", announcements: "not checked" });
  assert.equal(summarize(report).answered, 1);
  assert.ok(Object.keys(ROUTES).includes("my_items_due"));
});
