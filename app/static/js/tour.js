// The walkthrough (services/tour.py). Each step highlights one thing on one page and explains it; the
// student can still use the page underneath. A step on another page waits behind a "Continue the tour"
// pill, so they can wander. Setup steps check themselves off (polling /tour/status), and the one the
// server can't see has an "I've added it" button. Config: <script type="application/json" id="hh-tour">.
(() => {
  const data = document.getElementById("hh-tour");
  if (!data || !window.hh) return;
  let cfg;
  try { cfg = JSON.parse(data.textContent); } catch { return; }
  const steps = cfg.steps || [];
  if (!steps.length) return;
  const PAUSED = "hh_tour_paused";
  const narrow = () => matchMedia("(max-width: 900px)").matches;
  // The explanation docks at the bottom on phones and on short screens (a phone held sideways).
  const sheet = () => matchMedia("(max-width: 640px), (max-height: 520px)").matches;
  const CHECK_HINT = {
    school: "Not connected yet. Follow the highlighted card; this checks itself off.",
    files: "Not chosen yet. Tick the classes to keep, then Save.",
    google: "Not connected yet (this one's optional).",
    phone: "Copy the link, add it in your calendar app, then tap “I've added it”.",
  };
  let i = Math.max(0, steps.findIndex((s) => s.id === cfg.current));
  let spot = null, pop = null, pill = null, target = null, poll = null, frame = 0, busy = false, opener = null;
  let layout = "";  // which breakpoint the step was drawn for

  const here = (s) => (s.prefix ? location.pathname.startsWith(s.path) : location.pathname === s.path);
  const save = (url, body) => hh.post(url, body).catch(() => null);
  const store = (fn) => { try { return fn(sessionStorage); } catch { return null; } };
  const header = () => { const bar = document.querySelector(".mobilebar"); return bar && visible(bar) ? bar.offsetHeight : 0; };

  function visible(node) {
    return node && node.getClientRects().length > 0 && getComputedStyle(node).visibility !== "hidden";
  }
  function find(selectors) {
    for (const sel of (selectors || "").split(",").map((x) => x.trim()).filter(Boolean)) {
      for (const node of document.querySelectorAll(sel)) if (visible(node)) return node;
    }
    return null;
  }
  // The step's target and text: on narrow screens the phone target first, then the other. Whichever is
  // actually on screen decides the wording (the sync page swaps cards by touch, not width).
  function pick(step) {
    const order = narrow() ? [[step.phone_target, true], [step.target, false]] : [[step.target, false], [step.phone_target, true]];
    for (const [sel, phone] of order) {
      const node = find(sel);
      if (node) return { node, text: phone ? step.phone_body : step.body };
    }
    return { node: null, text: narrow() ? step.phone_body : step.body };
  }
  function make(tag, cls, html) {
    const node = document.createElement(tag);
    node.className = cls;
    if (html != null) node.innerHTML = html;
    node.addEventListener("click", (e) => e.stopPropagation());  // app.js closes the menu on outside clicks
    document.body.appendChild(node);
    return node;
  }
  function clear() {
    spot?.remove(); pop?.remove(); pill?.remove();
    spot = pop = pill = target = null;
    clearInterval(poll);
    document.body.classList.remove("tour-room");
    document.body.style.removeProperty("--tour-room");
  }
  function refocus() {
    const back = opener && opener.isConnected && opener !== document.body ? opener : document.querySelector("main h1, main");
    if (back) { if (!back.matches("a, button, input, select, textarea, [tabindex]")) back.setAttribute("tabindex", "-1"); back.focus({ preventScroll: true }); }
  }

  // Scroll the target into the space the tour leaves free: below the phone header, and above the sheet
  // (phones) or with room for the explanation under it (wider screens).
  function reveal() {
    if (!target || !pop) return;
    const r = target.getBoundingClientRect(), top = header() + 12, h = pop.offsetHeight;
    if (sheet()) { window.scrollBy({ top: r.top - top, behavior: "instant" }); return; }
    const room = r.height + 14 + h + 8;  // the target, then the explanation below it
    if (room > innerHeight - top) {      // too tall for both: the target's top, the explanation over its lower part
      window.scrollBy({ top: r.top - top, behavior: "instant" });
      return;
    }
    const slack = innerHeight - top - room;
    const want = top + Math.min(slack / 3, 80);  // a little above the middle of the free space
    if (r.top < top || r.top + room > innerHeight) window.scrollBy({ top: r.top - want, behavior: "instant" });
  }

  function place() {
    frame = 0;
    if (!pop) return;
    if ((narrow() ? "n" : "w") + (sheet() ? "s" : "") !== layout) { show(); return; }  // crossed a breakpoint: redraw
    if (!target || !spot) return;
    const r = target.getBoundingClientRect(), pad = 6;
    Object.assign(spot.style, { top: `${r.top - pad}px`, left: `${r.left - pad}px`, width: `${r.width + pad * 2}px`, height: `${r.height + pad * 2}px` });
    if (sheet()) return;  // the explanation sits at the bottom of the screen
    const w = pop.offsetWidth, h = pop.offsetHeight, gap = 14, edge = 8;
    let top, left;
    if (r.bottom + gap + h <= innerHeight - edge) { top = r.bottom + gap; left = r.left; }           // below
    else if (r.top - gap - h >= edge) { top = r.top - gap - h; left = r.left; }                       // above
    else if (r.right + gap + w <= innerWidth - edge) { top = r.top; left = r.right + gap; }           // right
    else if (r.left - gap - w >= edge) { top = r.top; left = r.left - gap - w; }                      // left
    else { top = innerHeight - h - edge; left = r.left; }                                             // over it
    // Always keep the buttons on screen, even after scrolling the target away.
    top = Math.min(Math.max(edge, top), innerHeight - h - edge);
    left = Math.min(Math.max(edge, left), innerWidth - w - edge);
    Object.assign(pop.style, { top: `${top}px`, left: `${left}px` });
  }
  const replace = () => { if (!frame) frame = requestAnimationFrame(place); };
  addEventListener("resize", replace);
  addEventListener("scroll", replace, true);

  function checkLine(step, done) {
    const line = pop?.querySelector(".tour-check");
    if (!line) return;
    line.textContent = done ? "✓ Done" : CHECK_HINT[step.check] || "";
    line.classList.toggle("ok", !!done);
    const next = pop.querySelector("[data-tour=next]");
    if (next && done) {
      next.classList.remove("btn-ghost");
      next.textContent = i === steps.length - 1 ? "Finish" : "Next";
    }
  }
  function watch(step) {
    clearInterval(poll);
    if (!step.check || step.done) return;
    poll = setInterval(async () => {
      if (document.hidden || !pop) return;
      const s = await hh.get(cfg.urls.status).catch(() => null);
      if (s && s[step.check]) { step.done = true; checkLine(step, true); clearInterval(poll); }
    }, 4000);
  }

  function show() {
    if (!pop && !pill) opener = document.activeElement;
    clear();
    store((s) => s.removeItem(PAUSED));
    const step = steps[i];
    const found = pick(step);
    target = found.node;
    layout = (narrow() ? "n" : "w") + (sheet() ? "s" : "");
    if (target) {
      spot = make("div", "tour-spot");
      spot.setAttribute("aria-hidden", "true");
    }
    const last = i === steps.length - 1;
    const pending = step.check && !step.done;
    pop = make("div", `tour-pop${target ? "" : " tour-center"}${target && sheet() ? " tour-sheet" : ""}`, `
      <p class="tour-count">Step ${i + 1} of ${steps.length}</p>
      <h2 id="tour-title" tabindex="-1"></h2>
      <p class="tour-body"></p>
      ${step.check ? '<p class="tour-check small" role="status"></p>' : ""}
      <div class="btn-row tour-actions">
        ${i ? '<button type="button" class="btn btn-ghost btn-sm" data-tour="back">Back</button>' : ""}
        ${step.mark && !step.done ? '<button type="button" class="btn btn-ghost btn-sm" data-tour="mark">I\'ve added it</button>' : ""}
        <button type="button" class="btn btn-sm${pending ? " btn-ghost" : ""}" data-tour="next">${last ? "Finish" : !i ? "Start the tour" : pending ? "Skip for now" : "Next"}</button>
      </div>
      <p class="tour-foot small"><button type="button" class="linklike" data-tour="pause">Hide for now</button> · <button type="button" class="linklike" data-tour="end">End the tour</button></p>`);
    pop.setAttribute("role", "dialog");
    pop.setAttribute("aria-labelledby", "tour-title");
    pop.querySelector("h2").textContent = step.title;
    pop.querySelector(".tour-body").textContent = found.text;
    checkLine(step, step.done);
    pop.addEventListener("click", act);
    if (target) {  // room under the page, so a target near the end can scroll clear of the explanation
      document.body.classList.add("tour-room");
      document.body.style.setProperty("--tour-room", `${pop.offsetHeight + 24}px`);
    }
    reveal();
    place();
    pop.querySelector("h2").focus({ preventScroll: true });
    watch(step);
  }

  function showPill(focus = false) {
    clear();
    const step = steps[i];
    pill = make("div", "tour-pill", `<button type="button" class="tour-pill-go">Tour · step ${i + 1} of ${steps.length}: <strong></strong> →</button>
      <button type="button" class="tour-pill-x" aria-label="Hide the tour">×</button>`);
    pill.setAttribute("role", "region");
    pill.setAttribute("aria-label", "Walkthrough");
    pill.querySelector("strong").textContent = step.title;
    pill.querySelector(".tour-pill-go").addEventListener("click", () => {
      store((s) => s.removeItem(PAUSED));
      if (here(step)) show(); else location.href = step.url;
    });
    pill.querySelector(".tour-pill-x").addEventListener("click", () => {
      store((s) => s.setItem(PAUSED, "hidden"));
      clear();
      refocus();
    });
    if (focus) pill.querySelector(".tour-pill-go").focus({ preventScroll: true });
  }

  function toast(text) {
    const t = make("div", "tour-toast", "");
    t.setAttribute("role", "status");
    t.textContent = text;
    setTimeout(() => t.remove(), 6000);
  }

  async function go(n) {
    if (busy || n < 0) return;
    busy = true;  // one step at a time: a double click or a held arrow key doesn't skip steps
    pop?.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    if (n >= steps.length) {
      await save(cfg.urls.end, {});
      clear();
      refocus();
      toast("Tour finished. You can restart it any time from your profile.");
      busy = false;
      return;
    }
    await save(cfg.urls.step, { step: steps[n].id });
    i = n;
    if (here(steps[i])) { show(); busy = false; } else location.href = steps[i].url;
  }

  async function act(e) {
    const button = e.target.closest("[data-tour]");
    const what = button?.dataset.tour;
    if (!what || button.disabled) return;
    if (what === "next") go(i + 1);
    else if (what === "back") go(i - 1);
    else if (what === "pause") { store((s) => s.setItem(PAUSED, steps[i].id)); showPill(true); }
    else if (what === "end") {
      busy = true;
      await save(cfg.urls.end, {});
      clear();
      refocus();
      toast("Tour ended. You can restart it any time from your profile.");
    } else if (what === "mark") {
      const step = steps[i];
      button.disabled = true;
      await save(cfg.urls.mark, { key: step.mark });
      step.done = true;
      button.remove();
      checkLine(step, true);
      pop?.querySelector("[data-tour=next]")?.focus({ preventScroll: true });
    }
  }

  document.addEventListener("keydown", (e) => {
    if (!pop || e.altKey || e.ctrlKey || e.metaKey || e.target.closest("input, textarea, select, [contenteditable]")) return;
    if (e.key === "Escape") { store((s) => s.setItem(PAUSED, steps[i].id)); showPill(true); }
    else if (e.repeat) return;
    else if (e.key === "ArrowRight" && pop.contains(document.activeElement)) go(i + 1);
    else if (e.key === "ArrowLeft" && pop.contains(document.activeElement)) go(i - 1);
  });

  // A fresh start (after /welcome or "Restart the tour") forgets an earlier "hide the tour".
  const url = new URL(location.href);
  if (url.searchParams.get("tour") === "start") {
    store((s) => s.removeItem(PAUSED));
    url.searchParams.delete("tour");
    history.replaceState(history.state, "", url.pathname + url.search + url.hash);
  }
  const paused = store((s) => s.getItem(PAUSED));
  if (paused === "hidden") return;
  if (here(steps[i]) && paused !== steps[i].id) show();
  else showPill();
})();
