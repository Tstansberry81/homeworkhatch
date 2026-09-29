// Upload protocol between the extension and the Hatch storage server. Pure module (no
// chrome.* calls) so it can be tested in Node against the reference server.
//
//   1. POST /v1/snapshots          { snapshot, files: [manifest] }
//        -> { snapshot_id, files_needed: [file ids the server lacks at that version] }
//   2. PUT  /v1/files/:id?updated_at=…   raw bytes, one request per needed file
//   3. POST /v1/snapshots/:id/complete   { uploaded, failed }
//
// The server decides what it needs, so an hourly sync where nothing changed uploads
// only the JSON, and a single new PDF uploads only that PDF.

import { pool } from "./canvas.js";

async function call(fetchImpl, url, init, what) {
  const r = await fetchImpl(url, init);
  if (r.status === 401 || r.status === 403) throw new Error(`Server rejected the upload token (${what})`);
  if (!r.ok) throw new Error(`Server error ${r.status} on ${what}`);
  return r.json();
}

export async function uploadSnapshot({
  serverUrl, token, snapshot, plan, fetchBytes, fetchImpl = fetch, concurrency = 3, onProgress = () => {},
}) {
  const base = serverUrl.replace(/\/+$/, "");
  const auth = token ? { Authorization: `Bearer ${token}` } : {};

  const manifest = plan.map(({ file, path, course_id }) => ({
    id: file.id, updated_at: file.updated_at ?? null, size: file.size ?? null,
    name: file.name, content_type: file.content_type ?? null, course_id: course_id ?? null, path,
  }));
  const { snapshot_id, files_needed = [] } = await call(fetchImpl, `${base}/v1/snapshots`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...auth },
    body: JSON.stringify({ snapshot, files: manifest }),
  }, "snapshot");

  const byId = new Map(plan.map((j) => [j.file.id, j]));
  const needed = files_needed.map((id) => byId.get(String(id))).filter(Boolean);
  let done = 0;
  const failed = [];
  onProgress({ done, total: needed.length });

  await pool(needed, concurrency, async ({ file, path }) => {
    try {
      const data = await fetchBytes(file);
      if (!data) throw new Error("could not fetch from Canvas");
      const q = new URLSearchParams({ updated_at: file.updated_at ?? "" });
      await call(fetchImpl, `${base}/v1/files/${encodeURIComponent(file.id)}?${q}`, {
        method: "PUT",
        headers: {
          "Content-Type": file.content_type || "application/octet-stream",
          "X-Snapshot-Id": snapshot_id,
          ...auth,
        },
        body: data,
      }, `file ${file.id}`);
    } catch (e) {
      failed.push({ id: file.id, path, error: String(e.message || e) });
    }
    onProgress({ done: ++done, total: needed.length });
  });

  await call(fetchImpl, `${base}/v1/snapshots/${encodeURIComponent(snapshot_id)}/complete`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...auth },
    body: JSON.stringify({ uploaded: needed.length - failed.length, failed }),
  }, "complete");

  return { snapshot_id, uploaded: needed.length - failed.length, skipped: plan.length - needed.length, failed };
}
