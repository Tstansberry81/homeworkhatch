// Brightspace (D2L) diagnostics: a shape-only check of what a school's Brightspace answers to the
// signed-in student, so the real adapter can be built from facts instead of guesses. Pure ES module
// (no chrome.* APIs), like canvas.js: callers inject the transport,
//   get(url, { signal, timeoutMs }) -> { status, text, headers? }
// so the service worker, an open Brightspace tab, or a Node test can drive it.
//
// What the report keeps: which API routes answered, their HTTP status, how long they took, how
// many items came back, the JSON field names and value TYPES ("str", "date", "num"...), and the
// values of a short whitelist of enum fields (like SubmissionType). What it never keeps: any other
// string or number from a response, so no names, grades, course titles, assignment names or text.
// The responses themselves only live in memory while the check runs. GET requests only.

import { pool } from "./canvas.js";

export const DIAG_SCHEMA = 1;

// Field names whose values are D2L enums (types and states, never people or content). Only these
// fields' values are recorded, and only when they look like an enum (see enumValue).
export const ENUM_FIELDS = new Set([
  "ActivityType", "AssociatedEntityType", "CalcTypeId", "CompletionType", "DropboxType", "EndDateAvailabilityType",
  "EntityType", "EventType", "GradeObjectType", "GradeObjectTypeName", "GradeType", "GradingSystem", "ItemType",
  "LateSubmissionOption", "ObjectType", "PagingTypeId", "StartDateAvailabilityType", "Status", "SubmissionRule",
  "SubmissionType", "TopicType", "Type", "TypeIdentifier", "WeightDistributionType",
]);
// Letters and spaces only ("File", "OnPaper", "Weighted"). Anything with digits, punctuation or
// more than 31 characters is never an enum value worth keeping.
export const ENUM_VALUE = /^[A-Za-z][A-Za-z ]{0,30}$/;
// D2L's dotted type names, like "D2L.LE.Dropbox.Dropbox" (a calendar event's AssociatedEntityType).
export const D2L_TYPE_VALUE = /^D2L(?:\.[A-Za-z]{1,40}){1,6}$/;

// The only notes a report can carry (the server accepts nothing else).
export const NOTES = Object.freeze({
  versions_unavailable: "The public version list didn't answer, so nothing else was checked.",
  signed_out: "Brightspace said the student isn't signed in (whoami 401/403).",
  whoami_unexpected: "whoami answered with something other than 200/401/403.",
  worker_signed_out: "The extension's own request looked signed out; the check ran inside the Brightspace tab instead.",
  no_courses: "No accessible course enrollments, so the per-course checks were skipped.",
  paging_capped: "Enrollments had more than 5 pages; only the first 5 were read.",
  paging_error: "A later page of enrollments failed.",
  rate_limited: "Brightspace answered 429 (too many requests), so the check stopped early.",
  request_cap: "The check hit its request cap and skipped the rest.",
  keys_capped: "Some objects had more than 60 fields; only the first 60 (sorted) are listed.",
});

// Routes, in the order the report lists them. {lp}/{le} become the negotiated versions in the
// recorded path; {ou}, {id} and {date} stay placeholders (the real ids and dates never appear).
export const ROUTES = {
  versions: "/d2l/api/versions/",
  whoami: "/d2l/api/lp/{lp}/users/whoami",
  enrollments: "/d2l/api/lp/{lp}/enrollments/myenrollments/?orgUnitTypeId=3",
  calendar_events: "/d2l/api/le/{le}/calendar/events/myEvents/?orgUnitIdsCSV={ou}&startDateTime={date}&endDateTime={date}",
  my_items_due: "/d2l/api/le/{le}/content/myItems/due/?orgUnitIdsCSV={ou}",
  dropbox_folders: "/d2l/api/le/{le}/{ou}/dropbox/folders/",
  dropbox_mysubmissions: "/d2l/api/le/{le}/{ou}/dropbox/folders/{id}/submissions/mysubmissions/",
  quizzes: "/d2l/api/le/{le}/{ou}/quizzes/",
  grade_values: "/d2l/api/le/{le}/{ou}/grades/values/myGradeValues/",
  grade_objects: "/d2l/api/le/{le}/{ou}/grades/",
  grade_categories: "/d2l/api/le/{le}/{ou}/grades/categories/",
  grade_setup: "/d2l/api/le/{le}/{ou}/grades/setup/",
  news: "/d2l/api/le/{le}/{ou}/news/",
  content_toc: "/d2l/api/le/{le}/{ou}/content/toc",
};

