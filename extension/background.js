import { syncCanvas, NotLoggedInError, zipPlan, fileKey, pool } from "./canvas.js";
import { buildZip } from "./zip.js";
import { uploadSnapshot, HATCH_URL } from "./upload.js";

const DEFAULTS = {
  // Empty until the student connects their school's Canvas from the popup.
  baseUrl: "",
  intervalMinutes: 60,
  // { [courseId]: true|false } — overrides the Canvas dashboard default.
  courseVisibility: {},
  endpointUrl: HATCH_URL,
  endpointToken: "",
};

async function settings() {
  const { settings = {} } = await chrome.storage.local.get("settings");
  return { ...DEFAULTS, ...settings };
}

// If Chrome killed the previous worker mid-sync or mid-download, its "in progress" status is
// still in storage. Nothing is running in this fresh worker, so mark it interrupted.
chrome.storage.local.get("status").then(({ status }) => {
  if (!status || (status.state !== "syncing" && !status.downloading)) return;
  if (running || downloadRunning) return;  // that status belongs to work this worker already started
  const patch = { downloading: null };
  if (status.state === "syncing") Object.assign(patch, { state: "error", progress: null, error: "The last sync was interrupted. Click Sync now." });
  setStatus(patch);
});

// Progress updates fire concurrently from the course pool, so merge into an in-memory
// copy rather than read-modify-writing storage (which would drop fields).
let statusCache = null;
async function setStatus(patch) {
  statusCache ??= chrome.storage.local.get("status").then((r) => r.status || {});
  const next = { ...(await statusCache), ...patch };
  statusCache = Promise.resolve(next);
  await chrome.storage.local.set({ status: next });
  chrome.runtime.sendMessage({ type: "status", status: next }).catch(() => {});
}

// Transport 1: fetch straight from the service worker. With host permission for the
// Canvas origin, Chrome attaches the user's Canvas cookies.
async function swGet(url) {
  const r = await fetch(url, { credentials: "include", headers: { Accept: "application/json" } });
  return { status: r.status, link: r.headers.get("link"), text: await r.text() };
}

// Transport 2: run the fetch inside an open Canvas tab, i.e. as a genuine first-party
// request. Used when the service worker's request comes back unauthenticated.
function tabGet(tabId) {
  return async (url) => {
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId },
      func: async (u) => {
        const r = await fetch(u, { credentials: "include", headers: { Accept: "application/json" } });
        return { status: r.status, link: r.headers.get("link"), text: await r.text() };
      },
      args: [url],
    });
    return result;
  };
}

async function findCanvasTab(baseUrl) {
  const tabs = await chrome.tabs.query({ url: `${new URL(baseUrl).origin}/*` });
  return tabs.find((t) => t.status === "complete") || tabs[0] || null;
}

// Full pipeline to the app's storage: snapshot JSON, then only the files the server
// doesn't already have at that version. Runs after every sync, including the hourly one.
async function push(snapshot, s, onProgress) {
  if (!s.endpointUrl) return null;
  const tab = await findCanvasTab(s.baseUrl);
  const result = await uploadSnapshot({
    serverUrl: s.endpointUrl,
    token: s.endpointToken,
    snapshot,
    plan: zipPlan(snapshot, s.courseVisibility),
    fetchBytes: (file) => fetchFileBytes(file.download_url, tab, file.content_type),
    onProgress: ({ done, total }) => onProgress({ step: "upload", index: done, total }),
  });
  return { ...result, at: new Date().toISOString() };
}

let running = null;

// One sync at a time; concurrent callers share the in-flight run. The flag is cleared
// in .finally() so *every* exit path (including "not connected yet") releases it.
function runSync(trigger) {
  running ??= syncOnce(trigger).finally(() => { running = null; });
  return running;
}

