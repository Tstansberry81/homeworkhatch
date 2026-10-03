// A small fake D2L Brightspace, built from the sample JSON in D2L's API reference
// (docs.valence.desire2learn.com/res/*.html) with made-up values. It reproduces what matters for
// the diagnostic: a public version list, cookie-session auth with D2L's signed-out answer (403 and
// a not-quite-JSON body), bookmark paging on myenrollments, ObjectListPage envelopes, a tool the
// student can't open (403), and a token-bucket rate limit (429 with Retry-After) when asked to.
import http from "node:http";

export const NOW = Date.parse("2026-10-03T16:00:00Z");
const d = (days) => new Date(NOW + days * 864e5).toISOString().replace(/\.\d{3}Z$/, ".000Z");
const rich = (text) => ({ Text: text, Html: `<p>${text}</p>` });

const LP = ["1.49", "1.50", "1.55", "1.60", "1.62", "1.63"];
const LE = ["1.82", "1.90", "1.95", "1.98", "1.99"];

const orgUnit = (id, name, code) => ({
  Id: id, Type: { Id: 3, Code: "Course Offering", Name: "Course Offering" }, Name: name, Code: code,
  HomeUrl: `/d2l/home/${id}`, ImageUrl: `/d2l/api/lp/1.63/courses/${id}/image`,
});
const access = (active, canAccess, lastAccessed) => ({
  IsActive: active, StartDate: d(-40), EndDate: d(70), CanAccess: canAccess, ClasslistRoleName: "Student",
  LISRoles: ["urn:lti:role:ims/lis/Learner"], LastAccessed: lastAccessed,
});

// Fake people and courses. Every string here must stay out of the report (see d2l.test.mjs).
export const ENROLLMENTS = [
  { OrgUnit: orgUnit(6606, "FAKE Biology 101 - Section 2", "BIOL-101-02"), Access: access(true, true, d(-1)), PinDate: d(-30) },
  { OrgUnit: orgUnit(6607, "FAKE World History", "HIST-210"), Access: access(true, true, d(-2)), PinDate: null },
  { OrgUnit: orgUnit(6608, "FAKE Calculus I", "MATH-150"), Access: access(true, true, d(-3)), PinDate: null },
  { OrgUnit: orgUnit(6609, "FAKE Orientation 2025", "ORIENT-25"), Access: access(false, true, d(-300)), PinDate: null },
  { OrgUnit: orgUnit(6610, "FAKE Locked Course", "LOCK-1"), Access: access(true, false, null), PinDate: null },
];

const folder = (id, name, due, submissionType, extra = {}) => ({
  Id: id, CategoryId: null, Name: name, CustomInstructions: rich(`Upload ${name} as a PDF. Pat's rubric applies.`),
  Attachments: [{ FileId: 11, FileName: "instructions-FAKE.pdf", Size: 20480 }],
  TotalFiles: 1, UnreadFiles: 0, FlaggedFiles: 0, TotalUsers: 30, TotalUsersWithSubmissions: 12, TotalUsersWithFeedback: 3,
  Availability: id === 1001 ? null : { StartDate: d(-10), EndDate: d(20), StartDateAvailabilityType: 1, EndDateAvailabilityType: 0 },
  GroupTypeId: null, DueDate: due, DisplayInCalendar: true,
  Assessment: { ScoreDenominator: 10, Rubrics: [] }, NotificationEmail: null, IsHidden: false,
  LinkAttachments: [{ LinkId: 5, LinkName: "FAKE lab handout", Href: "https://example.edu/fake/handout" }],
  ActivityId: `https://ids.brightspace.com/activities/dropbox/FAKE-${id}`, IsAnonymous: false,
  DropboxType: 2, SubmissionType: submissionType, CompletionType: 0, SubmissionRule: 2, GradeItemId: 501,
  AllowOnlyUsersWithSpecialAccess: false, ...extra,
});

const FOLDERS = {
  6606: [folder(1001, "FAKE Lab Report 1", d(3), 0), folder(1002, "FAKE In-class Quiz", d(9), 2)],
  6607: [],
};

const MY_SUBMISSIONS = [{
  Entity: { EntityId: 31337, EntityType: "User", DisplayName: "Pat Example" },
  Status: 3,
  Feedback: { Score: 8.75, Feedback: rich("Nice work, Pat - check your units."), RubricAssessments: [], IsGraded: true,
    Files: [], Links: [{ Type: "External", LinkId: 9, LinkName: "FAKE feedback video", Href: null }], GradedSymbol: "B+" },
  Submissions: [{
    Id: 77, SubmittedBy: { Id: "31337", DisplayName: "Pat Example" }, SubmissionDate: d(-2), Comment: rich("My lab report"),
    Files: [{ FileId: 12, FileName: "pat-lab-report-FAKE.pdf", Size: 51200, isRead: true, isFlagged: false }],
  }],
  CompletionDate: null,
}];

