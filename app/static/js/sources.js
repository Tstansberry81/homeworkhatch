// The source picker (templates/_source_picker.html): pick files, pages and your own uploads from
// a class, upload more, or import from Google Drive. Fires "picker:change" with {refs, sources}.
(() => {
  const KIND = { file: "", page: "page", upload: "yours" };
  const kb = (n) => (n == null ? "" : n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} KB`);

  function init(root) {
    const $ = (sel) => root.querySelector(sel);
    const selected = new Set(JSON.parse(root.dataset.selected || "[]"));
    const known = new Map();
    let items = [];
    let timer = null;
    const msg = (text, error = false) => { $("[data-msg]").textContent = text || ""; $("[data-msg]").style.color = error ? "var(--err)" : ""; };
    const courseId = () => $("[data-course]")?.value || "";

    function emit() {
      $("[data-hidden]").innerHTML = [...selected].map((r) =>
        `<input type="hidden" name="${hh.escape(root.dataset.name)}" value="${hh.escape(r)}">`).join("");
      $("[data-count]").textContent = selected.size ? ` · ${selected.size} selected` : "";
      root.dispatchEvent(new CustomEvent("picker:change", { bubbles: true,
        detail: { refs: [...selected], sources: [...selected].map((r) => known.get(r)).filter(Boolean) } }));
    }

    function visible() {
      const q = ($("[data-filter]").value || "").trim().toLowerCase();
      return items.filter((s) => !q || s.title.toLowerCase().includes(q));
    }

    // Sections (folders, pages, uploads) start folded so a big class stays one short list; a section
    // opens when it's the only one, while filtering, when it has picks, or once the student opens it.
    const opened = new Set();
    const row = (s) => {
      const on = selected.has(s.ref), ok = s.status === "ready";
      return `<li class="${ok ? "" : "muted"}"><label class="check grow"><input type="checkbox" value="${hh.escape(s.ref)}"
        ${on ? "checked" : ""} ${ok || on ? "" : "disabled"}> ${hh.escape(s.title)}${KIND[s.kind] ? ` <span class="kind">${KIND[s.kind]}</span>` : ""}</label>
        <span class="small muted">${hh.escape(ok ? kb(s.size) : s.note)}</span></li>`;
    };

    function render() {
      const list = visible();
      const filtering = Boolean(($("[data-filter]").value || "").trim());
      const groups = new Map();
      list.forEach((s) => { const g = s.group || "Files"; if (!groups.has(g)) groups.set(g, []); groups.get(g).push(s); });
      $("[data-list]").innerHTML = [...groups].map(([g, rows]) => {
        const picked = rows.filter((s) => selected.has(s.ref)).length;
        const open = groups.size === 1 || filtering || picked || opened.has(g);
        return `<li class="picker-group"><details data-group="${hh.escape(g)}" ${open ? "open" : ""}>
          <summary><span class="grow">${hh.escape(g)}</span> <span class="small muted">${rows.length}${picked ? ` · ${picked} picked` : ""}</span></summary>
          <div class="small picker-group-tools"><button type="button" class="linklike" data-group-all="${hh.escape(g)}">Select all in ${hh.escape(g)}</button></div>
          <ul class="list">${rows.map(row).join("")}</ul></details></li>`;
      }).join("") || `<li class="muted">${items.length ? "Nothing matches that filter." :
        courseId() ? "No files or pages synced for this class yet." : "No files of your own yet. Upload some below."}</li>`;
    }

    async function load() {
      clearTimeout(timer);
      try {
        const data = await hh.get(`${root.dataset.sourcesUrl}?course_id=${encodeURIComponent(courseId())}`);
        items = data.sources;
        items.forEach((s) => known.set(s.ref, s));
        render();
        emit();
        if (items.some((s) => s.status === "reading")) timer = setTimeout(load, 5000);
      } catch (e) { msg(e.message, true); }
    }

    $("[data-list]").addEventListener("toggle", (e) => {
      const g = e.target.dataset?.group;
      if (g != null) e.target.open ? opened.add(g) : opened.delete(g);
    }, true);
    $("[data-list]").addEventListener("click", (e) => {
      const g = e.target.closest("[data-group-all]")?.dataset.groupAll;
      if (g == null) return;
      visible().filter((s) => (s.group || "Files") === g && s.status === "ready").forEach((s) => selected.add(s.ref));
      opened.add(g);
      render(); emit();
    });
    $("[data-list]").addEventListener("change", (e) => {
      if (e.target.type !== "checkbox") return;
      e.target.checked ? selected.add(e.target.value) : selected.delete(e.target.value);
      emit();
    });
    $("[data-course]")?.addEventListener("change", load);
    $("[data-filter]").addEventListener("input", render);
    $("[data-all]").onclick = () => { visible().filter((s) => s.status === "ready").forEach((s) => selected.add(s.ref)); render(); emit(); };
    $("[data-none]").onclick = () => { selected.clear(); render(); emit(); };

    $("[data-upload]").addEventListener("change", async (e) => {
      const files = [...e.target.files];
      if (!files.length) return;
      const body = new FormData();
      files.forEach((f) => body.append("files", f));
      body.append("course_id", courseId());
      msg(`Uploading ${files.length} file${files.length === 1 ? "" : "s"}…`);
      try {
        const res = await fetch(root.dataset.uploadUrl, { method: "POST", body,
          headers: { "X-CSRFToken": hh.csrf, "X-Requested-With": "fetch", Accept: "application/json" } });
        const data = await res.json().catch(() => ({}));
        (data.sources || []).forEach((s) => { known.set(s.ref, s); selected.add(s.ref); });
        msg([data.sources?.length ? `Added ${data.sources.length}. Reading the text now; they're selected once ready.` : "",
             ...(data.errors || []), !res.ok && !data.errors ? data.error || "Upload failed." : ""].filter(Boolean).join(" "), !res.ok);
      } catch (err) { msg(err.message, true); }
      e.target.value = "";
      load();
    });

    const drivePanel = $("[data-drive-panel]");
    if (drivePanel) {
      let seq = 0, debounce;
      const search = async () => {
        const mine = ++seq;
        $("[data-drive-list]").innerHTML = '<li class="muted">Searching your Drive…</li>';
        try {
          const data = await hh.get(`${root.dataset.driveSearch}?q=${encodeURIComponent($("[data-drive-q]").value.trim())}`);
          if (mine !== seq) return;
          if (!data.connected) {
            const next = encodeURIComponent(location.pathname + location.search);
            $("[data-drive-list]").innerHTML = `<li><a class="btn btn-sm" href="${hh.escape(data.connect_url)}?next=${next}">Connect Google Drive</a>
              <span class="small muted">You'll come right back here.</span></li>`;
            return;
          }
          $("[data-drive-list]").innerHTML = data.files.map((f) => `<li><span class="grow">${f.icon ? `<img src="${hh.escape(f.icon)}" alt="" width="16" height="16"> ` : ""}${hh.escape(f.name)}</span>
            <button type="button" class="btn btn-ghost btn-sm" data-import="${hh.escape(f.id)}" data-mime="${hh.escape(f.mimeType)}">Add</button></li>`).join("")
            || '<li class="muted">No files found.</li>';
        } catch (e) { $("[data-drive-list]").innerHTML = `<li class="muted">${hh.escape(e.message)}</li>`; }
      };
      $("[data-drive]").onclick = () => { drivePanel.hidden = !drivePanel.hidden; if (!drivePanel.hidden) search(); };
      $("[data-drive-q]").addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(search, 400); });
      $("[data-drive-list]").addEventListener("click", async (e) => {
        const btn = e.target.closest("[data-import]");
        if (!btn) return;
        btn.disabled = true; btn.textContent = "Adding…";
        try {
          const data = await hh.post(root.dataset.driveImport, { id: btn.dataset.import, mimeType: btn.dataset.mime, course_id: courseId() });
          known.set(data.source.ref, data.source);
          selected.add(data.source.ref);
          btn.textContent = "Added ✓";
          load();
        } catch (err) { btn.disabled = false; btn.textContent = "Add"; msg(err.message, true); }
      });
    }
    root.picker = { remove(ref) { selected.delete(ref); render(); emit(); } };
    load();
  }

  document.querySelectorAll("[data-picker]").forEach(init);
})();