async function syncOnce(trigger) {
  const s = await settings();
  if (!s.baseUrl) {
    await setStatus({ state: "not_connected", progress: null, error: null });
    chrome.action.setBadgeBackgroundColor({ color: "#b45309" });
    chrome.action.setBadgeText({ text: "!" });
    return;
  }
  await setStatus({ state: "syncing", trigger, progress: null, error: null });
  const onProgress = (p) => setStatus({ progress: p });
  try {
    let snapshot;
    try {
      snapshot = await syncCanvas({ baseUrl: s.baseUrl, get: swGet, onProgress });
    } catch (e) {
      if (!(e instanceof NotLoggedInError)) throw e;
      const tab = await findCanvasTab(s.baseUrl);
      if (!tab) throw e;
      snapshot = await syncCanvas({ baseUrl: s.baseUrl, get: tabGet(tab.id), onProgress });
    }
    await chrome.storage.local.set({ snapshot });
    let lastPush = null;
    let pushError = null;
    if (s.endpointUrl && !s.endpointToken) {
      pushError = `Not linked to Homework Hatch yet: open ${s.endpointUrl}/settings/sync while signed in and it links itself.`;
    } else {
      try {
        lastPush = await push(snapshot, s, onProgress);
        const n = lastPush?.failed.length || 0;
        if (n) pushError = `${n} file${n === 1 ? "" : "s"} couldn't be uploaded; ${n === 1 ? "it" : "they"} will be retried at the next sync.`;
      } catch (e) {
        pushError = `Upload failed: ${e.message || e}`;
      }
    }
    await setStatus({
      state: "ok", progress: null, lastSync: snapshot.synced_at, downloadError: null,
      lastPush, pushError, user: snapshot.user.name,
    });
    chrome.action.setBadgeText({ text: "" });
  } catch (e) {
    const loggedOut = e instanceof NotLoggedInError;
    await setStatus({
      state: loggedOut ? "logged_out" : "error",
      progress: null,
      error: loggedOut ? "Log in to Canvas (keep a Canvas tab open if this keeps happening)." : String(e.message || e),
    });
    chrome.action.setBadgeBackgroundColor({ color: loggedOut ? "#b45309" : "#b91c1c" });
    chrome.action.setBadgeText({ text: "!" });
  }
}

// ---------- zipped file download ----------

function base64ToBytes(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

// Canvas download URLs carry a verifier and redirect to the school's file store, which
// behaves differently across Canvas hosts. Try, in order: no cookies (verifier only),
// with cookies, then from inside an open Canvas tab as a first-party request.
// A download that silently redirected to Canvas's sign-in page returns HTML with a 200.
// Unless the file really is HTML, treat that as a failed download rather than the file.
function looksLikeLoginPage(contentType, expectedType) {
  return /text\/html/i.test(contentType || "") && !/html/i.test(expectedType || "");
}

async function fetchFileBytes(url, tab, expectedType) {
  for (const credentials of ["omit", "include"]) {
    try {
      const r = await fetch(url, { credentials });
      if (r.ok && !looksLikeLoginPage(r.headers.get("content-type"), expectedType)) {
        return new Uint8Array(await r.arrayBuffer());
      }
    } catch {}
  }
  if (!tab) return null;
  try {
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: async (u, expected) => {
        const r = await fetch(u);
        if (!r.ok) return null;
        if (/text\/html/i.test(r.headers.get("content-type") || "") && !/html/i.test(expected || "")) return null;
        const b = new Uint8Array(await r.arrayBuffer());
        let s = "";
        for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
        return btoa(s);
      },
      args: [url, expectedType || ""],
    });
    return result ? base64ToBytes(result) : null;
  } catch {
    return null;
  }
}

// Service workers can't create blob: URLs, and runtime messages can't carry a Blob.
// So the zip goes into IndexedDB (shared across the extension's origin) and an
// offscreen document turns it into a URL chrome.downloads can save.
function idb(mode, fn) {
  return new Promise((resolve, reject) => {
    const open = indexedDB.open("hatch", 1);
    open.onupgradeneeded = () => open.result.createObjectStore("blobs");
    open.onerror = () => reject(open.error);
    open.onsuccess = () => {
      const tx = open.result.transaction("blobs", mode);
      const req = fn(tx.objectStore("blobs"));
      tx.oncomplete = () => resolve(req?.result);
      tx.onerror = () => reject(tx.error);
    };
  });
}

// The offscreen page reads the zip from IndexedDB as soon as it loads and reports its
// blob: URL (or error) to us. We never *ask* it via sendMessage, because a request would
// be answered by whichever extension page responds first — e.g. an open popup.
let offscreenWaiter = null;

