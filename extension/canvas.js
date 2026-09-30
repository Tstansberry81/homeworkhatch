// Canvas sync core. Pure ES module: no chrome.* APIs, so it runs in the extension
// service worker and in Node tests alike.
//
// It talks to the same /api/v1 endpoints the Canvas web UI uses, authenticated by the
// student's browser session. Callers inject `get(url) -> {status, link, text}` so the
// transport (service-worker fetch, in-tab fetch, Node fetch with a cookie) is swappable.

export const SCHEMA_VERSION = 1;

export class NotLoggedInError extends Error {
  constructor() {
    super("Not logged in to Canvas");
    this.name = "NotLoggedInError";
  }
}

const DAY = 24 * 60 * 60 * 1000;

// Canvas prefixes session-authenticated JSON with `while(1);` as XSSI protection.
export function parseCanvasJson(text) {
  return JSON.parse(text.replace(/^\s*while\(1\);/, ""));
}

export function nextLink(linkHeader) {
  if (!linkHeader) return null;
  for (const part of linkHeader.split(",")) {
    const m = part.match(/<([^>]+)>\s*;\s*rel="next"/);
    if (m) return m[1];
  }
  return null;
}

function buildUrl(baseUrl, path, params = {}) {
  const url = new URL(`/api/v1${path}`, baseUrl);
  for (const [k, v] of Object.entries(params)) {
    for (const item of Array.isArray(v) ? v : [v]) url.searchParams.append(k, item);
  }
  return url.toString();
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Runs async tasks with bounded concurrency so we stay polite to Canvas's rate limiter.
export async function pool(items, limit, fn) {
  const results = new Array(items.length);
  let i = 0;
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (i < items.length) {
      const idx = i++;
      results[idx] = await fn(items[idx], idx);
    }
  });
  await Promise.all(workers);
  return results;
}

export function createClient({ baseUrl, get, maxRetries = 4 }) {
  const restricted = [];

  async function request(url, label) {
    for (let attempt = 0; ; attempt++) {
      const res = await get(url);
      if (res.status === 401) {
        // Canvas answers 401 both when logged out ("unauthenticated") and when logged in but
        // not allowed to see one resource ("unauthorized"). Only the first ends the sync.
        let body = {};
        try { body = parseCanvasJson(res.text || "{}"); } catch { /* not JSON */ }
        if (body.status !== "unauthorized") throw new NotLoggedInError();
        restricted.push({ endpoint: label, status: 401 });
        return { data: null, link: null };
      }
      const throttled =
        res.status === 429 || (res.status === 403 && /rate limit exceeded/i.test(res.text || ""));
      if (throttled && attempt < maxRetries) {
        await sleep(500 * 2 ** attempt);
        continue;
      }
      // 403/404 on a course sub-resource usually means the instructor hid that tab
      // (e.g. Files). Record it and move on rather than failing the whole sync.
      if (res.status === 403 || res.status === 404) {
        restricted.push({ endpoint: label, status: res.status });
        return { data: null, link: null };
      }
      if (res.status < 200 || res.status >= 300) {
        throw new Error(`Canvas ${res.status} on ${label}`);
      }
      return { data: parseCanvasJson(res.text), link: res.link };
    }
  }

  async function one(path, params) {
    return (await request(buildUrl(baseUrl, path, params), path)).data;
  }

  // Follows Link rel="next" until exhausted. Returns null if the endpoint is restricted.
  async function all(path, params = {}) {
    let url = buildUrl(baseUrl, path, { per_page: 100, ...params });
    const out = [];
    while (url) {
      const { data, link } = await request(url, path);
      if (data === null) return out.length ? out : null;
      out.push(...data);
      url = nextLink(link);
    }
    return out;
  }

  return { one, all, restricted };
}

// ---------- normalization ----------

// Items Canvas never collects a submission for: in-class exams, participation, paper work.
const NO_SUBMIT_TYPES = ["none", "not_graded", "on_paper"];

// `missing` is only ever Canvas's own flag. A past-due item Canvas didn't flag (common
// with external tools and in-class activities) is `past_due`, so we never tell a
// student they missed something Canvas itself doesn't consider missing.
export function assignmentStatus(a, sub, now = Date.now()) {
  const types = a.submission_types || [];
  const pastDue = a.due_at && Date.parse(a.due_at) < now;
  if (sub?.excused) return "excused";
  if (sub?.workflow_state === "graded" && sub.score != null) return "graded";
  if (sub?.missing) return "missing";
  // pending_review = submitted through an external tool, awaiting a grade; LTI tools
  // often leave submitted_at null.
  if (sub?.submitted_at || ["submitted", "pending_review"].includes(sub?.workflow_state)) {
    return sub.late ? "submitted_late" : "submitted";
  }
  if (!types.length || types.some((t) => NO_SUBMIT_TYPES.includes(t))) return "no_submission";
  return pastDue ? "past_due" : "upcoming";
}

