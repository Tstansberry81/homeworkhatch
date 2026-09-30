// Shared helpers for every page.
(function () {
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";

  // fetch() that sends the CSRF token and JSON by default.
  window.hh = {
    csrf,
    async post(url, body = {}) {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf, Accept: "application/json" },
        body: JSON.stringify(body),
      });
      let data = {};
      try { data = await res.json(); } catch { /* empty body */ }
      if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
      return data;
    },
    async get(url) {
      const res = await fetch(url, { headers: { Accept: "application/json" } });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
      return data;
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

  // Render any math already on the page once KaTeX loads.
  window.addEventListener("load", () => document.querySelectorAll("[data-math]").forEach((el) => hh.renderMath(el)));
})();
