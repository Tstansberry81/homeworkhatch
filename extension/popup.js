import { upcoming, isVisible, zipPlan } from "./canvas.js";
import { HATCH_URL } from "./upload.js";
import { isBrightspaceUrl, summaryText } from "./d2l.js";

const $ = (id) => document.getElementById(id);
const FIELDS = ["baseUrl", "intervalMinutes", "endpointUrl", "endpointToken"];
const DEFAULTS = { baseUrl: "", intervalMinutes: 60, endpointUrl: HATCH_URL, endpointToken: "", courseVisibility: {} };
const STATE_LABEL = {
  ok: "Synced", syncing: "Syncing…", error: "Error", logged_out: "Logged out", not_connected: "Not connected",
};

const ago = (iso) => {
  const m = Math.round((Date.now() - Date.parse(iso)) / 60000);
  return m < 1 ? "just now" : m < 60 ? `${m}m ago` : m < 1440 ? `${Math.round(m / 60)}h ago` : `${Math.round(m / 1440)}d ago`;
};
const when = (iso) => new Date(iso).toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

function el(tag, props = {}, ...kids) {
  const n = Object.assign(document.createElement(tag), props);
  n.append(...kids);
  return n;
}

let settings = { ...DEFAULTS };
let snapshot = null;
let savedKeys = new Set();  // files already in an earlier zip
let nextSyncAt = null;

async function saveSettings(patch) {
  settings = { ...settings, ...patch };
  await chrome.storage.local.set({ settings });
}

// ---------- connecting to a school's Canvas ----------

// Opening the popup grants activeTab, so we can ask the current tab whether it is a
// Canvas instance — any school, instructure.com or a custom domain — by calling the
// Canvas API from inside it.
async function probeTab(tab) {
  try {
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: async () => {
        try {
          const r = await fetch("/api/v1/users/self", { headers: { Accept: "application/json" } });
          const body = JSON.parse((await r.text()).replace(/^\s*while\(1\);/, ""));
          if (r.status === 200 && body.id != null) return { canvas: true, loggedIn: true, name: body.name };
          if (r.status === 401 && body.status === "unauthenticated") return { canvas: true, loggedIn: false };
        } catch {}
        return { canvas: false };
      },
    });
    return result;
  } catch {
    return { canvas: false }; // chrome:// pages, the web store, etc.
  }
}

let detected = null; // { origin, loggedIn, name }

async function detectCanvas() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  let origin = null;
  try { origin = new URL(tab?.url).origin; } catch {}
  if (!origin?.startsWith("http")) return null;
  const probe = await probeTab(tab);
  return probe.canvas ? { origin, ...probe } : null;
}

// Chrome's permission prompt usually closes this popup before request() resolves, so
// nothing after it can be relied on. We record the pending origin first and let the
// background worker finish the connection when the grant lands (permissions.onAdded).
function connect(origin) {
  // Clicking "Agree and connect" is the student's consent to the disclosure above it.
  chrome.storage.local.set({ pendingConnect: origin, consent: { at: new Date().toISOString(), origin } }); // not awaited: keep the user gesture
  chrome.permissions.request({ origins: [`${origin}/*`] })
    .then(async (granted) => {
      if (!granted) {
        $("setupMsg").textContent = "Permission is needed to read your Canvas. Nothing was saved.";
        return;
      }
      // Already-granted origins fire no onAdded event, so nudge the worker directly.
      await chrome.runtime.sendMessage({ type: "connect" });
      render();
    })
    .catch((e) => { $("setupMsg").textContent = `Couldn't request access: ${e.message}`; });
}

$("connect").onclick = () => detected && connect(detected.origin);
$("connectManual").onclick = () => {
  let origin;
  try { origin = new URL($("manualUrl").value.trim()).origin; } catch {}
  if (!origin?.startsWith("http")) {
    $("setupMsg").textContent = "That doesn't look like a web address.";
    return;
  }
  connect(origin);
};