// Canvas has no link between a class's separate section shells (lecture, discussion,
// lab). The one school-independent signal is that they share a name within a term, so
// that is all we merge on. A missed merge shows a duplicate row; a wrong merge would
// mix up two classes, so this stays conservative.
export function classKey(course) {
  const name = String(course.name || "").toLowerCase().replace(/\s+/g, " ").trim();
  return `${course.term?.id ?? "none"}::${name}`;
}

// Which courses to show. The student's own Canvas dashboard choice (favorites) is the
// default; a per-course override from the extension settings wins.
export function isVisible(course, overrides = {}) {
  return overrides[course.id] ?? course.on_dashboard !== false;
}

function normFile(f, source = "files_tab") {
  return {
    id: String(f.id), name: f.display_name, content_type: f["content-type"], size: f.size,
    updated_at: f.updated_at, folder_id: f.folder_id != null ? String(f.folder_id) : null,
    // Signed link; valid with the student's session, may expire — refetch via id if so.
    download_url: f.url, locked: Boolean(f.locked_for_user), sources: [source],
  };
}

// Canvas rich content links files as /courses/:cid/files/:id, /files/:id or
// data-api-endpoint=".../api/v1/courses/:cid/files/:id".
export function fileIdsInHtml(html) {
  const ids = new Set();
  for (const m of (html || "").matchAll(/\/files\/(\d+)/g)) ids.add(m[1]);
  return ids;
}

// Merge Files-tab listings with files reachable from modules and from links embedded in
// pages, assignments, the syllabus, announcements and discussions. Files the tab hides
// are usually still readable individually when the instructor links them.
async function resolveFiles(courses, client, safe) {
  for (const c of courses) {
    const known = new Map((c.files || []).map((f) => [f.id, f]));
    const wanted = new Map(); // id -> source
    for (const id of c.__moduleFileIds) wanted.set(id, "module");
    const html = [
      c.syllabus_html,
      ...(c.assignments || []).map((a) => a.description_html),
      ...c.pages.map((p) => p.body_html),
      ...c.announcements.map((a) => a.message_html),
      ...(c.discussions || []).map((d) => d.message_html),
    ];
    for (const h of html) for (const id of fileIdsInHtml(h)) if (!wanted.has(id)) wanted.set(id, "embedded");
    delete c.__moduleFileIds;

    const missing = [...wanted.keys()].filter((id) => !known.has(id));
    const fetched = await pool(missing, 3, (fid) => safe(`file:${fid}`, () => client.one(`/files/${fid}`)));
    for (const [fid, source] of wanted) {
      const f = known.get(fid);
      if (f) { if (!f.sources.includes(source)) f.sources.push(source); continue; }
      const raw = fetched[missing.indexOf(fid)];
      if (raw) known.set(fid, normFile(raw, source));
    }
    c.files = [...known.values()];
  }
}

function normAssignment(a, now, detail) {
  const sub = a.submission || null;
  return {
    id: String(a.id),
    name: a.name,
    group_id: a.assignment_group_id != null ? String(a.assignment_group_id) : null,
    due_at: a.due_at,
    unlock_at: a.unlock_at,
    lock_at: a.lock_at,
    points_possible: a.points_possible,
    grading_type: a.grading_type,
    submission_types: a.submission_types || [],
    is_quiz: Boolean(a.is_quiz_assignment || a.quiz_id),
    omit_from_final_grade: Boolean(a.omit_from_final_grade),
    html_url: a.html_url,
    description_html: a.description || null,
    status: assignmentStatus(a, sub, now),
    submission: sub && {
      submitted_at: sub.submitted_at,
      score: sub.score,
      grade: sub.grade,
      late: Boolean(sub.late),
      missing: Boolean(sub.missing),
      excused: Boolean(sub.excused),
      workflow_state: sub.workflow_state,
      // A null posted_at on a graded submission means the teacher hasn't released it.
      grade_posted: sub.posted_at != null || sub.score != null,
      attempt: sub.attempt,
      attachments: (detail?.attachments || sub.attachments || []).map((f) => ({
        id: String(f.id), name: f.display_name, content_type: f["content-type"], size: f.size,
      })),
      comments: (detail?.submission_comments || []).map((cm) => ({
        author: cm.author_name, created_at: cm.created_at, comment: cm.comment,
      })),
      rubric_assessment: detail?.rubric_assessment ?? null,
    },
    rubric: (a.rubric || []).map((r) => ({
      id: r.id, description: r.description, long_description: r.long_description || null, points: r.points,
      ratings: (r.ratings || []).map((x) => ({ description: x.description, points: x.points })),
    })),
  };
}