// What the plain summary calls each group of routes.
export const FEATURES = {
  assignments: ["dropbox_folders", "dropbox_mysubmissions"],
  quizzes: ["quizzes"],
  grades: ["grade_values", "grade_objects", "grade_categories", "grade_setup"],
  calendar: ["calendar_events"],
  content: ["content_toc", "my_items_due"],
  announcements: ["news"],
};

const DAY = 864e5;
const MAX_DEPTH = 5;
const MAX_KEYS = 60;
const ARRAY_SAMPLE = 5;
const MAX_ENUM_VALUES = 20;
const MAX_PAGES = 5;
const MAX_CSV_COURSES = 25;

// Brightspace pages all live under /d2l/ (/d2l/home, /d2l/le/content/...), on any school's domain.
export function isBrightspaceUrl(url) {
  try {
    const u = new URL(url);
    return /^https?:$/.test(u.protocol) && u.pathname.includes("/d2l/");
  } catch {
    return false;
  }
}

// ---------- shapes ----------

const ISO_DATE = /^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:?\d{2})?)?$/;
const HTTP_URL = /^https?:\/\/\S+$/i;
const HTML_TAG = /<\/?[a-z][a-z0-9-]*(?:\s[^<>]*)?\/?>/i;
const KEY_OK = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;

function stringKind(s) {
  if (ISO_DATE.test(s)) return "date";
  if (HTTP_URL.test(s)) return "url";
  if (HTML_TAG.test(s)) return "html";
  return "str";
}

// D2L's keys are field names ("DueDate"). A key that isn't identifier-like could be data (an id or
// a name used as a key), so it's never written out: ids become "{id}", anything else "{key}".
function safeKey(k) {
  if (KEY_OK.test(k)) return k;
  return /^\d{1,20}$/.test(k) ? "{id}" : "{key}";
}

const isArrShape = (s) => s !== null && typeof s === "object" && Number.isInteger(s.len);

function joinTokens(list) {
  return [...new Set(list.flatMap((t) => t.split("|")))].sort().join("|");
}

function sortKeys(obj) {
  return Object.fromEntries(Object.keys(obj).sort().map((k) => [k, obj[k]]));
}

function withOr(shape, tokens) {
  if (!tokens.length) return shape;
  return sortKeys({ ...shape, "{or}": joinTokens([...(shape["{or}"] ? [shape["{or}"]] : []), ...tokens]) });
}

// The union of two shapes: primitives join as "date|null"; objects merge their fields; an object
// or array that is sometimes null (or a string...) records the alternatives under "{or}".
export function unionShapes(a, b) {
  if (a === undefined) return b;
  if (b === undefined) return a;
  if (typeof a === "string" && typeof b === "string") return joinTokens([a, b]);
  if (typeof a === "string") return unionShapes(b, a);
  if (typeof b === "string") return withOr(a, [b]);
  const aArr = isArrShape(a), bArr = isArrShape(b);
  if (aArr && bArr) {
    const out = { len: Math.max(a.len, b.len) };
    const items = unionShapes(a["[]"], b["[]"]);
    if (items !== undefined) out["[]"] = items;
    const or = [a["{or}"], b["{or}"]].filter(Boolean);
    return withOr(sortKeys(out), or);
  }
  if (!aArr && !bArr) {
    const out = { ...a };
    for (const [k, v] of Object.entries(b)) out[k] = k in out ? unionShapes(out[k], v) : v;
    return sortKeys(out);
  }
  const [obj, arr] = aArr ? [b, a] : [a, b];
  return withOr(obj, ["arr", ...(arr["{or}"] ? [arr["{or}"]] : [])]);
}