async function renderSetup() {
  $("setup").hidden = false;
  $("main").hidden = true;
  $("state").textContent = STATE_LABEL.not_connected;
  $("state").className = "pill not_connected";
  $("setupMsg").textContent = "Checking this tab…";
  detected = await detectCanvas();
  if (detected) {
    const host = new URL(detected.origin).host;
    $("setupMsg").textContent = detected.loggedIn
      ? `Found Canvas at ${host}, signed in as ${detected.name}.`
      : `Found Canvas at ${host}. Connect below, then log in as usual.`;
    $("connect").hidden = false;
    $("connect").textContent = `Agree and connect ${host}`;
  } else {
    $("setupMsg").textContent = "Open your school's Canvas in this tab, then click the extension icon again.";
    $("connect").hidden = true;
  }
}

// Installs from before the disclosure existed: already connected, but syncing waits for agreement.
function renderConsent(origin) {
  $("setup").hidden = false;
  $("main").hidden = true;
  $("state").textContent = "Paused";
  $("state").className = "pill not_connected";
  const host = new URL(origin).host;
  $("setupMsg").textContent = `Homework Hatch updated. Review what it syncs from ${host}, then agree to keep syncing.`;
  detected = { origin };
  $("connect").hidden = false;
  $("connect").textContent = `Agree and keep syncing ${host}`;
}

// ---------- synced view ----------

