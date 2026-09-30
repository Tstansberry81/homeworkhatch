// The real extension in a real Chrome (Chrome for Testing), clicking its real popup, against a
// mock Canvas and a live Homework Hatch server. Prints one JSON line of observations.
//
//   node extension_chrome.mjs <serverUrl> <username> <password> <chromePath> <extensionDir> [deckId]
//
// Chrome's permission prompt can't be automated, so the copy under test has 127.0.0.1
// pre-granted in host_permissions (the mock Canvas, the server and the storage emulator all
// listen there), the site's origin is added to externally_connectable, and the built-in
// server address points at the test server. Everything after "Connect" is the shipped code.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";
import puppeteer from "../../extension/node_modules/puppeteer-core/lib/esm/puppeteer/puppeteer-core.js";
import { startMockCanvas } from "../../extension/tests/mock-canvas.mjs";

const [serverUrl, username, password, chromePath, ext, deckId] = process.argv.slice(2);
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "hatch-chrome-"));
const downloads = path.join(tmp, "downloads");
fs.mkdirSync(downloads);
const canvas = await startMockCanvas();
const pdf = (id, name, size) => ({ id, display_name: name, "content-type": "application/pdf", size, folder_id: 5,
  updated_at: "2026-09-20T12:00:00Z", url: `https://x/files/${id}/download` });
// The same notes.pdf posted twice under a second Canvas id.
canvas.addFile("101", pdf(4, "notes.pdf", 1234));

fs.cpSync(new URL("../../extension", import.meta.url).pathname, ext, {
  recursive: true, filter: (p) => !p.includes("node_modules") && !p.includes(`${path.sep}tests`),
});
const manifest = JSON.parse(fs.readFileSync(path.join(ext, "manifest.json")));
manifest.host_permissions = ["http://127.0.0.1/*"];
manifest.externally_connectable = { matches: ["http://127.0.0.1/*"] };
fs.writeFileSync(path.join(ext, "manifest.json"), JSON.stringify(manifest));
const upload = path.join(ext, "upload.js");
fs.writeFileSync(upload, fs.readFileSync(upload, "utf8").replace("https://homeworkhatch.onrender.com", serverUrl));