const quiz = (id, name) => ({
  QuizId: id, Name: name, IsActive: true, SortOrder: 1, AutoExportToGrades: true, GradeItemId: 502, IsAutoSetGraded: true,
  Instructions: { Text: rich("Closed book. FAKE instructions."), IsDisplayed: true },
  Description: { Text: rich(""), IsDisplayed: false },
  StartDate: d(5), EndDate: d(6), DueDate: d(6), DisplayInCalendar: true,
  AttemptsAllowed: { IsUnlimited: false, NumberOfAttemptsAllowed: 1 },
  LateSubmissionInfo: { LateSubmissionOption: 0, LateLimitMinutes: null },
  SubmissionTimeLimit: { IsEnforced: true, ShowClock: true, TimeLimitValue: 50 }, SubmissionGracePeriod: 5,
  Password: "FAKE-quiz-password", Header: { Text: rich(""), IsDisplayed: false }, Footer: { Text: rich(""), IsDisplayed: false },
  AllowHints: false, DisableRightClick: false, DisablePagerAndAlerts: false, NotificationEmail: null, CalcTypeId: 1,
  RestrictIPAddressRange: null, CategoryId: null, PreventMovingBackwards: false, Shuffle: true, ActivityId: null,
  AllowOnlyUsersWithSpecialAccess: false, IsRetakeIncorrectOnly: false, PagingTypeId: 0, IsSynchronous: false,
  DeductionPercentage: null, HideQuestionPoints: false, IsSingleSession: false, AnnotationToolsEnabled: false,
});

const GRADE_VALUES = [
  { DisplayedGrade: "87.25 %", GradeObjectIdentifier: "501", GradeObjectName: "FAKE Lab Report 1", GradeObjectType: 1,
    GradeObjectTypeName: "Numeric", Comments: rich("Good job Pat"), PrivateComments: rich(""), LastModified: d(-1),
    LastModifiedBy: "9001", ReleasedDate: d(-1), PointsNumerator: 8.725, PointsDenominator: 10, WeightedDenominator: 15,
    WeightedNumerator: 13.0875 },
  { DisplayedGrade: "Pass", GradeObjectIdentifier: "503", GradeObjectName: "FAKE Participation", GradeObjectType: 2,
    GradeObjectTypeName: "PassFail", Comments: rich(""), PrivateComments: rich(""), LastModified: null, LastModifiedBy: null,
    ReleasedDate: null, PointsNumerator: null, PointsDenominator: 1, WeightedDenominator: 5, WeightedNumerator: null },
];

const gradeObject = (id, name, type, extra = {}) => ({
  MaxPoints: 10, CanExceedMaxPoints: false, IsBonus: false, ExcludeFromFinalGradeCalculation: false, GradeSchemeId: null,
  Id: id, Name: name, ShortName: name.slice(0, 8), GradeType: type, CategoryId: 41, Description: rich("FAKE"),
  GradeSchemeUrl: `/d2l/api/le/1.99/6606/grades/schemes/0`, Weight: 15, AssociatedTool: { ToolId: 2000, ToolItemId: 1001 },
  IsHidden: false, ...extra,
});
const GRADE_OBJECTS = [gradeObject(501, "FAKE Lab Report 1", "Numeric"), gradeObject(503, "FAKE Participation", "PassFail", { AssociatedTool: null })];
const GRADE_CATEGORIES = [{
  Id: 41, Grades: GRADE_OBJECTS, Name: "FAKE Labs", ShortName: "Labs", CanExceedMax: false, ExcludeFromFinalGrade: false,
  StartDate: null, EndDate: null, Weight: 40, MaxPoints: 100, AutoPoints: true, WeightDistributionType: 1,
  NumberOfHighestToDrop: 0, NumberOfLowestToDrop: 1,
}];

const NEWS = [{
  Id: 88, IsHidden: false, Attachments: [{ FileId: 13, FileName: "FAKE-syllabus.pdf", FileSize: 2048 }],
  Title: "FAKE Exam moved to Room 317", Body: rich("Professor Example says the exam moved."), CreatedBy: 9001,
  CreatedDate: d(-3), LastModifiedBy: null, LastModifiedDate: null, StartDate: d(-3), EndDate: null, IsGlobal: false,
  IsPublished: true, ShowOnlyInCourseOfferings: false, IsAuthorInfoShown: true, IsPinned: false, PinnedDate: null,
  IsStartDateShown: true, SortOrder: 0,
}];