function renderStatus(status = {}) {
  $("state").textContent = STATE_LABEL[status.state] || "Not synced";
  $("state").className = `pill ${status.state || ""}`;
  $("sync").disabled = status.state === "syncing";
  const p = status.progress;
  $("summary").textContent =
    status.state === "syncing" && p?.step === "course" ? `Course ${p.index}/${p.total}: ${p.name}`
    : status.state === "syncing" && p?.step === "upload"
      ? `Uploading new files ${p.index}/${p.total}${p.skipped ? ` · ${p.skipped} already on Homework Hatch` : ""}…`
    : status.state === "syncing" ? `Fetching ${p?.step || "…"}`
    : status.lastSync ? `${status.user} · synced ${ago(status.lastSync)}${pushSummary(status.lastPush)}`
    : "Not synced yet.";
  $("auto").textContent = `Syncs automatically every ${settings.intervalMinutes} min while Chrome is open` +
    (nextSyncAt && status.state !== "syncing" ? ` · next at ${new Date(nextSyncAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : "") + ".";
  const dl = status.downloading;
  const fresh = snapshot ? zipPlan(snapshot, settings.courseVisibility, { dedupe: true, skipKeys: savedKeys }) : [];
  const all = snapshot ? zipPlan(snapshot, settings.courseVisibility, { dedupe: true }) : [];
  $("download").disabled = Boolean(dl) || !fresh.length;
  $("download").textContent = dl ? `Zipping ${dl.done}/${dl.total}…` : downloadLabel(fresh, all);
  $("downloadAll").hidden = Boolean(dl) || !all.length || fresh.length === all.length;
  $("downloadAll").textContent = `Download all ${all.length}`;
  if (!dl && status.lastDownload && status.state !== "syncing") {
    const { done, failed, filename } = status.lastDownload;
    $("summary").textContent += ` · ${done} files zipped to ${filename}${failed ? ` (${failed} couldn't be included)` : ""}`;
  }
  const err = status.error || status.pushError || status.downloadError;
  $("error").hidden = !err;
  $("error").textContent = err || "";
  $("linkSite").hidden = Boolean(settings.endpointToken);
  $("open").hidden = status.state !== "logged_out";
}

function pushSummary(push) {
  if (!push) return "";
  const n = push.uploaded, old = push.skipped || 0;
  const s = (k) => (k === 1 ? "" : "s");
  if (!n) return old ? ` · no new files (all ${old} already on Homework Hatch)` : " · sent to Homework Hatch";
  return ` · ${n} new file${s(n)} uploaded${old ? `, ${old} already on Homework Hatch` : ""}`;
}

const mb = (bytes) => bytes >= 1e9 ? `${(bytes / 1e9).toFixed(1)} GB` : bytes >= 1e6 ? `${Math.round(bytes / 1e6)} MB`
  : `${Math.max(1, Math.round(bytes / 1e3))} KB`;

function downloadLabel(fresh, all) {
  if (!snapshot) return "Download files";
  if (!all.length) return "No files to download";
  if (!fresh.length) return "No new files to download";
  const size = mb(fresh.reduce((s, j) => s + (j.file.size || 0), 0));
  const n = fresh.length, files = `file${n === 1 ? "" : "s"}`;
  return n === all.length ? `Download ${n} ${files} (.zip, ~${size})` : `Download ${n} new ${files} (.zip, ~${size})`;
}

const pct = (g) => g.current_score == null ? "—" : `${g.current_score}%${g.current_grade ? ` (${g.current_grade})` : ""}`;

function renderSnapshot() {
  if (!snapshot) return;
  const vis = settings.courseVisibility;
  const due = upcoming(snapshot, 14, Date.now(), vis);
  const tag = { upcoming: "", no_submission: "in class · ", past_due: "past due · ", missing: "MISSING · " };
  $("upcoming").replaceChildren(...(due.length ? due.map((a) =>
    el("li", { className: a.status },
      el("a", { href: a.html_url, target: "_blank", textContent: `${a.course} · ${a.name}` }),
      el("span", { className: "when", textContent: `${tag[a.status] ?? ""}${when(a.due_at)}` })))
    : [el("li", { className: "muted", textContent: "Nothing due." })]));

  // One row per class: sections sharing a class_key merge, and the section that
  // carries the grade wins.
  const classes = new Map();
  for (const c of snapshot.courses.filter((c) => isVisible(c, vis))) {
    const prev = classes.get(c.class_key);
    if (!prev || (prev.grade.current_score == null && c.grade.current_score != null)) classes.set(c.class_key, c);
  }
  $("grades").replaceChildren(...[...classes.values()].map((c) =>
    el("tr", {}, el("td", { textContent: c.name }), el("td", { textContent: pct(c.grade) }))));

  $("courses").replaceChildren(...snapshot.courses.map((c) => {
    const box = el("input", { type: "checkbox", checked: isVisible(c, vis) });
    box.onchange = async () => {
      await saveSettings({ courseVisibility: { ...settings.courseVisibility, [c.id]: box.checked } });
      renderSnapshot();
      renderStatus((await chrome.storage.local.get("status")).status);
    };
    return el("label", { className: "check" }, box, ` ${c.name}`, el("span", { className: "muted", textContent: c.term?.name ? ` · ${c.term.name}` : "" }));
  }));

  const n = (k) => snapshot.courses.reduce((s, c) => s + (c[k]?.length || 0), 0);
  const allFiles = snapshot.courses.flatMap((c) => c.files || []);
  const viaLinks = allFiles.filter((f) => !f.sources?.includes("files_tab")).length;
  const pageBodies = snapshot.courses.reduce((s, c) => s + c.pages.filter((p) => p.body_html).length, 0);
  $("counts").textContent = `${snapshot.courses.length} courses · ${n("assignments")} assignments · ${n("modules")} modules · ` +
    `${allFiles.length} files (${viaLinks} found via links/modules) · ${pageBodies} pages · ${n("announcements")} announcements · ` +
    `${snapshot.planner.length} planner items · ${snapshot.missing.length} missing`;
  $("restricted").textContent = snapshot.restricted.length
    ? `Hidden by instructors (normal): ${snapshot.restricted.map((r) => r.endpoint).join(", ")}` : "";
}

// ---------- Brightspace check (d2l.js; runs in the background worker) ----------

let brightspace = null;  // { origin, tabId } when the active tab is a Brightspace page
let showCanvas = false;  // the student chose "Back to my Canvas sync"
let lastDiag = null;

async function detectBrightspace() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.url || !isBrightspaceUrl(tab.url)) return null;
  return { origin: new URL(tab.url).origin, tabId: tab.id };
}

function renderBrightspace(diag) {
  $("setup").hidden = true;
  $("main").hidden = true;
  $("d2l").hidden = false;
  $("state").textContent = "Brightspace";
  $("state").className = "pill";
  $("d2lBack").hidden = !settings.baseUrl;
  renderDiag(diag);
}

function renderDiag(diag) {
  const mine = diag?.origin === brightspace.origin ? diag : null;
  lastDiag = mine;
  const running = mine?.state === "running";
  const report = mine?.state === "done" ? mine.report : null;
  $("d2lRun").disabled = running;
  $("d2lRun").textContent = report || mine?.state === "error" ? "Run the check again" : "Run a 1-minute check";
  const p = mine?.progress;
  $("d2lMsg").textContent =
    running ? (p ? `Checking… ${p.done} of about ${p.total} requests done.` : "Checking…")
    : mine?.state === "error" ? mine.error
    : report && report.notes.includes("versions_unavailable") ? "This page didn't answer like a Brightspace site, so there was nothing to check."
    : report && !report.signed_in ? "Sign in to Brightspace in this tab first, then run the check again."
    : report?.notes.includes("rate_limited") ? "Brightspace asked us to slow down, so the check stopped early. The report has what it got."
    : "";
  $("d2lResult").hidden = !report?.signed_in;
  if (!report?.signed_in) return;
  $("d2lSummary").textContent = `${summaryText(report)}.`;
  $("d2lReport").textContent = JSON.stringify(report, null, 2);
  const linked = Boolean(settings.endpointUrl && settings.endpointToken);
  $("d2lSend").disabled = !linked || Boolean(mine.sending || mine.sent);
  $("d2lSend").textContent = mine.sent ? "Sent" : mine.sending ? "Sending…" : "Send to Homework Hatch";
  $("d2lSendMsg").textContent = mine.sendError
    || (mine.sent ? "Thank you! The report reached Homework Hatch."
      : linked ? "" : "To send it, link the extension to your Homework Hatch account first, or download it and email it to us.");
}

// Same popup-closing problem as connect(): record the request first (not awaited, so the click's
// user gesture survives), then ask for access. The worker starts the check when the grant lands.
$("d2lRun").onclick = () => {
  if (!brightspace) return;
  const { origin, tabId } = brightspace;
  chrome.storage.local.set({ pendingDiag: { origin, tabId, at: Date.now() } });
  $("d2lMsg").textContent = "Asking for your OK to read this site…";
  chrome.permissions.request({ origins: [`${origin}/*`] })
    .then(async (granted) => {
      if (!granted) {
        chrome.storage.local.remove("pendingDiag");
        $("d2lMsg").textContent = "The check needs your OK to read this Brightspace site. Nothing was read.";
        return;
      }
      $("d2lMsg").textContent = "Checking…";
      $("d2lRun").disabled = true;
      // Already-granted origins fire no onAdded event, so ask the worker directly.
      await chrome.runtime.sendMessage({ type: "diag:run", origin, tabId });
    })
    .catch((e) => { $("d2lMsg").textContent = `Couldn't ask for access: ${e.message}`; });
};

$("d2lSend").onclick = () => chrome.runtime.sendMessage({ type: "diag:send" });

$("d2lDownload").onclick = () => {
  const report = lastDiag?.report;
  if (!report) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }));
  el("a", { href: url, download: `brightspace-check-${report.host}-${report.ran_at.slice(0, 10)}.json` }).click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};

$("d2lCopy").onclick = async () => {
  const report = lastDiag?.report;
  if (!report) return;
  const text = JSON.stringify(report, null, 2);
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const area = el("textarea", { value: text });
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  $("d2lCopy").textContent = "Copied";
  setTimeout(() => { $("d2lCopy").textContent = "Copy"; }, 2000);
};

$("d2lBack").onclick = () => {
  showCanvas = true;
  render();
};

// Not async, for the same reason as the status listener below.
chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type !== "diag" || !brightspace || $("d2l").hidden) return;
  renderDiag(msg.diag);
});

async function render() {
  const stored = await chrome.storage.local.get(["status", "snapshot", "settings", "downloadedKeys", "diag"]);
  settings = { ...DEFAULTS, ...stored.settings };
  snapshot = stored.snapshot || null;
  savedKeys = new Set(stored.downloadedKeys || []);
  nextSyncAt = (await chrome.alarms.get("sync"))?.scheduledTime || null;
  brightspace ??= await detectBrightspace();
  const onCanvas = settings.baseUrl && brightspace && new URL(settings.baseUrl).origin === brightspace.origin;
  if (brightspace && !showCanvas && !onCanvas) return renderBrightspace(stored.diag);
  $("d2l").hidden = true;
  if (!settings.baseUrl) return renderSetup();
  if (stored.status?.state === "needs_consent") return renderConsent(new URL(settings.baseUrl).origin);
  $("setup").hidden = true;
  $("main").hidden = false;
  renderStatus(stored.status);
  renderSnapshot();
  for (const f of FIELDS) $(f).value = settings[f];
}

$("sync").onclick = () => chrome.runtime.sendMessage({ type: "sync" });
// Opens the website's Connect Canvas page with this copy's ID, so the page can link it.
$("linkSite").onclick = () => chrome.tabs.create({
  url: `${(settings.endpointUrl || HATCH_URL).replace(/\/+$/, "")}/settings/sync?ext=${chrome.runtime.id}` });
$("download").onclick = () => chrome.runtime.sendMessage({ type: "download", mode: "new" });
$("downloadAll").onclick = () => chrome.runtime.sendMessage({ type: "download", mode: "all" });
$("open").onclick = () => chrome.tabs.create({ url: settings.baseUrl });

$("export").onclick = () => {
  if (!snapshot) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(snapshot, null, 2)], { type: "application/json" }));
  el("a", { href: url, download: `canvas-${snapshot.synced_at.slice(0, 10)}.json` }).click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};

$("save").onclick = () => {
  const s = Object.fromEntries(FIELDS.map((f) => [f, $(f).value.trim()]));
  let origins;
  try {
    s.baseUrl = s.baseUrl ? new URL(s.baseUrl).origin : "";
    origins = [s.baseUrl, s.endpointUrl].filter(Boolean).map((u) => `${new URL(u).origin}/*`);
  } catch {
    $("saved").textContent = "Invalid URL — not saved.";
    return;
  }
  // Same popup-closing problem as connect(): write first (without awaiting, so the
  // click's user gesture survives), then ask for access. The worker re-syncs on grant.
  const switched = s.baseUrl !== settings.baseUrl;
  settings = { ...settings, ...s, ...(switched ? { courseVisibility: {} } : {}) };
  chrome.storage.local.set({ settings });
  if (switched) chrome.storage.local.remove("snapshot");
  const request = origins.length ? chrome.permissions.request({ origins }) : Promise.resolve(true);
  request.then(async (granted) => {
    $("saved").textContent = granted ? "Saved." : "Saved, but access was denied — sync will fail until you allow it.";
    await chrome.runtime.sendMessage({ type: "reschedule" });
    if (switched) {
      chrome.runtime.sendMessage({ type: "sync" });
      render();
    }
  }).catch((e) => { $("saved").textContent = `Couldn't request access: ${e.message}`; });
};

// Deliberately not async: an async listener returns a Promise, which Chrome treats as a
// reply to *every* message — including ones meant for the worker or offscreen page.
chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type !== "status" || !settings.baseUrl) return;
  (async () => {
    const fresh = await chrome.storage.local.get(["snapshot", "downloadedKeys"]);
    if (msg.status.state === "ok") snapshot = fresh.snapshot;
    savedKeys = new Set(fresh.downloadedKeys || []);
    nextSyncAt = (await chrome.alarms.get("sync"))?.scheduledTime || null;
    renderStatus(msg.status);
    if (msg.status.state === "ok") renderSnapshot();
  })();
});

render();