const browser = await puppeteer.launch({
  executablePath: chromePath, headless: true, pipe: true, enableExtensions: [ext],
  args: ["--no-first-run", "--no-default-browser-check"],
});
const out = {};
try {
  const session = await browser.target().createCDPSession();
  await session.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: downloads });
  const sw = await browser.waitForTarget((t) => t.type() === "service_worker" && t.url().endsWith("background.js"));
  const extId = new URL(sw.url()).host;
  const worker = await sw.worker();
  worker.on("console", (m) => m.type() === "error" && console.error("[worker]", m.text()));

  // First install opens the page that hands out the sync token.
  const pages = await browser.pages();
  out.opened_setup_page = pages.some((p) => p.url().includes("/settings/sync")) ||
    Boolean(await browser.waitForTarget((t) => t.url().includes("/settings/sync"), { timeout: 5000 }).catch(() => null));

  await browser.setCookie({ name: "canvas_session", value: "valid", domain: "127.0.0.1", path: "/" });
  const tab = await browser.newPage();
  await tab.goto(`${canvas.url}/`);
  // End state of the Connect flow in the popup: the extension knows the student's Canvas.
  await worker.evaluate((base) => chrome.storage.local.set({ settings: { baseUrl: base } }), canvas.url);
  const popup = await browser.newPage();
  popup.on("console", (m) => m.type() === "error" && console.error("[popup]", m.text()));
  await popup.goto(`chrome-extension://${extId}/popup.html`);
  out.default_server = await popup.$eval("#endpointUrl", (i) => i.value);

  // The student opens Connect Canvas on the site while signed in: the page finds the
  // extension and links it, with no token copied anywhere.
  const site = await browser.newPage();
  await site.goto(`${serverUrl}/login`);
  await site.type("input[name=identifier]", username);
  await site.type("input[name=password]", password);
  await Promise.all([site.waitForNavigation(), site.click("form button")]);
  await site.goto(`${serverUrl}/settings/sync`);
  await site.waitForFunction(() => /Linked and syncing|Syncing from/.test(document.getElementById("ext-status").textContent), { timeout: 30000 });
  const linked = await worker.evaluate(async () => (await chrome.storage.local.get("settings")).settings);
  out.linked = { server: linked.endpointUrl, token_looks_right: /^hh_[\w-]{20,}$/.test(linked.endpointToken || "") };
  const storage = (key) => popup.evaluate((k) => chrome.storage.local.get(k).then((r) => r[k]), key);
  const waitFor = async (fn, what, ms = 60000) => {
    const end = Date.now() + ms;
    for (;;) {
      const v = await fn();
      if (v) return v;
      if (Date.now() > end) throw new Error(`Timed out waiting for ${what}; status=${JSON.stringify(await storage("status"))}`);
      await new Promise((r) => setTimeout(r, 250));
    }
  };
  const syncAndWait = async (label) => {
    const before = (await storage("status"))?.lastSync;
    await popup.reload();
    await popup.click("#sync");
    const s = await waitFor(async () => {
      const st = await storage("status");
      return st?.state && st.state !== "syncing" && st.lastSync && st.lastSync !== before ? st : null;
    }, label);
    return { state: s.state, error: s.error ?? null, pushError: s.pushError ?? null,
             uploaded: s.lastPush?.uploaded, skipped: s.lastPush?.skipped, failed: s.lastPush?.failed?.length };
  };
  const text = (sel) => popup.$eval(sel, (e) => (e.hidden ? null : e.textContent));
  const zipAfterClick = async (sel) => {
    const seen = new Set(fs.readdirSync(downloads));
    await popup.click(sel);
    const name = await waitFor(() => fs.readdirSync(downloads).find((f) => f.endsWith(".zip") && !seen.has(f)), "zip download");
    const file = path.join(downloads, name);
    await waitFor(() => { try { execFileSync("unzip", ["-tq", file]); return true; } catch { return false; } }, "zip complete");
    return execFileSync("unzip", ["-Z1", file]).toString().trim().split("\n").sort();
  };

  const first = await waitFor(async () => {
    const st = await storage("status");
    return st?.state && st.state !== "syncing" && st.lastSync ? st : null;
  }, "the sync started by linking");
  out.first = { state: first.state, error: first.error ?? null, pushError: first.pushError ?? null,
                uploaded: first.lastPush?.uploaded, skipped: first.lastPush?.skipped, failed: first.lastPush?.failed?.length };
  await site.reload();
  await site.waitForFunction(() => /last sync/.test(document.getElementById("ext-status").textContent), { timeout: 15000 });
  out.site_status = await site.$eval("#ext-status", (e) => e.textContent);
  await popup.bringToFront();  // background tabs don't render, so clicks there never land
  out.second = await syncAndWait("second sync");
  await popup.reload();
  out.popup = { summary: await text("#summary"), auto: await text("#auto"), download: await text("#download"),
                downloadAll: await text("#downloadAll") };
  out.alarm_minutes = await worker.evaluate(async () => (await chrome.alarms.get("sync"))?.periodInMinutes ?? null);

  out.zip1 = await zipAfterClick("#download");
  await popup.reload();
  out.after_zip = { download: await text("#download"), downloadAll: await text("#downloadAll"),
                    disabled: await popup.$eval("#download", (b) => b.disabled) };

  // A new file appears in Canvas: the next sync uploads just it, and the zip has just it.
  canvas.addFile("101", pdf(5, "week2.pdf", 4321));
  out.third = await syncAndWait("sync with a new file");
  await popup.reload();
  out.new_download_label = await text("#download");
  out.zip2 = await zipAfterClick("#download");
  await popup.reload();
  out.zip_all = await zipAfterClick("#downloadAll");

  // Flashcard study on the site: flip, arrows either side, x / n underneath.
  if (deckId) {
    await site.bringToFront();
    await site.goto(`${serverUrl}/study/decks/${deckId}/review`);
    const pos = () => site.$eval("#pos", (p) => p.textContent.trim());
    const flipped = () => site.$eval("#card", (c) => c.classList.contains("flipped"));
    const study = { start: await pos() };
    await site.click("#flip");
    study.flipped = await flipped();
    await site.click("#next");
    study.after_next = await pos();
    study.flipped_after_next = await flipped();
    await site.click("#prev");
    await site.click("#prev");
    study.wrapped = await pos();
    study.back_text = await site.$eval("#back", (b) => b.textContent.trim());
    out.study = study;
  }
} finally {
  await browser.close();
  canvas.close();
}
console.log(JSON.stringify(out));