const topic = (id, title, typeIdentifier, activityType) => ({
  TopicId: id, Identifier: String(id), TypeIdentifier: typeIdentifier, Title: title, Bookmarked: false, Unread: true,
  Url: `/content/enforced/6606-FAKE/${id}.pdf`, SortOrder: 1, StartDateTime: null, EndDateTime: null, ActivityId: null,
  CompletionType: 2, IsExempt: false, IsHidden: false, IsLocked: false, IsBroken: false, ToolId: null, ToolItemId: null,
  ActivityType: activityType, GradeItemId: null, LastModifiedDate: d(-20),
});
const TOC = {
  Modules: [{
    ModuleId: 300, Title: "FAKE Week 1", SortOrder: 1, StartDateTime: null, EndDateTime: null, IsHidden: false, IsLocked: false,
    PacingStartDate: null, PacingEndDate: null, DefaultPath: "/content/enforced/6606-FAKE/", LastModifiedDate: d(-20),
    Modules: [{
      ModuleId: 301, Title: "FAKE Readings", SortOrder: 1, StartDateTime: null, EndDateTime: null, Modules: [],
      Topics: [topic(401, "FAKE Chapter 1 slides", "File", 1)], IsHidden: false, IsLocked: false, PacingStartDate: null,
      PacingEndDate: null, DefaultPath: "/content/enforced/6606-FAKE/", LastModifiedDate: d(-20),
    }],
    Topics: [topic(402, "FAKE Course website", "Link", 2)],
  }],
};

const EVENTS = {
  Objects: [{
    CalendarEventId: 700, OrgUnitId: 6606, Title: "FAKE Lab Report 1 - Due", Description: "Submit to Pat's dropbox",
    StartDateTime: d(3), EndDateTime: d(3), IsAllDayEvent: false, StartDay: null, EndDay: null, GroupId: null,
    IsRecurring: false, RecurrenceInfo: null, LocationId: null, LocationName: "FAKE Hall 101", OrgUnitName: "FAKE Biology 101",
    OrgUnitCode: "BIOL-101-02", IsAssociatedWithEntity: true,
    AssociatedEntity: { AssociatedEntityType: "D2L.LE.Dropbox.Dropbox", AssociatedEntityId: 1001, Link: "/d2l/lms/dropbox/user/folder_submit_files.d2l?ou=6606&db=1001" },
    HasVisibilityRestrictions: false, VisibilityRestrictions: { Type: 1, Range: null, HiddenRangeUnitType: null, StartDate: null, EndDate: null },
    CalendarEventViewUrl: "https://fake.brightspace.example/d2l/le/calendar/6606/event/700/detailsview", EventType: 6, Presenters: [],
  }],
  Next: null,
};

const VERSIONS = [
  { ProductCode: "lp", LatestVersion: "1.63", SupportedVersions: LP },
  { ProductCode: "le", LatestVersion: "1.99", SupportedVersions: LE },
  { ProductCode: "ep", LatestVersion: "2.5", SupportedVersions: ["2.5"] },
];