function normCourse(c) {
  const enr = (c.enrollments || []).find((e) => e.type === "student" || e.type === "StudentEnrollment") || c.enrollments?.[0] || {};
  return {
    id: String(c.id),
    name: c.name,
    course_code: c.course_code,
    class_key: classKey(c),
    on_dashboard: c.is_favorite ?? null,
    term: c.term ? { id: String(c.term.id), name: c.term.name, start_at: c.term.start_at, end_at: c.term.end_at } : null,
    grade: {
      current_score: enr.computed_current_score ?? null,
      current_grade: enr.computed_current_grade ?? null,
      final_score: enr.computed_final_score ?? null,
      final_grade: enr.computed_final_grade ?? null,
    },
    group_weighting: typeof c.apply_assignment_group_weights === "boolean" ? c.apply_assignment_group_weights : null,
    html_url: new URL(`/courses/${c.id}`, c.__baseUrl).toString(),
    syllabus_html: c.syllabus_body || null,
  };
}

// ---------- sync ----------

export async function syncCanvas({ baseUrl, get, now = Date.now(), concurrency = 4, onProgress = () => {} }) {
  const client = createClient({ baseUrl, get });
  const errors = [];
  // A list that couldn't be fetched (error or hidden tab) is sent as null, never [], so the
  // server keeps what it already has instead of deleting it.
  const list = (items, fn) => (Array.isArray(items) ? items.map(fn) : null);
  const safe = async (label, fn) => {
    try {
      return await fn();
    } catch (e) {
      if (e instanceof NotLoggedInError) throw e;
      errors.push({ endpoint: label, message: String(e.message || e) });
      return null;
    }
  };

  onProgress({ step: "user" });
  const self = await client.one("/users/self");
  if (!self) throw new NotLoggedInError();

  onProgress({ step: "courses" });
  const rawCourses = await client.all("/courses", {
    enrollment_state: "active",
    "include[]": ["term", "total_scores", "syllabus_body", "favorites"],
  });
  // Courses outside their date window come back as {id, access_restricted_by_date: true}.
  const visible = (rawCourses || []).filter((c) => !c.access_restricted_by_date && c.name);
  // A student who never starred courses has no favorites; treat everything as on the dashboard.
  if (visible.length && visible.every((c) => c.is_favorite === false)) for (const c of visible) c.is_favorite = true;

  const courses = await pool(visible, concurrency, async (c, idx) => {
    onProgress({ step: "course", index: idx + 1, total: visible.length, name: c.name });
    const id = c.id;
    const [assignments, groups, modules, pages, files, discussions, quizzes, submissions] = await Promise.all([
      safe(`assignments:${id}`, () =>
        client.all(`/courses/${id}/assignments`, { "include[]": ["submission"], order_by: "due_at" })),
      safe(`assignment_groups:${id}`, () => client.all(`/courses/${id}/assignment_groups`)),
      safe(`modules:${id}`, () => client.all(`/courses/${id}/modules`, { "include[]": ["items"] })),
      safe(`pages:${id}`, () => client.all(`/courses/${id}/pages`, { sort: "updated_at", order: "desc" })),
      safe(`files:${id}`, () => client.all(`/courses/${id}/files`, { sort: "updated_at", order: "desc" })),
      safe(`discussions:${id}`, () => client.all(`/courses/${id}/discussion_topics`)),
      safe(`quizzes:${id}`, () => client.all(`/courses/${id}/quizzes`)),
      // Teacher comments, rubric scores and your own uploaded attachments.
      // With no student_ids, Canvas returns the calling student's own submissions.
      safe(`submissions:${id}`, () => client.all(`/courses/${id}/students/submissions`, {
        "include[]": ["submission_comments", "rubric_assessment"],
      })),
    ]);

    // A hidden Pages tab only blocks the *list*; pages linked from modules still open.
    const pageUrls = new Map();
    for (const p of pages || []) pageUrls.set(p.url, p);
    for (const m of modules || []) for (const it of m.items || []) {
      if (it.type === "Page" && it.page_url && !pageUrls.has(it.page_url)) pageUrls.set(it.page_url, { url: it.page_url, title: it.title });
    }
    const pageBodies = await pool([...pageUrls.keys()], 2, (u) =>
      safe(`page:${id}/${u}`, () => client.one(`/courses/${id}/pages/${encodeURIComponent(u)}`)));

    const subsByAssignment = new Map((submissions || []).map((s) => [String(s.assignment_id), s]));
    return {
      ...normCourse({ ...c, __baseUrl: baseUrl }),
      assignment_groups: list(groups, (g) => ({
        id: String(g.id), name: g.name, weight: g.group_weight, position: g.position,
        drop_lowest: g.rules?.drop_lowest ?? 0, drop_highest: g.rules?.drop_highest ?? 0,
        never_drop: (g.rules?.never_drop || []).map(String),
      })),
      assignments: list(assignments, (a) => normAssignment(a, now, subsByAssignment.get(String(a.id)))),
      modules: list(modules, (m) => ({
        id: String(m.id), name: m.name, position: m.position, unlock_at: m.unlock_at, state: m.state ?? null,
        items: (m.items || []).map((it) => ({
          id: String(it.id), title: it.title, type: it.type,
          content_id: it.content_id != null ? String(it.content_id) : null,
          page_url: it.page_url ?? null,
          html_url: it.html_url, external_url: it.external_url ?? null,
          completed: it.completion_requirement?.completed ?? null,
        })),
      })),
      pages: [...pageUrls.values()].map((p, i) => {
        const full = pageBodies[i];
        return {
          url: p.url, title: full?.title ?? p.title, updated_at: full?.updated_at ?? p.updated_at ?? null,
          html_url: full?.html_url ?? p.html_url ?? null, body_html: full?.body ?? null,
        };
      }),
      // Filled in by resolveFiles() once every HTML source (incl. announcements) is known.
      files: files === null ? null : files.map((f) => normFile(f)),
      files_tab_hidden: files === null,
      discussions: list(discussions, (d) => ({
        id: String(d.id), title: d.title, posted_at: d.posted_at, due_at: d.assignment?.due_at ?? null,
        html_url: d.html_url, message_html: d.message,
      })),
      quizzes: list(quizzes, (q) => ({
        id: String(q.id), title: q.title, due_at: q.due_at, time_limit: q.time_limit,
        question_count: q.question_count, points_possible: q.points_possible, html_url: q.html_url,
      })),
      // The Pages list was unavailable, so `pages` only holds module-linked pages: not a full list.
      pages_partial: !Array.isArray(pages),
      announcements: [],
      __moduleFileIds: (modules || []).flatMap((m) => (m.items || []).filter((it) => it.type === "File").map((it) => String(it.content_id))),
    };
  });

  const contextCodes = courses.map((c) => `course_${c.id}`);
  const chunks = [];
  for (let i = 0; i < contextCodes.length; i += 10) chunks.push(contextCodes.slice(i, i + 10));
  const iso = (t) => new Date(t).toISOString();

  onProgress({ step: "announcements" });
  const announcements = (await Promise.all(chunks.map((codes) =>
    safe("announcements", () => client.all("/announcements", {
      "context_codes[]": codes, start_date: iso(now - 180 * DAY), end_date: iso(now + DAY),
    }))))).flat().filter(Boolean);
  const byId = new Map(courses.map((c) => [c.id, c]));
  for (const a of announcements) {
    const course = byId.get(String(a.context_code || "").replace("course_", ""));
    course?.announcements.push({
      id: String(a.id), title: a.title, posted_at: a.posted_at, author: a.author?.display_name ?? null,
      message_html: a.message, html_url: a.html_url,
    });
  }

  onProgress({ step: "planner" });
  const planner = await safe("planner", () => client.all("/planner/items", {
    start_date: iso(now - 14 * DAY), end_date: iso(now + 120 * DAY),
  }));

  const events = (await Promise.all(chunks.map((codes) =>
    safe("calendar_events", () => client.all("/calendar_events", {
      type: "event", "context_codes[]": codes, start_date: iso(now - 14 * DAY), end_date: iso(now + 120 * DAY),
    }))))).flat().filter(Boolean);

  const missing = await safe("missing_submissions", () => client.all("/users/self/missing_submissions"));

  onProgress({ step: "files" });
  await resolveFiles(courses, client, safe);

  return {
    schema_version: SCHEMA_VERSION,
    synced_at: new Date(now).toISOString(),
    base_url: baseUrl,
    user: { id: String(self.id), name: self.name, short_name: self.short_name ?? null },
    courses,
    planner: (planner || []).map((p) => ({
      course_id: p.course_id != null ? String(p.course_id) : null,
      type: p.plannable_type, title: p.plannable?.title ?? null, due_at: p.plannable_date,
      html_url: p.html_url ? new URL(p.html_url, baseUrl).toString() : null,
      submitted: p.submissions?.submitted ?? null, graded: p.submissions?.graded ?? null,
      missing: p.submissions?.missing ?? null, late: p.submissions?.late ?? null,
      marked_complete: p.planner_override?.marked_complete ?? false,
    })),
    calendar_events: events.map((e) => ({
      id: String(e.id), title: e.title, start_at: e.start_at, end_at: e.end_at,
      course_id: String(e.context_code || "").replace("course_", "") || null,
      location: e.location_name ?? null, html_url: e.html_url,
    })),
    missing: (missing || []).map((a) => ({
      id: String(a.id), course_id: String(a.course_id), name: a.name, due_at: a.due_at, html_url: a.html_url,
    })),
    restricted: client.restricted,
    errors,
  };
}

