// Upload protocol between the extension and the Hatch storage server. Pure module (no
// chrome.* calls) so it can be tested in Node against the reference server.
//
//   1. POST /v1/snapshots          { snapshot, files: [manifest] }
//        -> { snapshot_id, files_needed: [file ids the server lacks at that version],
//             upload_urls: { id: { url, headers } } }   (only when storage takes direct uploads)
//   2. Per needed file, either
//        PUT  upload_urls[id].url with exactly upload_urls[id].headers, then
//        POST /v1/files/:id/uploaded?updated_at=…   (the server checks the object arrived)
//      or, without an upload URL or if the direct upload fails,
//        PUT  /v1/files/:id?updated_at=…   raw bytes through the server
//   3. POST /v1/snapshots/:id/complete   { uploaded, failed }
//
// Direct uploads keep the server (a small web instance) out of the byte path; it reads each
// file's text afterwards in the background.
//
// The server decides what it needs, so an hourly sync where nothing changed uploads
// only the JSON, and a single new PDF uploads only that PDF.

import { pool } from "./canvas.js";

// The hosted Homework Hatch, the default server. Self-hosters change it under Settings.
export const HATCH_URL = "https://homeworkhatch.onrender.com";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Network drops and 5xx answers (a server restart, a proxy hiccup) are retried a few times
// before a file is given up on until the next sync.
async function call(fetchImpl, url, init, what, retryDelayMs = 1000) {
  for (let attempt = 0; ; attempt++) {
    let r;
    try {
      r = await fetchImpl(url, init);
    } catch (e) {
      if (attempt < 2) { await sleep(retryDelayMs * 2 ** attempt); continue; }
      throw new Error(`Couldn't reach the server (${what}): ${e.message || e}`);
    }
    if (r.status >= 500 && attempt < 2) { await sleep(retryDelayMs * 2 ** attempt); continue; }
    if (r.status === 401 || r.status === 403) throw new Error(`Server rejected the upload token (${what})`);
    if (!r.ok) throw new Error(`Server error ${r.status} on ${what}`);
    return r.json();
  }
}

export async function uploadSnapshot({
  serverUrl, token, snapshot, plan, fetchBytes, fetchImpl = fetch, concurrency = 4, onProgress = () => {}, retryDelayMs = 500,
}) {
  const base = serverUrl.replace(/\/+$/, "");
  const auth = token ? { Authorization: `Bearer ${token}` } : {};

  const manifest = plan.map(({ file, path, course_id }) => ({
    id: file.id, updated_at: file.updated_at ?? null, size: file.size ?? null,
    name: file.name, content_type: file.content_type ?? null, course_id: course_id ?? null, path,
  }));
  const { snapshot_id, files_needed = [], upload_urls: targets = {} } = await call(fetchImpl, `${base}/v1/snapshots`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...auth },
    body: JSON.stringify({ snapshot, files: manifest }),
  }, "snapshot", retryDelayMs);

  const byId = new Map(plan.map((j) => [j.file.id, j]));
  const needed = files_needed.map((id) => byId.get(String(id))).filter(Boolean);
  const skipped = plan.length - needed.length;  // already on the server
  let done = 0;
  const failed = [];
  onProgress({ done, total: needed.length, skipped });

  await pool(needed, concurrency, async ({ file, path }) => {
    try {
      const data = await fetchBytes(file);
      if (!data) throw new Error("could not fetch from Canvas");
      const q = new URLSearchParams({ updated_at: file.updated_at ?? "" });
      const fileUrl = `${base}/v1/files/${encodeURIComponent(file.id)}`;
      let stored = false;
      const target = targets[file.id];
      // Storage sometimes resets one of several large uploads sharing an HTTP/2 connection;
      // retrying fixes that, and uploading through the server is the last resort.
      for (let attempt = 0; target?.url && !stored && attempt < 3; attempt++) {
        if (attempt) await new Promise((r) => setTimeout(r, retryDelayMs * attempt));
        try {
          stored = (await fetchImpl(target.url, { method: "PUT", headers: target.headers || {}, body: data })).ok;
        } catch {}
      }
      if (stored) {
        await call(fetchImpl, `${fileUrl}/uploaded?${q}`, {
          method: "POST", headers: { "X-Snapshot-Id": snapshot_id, ...auth },
        }, `file ${file.id}`, retryDelayMs);
      } else {
        await call(fetchImpl, `${fileUrl}?${q}`, {
          method: "PUT",
          headers: {
            "Content-Type": file.content_type || "application/octet-stream",
            "X-Snapshot-Id": snapshot_id,
            ...auth,
          },
          body: data,
        }, `file ${file.id}`, retryDelayMs);
      }
    } catch (e) {
      failed.push({ id: file.id, path, error: String(e.message || e) });
    }
    onProgress({ done: ++done, total: needed.length, skipped });
  });

  await call(fetchImpl, `${base}/v1/snapshots/${encodeURIComponent(snapshot_id)}/complete`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...auth },
    body: JSON.stringify({ uploaded: needed.length - failed.length, failed }),
  }, "complete", retryDelayMs);

  return { snapshot_id, uploaded: needed.length - failed.length, skipped, failed };
}
