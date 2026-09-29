// Runs the real extension sync + upload code against a mock Canvas and a live
// Homework Hatch server. Usage: node extension_roundtrip.mjs <serverUrl> <token>
import { startMockCanvas } from "../../extension/tests/mock-canvas.mjs";
import { syncCanvas, zipPlan } from "../../extension/canvas.js";
import { uploadSnapshot } from "../../extension/upload.js";

const [serverUrl, token] = process.argv.slice(2);
const canvas = await startMockCanvas();
const get = async (url) => {
  const r = await fetch(url, { headers: { Cookie: "canvas_session=valid" } });
  return { status: r.status, link: r.headers.get("link"), text: await r.text() };
};
const fetchBytes = async (file) => {
  const r = await fetch(file.download_url);
  return r.ok ? new Uint8Array(await r.arrayBuffer()) : null;
};
try {
  const snapshot = await syncCanvas({ baseUrl: canvas.url, get, now: canvas.NOW });
  const plan = zipPlan(snapshot);
  const first = await uploadSnapshot({ serverUrl, token, snapshot, plan, fetchBytes });
  const second = await uploadSnapshot({ serverUrl, token, snapshot, plan, fetchBytes });
  console.log(JSON.stringify({ planned: plan.length, first, second, courses: snapshot.courses.length }));
} finally {
  canvas.close();
}