// Work that still needs doing, due between a week ago and `days` out. In-class items
// (exams, checkpoints) are included when they carry points, but only while upcoming.
export function upcoming(snapshot, days = 14, now = Date.now(), overrides = {}) {
  return snapshot.courses
    .filter((c) => isVisible(c, overrides))
    .flatMap((c) => (c.assignments || []).map((a) => ({ ...a, course: c.name })))
    .filter((a) => {
      if (!a.due_at) return false;
      const due = Date.parse(a.due_at);
      if (due > now + days * DAY || due < now - 7 * DAY) return false;
      if (a.status === "no_submission") return due >= now && a.points_possible > 0;
      return ["upcoming", "past_due", "missing"].includes(a.status);
    })
    .sort((a, b) => Date.parse(a.due_at) - Date.parse(b.due_at));
}

const safeName = (s) => String(s).replace(/[\\/:*?"<>|\u0000-\u001f]+/g, "_").replace(/\s+/g, " ").trim().slice(0, 120) || "untitled";

// Every file the student can reach (Files tab, modules, embedded links) laid out as
// "<course>/<file>" zip paths. Merged sections share a folder, each file appears once,
// and clashing names get " (2)", " (3)"… so nothing is silently overwritten.
// A file's identity before downloading it: name, type and size (the server uses the same
// rule). Name and type alone aren't enough: courses reuse names like "solution.py".
export function fileKey(f) {
  return `${String(f.name || "").trim().toLowerCase()}|${String(f.content_type || "").toLowerCase()}|${f.size ?? "?"}`;
}

// `skipKeys` (a Set of fileKey values) leaves out files already downloaded, and copies of
// the same file posted under different IDs are zipped once.
export function zipPlan(snapshot, overrides = {}, { skipKeys = null, dedupe = false } = {}) {
  const jobs = [];
  const seenFiles = new Set();
  const seenContent = new Set();
  const usedPaths = new Set();
  const folders = new Map();
  for (const c of snapshot.courses.filter((c) => isVisible(c, overrides))) {
    if (!folders.has(c.class_key)) folders.set(c.class_key, safeName(c.name));
    const folder = folders.get(c.class_key);
    for (const f of c.files || []) {
      if (f.locked || !f.download_url || seenFiles.has(f.id)) continue;
      seenFiles.add(f.id);
      if (dedupe || skipKeys) {
        const key = fileKey(f);
        if (skipKeys?.has(key) || (dedupe && seenContent.has(key))) continue;
        seenContent.add(key);
      }
      const name = safeName(f.name);
      const dot = name.lastIndexOf(".");
      const [stem, ext] = dot > 0 ? [name.slice(0, dot), name.slice(dot)] : [name, ""];
      let path = `${folder}/${name}`;
      for (let n = 2; usedPaths.has(path.toLowerCase()); n++) path = `${folder}/${stem} (${n})${ext}`;
      usedPaths.add(path.toLowerCase());
      jobs.push({ file: f, path, course_id: c.id });
    }
  }
  return jobs;
}