async function zipDownloadUrl(blob) {
  await idb("readwrite", (store) => store.put(blob, "zip"));
  // A fresh document each time, so it always loads (and reports) the zip just written.
  await chrome.offscreen.closeDocument().catch(() => {});
  const reported = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Timed out preparing the zip")), 60000);
    offscreenWaiter = (msg) => {
      clearTimeout(timer);
      offscreenWaiter = null;
      msg.url ? resolve(msg.url) : reject(new Error(`Could not prepare the zip: ${msg.error || "unknown error"}`));
    };
  });
  await chrome.offscreen.createDocument({
    url: "offscreen.html", reasons: ["BLOBS"], justification: "Create a download URL for zipped course files",
  });
  return reported;
}

// Closing the offscreen document also revokes its blob: URL.
async function cleanupZip() {
  await chrome.offscreen.closeDocument().catch(() => {});
  await idb("readwrite", (store) => store.delete("zip")).catch(() => {});
}

let zipDownloadId = null;
chrome.downloads.onChanged.addListener((d) => {
  if (d.id === zipDownloadId && ["complete", "interrupted"].includes(d.state?.current)) {
    zipDownloadId = null;
    cleanupZip();
  }
});

let downloadRunning = null;

function downloadFiles(mode) {
  downloadRunning ??= downloadFilesOnce(mode).finally(() => { downloadRunning = null; });
  return downloadRunning;
}

// Files already saved in an earlier zip, by name + type + size (canvas.js fileKey).
async function downloadedKeys() {
  const { downloadedKeys: keys = [] } = await chrome.storage.local.get("downloadedKeys");
  return new Set(keys);
}

// mode "new": only files not in an earlier zip; "all": everything. Copies of one file are
// zipped once either way.
async function downloadFilesOnce(mode = "new") {
  const { snapshot } = await chrome.storage.local.get("snapshot");
  if (!snapshot) return;
  const s = await settings();
  const saved = await downloadedKeys();
  const jobs = zipPlan(snapshot, s.courseVisibility, { dedupe: true, skipKeys: mode === "all" ? null : saved });
  const tab = await findCanvasTab(s.baseUrl);
  let done = 0;
  const failed = [];
  await setStatus({ downloading: { done, total: jobs.length }, downloadError: null });
  try {
    const results = await pool(jobs, 3, async ({ file, path }) => {
      const data = await fetchFileBytes(file.download_url, tab, file.content_type);
      if (!data) failed.push(path);
      await setStatus({ downloading: { done: ++done, total: jobs.length } });
      return data && { path, data, key: fileKey(file), date: file.updated_at ? new Date(file.updated_at) : new Date() };
    });
    const entries = results.filter(Boolean);
    if (failed.length) {
      const note = `These files couldn't be downloaded (locked, removed, or access denied):\n\n${failed.join("\n")}\n`;
      entries.push({ path: "_not_included.txt", data: new TextEncoder().encode(note) });
    }
    if (!entries.length) throw new Error("No files could be downloaded.");
    const blob = new Blob(buildZip(entries), { type: "application/zip" });
    const filename = `Canvas files ${new Date().toISOString().slice(0, 10)}.zip`;
    const url = await zipDownloadUrl(blob);
    zipDownloadId = await chrome.downloads.download({ url, filename, saveAs: false, conflictAction: "uniquify" });
    for (const e of entries) if (e.key) saved.add(e.key);
    await chrome.storage.local.set({ downloadedKeys: [...saved] });
    await setStatus({
      downloading: null,
      lastDownload: { done: entries.length - (failed.length ? 1 : 0), failed: failed.length, bytes: blob.size, filename, mode, at: new Date().toISOString() },
    });
  } catch (e) {
    await cleanupZip();
    await setStatus({ downloading: null, downloadError: String(e.message || e) });
  }
}

// Completes a connection started in the popup (see connect() there). Runs on
// permissions.onAdded because the popup is usually closed by the permission prompt.
async function finishConnect() {
  const { pendingConnect: origin, settings: stored = {} } = await chrome.storage.local.get(["pendingConnect", "settings"]);
  if (!origin || !(await chrome.permissions.contains({ origins: [`${origin}/*`] }))) return;
  const switched = stored.baseUrl && stored.baseUrl !== origin;
  await chrome.storage.local.set({ settings: { ...stored, baseUrl: origin, ...(switched ? { courseVisibility: {} } : {}) } });
  if (switched) await chrome.storage.local.remove("snapshot");
  await chrome.storage.local.remove("pendingConnect");
  await schedule();
  runSync("connect");
}

