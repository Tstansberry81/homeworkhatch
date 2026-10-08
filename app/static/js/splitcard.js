// Brain Grade's share card (templates/courses/_brain_grade.html): a 1080x1350 image drawn here in
// the browser, so nothing is uploaded. By default it shows only how much of the grade went to
// missing and late work; the student can add their grades. Never the class, school or teacher.
(() => {
  const btn = document.querySelector("[data-brain-share]");
  const dialog = document.getElementById("brain-dialog");
  if (!btn || !dialog) return;
  const canvas = document.getElementById("brain-canvas");
  const letters = document.getElementById("brain-letters");
  const d = btn.dataset;
  const num = (v) => (v === "" || v == null ? null : Number(v));
  const tests = num(d.tests), grade = num(d.canvas), lost = num(d.lost) || 0, late = num(d.late) || 0;
  // "Not missing knowledge" only when there's a real test grade and no test was skipped.
  const knowledge = d.status === "ok" && !Number(d.missedTests);
  const INK = "#141311", PAPER = "#f6efe0", YOLK = "#ffc629", MINT = "#3ddc8a", MUTED = "#a69f91";

  function wrap(ctx, text, x, y, width, lineHeight) {
    const words = text.split(" ");
    let line = "";
    for (const w of words) {
      const next = line ? `${line} ${w}` : w;
      if (ctx.measureText(next).width > width && line) { ctx.fillText(line, x, y); line = w; y += lineHeight; } else line = next;
    }
    ctx.fillText(line, x, y);
    return y + lineHeight;
  }

  function bar(ctx, y, label, value, letter, color) {
    ctx.fillStyle = MUTED; ctx.font = "600 34px 'Geist Mono', monospace";
    ctx.fillText(label.toUpperCase(), 90, y);
    ctx.fillStyle = "#2a2721"; ctx.fillRect(90, y + 24, 900, 44);
    ctx.fillStyle = color; ctx.fillRect(90, y + 24, Math.max(0, Math.min(1, value / 100)) * 900, 44);
    ctx.fillStyle = PAPER; ctx.font = "800 64px 'Bricolage Grotesque', sans-serif";
    if (!letter) { ctx.fillText(`${value.toFixed(1)}%`, 90, y + 140); return; }  // no class letter to trust
    ctx.fillText(letter, 90, y + 140);
    const after = 90 + ctx.measureText(letter).width + 24;
    ctx.fillStyle = MUTED; ctx.font = "500 40px 'Geist', sans-serif";
    ctx.fillText(`${value.toFixed(1)}%`, after, y + 140);
  }

  function draw() {
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = INK; ctx.fillRect(0, 0, 1080, 1350);
    ctx.fillStyle = "#ffffff10";
    for (let x = 30; x < 1080; x += 44) for (let y = 30; y < 1350; y += 44) ctx.fillRect(x, y, 4, 4);
    ctx.fillStyle = YOLK; ctx.font = "700 36px 'Geist Mono', monospace";
    ctx.fillText("BRAIN GRADE", 90, 150);
    ctx.fillStyle = PAPER;
    let y = 290;
    if (lost > 0.05) {
      ctx.font = "800 190px 'Bricolage Grotesque', sans-serif";
      ctx.fillStyle = YOLK; ctx.fillText(`−${lost.toFixed(1)}%`, 80, y + 60);
      ctx.fillStyle = PAPER; ctx.font = "700 58px 'Bricolage Grotesque', sans-serif";
      const what = late > 0.05 ? "missing and late work" : "missing work";
      y = wrap(ctx, knowledge ? `of my grade is ${what}, not missing knowledge.` : `of my grade went to ${what}.`, 90, y + 170, 900, 72);
    } else {
      ctx.font = "800 120px 'Bricolage Grotesque', sans-serif";
      ctx.fillStyle = MINT; ctx.fillText("0% lost", 80, y + 40);
      ctx.fillStyle = PAPER; ctx.font = "700 58px 'Bricolage Grotesque', sans-serif";
      const matches = knowledge && tests != null && grade != null && Math.abs(tests - grade) < 2;
      y = wrap(ctx, matches ? "to missing work. My grade is my knowledge." : "to missing work.", 90, y + 150, 900, 72);
    }
    if (letters.checked) {
      y += 50;
      if (tests != null) { bar(ctx, y, "My tests", tests, "", YOLK); y += 210; }
      if (grade != null && !d.estimate) bar(ctx, y, "My grade", grade, d.canvasLetter, "#7b9bff");
    } else {
      ctx.fillStyle = MUTED; ctx.font = "500 42px 'Geist', sans-serif";
      wrap(ctx, "Every grade is two grades: what you know, and what you turned in.", 90, Math.max(y + 60, 900), 900, 56);
    }
    ctx.fillStyle = YOLK; ctx.fillRect(90, 1190, 60, 8);
    ctx.fillStyle = PAPER; ctx.font = "800 46px 'Bricolage Grotesque', sans-serif";
    ctx.fillText("homeworkhatch.com", 90, 1260);
    const said = [lost > 0.05 ? `${lost.toFixed(1)} percent of my grade went to missing work` : "nothing lost to missing work"];
    if (letters.checked && tests != null) said.push(`tests ${tests.toFixed(1)} percent`);
    if (letters.checked && grade != null && !d.estimate) said.push(`grade ${grade.toFixed(1)} percent`);
    canvas.setAttribute("aria-label", `Brain Grade image: ${said.join(", ")}.`);
  }

  const blob = () => new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
  btn.addEventListener("click", async () => {
    await document.fonts?.ready;
    draw();
    dialog.showModal();
  });
  letters.addEventListener("change", draw);
  document.getElementById("brain-send").addEventListener("click", async () => {
    const file = new File([await blob()], "brain-grade.png", { type: "image/png" });
    if (navigator.canShare?.({ files: [file] })) {
      try { await navigator.share({ files: [file], text: "My Brain Grade, from homeworkhatch.com" }); } catch { /* cancelled */ }
    } else document.getElementById("brain-save").click();
  });
  document.getElementById("brain-save").addEventListener("click", async () => {
    const url = URL.createObjectURL(await blob());
    const a = Object.assign(document.createElement("a"), { href: url, download: "brain-grade.png" });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  });
})();
