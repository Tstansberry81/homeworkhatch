// A small fake Canvas that reproduces the behaviours that break naive clients:
// session-cookie auth, `while(1);` prefixed JSON, Link-header pagination (forced to
// 2 per page), instructor-hidden tabs (403/404), date-restricted courses, and a
// one-off rate-limit response.
import http from "node:http";

const NOW = Date.parse("2026-09-29T12:00:00Z");
const d = (days) => new Date(NOW + days * 864e5).toISOString();

const courses = [
  { id: 101, name: "Calculus II", course_code: "MATH 1320", is_favorite: true, term: { id: 9, name: "Fall 2026", start_at: d(-40), end_at: d(70) },
    syllabus_body: "<p>Exams 60%</p>",
    enrollments: [{ type: "student", computed_current_score: 91.4, computed_current_grade: "A-", computed_final_score: 70.2, computed_final_grade: "C-" }] },
  { id: 102, name: "Intro to CS", course_code: "CS 1110", term: { id: 9, name: "Fall 2026" }, is_favorite: true,
    enrollments: [{ type: "student", computed_current_score: null, computed_current_grade: null }] },
  { id: 103, access_restricted_by_date: true },
  // Not on the student's dashboard (e.g. a placement test or old org site).
  { id: 104, name: "Placement Test", course_code: "PLACEMENT", term: { id: 1, name: "Default Term" }, enrollments: [], is_favorite: false },
  // A second section shell of the same class: same name, same term.
  { id: 105, name: "Intro to CS", course_code: "CS 1110-102", term: { id: 9, name: "Fall 2026" }, enrollments: [], is_favorite: true },
];

const assignments = {
  101: [
    { id: 1, name: "PS1", due_at: d(-10), points_possible: 10, submission_types: ["online_upload"], assignment_group_id: 11,
      submission: { submitted_at: d(-11), score: 9, grade: "9", workflow_state: "graded", posted_at: d(-5), late: false, missing: false } },
    { id: 2, name: "PS2", due_at: d(-3), points_possible: 10, submission_types: ["online_upload"], assignment_group_id: 11,
      submission: { submitted_at: null, score: null, workflow_state: "unsubmitted", missing: true } },
    { id: 3, name: "PS3", due_at: d(2), points_possible: 10, submission_types: ["online_upload"], assignment_group_id: 11,
      submission: { submitted_at: null, score: null, workflow_state: "unsubmitted", missing: false } },
    { id: 4, name: "Midterm 1", due_at: d(9), points_possible: 100, submission_types: ["on_paper"], assignment_group_id: 12,
      submission: { submitted_at: null, score: null, workflow_state: "unsubmitted" } },
    { id: 5, name: "PS0", due_at: d(-20), points_possible: 10, submission_types: ["online_upload"], assignment_group_id: 11,
      submission: { submitted_at: d(-19), score: null, workflow_state: "submitted", late: true } },
    // Real UVA patterns: LTI submission with null submitted_at, in-class checkpoint
    // past due, external-tool activity past due that Canvas did not flag missing.
    { id: 6, name: "PA03", due_at: d(1), points_possible: 20, submission_types: ["external_tool"], assignment_group_id: 11,
      submission: { submitted_at: null, score: null, workflow_state: "pending_review", missing: false } },
    { id: 7, name: "Knowledge Checkpoint 1", due_at: d(-5), points_possible: 0, submission_types: ["none"], assignment_group_id: 12,
      submission: { submitted_at: null, score: null, workflow_state: "unsubmitted", missing: false } },
    { id: 8, name: "Gallery Walk", due_at: d(-4), points_possible: 5, submission_types: ["external_tool"], assignment_group_id: 11,
      submission: { submitted_at: null, score: null, workflow_state: "unsubmitted", missing: false } },
  ],
  102: [
    { id: 21, name: "Lab 1", due_at: d(1), points_possible: 5, submission_types: ["online_text_entry"], assignment_group_id: 31, is_quiz_assignment: false,
      description: '<p>Starter code: <a href="/courses/102/files/78/download">lab1.py</a></p>',
      rubric: [{ id: "r1", description: "Correctness", points: 4, ratings: [{ description: "Full", points: 4 }, { description: "None", points: 0 }] }] },
  ],
};

