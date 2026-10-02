// Learn mode: rounds of 7 cards. Each card is asked as multiple choice first, then typed;
// it's mastered when both are right. Misses come back later in the round. Answers are checked
// here in the browser, and each round's results go to the server in one POST (spaced repetition).
// Card HTML (th/dh) is sanitized on the server (study.render_markdown); everything else is text.
(() => {
  const root = document.getElementById("learn");
  const dataEl = document.getElementById("learn-data");
  if (!root || !dataEl) return;
  const data = JSON.parse(dataEl.textContent);
  const cards = data.cards;
  const $ = (sel) => root.querySelector(sel);
  const stage = $("#learn-stage");
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ------------------------------------------------------------ answer checking (mirrors services/learn.py)
  const SOFT = /[.,;:!?'"`()[\]{}…–—‘’“”«»¿¡$\\*_~#]/g;
  const NUM = /^([-+−]?)(\$?)(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(%?)$/;
  const normalize = (text, strict) => {
    let s = String(text ?? "").normalize("NFC");
    if (strict) return s.trim().split(/\s+/).filter(Boolean).join(" ").toLowerCase();
    s = s.normalize("NFKD").replace(/\p{M}/gu, "").toLowerCase();
    return s.replace(/(\d)\.(?=\d)/g, "$1\u0000").replace(SOFT, "").replace(/\u0000/g, ".").replace(/\s+/g, "");
  };
  const isNumber = (text) => NUM.test(String(text ?? "").trim());
  const oneEditApart = (a, b) => {
    if (a === b || Math.abs(a.length - b.length) > 1) return false;
    if (a.length > b.length) [a, b] = [b, a];
    let i = 0;
    while (i < a.length && a[i] === b[i]) i++;
    if (a.length !== b.length) return a.slice(i) === b.slice(i + 1);
    if (a.slice(i + 1) === b.slice(i + 1)) return true;
    return i + 1 < a.length && a[i] === b[i + 1] && a[i + 1] === b[i] && a.slice(i + 2) === b.slice(i + 2); // swapped pair
  };
  const check = (given, expected, strict) => {
    const g = normalize(given, strict), e = normalize(expected, strict);
    if (!g) return "wrong";
    if (g === e) return "right";
    if (!strict && e.length >= 4 && !isNumber(expected) && oneEditApart(g, e)) return "almost";
    return "wrong";
  };

  // ------------------------------------------------------------ options
  const prefs = { answerWith: data.answerWith === "term" ? "term" : "definition", strict: false, starredOnly: false };
  try { prefs.strict = localStorage.getItem("hh_learn_strict") === "1"; } catch { /* storage blocked */ }
  const optAnswer = root.querySelectorAll('input[name="answer_with"]');
  const optStrict = $("#opt-strict"), optStarred = $("#opt-starred");
  optAnswer.forEach((r) => { r.checked = r.value === prefs.answerWith; });
  if (optStrict) optStrict.checked = prefs.strict;
  optAnswer.forEach((r) => r.addEventListener("change", () => {
    if (r.checked) { prefs.answerWith = r.value; if (current && !current.answered) ask(); }
  }));
  optStrict?.addEventListener("change", () => {
    prefs.strict = optStrict.checked;
    try { localStorage.setItem("hh_learn_strict", prefs.strict ? "1" : "0"); } catch { /* fine */ }
  });
  optStarred?.addEventListener("change", () => {
    prefs.starredOnly = optStarred.checked;
    if (round) send();
    round = null;
    startRound();
  });

  // ------------------------------------------------------------ session state
  // stage 0: multiple choice next; 1: typed next; 2: mastered (this session).
  const state = cards.map(() => ({ stage: 0, seen: false }));
  let poolEnd = data.today; // cards[0..poolEnd) are today's; the rest is "keep going"
  let round = null, current = null, roundNo = 0;
  const unsent = [];

  const inPool = (i) => i < poolEnd && (!prefs.starredOnly || cards[i].s);
  const answerText = (i) => (prefs.answerWith === "term" ? cards[i].t : cards[i].d);
  const answerHtml = (i) => (prefs.answerWith === "term" ? cards[i].th : cards[i].dh);
  const promptHtml = (i) => (prefs.answerWith === "term" ? cards[i].dh : cards[i].th);

  function updateProgress() {
    let fresh = 0, learning = 0, mastered = 0;
    cards.forEach((c, i) => {
      if (!inPool(i)) return;
      if (state[i].stage >= 2) mastered++;
      else if (state[i].seen) learning++;
      else fresh++;
    });
    const total = fresh + learning + mastered || 1;
    $("#bar-new").style.width = `${(100 * fresh) / total}%`;
    $("#bar-learning").style.width = `${(100 * learning) / total}%`;
    $("#bar-mastered").style.width = `${(100 * mastered) / total}%`;
    $("#count-new").textContent = fresh;
    $("#count-learning").textContent = learning;
    $("#count-mastered").textContent = mastered;
    $("#learn-progress").setAttribute("aria-label",
      `${fresh} not studied, ${learning} learning, ${mastered} mastered`);
  }

  function shuffle(list) {
    for (let i = list.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [list[i], list[j]] = [list[j], list[i]];
    }
    return list;
  }

  // ------------------------------------------------------------ rounds
  function startRound() {
    updateProgress();
    const pool = cards.map((_, i) => i).filter((i) => inPool(i) && state[i].stage < 2);
    if (!pool.length) return finish();
    const items = pool.filter((i) => state[i].seen).concat(pool.filter((i) => !state[i].seen)).slice(0, data.roundSize);
    roundNo += 1;
    round = { items, queue: items.slice(), results: new Map(), missed: new Map(), sent: new Set() };
    nextQuestion();
  }

  function nextQuestion() {
    if (!round.queue.length) return endRound();
    const i = round.queue.shift();
    current = { i, answered: false };
    ask();
  }

  function choicesFor(i) {
    const tokens = prefs.answerWith === "term" ? cards[i].ot : cards[i].o;
    const wrong = (tokens || []).map((t) => (typeof t === "number"
      ? { html: prefs.answerWith === "term" ? cards[t].th : cards[t].dh, text: prefs.answerWith === "term" ? cards[t].t : cards[t].d }
      : { html: hh.escape(t), text: t }));
    return shuffle([{ html: answerHtml(i), text: answerText(i), right: true }, ...wrong]);
  }

  function starButton(i) {
    const on = cards[i].s;
    return `<button type="button" class="star-btn" data-star="${i}" aria-pressed="${on}" aria-label="${on ? "Unstar" : "Star"} this card">${on ? "★" : "☆"}</button>`;
  }

  function ask() {
    const i = current.i;
    const choices = state[i].stage === 0 ? choicesFor(i) : null;
    current.kind = choices && choices.length >= 2 ? "mc" : "typed";
    current.answered = false;
    const label = current.kind === "mc" ? "Multiple choice" : "Type the answer";
    let html = `<div class="card learn-q" tabindex="-1"><div class="learn-q-head"><span class="kicker">${label}${current.kind === "mc" ? " · keys 1–" + choices.length : ""}</span>${starButton(i)}</div>
      <div class="learn-prompt prose">${promptHtml(i)}</div>`;
    if (current.kind === "mc") {
      current.choices = choices;
      html += `<div class="mc-options" role="group" aria-label="Choices">${choices.map((c, n) =>
        `<button type="button" class="mc-option" data-choice="${n}"><span class="mc-key" aria-hidden="true">${n + 1}</span><span class="prose">${c.html}</span></button>`).join("")}</div>
        <div class="learn-actions"><button type="button" class="linklike small" data-dontknow>Don't know?</button></div>`;
    } else {
      html += `<form class="typed-form" autocomplete="off">
          <label class="sr-only" for="typed-answer">Your answer</label>
          <input type="text" id="typed-answer" autocapitalize="off" autocomplete="off" spellcheck="false" placeholder="Type the ${prefs.answerWith === "term" ? "term" : "definition"}">
          <div class="learn-actions"><button class="btn" type="submit">Answer</button><button type="button" class="linklike small" data-dontknow>Don't know?</button></div>
        </form>`;
    }
    html += `<div class="learn-feedback" id="learn-feedback"></div></div>`;
    stage.innerHTML = html;
    hh.renderMath(stage);
    if (current.kind === "typed") {
      const input = stage.querySelector("#typed-answer");
      input.focus();
      stage.querySelector(".typed-form").addEventListener("submit", (e) => {
        e.preventDefault();
        if (current.answered) return;
        if (!input.value.trim()) { input.focus(); return; }
        answerTyped(input.value);
      });
    } else {
      stage.querySelector(".learn-q").focus({ preventScroll: true }); // not an option: Enter mustn't pick one
    }
  }

  function answerMc(n) {
    if (current.answered) return;
    current.answered = true;
    const choice = current.choices[n];
    const right = !!choice?.right || normalize(choice?.text) === normalize(answerText(current.i));
    stage.querySelectorAll(".mc-option").forEach((b, k) => {
      b.disabled = true;
      if (current.choices[k].right) b.classList.add("correct");
      else if (k === n) b.classList.add("wrong");
    });
    feedback(right ? "right" : "wrong", { mc: true });
  }

  function answerTyped(given) {
    current.answered = true;
    const input = stage.querySelector("#typed-answer");
    input.readOnly = true;
    stage.querySelectorAll(".typed-form button").forEach((b) => { b.disabled = true; });
    feedback(check(given, answerText(current.i), prefs.strict), { given });
  }

  function dontKnow() {
    if (current.answered) return;
    current.answered = true;
    stage.querySelectorAll(".mc-option, .typed-form button").forEach((b) => { b.disabled = true; });
    stage.querySelectorAll(".mc-option").forEach((b, k) => { if (current.choices?.[k]?.right) b.classList.add("correct"); });
    feedback("wrong", { mc: current.kind === "mc", dontKnow: true });
  }

  function feedback(verdict, { mc = false, given = "", dontKnow: skipped = false } = {}) {
    const box = stage.querySelector("#learn-feedback");
    const i = current.i;
    const answer = `<div class="learn-answer"><div class="kicker">Correct answer</div><div class="prose">${answerHtml(i)}</div></div>`;
    if (verdict === "right") {
      box.innerHTML = `<p class="fb fb-right" role="status">Correct!</p>
        <div class="learn-actions"><button type="button" class="btn" data-primary data-continue>Continue</button></div>`;
      current.result = { correct: true, almost: false };
    } else if (verdict === "almost") {
      box.innerHTML = `<p class="fb fb-almost" role="status">Almost: one letter off. Count it?</p>${answer}
        <div class="learn-actions"><button type="button" class="btn" data-primary data-accept>Count it</button>
        <button type="button" class="btn btn-ghost" data-reject>No, I was wrong</button></div>`;
      current.result = null;
    } else {
      const yours = given ? `<div class="learn-answer yours"><div class="kicker">You said</div><div class="given"></div></div>` : "";
      box.innerHTML = `<p class="fb fb-wrong" role="status">${skipped ? "No problem: here it is." : "Not quite."} You'll see this one again.</p>${mc ? "" : answer}${yours}
        <div class="learn-actions"><button type="button" class="btn" data-primary data-continue>Continue</button>
        ${skipped || mc ? "" : `<button type="button" class="btn btn-ghost" data-override>I was right</button>`}</div>`;
      if (given) box.querySelector(".given").textContent = given;
      current.result = { correct: false, almost: false };
    }
    hh.renderMath(box);
    box.querySelector("[data-primary]").focus({ preventScroll: true });
    if (verdict === "right" && mc && !reduceMotion) {
      const token = current;
      setTimeout(() => { if (current === token && !token.done) commit(); }, 900);
    }
  }

  function commit(result = current.result) {
    if (current.done) return;
    current.done = true;
    const i = current.i;
    state[i].seen = true;
    if (!round.results.has(i)) round.results.set(i, { card_id: cards[i].id, correct: result.correct, almost: !!result.almost });
    if (result.correct) {
      state[i].stage = current.kind === "mc" ? Math.max(state[i].stage, 1) : 2;
    } else {
      const misses = (round.missed.get(i) || 0) + 1;
      round.missed.set(i, misses);
      if (misses === 1) round.queue.push(i); // comes back later in this round
    }
    updateProgress();
    nextQuestion();
  }

  async function send(keepalive = false) {
    if (round) {
      round.results.forEach((r, i) => { if (!round.sent.has(i)) { round.sent.add(i); unsent.push(r); } });
    }
    if (!unsent.length) return;
    const batch = unsent.splice(0);
    if (keepalive) {
      fetch(data.urls.answers, { method: "POST", keepalive: true, body: JSON.stringify({ answers: batch }),
        headers: { "Content-Type": "application/json", "X-CSRFToken": hh.csrf, Accept: "application/json" } }).catch(() => {});
      return;
    }
    try {
      const res = await hh.post(data.urls.answers, { answers: batch });
      if (res.coins) note("+3 Buddy Coins for studying today.", "success");
    } catch {
      unsent.unshift(...batch);
      note("Couldn't save your progress just now; we'll try again after the next round.", "warning");
    }
  }

  function note(text, kind) {
    const el = $("#learn-note");
    el.className = `flash ${kind}`;
    el.textContent = text;
    el.hidden = false;
  }

  function endRound() {
    send();
    const rows = round.items.map((i) => {
      const r = round.results.get(i);
      const mark = state[i].stage >= 2 ? "Mastered" : r && r.correct ? "Getting there" : "Still learning";
      return `<li><div class="grow"><div class="prose">${cards[i].th}</div><div class="small muted prose">${cards[i].dh}</div></div>
        <span class="badge ${state[i].stage >= 2 ? "ok" : r && r.correct ? "info" : "warn"}">${mark}</span></li>`;
    }).join("");
    const left = cards.filter((_, i) => inPool(i) && state[i].stage < 2).length;
    stage.innerHTML = `<div class="card learn-q"><p class="kicker">Round ${roundNo} done</p>
      <h2>${left ? `${left} card${left === 1 ? "" : "s"} to go` : "That's everything for now"}</h2>
      <ul class="list">${rows}</ul>
      <div class="learn-actions"><button type="button" class="btn" data-primary data-next-round>${left ? "Next round" : "Finish"}</button></div></div>`;
    hh.renderMath(stage);
    stage.querySelector("[data-primary]").focus({ preventScroll: true });
  }

  function finish() {
    round = null;
    current = null;
    updateProgress();
    const studied = cards.filter((_, i) => inPool(i)).length;
    const more = cards.length - poolEnd;
    let body;
    if (!studied && prefs.starredOnly) {
      body = `<h2>No starred cards ${poolEnd < cards.length ? "due today" : "here"}</h2>
        <p class="muted">Tap ☆ on a question to star a card, or turn off "Starred only".</p>`;
    } else if (!studied) {
      body = `<h2>You're all caught up</h2><p class="muted">Nothing is due today. Your next review is already scheduled, so this is a bonus.</p>`;
    } else {
      body = `<h2>Nice work!</h2><p class="muted">You've mastered ${studied} card${studied === 1 ? "" : "s"} this session. Spaced repetition will bring them back right before you'd forget.</p>`;
    }
    const keepGoing = more > 0
      ? `<button type="button" class="btn${studied ? " btn-ghost" : ""}" data-primary data-keep-going>Keep going: ${more} more card${more === 1 ? "" : "s"}</button>` : "";
    const after = document.getElementById("learn-after")?.innerHTML || "";
    stage.innerHTML = `<div class="card learn-q center">${body}<div class="learn-actions center-row">${keepGoing}${after}</div></div>`;
    (stage.querySelector("[data-primary]") || stage.querySelector("a, button"))?.focus({ preventScroll: true });
  }

  // ------------------------------------------------------------ events
  stage.addEventListener("click", (e) => {
    const t = e.target.closest("button");
    if (!t) return;
    if (t.dataset.choice !== undefined) answerMc(Number(t.dataset.choice));
    else if (t.hasAttribute("data-dontknow")) dontKnow();
    else if (t.hasAttribute("data-continue")) commit();
    else if (t.hasAttribute("data-accept")) commit({ correct: true, almost: true });
    else if (t.hasAttribute("data-reject")) commit({ correct: false });
    else if (t.hasAttribute("data-override")) commit({ correct: true, almost: true });
    else if (t.hasAttribute("data-next-round")) startRound();
    else if (t.hasAttribute("data-keep-going")) { poolEnd = cards.length; startRound(); }
    else if (t.dataset.star !== undefined) toggleStar(Number(t.dataset.star), t);
  });

  async function toggleStar(i, btn) {
    const want = !cards[i].s;
    cards[i].s = want;
    btn.textContent = want ? "★" : "☆";
    btn.setAttribute("aria-pressed", String(want));
    btn.setAttribute("aria-label", `${want ? "Unstar" : "Star"} this card`);
    try {
      await hh.post(data.urls.star.replace("/0/", `/${cards[i].id}/`), { starred: want });
    } catch {
      cards[i].s = !want;
      btn.textContent = want ? "☆" : "★";
      btn.setAttribute("aria-pressed", String(!want));
    }
  }

  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const typing = e.target.closest("input, textarea, select");
    if (current && !current.answered && current.kind === "mc" && !typing && /^[1-9]$/.test(e.key)) {
      const n = Number(e.key) - 1;
      if (n < current.choices.length) { e.preventDefault(); answerMc(n); }
      return;
    }
    if (e.key === "Enter" && !typing && !e.target.closest("button, a")) {
      const primary = stage.querySelector("[data-primary]");
      if (primary) { e.preventDefault(); primary.click(); }
    }
  });

  window.addEventListener("pagehide", () => send(true));
  window.addEventListener("load", () => hh.renderMath(stage)); // KaTeX loads deferred, after this script

  // ------------------------------------------------------------ go
  if (poolEnd === 0) finish();
  else startRound();
})();
