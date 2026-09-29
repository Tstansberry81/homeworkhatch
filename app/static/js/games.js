// Arcade games. Each game gets a stage element and calls finish(score) once.
(() => {
  const cfg = window.HH_GAME;
  const stage = document.getElementById("stage");
  const msg = document.getElementById("msg");
  let token = null;

  async function finish(score) {
    try {
      const r = await hh.post(cfg.scoreUrl, { token, score });
      msg.innerHTML = `Score: <strong>${r.score}</strong> · Your best: <strong>${r.best}</strong>`;
    } catch (e) {
      msg.textContent = e.message;
    }
    const again = document.createElement("button");
    again.className = "btn mt";
    again.textContent = "Play again";
    again.onclick = () => location.reload();
    stage.appendChild(again);
  }

  document.getElementById("start").addEventListener("click", async () => {
    try {
      const r = await hh.post(cfg.startUrl);
      token = r.token;
      document.getElementById("balance").textContent = r.balance;
    } catch (e) {
      msg.textContent = e.message;
      return;
    }
    stage.innerHTML = "";
    msg.textContent = "";
    ({ "math-sprint": mathSprint, snake, memory })[cfg.slug](stage, finish);
  });

  // ---------------------------------------------------------------- Math Sprint
  function mathSprint(el, done) {
    let score = 0, left = 60, answer = 0;
    el.innerHTML = `<div class="timer" id="t">60s</div><div style="font-size:2.2rem;font-weight:700" id="q"></div>
      <input id="a" type="number" inputmode="numeric" style="max-width:180px;text-align:center;font-size:1.4rem" autocomplete="off">
      <div class="muted">Score: <strong id="s">0</strong> · Enter to submit</div>`;
    const q = el.querySelector("#q"), a = el.querySelector("#a");
    const next = () => {
      const level = Math.min(4, 1 + Math.floor(score / 8));
      const ops = ["+", "−", "×", "÷"].slice(0, Math.min(4, level + 1));
      const op = ops[Math.floor(Math.random() * ops.length)];
      let x = 2 + Math.floor(Math.random() * (8 * level)), y = 2 + Math.floor(Math.random() * (6 * level));
      if (op === "+") answer = x + y;
      if (op === "−") { if (y > x) [x, y] = [y, x]; answer = x - y; }
      if (op === "×") { x = 2 + (x % 12); y = 2 + (y % 12); answer = x * y; }
      if (op === "÷") { y = 2 + (y % 11); answer = 2 + (x % 12); x = answer * y; }
      q.textContent = `${x} ${op} ${y} = ?`;
      a.value = "";
    };
    a.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      if (Number(a.value) === answer) { score += 1; el.querySelector("#s").textContent = score; }
      else { a.style.outline = "2px solid var(--err)"; setTimeout(() => (a.style.outline = ""), 250); }
      next();
    });
    next(); a.focus();
    const timer = setInterval(() => {
      left -= 1; el.querySelector("#t").textContent = `${left}s`;
      if (left <= 0) { clearInterval(timer); a.disabled = true; done(score * 10); }
    }, 1000);
  }

  // ---------------------------------------------------------------- Snake
  function snake(el, done) {
    const size = 20, cells = 20;
    el.innerHTML = `<canvas width="${size * cells}" height="${size * cells}"></canvas><div class="muted small">Arrow keys / WASD, or swipe. Score: <strong id="s">0</strong></div>`;
    const cv = el.querySelector("canvas"), ctx = cv.getContext("2d");
    const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#dd5a12";
    let body = [{ x: 10, y: 10 }], dir = { x: 1, y: 0 }, nextDir = dir, food = spot(), score = 0, over = false;
    function spot() {
      let p;
      do { p = { x: Math.floor(Math.random() * cells), y: Math.floor(Math.random() * cells) }; }
      while (body?.some((b) => b.x === p.x && b.y === p.y));
      return p;
    }
    const turn = (d) => { if (d.x !== -dir.x || d.y !== -dir.y) nextDir = d; };
    const keys = { ArrowUp: [0, -1], w: [0, -1], ArrowDown: [0, 1], s: [0, 1], ArrowLeft: [-1, 0], a: [-1, 0], ArrowRight: [1, 0], d: [1, 0] };
    document.addEventListener("keydown", (e) => { const k = keys[e.key]; if (k) { e.preventDefault(); turn({ x: k[0], y: k[1] }); } });
    let sx, sy;
    cv.addEventListener("touchstart", (e) => { sx = e.touches[0].clientX; sy = e.touches[0].clientY; }, { passive: true });
    cv.addEventListener("touchend", (e) => {
      const dx = e.changedTouches[0].clientX - sx, dy = e.changedTouches[0].clientY - sy;
      if (Math.abs(dx) > Math.abs(dy)) turn({ x: Math.sign(dx), y: 0 }); else turn({ x: 0, y: Math.sign(dy) });
    });
    const loop = setInterval(() => {
      if (over) return;
      dir = nextDir;
      const head = { x: body[0].x + dir.x, y: body[0].y + dir.y };
      if (head.x < 0 || head.y < 0 || head.x >= cells || head.y >= cells || body.some((b) => b.x === head.x && b.y === head.y)) {
        over = true; clearInterval(loop); done(score); return;
      }
      body.unshift(head);
      if (head.x === food.x && head.y === food.y) { score += 10; el.querySelector("#s").textContent = score; food = spot(); }
      else body.pop();
      ctx.clearRect(0, 0, cv.width, cv.height);
      ctx.fillStyle = "#25913f"; ctx.fillRect(food.x * size + 3, food.y * size + 3, size - 6, size - 6);
      ctx.fillStyle = accent; body.forEach((b) => ctx.fillRect(b.x * size + 1, b.y * size + 1, size - 2, size - 2));
    }, 110);
  }

  // ---------------------------------------------------------------- Memory Match
  function memory(el, done) {
    const icons = ["📐", "🧪", "📚", "🧮", "🌍", "🎨", "🔭", "🧬"];
    const deck = [...icons, ...icons].sort(() => Math.random() - 0.5);
    let open = [], matched = 0, moves = 0, lock = false;
    el.innerHTML = `<div class="memory-grid"></div><div class="muted small">Moves: <strong id="m">0</strong></div>`;
    const grid = el.querySelector(".memory-grid");
    deck.forEach((icon, i) => {
      const b = document.createElement("button");
      b.dataset.i = i; b.textContent = "❔";
      b.onclick = () => {
        if (lock || b.classList.contains("up") || b.classList.contains("matched")) return;
        b.classList.add("up"); b.textContent = icon; open.push(b);
        if (open.length < 2) return;
        moves += 1; el.querySelector("#m").textContent = moves;
        const [x, y] = open; open = [];
        if (deck[x.dataset.i] === deck[y.dataset.i]) {
          x.classList.add("matched"); y.classList.add("matched"); matched += 1;
          if (matched === icons.length) done(Math.max(100, 1000 - (moves - icons.length) * 40));
        } else {
          lock = true;
          setTimeout(() => { [x, y].forEach((c) => { c.classList.remove("up"); c.textContent = "❔"; }); lock = false; }, 700);
        }
      };
      grid.appendChild(b);
    });
  }
})();