function walkShape(value, depth, flags) {
  if (value === null || value === undefined) return "null";
  if (typeof value === "boolean") return "bool";
  if (typeof value === "number") return "num";
  if (typeof value === "string") return stringKind(value);
  if (Array.isArray(value)) {
    if (depth > MAX_DEPTH) return "arr";
    const out = { len: value.length };
    const sample = value.slice(0, ARRAY_SAMPLE).map((v) => walkShape(v, depth + 1, flags));
    if (sample.length) out["[]"] = sample.reduce((x, y) => unionShapes(x, y));
    return sortKeys(out);
  }
  if (typeof value === "object") {
    if (depth > MAX_DEPTH) return "obj";
    const keys = Object.keys(value).sort();
    if (keys.length > MAX_KEYS) flags.keysCapped = true;
    const out = {};
    for (const k of keys.slice(0, MAX_KEYS)) {
      const key = safeKey(k);
      const s = walkShape(value[k], depth + 1, flags);
      out[key] = key in out ? unionShapes(out[key], s) : s;
    }
    return sortKeys(out);
  }
  return "null";  // functions, symbols: not JSON
}

// The structure of a JSON value with every value replaced by its type: objects become
// { field: shape } (sorted, at most 60 fields, 5 levels deep), arrays { "[]": shape of up to 5
// elements merged, len: n }, strings "str" / "date" / "url" / "html", numbers "num", booleans
// "bool", null "null". The strings themselves are never part of the result.
export function shapeOf(value, flags = {}) {
  return walkShape(value, 0, flags);
}

// ---------- enums ----------

function enumValue(v) {
  if (typeof v === "string" && (ENUM_VALUE.test(v) || D2L_TYPE_VALUE.test(v))) return v;
  if (typeof v === "number" && Number.isInteger(v) && Math.abs(v) <= 10000) return v;
  return undefined;
}

// Distinct values of the whitelisted enum fields, anywhere in a response (all items, not just the
// sample the shape looks at).
export function collectEnums(value, enums = {}, budget = { nodes: 50000 }, depth = 0) {
  if (depth > 12 || --budget.nodes < 0 || value === null || typeof value !== "object") return enums;
  if (Array.isArray(value)) {
    for (const v of value) collectEnums(v, enums, budget, depth + 1);
    return enums;
  }
  for (const [k, v] of Object.entries(value)) {
    if (ENUM_FIELDS.has(k)) {
      const e = enumValue(v);
      if (e !== undefined) {
        const seen = (enums[k] ??= []);
        if (!seen.includes(e) && seen.length < MAX_ENUM_VALUES) seen.push(e);
      }
    }
    collectEnums(v, enums, budget, depth + 1);
  }
  return enums;
}

function sortedEnums(enums) {
  const byValue = (x, y) => (typeof x === typeof y ? (x < y ? -1 : x > y ? 1 : 0) : typeof x === "number" ? -1 : 1);
  return Object.fromEntries(Object.keys(enums).sort().map((k) => [k, [...enums[k]].sort(byValue)]));
}

// ---------- the check ----------

const VERSION_RE = /^\d{1,2}\.\d{1,3}$/;
const BUILD_RE = /^\d{1,3}(?:\.\d{1,5}){1,3}$/;

function cmpVersion(a, b) {
  const [a1, a2] = a.split(".").map(Number), [b1, b2] = b.split(".").map(Number);
  return a1 - b1 || a2 - b2;
}

// The latest version the school's Brightspace supports for one product (lp, le).
export function pickVersion(list, code) {
  const entry = Array.isArray(list) ? list.find((x) => x && x.ProductCode === code) : null;
  if (!entry) return null;
  const candidates = [entry.LatestVersion, ...(Array.isArray(entry.SupportedVersions) ? entry.SupportedVersions : [])]
    .filter((v) => typeof v === "string" && VERSION_RE.test(v));
  return candidates.sort(cmpVersion).at(-1) || null;
}

