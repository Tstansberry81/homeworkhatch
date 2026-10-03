// Study session page: the timer (Pomodoro-style blocks, Flowtime, or a plain stopwatch), the
// blank-page autosave and the problem shuffler. Blocks end softly (a short chime and "Keep
// going"), never with a hard stop: students in the research said buzzers break their focus.
// The timer keeps counting while the student is in Learn or Test and comes back.
(() => {
  const box = document.getElementById("timer");
  const $ = (id) => document.getElementById(id);
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";

  // ---------------------------------------------------------------- blank page autosave
  const page = $("page");
  let saveTimer = null;
  const saveNow = async () => {
    clearTimeout(saveTimer);
    saveTimer = null;
    try {
      await hh.post(page.dataset.save, { notes: page.value });
      $("saved").textContent = "Saved";
    } catch (e) { $("saved").textContent = `Not saved: ${e.message}`; }
  };
  if (page) {
    page.addEventListener("input", () => { $("saved").textContent = ""; clearTimeout(saveTimer); saveTimer = setTimeout(saveNow, 1200); });
    // Leaving mid-sentence: send what's there (a beacon survives the page going away).
    window.addEventListener("pagehide", () => {
      if (!saveTimer) return;
      const form = new FormData();
      form.append("csrf_token", csrf);
      form.append("notes", page.value);
      navigator.sendBeacon?.(page.dataset.save, form);
    });
  }

  // ---------------------------------------------------------------- problem shuffler
  const shuffleBtn = $("shuffle");
  if (shuffleBtn) {
    shuffleBtn.addEventListener("click", () => {
      const lines = $("problems").value.split("\n").map((l) => l.trim()).filter(Boolean);
      for (let i = lines.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [lines[i], lines[j]] = [lines[j], lines[i]]; }
      $("shuffled").replaceChildren(...lines.map((l) => { const li = document.createElement("li"); li.textContent = l; return li; }));
    });
  }

  if (!box) return;

  // ---------------------------------------------------------------- timer
  const mode = box.dataset.pacing;                // 25_5 | 50_10 | 12_3 | flow | none
  const work = Number(box.dataset.work) * 60000;  // 0 for flow / none
  const rest = Number(box.dataset.rest) * 60000;
  const planned = Number(box.dataset.planned) * 60000;
  const key = `hh_session_${box.dataset.start}`;
  const load = () => { try { return JSON.parse(localStorage.getItem(key) || "{}"); } catch { return {}; } };
  const st = { phase: "idle", studied: 0, blockStart: 0, restEnd: 0, blocks: 0, chimed: false, started: false, at: 0, ...load() };
  // Away in this session's Learn or Test with the timer running: that time was studying too, up to
  // the planned time. Left any other way (tab closed), the timer comes back paused, not credited.
  if (st.phase === "work" && st.at) {
    if (st.away) st.studied = Math.min(st.studied + Math.max(0, Date.now() - st.at), Math.max(st.studied, planned + 10 * 60000));
    else st.phase = "paused";
  }
  st.away = false;
  if (st.phase === "rest" && st.restEnd <= Date.now()) st.phase = "paused";
  const save = () => { st.at = Date.now(); try { localStorage.setItem(key, JSON.stringify(st)); } catch { /* private mode */ } };
  let last = Date.now();

  const fmt = (ms) => { const s = Math.max(0, Math.round(ms / 1000)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
  const show = (id, on) => { const el = $(id); if (el) el.hidden = !on; };
  const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  const overdue = () => Boolean(work) && st.studied - st.blockStart >= work;

  function chime() {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      [660, 880].forEach((f, i) => {
        const o = ctx.createOscillator(); const g = ctx.createGain();
        const t0 = ctx.currentTime + i * 0.35;
        o.type = "sine"; o.frequency.value = f;
        g.gain.setValueAtTime(0.0001, t0); g.gain.exponentialRampToValueAtTime(0.06, t0 + 0.05);
        g.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.9);
        o.connect(g).connect(ctx.destination); o.start(t0); o.stop(t0 + 1);
      });
    } catch { /* no audio: the visual cue is enough */ }
    if (!reduce) { box.classList.add("ping"); setTimeout(() => box.classList.remove("ping"), 1600); }
  }

  const flowBreak = (workedMs) => {  // Flowtime: longer work earns a longer break (Smits et al., 2025)
    const m = workedMs / 60000;
    return (m <= 25 ? 5 : m <= 50 ? 8 : 10) * 60000;
  };

  function buttons() {
    const p = st.phase;
    show("go", p !== "work");
    $("go").textContent = p === "rest" ? "Skip break" : p === "idle" ? "Start" : st.restEnd ? "Back to it" : "Resume";
    show("pause", p === "work");
    show("more", p === "work" && overdue());
    show("rest", p === "work" && mode !== "none" && (mode === "flow" || overdue()));
  }

  function render() {
    const inBlock = st.studied - st.blockStart;
    let face;
    let text;
    if (st.phase === "rest") {
      face = fmt(st.restEnd - Date.now());
      text = "Break. Stand up, drink some water.";
    } else {
      const left = work - inBlock;
      face = !work ? fmt(inBlock) : left >= 0 ? fmt(left) : `+${fmt(-left)}`;
      text = st.phase === "paused" ? (st.restEnd ? "Break's over. Ready when you are." : "Paused")
        : st.phase === "idle" ? "Ready when you are"
          : work ? (left >= 0 ? "Focus" : "Block done. Keep going or take a break.")
            : mode === "flow" ? (inBlock > 90 * 60000 ? "Ninety minutes in: a break would help." : "Take a break when your focus dips.")
              : "Studying";
    }
    $("clock").textContent = face;
    $("phase").textContent = text;
    $("bar").style.width = `${Math.min(100, (100 * st.studied) / Math.max(planned, 1))}%`;
    const mins = Math.floor(st.studied / 60000);
    $("elapsed").textContent = `${mins} min studied${planned ? ` of ${Math.round(planned / 60000)} planned` : ""}`;
    $("done-minutes").value = Math.max(Number($("done-minutes").value) || 0, mins);
    $("tiny").hidden = st.studied > 5 * 60000;
  }

  function tick() {
    const now = Date.now();
    if (st.phase === "work") {
      st.studied += now - last;
      if (work && !st.chimed && overdue()) { st.chimed = true; chime(); buttons(); }
    } else if (st.phase === "rest" && now >= st.restEnd) {
      st.phase = "paused"; chime(); buttons();
    }
    last = now;
    if (st.phase !== "idle") save();
    render();
  }

  async function begin() {
    if (!st.started) { st.started = true; try { await hh.post(box.dataset.start); } catch { /* offline: still study */ } }
    if (st.phase === "rest" || (st.phase === "paused" && st.restEnd)) {  // after (or instead of) a break: a new block
      st.blockStart = st.studied; st.chimed = false; st.restEnd = 0;
    }
    if (st.phase === "idle") st.blockStart = st.studied;
    st.phase = "work"; last = Date.now();
    buttons(); save();
  }

  $("go").addEventListener("click", begin);
  $("pause").addEventListener("click", () => { st.phase = "paused"; buttons(); save(); });
  $("more").addEventListener("click", () => {
    // Five more minutes in this block, then the soft cue again.
    st.blockStart = st.studied - (work - 5 * 60000); st.chimed = false; buttons(); save();
  });
  $("rest").addEventListener("click", () => {
    st.blocks += 1;
    const length = mode === "flow" ? flowBreak(st.studied - st.blockStart)
      : st.blocks % 4 === 0 ? Math.max(rest * 3, 15 * 60000) : rest;   // a longer break after four blocks
    st.phase = "rest"; st.restEnd = Date.now() + length; last = Date.now();
    buttons(); save();
  });

  // "I'm done": send the minutes and the blank page's last words with it, then forget the timer.
  $("done-form")?.addEventListener("submit", (e) => {
    const notes = e.target.querySelector('input[name="notes"]');
    if (notes && page) notes.value = page.value;
    clearTimeout(saveTimer); saveTimer = null;
    try { localStorage.removeItem(key); } catch { /* fine */ }
  });

  document.querySelectorAll('a[href*="/study/learn"], a[href*="/study/test"]').forEach((a) => a.addEventListener("click", () => {
    if (st.phase === "work") { st.away = true; save(); }
  }));

  buttons();
  render();
  setInterval(tick, 500);
})();