// (Re)creates the hourly alarm. `keep` leaves an existing alarm alone when its period is
// already right, so a browser restart doesn't push the next sync a full interval away.
async function schedule(keep = false) {
  const s = await settings();
  const period = Math.max(15, Number(s.intervalMinutes) || 60);
  const existing = await chrome.alarms.get("sync");
  if (keep && existing && existing.periodInMinutes === period) return;
  await chrome.alarms.clear("sync");
  chrome.alarms.create("sync", { periodInMinutes: period });
}

chrome.runtime.onInstalled.addListener(({ reason }) => {
  schedule();
  runSync("install");
  // First install: open the page that hands out the sync token.
  if (reason === "install") chrome.tabs.create({ url: `${HATCH_URL}/settings/sync` });
});
chrome.runtime.onStartup.addListener(async () => {
  await schedule(true);
  const { status = {} } = await chrome.storage.local.get("status");
  const s = await settings();
  const stale = !status.lastSync || Date.now() - Date.parse(status.lastSync) > s.intervalMinutes * 60000;
  if (s.baseUrl && stale) runSync("startup");
});
chrome.permissions.onAdded.addListener(async () => {
  await schedule();  // a Settings save can change the interval and close the popup mid-save
  await finishConnect();
  // A grant from the Settings form (popup closed mid-save) should also kick a sync.
  const s = await settings();
  if (s.baseUrl && !(await chrome.storage.local.get("snapshot")).snapshot) runSync("permission");
});
chrome.alarms.onAlarm.addListener((a) => a.name === "sync" && runSync("alarm"));

// Logging in (or landing on Canvas after being logged out) is a good moment to sync.
chrome.tabs.onUpdated.addListener(async (_id, info, tab) => {
  if (info.status !== "complete" || !tab.url) return;
  const s = await settings();
  if (!s.baseUrl || !tab.url.startsWith(new URL(s.baseUrl).origin)) return;
  const { status = {} } = await chrome.storage.local.get("status");
  if (status.state === "logged_out") runSync("canvas_tab");
});

// The Homework Hatch site (only the origins in the manifest's externally_connectable) asks
// whether the extension is installed and linked, hands it a sync token, or starts a sync. The
// server is always the site that sent the token, and nothing is ever sent back but status.
chrome.runtime.onMessageExternal.addListener((msg, sender, reply) => {
  const origin = sender.origin || (sender.url ? new URL(sender.url).origin : null);
  if (!origin) return;
  (async () => {
    let s = await settings();
    if (msg?.type === "link") {
      if (!/^hh_[\w-]{20,}$/.test(msg.token || "")) return reply({ error: "bad token" });
      s = { ...s, endpointUrl: origin, endpointToken: msg.token };
      await chrome.storage.local.set({ settings: s });
      if (s.baseUrl) runSync("link");
    } else if (msg?.type === "sync") {
      if (s.baseUrl && s.endpointToken && s.endpointUrl === origin) runSync("site");
    } else if (msg?.type !== "ping") {
      return reply({ error: "unknown message" });
    }
    const { status = {} } = await chrome.storage.local.get("status");
    reply({
      version: chrome.runtime.getManifest().version,
      linked: Boolean(s.endpointToken) && s.endpointUrl === origin,
      canvas: s.baseUrl || null,
      state: running ? "syncing" : status.state || null,
      lastSync: status.lastSync || null,
    });
  })();
  return true;
});

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg.type === "offscreen:ready") {
    offscreenWaiter?.(msg);
    return;
  }
  if (msg.type === "sync") {
    runSync("manual").then(() => reply({ ok: true }));
    return true;
  }
  if (msg.type === "connect") {
    finishConnect().then(() => reply({ ok: true }));
    return true;
  }
  if (msg.type === "download") {
    downloadFiles(msg.mode).then(() => reply({ ok: true }));
    return true;
  }
  if (msg.type === "reschedule") {
    schedule().then(() => reply({ ok: true }));
    return true;
  }
});