function productBuild(list) {
  for (const entry of [list, ...(Array.isArray(list) ? list : [])]) {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) continue;
    for (const k of ["ProductVersion", "ProductBuild", "Build", "BuildNumber"]) {
      if (typeof entry[k] === "string" && BUILD_RE.test(entry[k])) return entry[k];
    }
  }
  return null;
}

function countOf(data) {
  if (Array.isArray(data)) return data.length;
  if (data && typeof data === "object") {
    for (const k of ["Items", "Objects", "Modules"]) if (Array.isArray(data[k])) return data[k].length;
  }
  return null;
}

function header(res, name) {
  const h = res?.headers;
  if (!h) return null;
  if (typeof h.get === "function") return h.get(name);
  const key = Object.keys(h).find((k) => k.toLowerCase() === name);
  return key ? h[key] : null;
}

function retryAfterSeconds(value, now) {
  if (value == null || value === "") return null;
  const n = Number(value);
  if (Number.isFinite(n)) return Math.min(86400, Math.max(0, Math.round(n)));
  const at = Date.parse(value);
  return Number.isFinite(at) ? Math.min(86400, Math.max(0, Math.round((at - now) / 1000))) : null;
}

const validId = (id) => (typeof id === "number" || typeof id === "string") && /^\d{1,12}$/.test(String(id));

// Most likely to be this term's real classes first: active and accessible, then recently opened.
function pickCourses(items, max) {
  const score = (e) => (e?.Access?.IsActive && e?.Access?.CanAccess ? 2 : e?.Access?.CanAccess ? 1 : 0);
  const last = (e) => (typeof e?.Access?.LastAccessed === "string" ? e.Access.LastAccessed : "");
  const usable = items.filter((e) => validId(e?.OrgUnit?.Id) && score(e) > 0);
  usable.sort((a, b) => score(b) - score(a) || (last(b) > last(a) ? 1 : last(b) < last(a) ? -1 : 0));
  const picked = usable.slice(0, max).map((e) => String(e.OrgUnit.Id));
  // The cross-course routes want "some or all of the user's active enrollments".
  const active = usable.filter((e) => score(e) === 2).map((e) => String(e.OrgUnit.Id));
  return { picked, active: active.length ? active : picked };
}

function timeoutError() {
  return Object.assign(new Error("timed out"), { name: "TimeoutError" });
}

async function withTimeout(get, url, ms) {
  const ctrl = typeof AbortController === "function" ? new AbortController() : null;
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => { ctrl?.abort(); reject(timeoutError()); }, ms);
  });
  try {
    return await Promise.race([get(url, { signal: ctrl?.signal, timeoutMs: ms }), timeout]);
  } finally {
    clearTimeout(timer);
  }
}

const clockNow = () => (typeof performance !== "undefined" ? performance.now() : Date.now());

