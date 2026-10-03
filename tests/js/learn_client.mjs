// Runs the real browser scripts under node for tests/test_learn.py. Input is JSON on stdin,
// output one JSON line on stdout.
//   node learn_client.mjs answers  {marks, vectors: [[given, expected, strict], ...]} -> verdicts
//   node learn_client.mjs app      {} -> what app.js's hh.post and hh.pendingImport do
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const js = (name) => join(here, "..", "..", "app", "static", "js", name);
const input = JSON.parse(readFileSync(0, "utf8") || "{}");
const mode = process.argv[2];

if (mode === "answers") {
  const { checker } = createRequire(import.meta.url)(js("answers.js"));
  const { check } = checker(input.marks);
  console.log(JSON.stringify(input.vectors.map(([given, expected, strict]) => check(given, expected, !!strict))));
} else if (mode === "app") {
  const source = readFileSync(js("app.js"), "utf8");
  const storage = new Map();
  const sessionStorage = {
    getItem: (k) => (storage.has(k) ? storage.get(k) : null),
    setItem: (k, v) => storage.set(k, String(v)),
    removeItem: (k) => storage.delete(k),
  };
  // One page load in the same tab: a fresh window, the same sessionStorage.
  const page = (path, uid = "", fetchImpl = null) => {
    const sandbox = {
      document: {
        body: { dataset: uid ? { uid } : {} },
        querySelector: () => null, querySelectorAll: () => [], getElementById: () => null, addEventListener() {},
      },
      location: { pathname: path, href: `https://hatch.test${path}` },
      sessionStorage, URL, fetch: fetchImpl, console,
      addEventListener() {},
    };
    sandbox.window = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(source, sandbox);
    return sandbox.hh;
  };
  const stored = () => (storage.has("hh_import_text") ? JSON.parse(storage.get("hh_import_text")) : null);
  const out = {};

  // Student A pastes on /free-learn, goes through sign-in, never imports, signs out (lands on /).
  page("/free-learn").pendingImport.save("A's set");
  out.saved = stored();
  out.onLogin = page("/login").pendingImport.get();
  out.signedInA = page("/study/", "7").pendingImport.get();
  out.boundTo = stored()?.uid ?? null;
  out.afterLogout = page("/").pendingImport.get();
  out.storageAfterLogout = stored();

  // The same tab, a different student signed in: never offered, and removed.
  page("/free-learn").pendingImport.save("A's set");
  page("/dashboard", "7").pendingImport.get();
  out.otherStudent = page("/dashboard", "8").pendingImport.get();
  out.storageAfterOther = stored();

  // Too old, a page outside sign-up, and an entry from before {text, ts}: all dropped.
  storage.set("hh_import_text", JSON.stringify({ text: "old", ts: Date.now() - 31 * 60 * 1000 }));
  out.expired = page("/free-learn").pendingImport.get();
  page("/free-learn").pendingImport.save("x");
  out.onTerms = page("/terms").pendingImport.get();
  storage.set("hh_import_text", "plain text from an older version");
  out.legacy = page("/study/", "7").pendingImport.get();
  page("/register").pendingImport.save("kept");
  out.onRegister = page("/register").pendingImport.get();
  out.onAge = page("/age", "9").pendingImport.get();

  // hh.post: a redirect to the login page (or any non-JSON 200) is a failure, not "saved".
  const reply = ({ status = 200, type = "application/json", body = "{}", redirectedTo = "" }) => async () => ({
    ok: status >= 200 && status < 300, status, redirected: !!redirectedTo,
    url: redirectedTo || "https://hatch.test/study/learn/answers",
    headers: { get: (h) => (h.toLowerCase() === "content-type" ? type : null) },
    json: async () => JSON.parse(body),
  });
  const attempt = async (spec) => {
    try {
      return { ok: true, data: await page("/study/", "7", reply(spec)).post("/x", {}) };
    } catch (err) {
      return { ok: false, message: err.message, signedOut: !!err.signedOut };
    }
  };
  out.post = {
    toLogin: await attempt({ type: "text/html; charset=utf-8", body: "<html>", redirectedTo: "https://hatch.test/login?next=%2Fx" }),
    html200: await attempt({ type: "text/html", body: "<html>" }),
    json200: await attempt({ body: '{"ok": true}' }),
    error400: await attempt({ status: 400, body: '{"error": "Send JSON."}' }),
    error500: await attempt({ status: 500, type: "text/html", body: "<html>" }),
  };
  console.log(JSON.stringify(out));
} else {
  console.error("usage: node learn_client.mjs answers|app < input.json");
  process.exit(2);
}
