// "Add to calendar" under tutor answers: the dated items the tutor suggested (or a blank row), which
// the student checks, edits and adds. Nothing is added without that tap. Config comes from the page:
// window.HH_CAL = {courses: [{id, name, label, code}], course: <chat's class id or null>, today: "YYYY-MM-DD",
//                  addUrl: "/tutor/messages/0/calendar"}.
(() => {
  const cfg = window.HH_CAL;
  if (!cfg) return;
  const MARK = "<hh-calendar";

  // While an answer streams, hide the machine-readable block: a line starting with "<hh-calendar"
  // (with a code fence just before it), or a last line that may be becoming one. Like
  // services/myevents.py, a mention of the tag mid-sentence isn't a block.
  window.hhCalVisible = (raw) => {
    const m = /(^|\n)[ \t]*(?:```[\w-]*[ \t]*\n[ \t]*)?<hh-calendar/.exec(raw);
    if (m) return raw.slice(0, m.index + m[1].length);
    const nl = raw.lastIndexOf("\n"), tail = raw.slice(nl + 1).trimStart();
    if (!tail || !MARK.startsWith(tail)) return raw;
    const before = raw.slice(0, Math.max(nl, 0)), prev = before.slice(before.lastIndexOf("\n") + 1);
    return raw.slice(0, nl >= 0 && /^[ \t]*```[\w-]*[ \t]*$/.test(prev) ? before.lastIndexOf("\n") + 1 : nl + 1);
  };

  const norm = (s) => (s || "").toLowerCase().replace(/\s+/g, " ").trim();
  function courseFor(name) {
    const n = norm(name);
    if (n) {
      const hit = cfg.courses.find((c) => [c.name, c.label, c.code].some((v) => v && norm(v) === n))
        || cfg.courses.find((c) => norm(c.name).includes(n) || n.includes(norm(c.name)));
      if (hit) return String(hit.id);
    }
    return cfg.course ? String(cfg.course) : "";
  }
  const options = (selected) => `<option value="">No class</option>` + cfg.courses.map((c) =>
    `<option value="${c.id}"${String(c.id) === selected ? " selected" : ""}>${hh.escape(c.label || c.name)}</option>`).join("");

  function row(item) {
    const li = document.createElement("li");
    li.className = "calrow";
    li.innerHTML = `
      <input type="checkbox" class="calrow-on"${item.have ? "" : " checked"} aria-label="Include this item">
      <input type="text" class="calrow-title" maxlength="120" value="${hh.escape(item.title || "")}" placeholder="What to do" aria-label="What">
      <input type="date" class="calrow-date" value="${hh.escape(item.date || cfg.today)}" aria-label="Date">
      <span class="calrow-times"><input type="time" class="calrow-start" value="${hh.escape(item.start || "")}" aria-label="Start time">
        <span aria-hidden="true">–</span><input type="time" class="calrow-end" value="${hh.escape(item.end || "")}" aria-label="End time"></span>
      <select class="calrow-class" aria-label="Class">${options(courseFor(item.class))}</select>
      <input type="text" class="calrow-notes" maxlength="500" value="${hh.escape(item.notes || "")}" placeholder="Notes (optional)" aria-label="Notes">
      ${item.have ? '<span class="calrow-have small muted">Already on your calendar</span>' : ""}`;
    return li;
  }

  function label(card) {
    const n = card.querySelectorAll(".calrow-on:checked:not(:disabled)").length;
    const btn = card.querySelector("[data-cal-add]");
    btn.textContent = n === 0 ? "Nothing to add" : n === 1 ? "Add 1 to calendar" : `Add ${n} to calendar`;
    btn.disabled = n === 0;
  }

  function open(msg, items) {
    let card = msg.querySelector(".calcard");
    if (card && !card.classList.contains("added")) return;
    card?.remove();  // adding more after an add starts a fresh list (ones already added are skipped)
    card = document.createElement("div");
    card.className = "calcard";
    card.innerHTML = `
      <div class="calcard-head"><strong>Add to your calendar</strong>
        <span class="small muted">Untick what you don't want and change anything first. Empty times make it an all-day item.</span></div>
      <ul class="calrows"></ul>
      <div class="btn-row"><button type="button" class="btn btn-sm" data-cal-add>Add</button>
        <button type="button" class="linklike small" data-cal-row>+ Another item</button></div>
      <p class="small calcard-status" role="status"></p>`;
    const list = card.querySelector(".calrows");
    (items.length ? items : [{}]).forEach((it) => list.appendChild(row(it)));
    const bar = msg.querySelector(".msg-actions");
    if (bar) bar.before(card); else msg.appendChild(card);
    label(card);
    card.addEventListener("change", () => label(card));
    card.querySelector("[data-cal-row]").addEventListener("click", () => {
      list.appendChild(row({ date: list.lastElementChild?.querySelector(".calrow-date").value }));
      list.lastElementChild.querySelector(".calrow-title").focus();
      label(card);
    });
    card.querySelector("[data-cal-add]").addEventListener("click", async (e) => {
      const btn = e.currentTarget, status = card.querySelector(".calcard-status");
      const picked = [...list.children].filter((li) => { const on = li.querySelector(".calrow-on"); return on.checked && !on.disabled; }).map((li) => ({
        title: li.querySelector(".calrow-title").value, date: li.querySelector(".calrow-date").value,
        start: li.querySelector(".calrow-start").value || null, end: li.querySelector(".calrow-end").value || null,
        course_id: li.querySelector(".calrow-class").value || null, notes: li.querySelector(".calrow-notes").value || null,
      }));
      status.textContent = ""; status.style.color = "";
      btn.disabled = true;
      try {
        const data = await hh.post(cfg.addUrl.replace("/0/", `/${msg.dataset.id}/`), { items: picked });
        const n = data.added, again = data.skipped ? ` (${data.skipped} already there)` : "";
        status.innerHTML = `${n ? `Added ${n} to your calendar` : "Already on your calendar"}${again}. <a href="${hh.escape(data.url)}">See it</a>`;
        list.querySelectorAll("input, select").forEach((el) => { el.disabled = true; });
        card.querySelector("[data-cal-row]").hidden = true;
        btn.textContent = "Added"; card.classList.add("added");
        if (data.earlier?.length) earlier(card, data);
        msg.dataset.added = String(Number(msg.dataset.added || 0) + n);
        actions(msg);
      } catch (err) {
        status.textContent = err.message; status.style.color = "var(--err)";
        btn.disabled = false;
      }
    });
  }

  // A revised plan: offer to remove what it replaced (items added from earlier answers in this chat).
  function earlier(card, data) {
    const p = document.createElement("p");
    p.className = "small calcard-earlier";
    p.innerHTML = `Still on your calendar from an earlier plan: ${data.earlier.map((e) =>
      `${hh.escape(e.title)} (${hh.escape(e.when)})`).join(", ")}. <button type="button" class="linklike small">Remove ${data.earlier.length === 1 ? "it" : "them"}</button>`;
    p.querySelector("button").addEventListener("click", async (e) => {
      e.currentTarget.disabled = true;
      try {
        const r = await hh.post(data.remove_url, { ids: data.earlier.map((x) => x.id) });
        p.textContent = `Removed ${r.removed} from your calendar.`;
      } catch (err) { p.textContent = err.message; p.style.color = "var(--err)"; }
    });
    card.appendChild(p);
  }

  // The row under an answer: "Add to calendar" (opens the list) or what was already added.
  function actions(msg) {
    let bar = msg.querySelector(".msg-actions");
    if (!bar) { bar = document.createElement("div"); bar.className = "msg-actions"; msg.appendChild(bar); }
    const items = JSON.parse(msg.dataset.calendar || "[]"), added = Number(msg.dataset.added || 0);
    const text = added ? `✓ ${added} added to calendar · add more`
      : items.length ? `Add ${items.length === 1 ? "this" : `these ${items.length}`} to calendar` : "Add to calendar";
    bar.innerHTML = `<button type="button" class="linklike small" data-cal-open>📅 ${text}</button>`;
  }

  window.hhCalAttach = (msg, { open: show = false } = {}) => {
    if (!msg.dataset.id) return;
    actions(msg);
    if (show && !Number(msg.dataset.added || 0)) open(msg, JSON.parse(msg.dataset.calendar || "[]"));
  };

  document.getElementById("log")?.addEventListener("click", (e) => {
    const b = e.target.closest("[data-cal-open]");
    if (!b) return;
    const msg = b.closest(".msg");
    open(msg, JSON.parse(msg.dataset.calendar || "[]"));
    msg.querySelector(".calcard")?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  });
  document.querySelectorAll("#log .msg.assistant[data-id]").forEach((msg) =>
    window.hhCalAttach(msg, { open: msg.dataset.id === String(cfg.open) }));
})();
