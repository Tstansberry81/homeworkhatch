// Study session page: the timer (Pomodoro-style blocks, Flowtime, or a plain stopwatch), the
// blank-page autosave and the problem shuffler. Blocks end softly (a short chime and "Keep
// going"), never with a hard stop: students in the research said buzzers break their focus.
(() => {
  const box = document.getElementById("timer");
  const $ = (id) => document.getElementById(id);

  // ---------------------------------------------------------------- blank page autosave
  const page = $("page");
  if (page) {
    let t = null;
    const save = async () => {
      try {
        await hh.post(page.dataset.save, { notes: page.value });
        $("saved").textContent = "Saved";
      } catch (e) { $("saved").textContent = `Not saved: ${e.message}`; }
    };
    page.addEventListener("input", () => { $("saved").textContent = ""; clearTimeout(t); t = setTimeout(save, 1200); });
  }

  // ---------------------------------------------------------------- problem shuffler
  const shuffleBtn = $("shuffle");
  if (shuffleBtn) {
    shuffleBtn.addEventListener("click", () => {
      const lines = $("problems").value.split("\n").map((l) => l.trim()).filter(Boolean);
      for (let i = lines.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [lines[i], lines[j]] = [lines[j], lines[i]]; }
      const out = $("shuffled");
      out.replaceChildren(...lines.map((l) => { const li = document.createElement("li"); li.textContent = l; return li; }));
    });
  }

  if (!box) return;

  // ---------------------------------------------------------------- timer
  const mode = box.dataset.pacing;                // 25_5 | 50_10 | 12_3 | flow | none
  const work = Number(box.dataset.work) * 60000;  // 0 for flow / none
  const rest = Number(box.dataset.rest) * 60000;
  const planned = Number(box.dataset.planned) * 60000;
  const key = `hh_session_${box.dataset.start}`;
  const store = { get() { try { return JSON.parse(localStorage.getItem(key) || "{}"); } catch { return {}; } },
                  set(v) { try { localStorage.setItem(key, JSON.stringify(v)); } catch { /* private mode */ } } };

  let phase = "idle";   // idle | work | rest | paused
  let studied = store.get().studied || 0;  // ms of work time
  let blockStart = 0;   // when the current work block started (ms of `studied`)
  let restEnd = 0;
  let blocks = 0;
  let last = 0;
  let chimed = false;
  let started = false;

  const fmt = (ms) => { const s = Math.max(0, Math.round(ms / 1000)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
  const show = (id, on) => { const el = $(id); if (el) el.hidden = !on; };
  const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

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

  function flowBreak(workedMs) {  // Flowtime: longer work earns a longer break (Smits et al., 2025)
    const m = workedMs / 60000;
    return (m <= 25 ? 5 : m <= 50 ? 8 : 10) * 60000;
  }

  function render() {
    const inBlock = studied - blockStart;
    let face = "", text = "";
    if (phase === "rest") {
      face = fmt(restEnd - Date.now());
      text = "Break. Stand up, drink some water.";
    } else if (work) {
      const left = work - inBlock;
      face = left >= 0 ? fmt(left) : `+${fmt(-left)}`;
      text = phase === "paused" ? (restEnd ? "Break's over. Ready when you are." : "Paused") : phase === "idle" ? "Ready when you are"
        : left >= 0 ? "Focus" : "Block done. Keep going or take a break.";
    } else {
      face = fmt(inBlock);
      text = phase === "paused" ? "Paused" : phase === "idle" ? "Ready when you are"
        : mode === "flow" ? (inBlock > 90 * 60000 ? "Ninety minutes in: a break would help." : "Take a break when your focus dips.") : "Studying";
    }
    $("clock").textContent = face;
    $("phase").textContent = text;
    $("bar").style.width = `${Math.min(100, (100 * studied) / Math.max(planned, 1))}%`;
    const mins = Math.floor(studied / 60000);
    $("elapsed").textContent = `${mins} min studied${planned ? ` of ${Math.round(planned / 60000)} planned` : ""}`;
    $("done-minutes").value = Math.max(Number($("done-minutes").value) || 0, mins);
    $("tiny").hidden = studied > 5 * 60000;
  }

  function tick() {
    const now = Date.now();
    if (phase === "work") {
      studied += now - last;
      if (work && !chimed && studied - blockStart >= work) {
        chimed = true; chime();
        show("more", true); show("rest", true);
      }
      store.set({ studied });
    } else if (phase === "rest" && now >= restEnd) {
      phase = "paused"; chime();
      $("go").textContent = "Back to it"; show("go", true); show("pause", false); show("rest", false);
    }
    last = now;
    render();
  }

  async function begin() {
    if (!started) { started = true; try { await hh.post(box.dataset.start); } catch { /* offline: still study */ } }
    if (phase === "rest" || (phase === "paused" && restEnd)) { blockStart = studied; chimed = false; restEnd = 0; }
    if (phase === "idle") { blockStart = studied; }
    phase = "work"; last = Date.now();
    show("go", false); show("pause", true);
    show("rest", mode === "flow"); show("more", false);
  }

  $("go").addEventListener("click", begin);
  $("pause").addEventListener("click", () => {
    phase = "paused"; $("go").textContent = "Resume"; show("go", true); show("pause", false);
  });
  $("more").addEventListener("click", () => {
    // Five more minutes in this block, then the soft cue again.
    blockStart = studied - (work - 5 * 60000); chimed = false; show("more", false); show("rest", false);
  });
  $("rest").addEventListener("click", () => {
    blocks += 1;
    const length = mode === "flow" ? flowBreak(studied - blockStart)
      : blocks % 4 === 0 ? Math.max(rest * 3, 15 * 60000) : rest;   // a longer break after four blocks
    phase = "rest"; restEnd = Date.now() + length; last = Date.now();
    show("rest", false); show("more", false); show("pause", false); show("go", false);
  });
  if (mode === "none") $("rest").remove();

  document.getElementById("done-form")?.addEventListener("submit", () => { store.set({}); });
  render();
  setInterval(tick, 500);
})();
