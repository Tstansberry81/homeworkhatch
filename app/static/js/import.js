// Live preview of pasted Quizlet / Anki / CSV cards, for the import page and the public
// /free-learn page. Card text is shown with textContent only (it's whatever the student pasted).
(() => {
  const form = document.querySelector("[data-import]");
  if (!form) return;
  const text = form.querySelector("[name=text]");
  const out = document.getElementById("import-preview");
  const termSel = form.querySelector("[name=term_sep]"), cardSel = form.querySelector("[name=card_sep]");
  const termCustom = form.querySelector("[name=term_sep_custom]"), cardCustom = form.querySelector("[name=card_sep_custom]");
  const KEY = "hh_import_text";

  // Synthetic examples only (never real course material).
  const EXAMPLES = {
    quizlet: "el gato\tthe cat\nla manzana\tthe apple\nel río\tthe river\nla biblioteca\tthe library\nescribir\tto write\n(also: to spell)",
    anki: "#separator:tab\n#html:true\n#tags column:3\nThe <b>glimmerase</b> enzyme\tturns starlight into sugar in moon-ferns\tbio::enzymes\n" +
      "{{c1::Floraxin}} is made in the {{c2::petal vault::organelle}}\t\tbio::cells",
    csv: "term,definition\n\"zorbic acid\",\"a made-up acid, pH 2\"\n\"quill cell\",\"stores ink in the imaginary squid\"",
  };

  const syncCustom = () => {
    if (termCustom) termCustom.hidden = termSel?.value !== "custom";
    if (cardCustom) cardCustom.hidden = cardSel?.value !== "custom";
  };

  const el = (tag, cls, txt) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (txt !== undefined) node.textContent = txt;
    return node;
  };

  function render(d) {
    out.replaceChildren();
    const head = el("p", "import-summary");
    if (!d.count) {
      head.append(el("strong", "", "No cards found yet."));
      head.append(" ", d.detected === "empty" ? "Paste your set above." :
        "Try picking the separators by hand, or check one of the examples.");
    } else {
      head.append(el("strong", "", `${d.count.toLocaleString()} card${d.count === 1 ? "" : "s"}`), ` · ${d.label}`);
    }
    out.append(head);
    if (d.skipped_count) {
      const det = el("details", "small");
      det.append(el("summary", "", `${d.skipped_count} line${d.skipped_count === 1 ? "" : "s"} we couldn't use`));
      const ul = el("ul", "skipped-lines");
      d.skipped.forEach((s) => ul.append(el("li", "", s)));
      det.append(ul);
      out.append(det);
    }
    if (d.cards.length) {
      const wrap = el("div", "table-wrap");
      const table = el("table", "table import-table");
      const thead = el("thead");
      const tr = el("tr");
      tr.append(el("th", "", "Term"), el("th", "", "Definition"));
      thead.append(tr);
      const body = el("tbody");
      d.cards.forEach((c) => {
        const row = el("tr");
        row.append(el("td", "", c.front), el("td", "", c.back));
        body.append(row);
      });
      table.append(thead, body);
      wrap.append(table);
      out.append(wrap);
      if (d.count > d.cards.length) out.append(el("p", "small muted", `…and ${(d.count - d.cards.length).toLocaleString()} more.`));
    }
  }

  let timer = null, seq = 0;
  async function preview() {
    if (!text.value.trim()) { render({ count: 0, cards: [], skipped: [], skipped_count: 0, detected: "empty" }); return; }
    const mine = ++seq;
    const body = { text: text.value, term_sep: termSel?.value || "auto", term_sep_custom: termCustom?.value || "",
      card_sep: cardSel?.value || "auto", card_sep_custom: cardCustom?.value || "" };
    try {
      const data = await hh.post(form.dataset.previewUrl, body);
      if (mine === seq) render(data);
    } catch (err) {
      if (mine !== seq) return;
      out.replaceChildren(el("p", "flash warning", err.message || "Couldn't preview that."));
    }
  }
  const later = () => { clearTimeout(timer); timer = setTimeout(preview, 300); };

  text.addEventListener("input", later);
  [termSel, cardSel].forEach((s) => s?.addEventListener("change", () => { syncCustom(); preview(); }));
  [termCustom, cardCustom].forEach((s) => s?.addEventListener("input", later));
  syncCustom();

  document.querySelectorAll("[data-example]").forEach((b) => b.addEventListener("click", () => {
    text.value = EXAMPLES[b.dataset.example] || "";
    if (termSel) termSel.value = "auto";
    if (cardSel) cardSel.value = "auto";
    syncCustom();
    preview();
    text.focus();
  }));

  const file = form.querySelector("[data-file]");
  file?.addEventListener("change", () => {
    const f = file.files?.[0];
    if (!f) return;
    if (f.size > 5 * 1024 * 1024) { out.replaceChildren(el("p", "flash warning", "That file is over 5 MB. Split it into smaller sets.")); return; }
    f.text().then((t) => {
      text.value = t;
      const title = form.querySelector("[name=title]");
      if (title && !title.value) title.value = f.name.replace(/\.(txt|csv|tsv)$/i, "").slice(0, 200);
      preview();
    });
  });

  // The public page keeps the pasted set for after sign-up; the import page picks it up once.
  document.querySelectorAll("[data-keep-text]").forEach((a) => a.addEventListener("click", () => {
    try { if (text.value.trim()) sessionStorage.setItem(KEY, text.value); } catch { /* storage blocked */ }
  }));
  if (form.hasAttribute("data-restore")) {
    try {
      const saved = sessionStorage.getItem(KEY);
      if (saved && !text.value.trim()) {
        text.value = saved;
        document.getElementById("restored-note")?.removeAttribute("hidden");
      }
      sessionStorage.removeItem(KEY);
    } catch { /* storage blocked */ }
  }

  if (text.value.trim()) preview();
})();