export async function diagnoseBrightspace({
  baseUrl, get, now = Date.now(), maxCourses = 3, extensionVersion = null, transport = null,
  onProgress = () => {}, timeoutMs = 10000, maxRequests = 40, concurrency = 2, clock = clockNow,
}) {
  const origin = new URL(baseUrl).origin;
  const flags = {};
  const enums = {};
  const notes = new Set();
  const entries = [];  // [sort order, entry]
  let requests = 0;
  let planned = 3;
  let stopped = false;
  let signedIn = false;
  const versions = { lp: null, le: null };

  const progress = () => onProgress({ done: requests, total: Math.max(requests, Math.min(maxRequests, planned)) });

  // One GET, recorded as one endpoint entry. Returns the parsed JSON (kept only in memory, for
  // picking course and folder ids) with the entry, or undefined when the check has stopped.
  async function call(sortOrder, name, url, path, extra = {}) {
    if (stopped) return undefined;
    if (requests >= maxRequests) { notes.add("request_cap"); return undefined; }
    requests++;
    const t0 = clock();
    let res = null, error = null;
    try {
      res = await withTimeout(get, url, timeoutMs);
      if (res?.error) error = /abort|timeout/i.test(String(res.error)) ? "timeout" : "network";
    } catch (e) {
      error = /abort|timeout/i.test(`${e?.name} ${e?.message}`) ? "timeout" : "network";
    }
    const status = Number.isInteger(res?.status) ? res.status : 0;
    const entry = { name, path, status, ms: Math.max(0, Math.round(clock() - t0)), count: null, shape: null, ...extra };
    if (error) entry.error = error;
    let data;
    if (status === 429) {
      stopped = true;
      notes.add("rate_limited");
      entry.retry_after = retryAfterSeconds(header(res, "retry-after"), now);
    } else if (status >= 200 && status < 300) {
      try {
        data = JSON.parse(res.text);
      } catch {
        entry.error = "not_json";
      }
      if (name === "whoami") {
        // Only "is this a JSON object" matters (a sign-in page can answer 200 too); nothing in it is kept.
        data = data !== null && typeof data === "object" && !Array.isArray(data) ? true : undefined;
      } else if (data !== undefined) {
        entry.shape = shapeOf(data, flags);
        entry.count = countOf(data);
        // Per route too: "CompletionType" means different things in dropbox and content.
        const own = collectEnums(data);
        if (Object.keys(own).length) entry.enums = sortedEnums(own);
        collectEnums(data, enums);
      }
    }
    entries.push([sortOrder, entry]);
    progress();
    return { status, data, entry };
  }

  // Request URLs get the real ids and dates; recorded paths keep {ou}/{id}/{date} placeholders.
  const pathFor = (name) => ROUTES[name].replace("{lp}", versions.lp).replace("{le}", versions.le);
  const urlFor = (name, { ou, id, dates = [] } = {}) => {
    let i = 0;
    const p = pathFor(name).replace(/\{ou\}/g, () => ou).replace(/\{id\}/g, () => id)
      .replace(/\{date\}/g, () => encodeURIComponent(dates[i++]));
    return new URL(p, origin).toString();
  };
  const routeOrder = Object.keys(ROUTES);
  const order = (name, course = 0) => course * 100 + routeOrder.indexOf(name);

  progress();
  const report = () => ({
    lms: "brightspace",
    schema: DIAG_SCHEMA,
    host: new URL(origin).hostname,
    extension_version: extensionVersion,
    ran_at: new Date(now).toISOString(),
    transport,
    versions: { lp: versions.lp, le: versions.le, ...(versions.product_build ? { product_build: versions.product_build } : {}) },
    signed_in: signedIn,
    endpoints: entries.sort((a, b) => a[0] - b[0]).map(([, e]) => e),
    enums: sortedEnums(enums),
    notes: [...notes, ...(flags.keysCapped ? ["keys_capped"] : [])].filter((n, i, all) => n in NOTES && all.indexOf(n) === i),
  });

  // 1. Versions (public, no sign-in needed).
  const v = await call(order("versions"), "versions", urlFor("versions"), ROUTES.versions);
  versions.lp = pickVersion(v?.data, "lp");
  versions.le = pickVersion(v?.data, "le");
  const build = productBuild(v?.data);
  if (build) versions.product_build = build;
  if (!versions.lp || !versions.le) {
    if (!stopped) notes.add("versions_unavailable");
    return report();
  }

  // 2. Signed in? Only the status is kept, never the answer (the student's name and ids).
  const who = await call(order("whoami"), "whoami", urlFor("whoami"), pathFor("whoami"));
  if (who?.status !== 200 || who.data !== true) {
    if (who && (who.status === 401 || who.status === 403)) notes.add("signed_out");
    else if (who && !stopped) notes.add("whoami_unexpected");
    return report();
  }
  signedIn = true;

  // 3. Course enrollments, following bookmarks for up to 5 pages, recorded as one entry.
  const items = [];
  let enrollEntry = null;
  let bookmark = null;
  for (let page = 1; page <= MAX_PAGES; page++) {
    const url = new URL(urlFor("enrollments"));
    if (bookmark) url.searchParams.set("bookmark", bookmark);
    const r = await call(order("enrollments") + page / 10, "enrollments", url.toString(), pathFor("enrollments"));
    if (!r) break;
    if (page === 1) {
      enrollEntry = r.entry;
    } else if (r.status === 200 && r.data !== undefined) {
      // A later page folds into the first page's entry (a failed one stays as its own entry).
      entries.splice(entries.findIndex(([, e]) => e === r.entry), 1);
      enrollEntry.ms += r.entry.ms;
      enrollEntry.pages = page;
      if (r.entry.shape) enrollEntry.shape = unionShapes(enrollEntry.shape, r.entry.shape);
    } else {
      if (r.status !== 429) notes.add("paging_error");
      break;
    }
    if (r.status !== 200 || !r.data) break;
    const pageItems = Array.isArray(r.data) ? r.data : Array.isArray(r.data.Items) ? r.data.Items : [];
    items.push(...pageItems);
    enrollEntry.count = items.length;
    const paging = r.data.PagingInfo;
    const next = paging?.HasMoreItems && typeof paging.Bookmark === "string" && paging.Bookmark ? paging.Bookmark : null;
    if (!next || next === bookmark) break;
    if (page === MAX_PAGES) { notes.add("paging_capped"); break; }
    bookmark = next;
  }

  const { picked, active } = pickCourses(items, maxCourses);
  if (!picked.length) {
    if (!stopped) notes.add("no_courses");
    return report();
  }
  planned = requests + 2 + picked.length * 9;

  // 4. Cross-course routes, then each picked course's tools, two requests at a time.
  const csv = active.slice(0, MAX_CSV_COURSES).join(",");
  // D2L's UTCDateTime is exactly toISOString()'s yyyy-MM-ddTHH:mm:ss.fffZ.
  const span = [new Date(now - 14 * DAY).toISOString(), new Date(now + 120 * DAY).toISOString()];
  const tasks = [
    () => call(order("calendar_events"), "calendar_events", urlFor("calendar_events", { ou: csv, dates: span }),
      pathFor("calendar_events")),
    () => call(order("my_items_due"), "my_items_due", urlFor("my_items_due", { ou: csv }), pathFor("my_items_due")),
  ];
  picked.forEach((ou, i) => {
    const course = i + 1;
    const perCourse = (name) => () => call(order(name, course), name, urlFor(name, { ou }), pathFor(name), { course });
    tasks.push(async () => {
      const folders = await call(order("dropbox_folders", course), "dropbox_folders", urlFor("dropbox_folders", { ou }),
        pathFor("dropbox_folders"), { course });
      const first = Array.isArray(folders?.data) ? folders.data.find((f) => validId(f?.Id)) : null;
      if (first) {
        await call(order("dropbox_mysubmissions", course), "dropbox_mysubmissions",
          urlFor("dropbox_mysubmissions", { ou, id: String(first.Id) }), pathFor("dropbox_mysubmissions"), { course });
      } else {
        planned--;
      }
    });
    for (const name of ["quizzes", "grade_values", "grade_objects", "grade_categories", "grade_setup", "news", "content_toc"]) {
      tasks.push(perCourse(name));
    }
  });
  await pool(tasks, concurrency, (task) => task());
  return report();
}

// ---------- summary ----------

const answered = (e) => e.status >= 200 && e.status < 300;

// "12 of 14 checks answered; assignments: yes; ..." for the popup.
export function summarize(report) {
  const endpoints = report?.endpoints || [];
  const features = {};
  for (const [feature, names] of Object.entries(FEATURES)) {
    const tried = endpoints.filter((e) => names.includes(e.name));
    const ok = tried.filter(answered).length;
    features[feature] = !tried.length ? "not checked" : ok === tried.length ? "yes" : ok ? "partly" : "no";
  }
  return { answered: endpoints.filter(answered).length, total: endpoints.length, features };
}

export function summaryText(report) {
  const { answered: n, total, features } = summarize(report);
  return [`${n} of ${total} checks answered`, ...Object.entries(features).map(([f, v]) => `${f}: ${v}`)].join("; ");
}
