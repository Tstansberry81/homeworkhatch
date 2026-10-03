// Shared helpers for every page.
(function () {
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const SIGNED_OUT = "You were signed out — sign in again and retry.";

  // Every JSON endpoint answers with JSON. A signed-out request is redirected to the login page
  // and fetch follows that to a 200 HTML page: that's a failure, never a silent success.
  async function readJson(res) {
    const isJson = /\bjson\b/i.test(res.headers.get("Content-Type") || "");
    let data = {};
    if (isJson) data = await res.json().catch(() => ({}));
    const toLogin = res.redirected && /^\/login(\/|$)/.test(new URL(res.url, location.href).pathname);
    if (toLogin || (res.ok && !isJson)) throw Object.assign(new Error(SIGNED_OUT), { signedOut: true });
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
    return data;
  }

  // fetch() that sends the CSRF token and JSON by default.
  window.hh = {
    csrf,
    async post(url, body = {}) {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf, Accept: "application/json" },
        body: JSON.stringify(body),
      });
      return readJson(res);
    },
    async get(url) {
      return readJson(await fetch(url, { headers: { Accept: "application/json" } }));
    },
    escape(s) {
      return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    },
    renderMath(el) {
      if (window.renderMathInElement && el) {
        window.renderMathInElement(el, {
          delimiters: [{ left: "$$", right: "$$", display: true }, { left: "$", right: "$", display: false }],
          throwOnError: false,
        });
      }
    },
  };

  // Fill hidden timezone fields with the browser's zone.
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  document.querySelectorAll("input[data-timezone]").forEach((el) => { if (!el.value) el.value = tz; });
  document.querySelectorAll("select[data-timezone]").forEach((el) => {
    if (el.dataset.current === "UTC" || !el.dataset.current) {
      const opt = [...el.options].find((o) => o.value === tz);
      if (opt) el.value = tz;
    }
  });

  // Mobile sidebar.
  document.querySelectorAll("[data-toggle-sidebar]").forEach((btn) =>
    btn.addEventListener("click", (e) => { e.stopPropagation(); document.getElementById("sidebar")?.classList.toggle("open"); }));
  document.addEventListener("click", (e) => {  // tap outside the open menu closes it
    const rail = document.getElementById("sidebar");
    if (rail?.classList.contains("open") && !rail.contains(e.target)) rail.classList.remove("open");
  });

  // Copy-to-clipboard buttons: <button data-copy="#target"> or data-copy-text="...".
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-copy], [data-copy-text]");
    if (!btn) return;
    const text = btn.dataset.copyText ?? document.querySelector(btn.dataset.copy)?.innerText ?? "";
    try {
      await navigator.clipboard.writeText(text.trim());
      const old = btn.textContent;
      btn.textContent = "Copied!";
      setTimeout(() => (btn.textContent = old), 1400);
    } catch { /* clipboard blocked */ }
  });

  // Confirm dangerous submits: <form data-confirm="Are you sure?">. Long-running posts can
  // show a busy note and disable their button: <form data-busy="Working…">.
  document.addEventListener("submit", (e) => {
    const msg = e.target.dataset.confirm;
    if (msg && !window.confirm(msg)) { e.preventDefault(); return; }
    const busy = e.target.dataset.busy;
    if (busy) {
      // A disabled button is left out of the submitted form, so keep the clicked button's
      // value (e.g. a flashcard rating) in a hidden field before disabling everything.
      const s = e.submitter;
      if (s && s.name) {
        const keep = Object.assign(document.createElement("input"), { type: "hidden", name: s.name, value: s.value });
        e.target.appendChild(keep);
      }
      e.target.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      const note = document.createElement("p");
      note.className = "help center";
      note.textContent = busy;
      e.target.appendChild(note);
    }
  });

  // A set pasted on the public /free-learn page before signing up waits in sessionStorage
  // ({text, ts, uid}) until the import page picks it up; until then it's offered on every page.
  // It belongs to whoever pasted it, which matters on a shared school computer: it's dropped
  // after 30 minutes, on any signed-out page other than the way into an account (signing out
  // lands on one, so it clears), and when a different student is signed in on the same tab.
  const PENDING_KEY = "hh_import_text", PENDING_TTL = 30 * 60 * 1000;
  const PENDING_PUBLIC = /^\/(free-learn|login|register|age)\/?$/;
  const uid = document.body.dataset.uid || "";
  hh.pendingImport = {
    save(text) {
      try { sessionStorage.setItem(PENDING_KEY, JSON.stringify({ text, ts: Date.now() })); } catch { /* storage blocked */ }
    },
    clear() {
      try { sessionStorage.removeItem(PENDING_KEY); } catch { /* storage blocked */ }
    },
    get() {  // the saved text while it's still this visitor's; otherwise it's removed
      let raw = null, item = null;
      try { raw = sessionStorage.getItem(PENDING_KEY); } catch { return null; }
      if (raw === null) return null;
      try { item = JSON.parse(raw); } catch { /* an old plain-text entry: dropped */ }
      const age = Date.now() - Number(item?.ts);
      const ok = typeof item?.text === "string" && item.text.trim() && age >= 0 && age < PENDING_TTL
        && (uid ? !item.uid || String(item.uid) === uid : PENDING_PUBLIC.test(location.pathname));
      if (!ok) { this.clear(); return null; }
      if (uid && !item.uid) {  // the first student signed in with it owns it
        item.uid = uid;
        try { sessionStorage.setItem(PENDING_KEY, JSON.stringify(item)); } catch { /* fine */ }
      }
      return item.text;
    },
  };
  const pendingText = hh.pendingImport.get();  // runs on every page, so the rules above apply
  const pending = document.getElementById("pending-import");
  if (pending) {
    if (pendingText) pending.hidden = false;
    pending.querySelector("[data-dismiss-import]")?.addEventListener("click", () => {
      hh.pendingImport.clear();
      pending.hidden = true;
    });
  }

  // Render any math already on the page once KaTeX loads.
  window.addEventListener("load", () => document.querySelectorAll("[data-math]").forEach((el) => hh.renderMath(el)));
})();