const routes = {
  "/api/v1/users/self": () => ({ id: 7, name: "Traveler Test", short_name: "Trav" }),
  "/api/v1/courses": () => courses,
  "/api/v1/planner/items": () => [
    { course_id: 101, plannable_type: "assignment", plannable: { title: "PS3" }, plannable_date: d(2), html_url: "/courses/101/assignments/3",
      submissions: { submitted: false, graded: false, missing: false, late: false } },
  ],
  "/api/v1/calendar_events": (q) => q.getAll("context_codes[]").includes("course_101")
    ? [{ id: 900, title: "Review session", start_at: d(8), end_at: d(8), context_code: "course_101", location_name: "Kerchof 317" }] : [],
  "/api/v1/users/self/missing_submissions": () => [{ id: 2, course_id: 101, name: "PS2", due_at: d(-3) }],
  "/api/v1/announcements": (q) => q.getAll("context_codes[]").includes("course_101")
    ? [{ id: 500, title: "Exam room change", posted_at: d(-1), context_code: "course_101", message: "<p>Room 317</p>", author: { display_name: "Prof X" } }] : [],
};

const extraFiles = {
  77: { id: 77, display_name: "week1-slides.pdf", "content-type": "application/pdf", size: 5000, updated_at: d(-2), url: "https://x/files/77/download" },
  78: { id: 78, display_name: "lab1.py", "content-type": "text/x-python", size: 300, updated_at: d(-2), url: "https://x/files/78/download" },
};

function courseRoute(path) {
  let m;
  if ((m = path.match(/^\/api\/v1\/files\/(\d+)$/))) {
    // 79 is linked from a page but the student can't open it.
    return extraFiles[m[1]] ? { status: 200, body: extraFiles[m[1]] } : { status: 403, body: { status: "unauthorized" } };
  }
  if (path === "/api/v1/courses/102/pages/week-1-notes") {
    return { status: 200, body: { url: "week-1-notes", title: "Week 1 notes", body: '<p>See <a href="/courses/102/files/79">answers</a></p>' } };
  }
  if (path === "/api/v1/courses/102/pages") return { status: 404, body: { errors: [] } };
  if (path === "/api/v1/courses/102/modules") {
    return { status: 200, body: [{ id: 2, name: "Week 1", position: 1, items: [
      { id: 10, title: "Week 1 slides", type: "File", content_id: 77 },
      { id: 11, title: "Week 1 notes", type: "Page", page_url: "week-1-notes" },
    ] }] };
  }
  if (path === "/api/v1/courses/101/students/submissions") {
    return { status: 200, body: [{ assignment_id: 1, submission_comments: [{ author_name: "Prof X", comment: "Show your work on 3b", created_at: d(-4) }],
      rubric_assessment: null, attachments: [{ id: 900, display_name: "ps1.pdf", "content-type": "application/pdf", size: 10, url: "https://x/files/900/download" }] }] };
  }
  if ((m = path.match(/^\/api\/v1\/courses\/\d+\/pages\/([\w-]+)$/))) {
    return { status: 200, body: { url: m[1], title: m[1], body: "<p>page</p>" } };
  }
  if (/^\/api\/v1\/courses\/\d+\/students\/submissions$/.test(path)) return { status: 200, body: [] };
  m = path.match(/^\/api\/v1\/courses\/(\d+)\/(\w+)$/);
  if (!m) return null;
  const [, id, what] = m;
  if (id === "102" && what === "files") return { status: 403, body: { status: "unauthorized" } };
  if (id === "102" && what === "quizzes") return { status: 404, body: { errors: [{ message: "The specified resource does not exist." }] } };
  const data = {
    assignments: assignments[id] || [],
    assignment_groups: [{ id: 11, name: "Problem sets", group_weight: 40, rules: { drop_lowest: 1 } }, { id: 12, name: "Exams", group_weight: 60, rules: {} }],
    modules: [{ id: 1, name: "Week 1", position: 1, items: [{ id: 1, title: "PS1", type: "Assignment", content_id: 1, completion_requirement: { completed: true } }] }],
    pages: [{ url: "syllabus", title: "Syllabus", updated_at: d(-30) }],
    files: [{ id: 1, display_name: "notes.pdf", "content-type": "application/pdf", size: 1234, folder_id: 5, url: "https://x/files/1/download" },
            { id: 2, display_name: "hw.pdf", "content-type": "application/pdf", size: 99, folder_id: 5, url: "https://x/files/2/download" },
            { id: 3, display_name: "exam.pdf", "content-type": "application/pdf", size: 77, folder_id: 5, url: "https://x/files/3/download" }],
    discussion_topics: [],
    quizzes: [{ id: 1, title: "Quiz 1", due_at: d(5), question_count: 10 }],
  }[what];
  return data ? { status: 200, body: data } : null;
}