function apiRoute(path, q, rec) {
  let m;
  if (path === "/d2l/api/lp/1.63/users/whoami") {
    return { status: 200, body: { Identifier: "31337", FirstName: "Pat", LastName: "Example", UniqueName: "pexample",
      ProfileIdentifier: "FAKEprof", Pronouns: null } };
  }
  if (path === "/d2l/api/lp/1.63/enrollments/myenrollments/") {
    if (q.get("orgUnitTypeId") !== "3") return { status: 400, body: { Errors: [{ Message: "bad orgUnitTypeId" }] } };
    const start = Number(q.get("bookmark") || 0);
    const size = rec.pageSize;
    const all = rec.enrollments;
    const items = all.slice(start, start + size);
    const more = start + size < all.length;
    return { status: 200, body: { PagingInfo: { Bookmark: more ? String(start + size) : "", HasMoreItems: more }, Items: items } };
  }
  if (path === "/d2l/api/le/1.99/calendar/events/myEvents/") {
    const ok = /^\d+(,\d+)*$/.test(q.get("orgUnitIdsCSV") || "")
      && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(q.get("startDateTime") || "")
      && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(q.get("endDateTime") || "");
    return ok ? { status: 200, body: EVENTS } : { status: 400, body: { Errors: [{ Message: "Invalid parameters" }] } };
  }
  // Students can't use this one at the mock school.
  if (path === "/d2l/api/le/1.99/content/myItems/due/") return { status: 403, body: { Errors: [{ Message: "Forbidden" }] } };
  if (!(m = path.match(/^\/d2l\/api\/le\/1\.99\/(\d+)\/(.+)$/))) return null;
  const [, ou, rest] = m;
  if (!["6606", "6607", "6608", "6609"].includes(ou)) return { status: 403, body: { Errors: [{ Message: "Forbidden" }] } };
  if (rest === "dropbox/folders/") {
    // The instructor of 6608 turned Assignments off for students.
    return ou === "6608" ? { status: 403, body: { Errors: [{ Message: "Forbidden" }] } } : { status: 200, body: FOLDERS[ou] || [] };
  }
  if ((m = rest.match(/^dropbox\/folders\/(\d+)\/submissions\/mysubmissions\/$/))) {
    return m[1] === "1001" ? { status: 200, body: MY_SUBMISSIONS } : { status: 200, body: [] };
  }
  if (rest === "quizzes/") return { status: 200, body: { Objects: ou === "6606" ? [quiz(601, "FAKE Midterm"), quiz(602, "FAKE Quiz 2")] : [], Next: null } };
  if (rest === "grades/values/myGradeValues/") return { status: 200, body: ou === "6606" ? GRADE_VALUES : [] };
  // Some schools don't let students read grade items; 6607 is one of those courses.
  if (rest === "grades/") return ou === "6607" ? { status: 403, body: { Errors: [{ Message: "Forbidden" }] } } : { status: 200, body: GRADE_OBJECTS };
  if (rest === "grades/categories/") return { status: 200, body: GRADE_CATEGORIES };
  if (rest === "grades/setup/") return { status: 200, body: { GradingSystem: ou === "6608" ? "Points" : "Weighted", IsNullGradeZero: false, DefaultGradeSchemeId: 1 } };
  if (rest === "news/") return { status: 200, body: NEWS };
  if (rest === "content/toc") return { status: 200, body: TOC };
  return { status: 404, body: { Errors: [{ Message: "Not Found" }] } };
}

// Every string value a response carried, so tests can prove none of them reached the report.
function strings(value, out) {
  if (typeof value === "string") out.add(value);
  else if (Array.isArray(value)) value.forEach((v) => strings(v, out));
  else if (value && typeof value === "object") Object.values(value).forEach((v) => strings(v, out));
  return out;
}

// Options: pageSize (enrollments per page), enrollments, rateLimitAfter (API requests before every
// answer is a 429), hang (a path regex that never answers), transform(path, body) (rewrites each
// JSON body before it's sent; the property test injects random strings with it), sameOriginOnly.
export function startMockD2L({
  pageSize = 2, enrollments = ENROLLMENTS, rateLimitAfter = Infinity, hang = null, transform = null, sameOriginOnly = false,
} = {}) {
  const hits = [];
  const served = new Set();
  const rec = { pageSize, enrollments };
  let apiCalls = 0;
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, `http://${req.headers.host}`);
    hits.push(url.pathname + url.search);
    const send = (status, body, headers = {}) => {
      const out = transform ? transform(url.pathname, body) : body;
      strings(out, served);
      res.writeHead(status, { "Content-Type": "application/json; charset=utf-8", ...headers });
      res.end(JSON.stringify(out));
    };
    if (!url.pathname.startsWith("/d2l/api/")) {
      res.writeHead(200, { "Content-Type": "text/html" });
      return res.end("<!doctype html><title>Homepage - FAKE Brightspace</title><d2l-navigation></d2l-navigation>");
    }
    if (req.method !== "GET") return send(405, { Errors: [{ Message: "Method not allowed" }] });
    if (hang && hang.test(url.pathname)) return;  // never answers
    if (url.pathname === "/d2l/api/versions/") return send(200, VERSIONS);
    if (++apiCalls > rateLimitAfter) return send(429, { Errors: [{ Message: "Too Many Requests" }] }, { "Retry-After": "30" });
    // D2L's signed-out answer: 403 with a text/html body that isn't quite JSON. With sameOriginOnly,
    // the session only counts on the page's own requests (a school whose cookies don't reach the
    // extension's service worker).
    const sameOrigin = req.headers["sec-fetch-site"] === "same-origin";
    if (!(req.headers.cookie || "").includes("d2lSessionVal=valid") || (sameOriginOnly && !sameOrigin)) {
      res.writeHead(403, { "Content-Type": "text/html" });
      return res.end('{ Errors: [ {Message: "Forbidden"} ] }');
    }
    const found = apiRoute(url.pathname, url.searchParams, rec);
    if (!found) return send(404, { Errors: [{ Message: "Not Found" }] });
    send(found.status, found.body);
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => {
    const close = () => new Promise((r) => { server.closeAllConnections?.(); server.close(() => r()); });
    resolve({ url: `http://127.0.0.1:${server.address().port}`, hits, served, close, NOW });
  }));
}