export function startMockCanvas({ port = 0 } = {}) {
  const hits = [];
  let rateLimited = false;
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, `http://${req.headers.host}`);
    hits.push(url.pathname);
    const origin = `http://${req.headers.host}`;
    const send = (status, body, headers = {}) => {
      res.writeHead(status, { "Content-Type": "application/json", ...headers });
      // Fixture file URLs point at "https://x/…"; serve them from this mock, with a
      // verifier like real Canvas download links.
      const json = JSON.stringify(body).replace(/https:\/\/x\/files\/(\d+)\/download/g, `${origin}/files/$1/download?verifier=v$1`);
      res.end(status === 200 ? `while(1);${json}` : json);
    };
    const loggedIn = (req.headers.cookie || "").includes("canvas_session=valid");
    // File downloads: allowed with the verifier or a session, like Canvas.
    let fm;
    if ((fm = url.pathname.match(/^\/files\/(\d+)\/download$/))) {
      if (!loggedIn && url.searchParams.get("verifier") !== `v${fm[1]}`) return send(401, { status: "unauthenticated" });
      res.writeHead(200, { "Content-Type": "application/pdf" });
      return res.end(`%PDF-mock file ${fm[1]}`);
    }
    // Any non-API path is a Canvas web page (so a real browser tab can sit on it).
    if (!url.pathname.startsWith("/api/")) {
      res.writeHead(200, { "Content-Type": "text/html" });
      return res.end("<!doctype html><title>Dashboard</title><h1>Mock Canvas</h1>");
    }
    if (!loggedIn) {
      return send(401, { status: "unauthenticated", errors: [{ message: "user authorization required" }] });
    }
    if (url.pathname === "/api/v1/courses/101/modules" && !rateLimited) {
      rateLimited = true;
      res.writeHead(403, { "Content-Type": "text/plain" });
      return res.end("403 Forbidden (Rate Limit Exceeded)");
    }
    let found = routes[url.pathname] ? { status: 200, body: routes[url.pathname](url.searchParams) } : courseRoute(url.pathname);
    if (!found) return send(404, { errors: [{ message: "not found" }] });
    if (found.status !== 200) return send(found.status, found.body);

    const per = Math.min(Number(url.searchParams.get("per_page") || 10), 2);
    const page = Number(url.searchParams.get("page") || 1);
    const slice = found.body;
    if (!Array.isArray(slice)) return send(200, slice);
    const headers = {};
    if (page * per < slice.length) {
      const next = new URL(url);
      next.searchParams.set("page", String(page + 1));
      headers.Link = `<${url.href}>; rel="current", <${next.href}>; rel="next"`;
    }
    send(200, slice.slice((page - 1) * per, page * per), headers);
  });
  return new Promise((resolve) => server.listen(port, "127.0.0.1", () => {
    resolve({ url: `http://127.0.0.1:${server.address().port}`, hits, close: () => server.close(), NOW });
  }));
}
