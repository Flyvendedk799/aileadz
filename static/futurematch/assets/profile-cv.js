/* Futurematch: CV import on the profile page (replaces the old /profil-upload portal).
   Flow: upload or paste -> live progress -> review (editable, per item) -> apply.
   Uses the same endpoints as before: POST /api/cv/parse, GET /api/cv/parse-stream (SSE:
   stage | chunk | result | error), POST /api/cv/apply, POST /api/cv/improve.
   Mount: #cvImport (hidden until opened by [data-cv-open] or the #cv hash).
   Emits `fm:cv-applied` on document so the page can reload its own data. */
(function () {
  "use strict";
  const root = document.getElementById("cvImport");
  if (!root) return;

  const STAGES = ["Udtrækker tekst", "Læser CV", "Analyserer", "Foreslår profil"];
  const LEVELS = ["Begynder", "Øvet", "Erfaren", "Ekspert", "Specialist"];
  const PROF = ["Grundlæggende", "Professionelt", "Flydende", "Modersmål"];
  const MAX_BYTES = 8 * 1024 * 1024;
  const PARSE_TIMEOUT_MS = 95000;

  const $ = (sel, el) => (el || root).querySelector(sel);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  let es = null;
  let timer = null;
  let sessionId = "";
  let proposal = {};
  let staged = null;
  let busy = false;

  const panes = ["intake", "progress", "review", "done"];
  function show(name) {
    panes.forEach((p) => { const el = $('[data-pane="' + p + '"]'); if (el) el.hidden = p !== name; });
  }
  function track(event, meta) {
    try {
      fetch("/api/learner/events", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ event: event, meta: meta || {} }),
      }).catch(() => {});
    } catch (e) { /* ignore */ }
  }
  function say(msg, bad) {
    const el = $("[data-cv-status]");
    if (!el) return;
    el.textContent = msg || "";
    el.classList.toggle("is-bad", !!bad);
  }

  /* ---------------- open / close ---------------- */
  function open() {
    root.hidden = false;
    document.querySelectorAll("[data-cv-open]").forEach((b) => b.setAttribute("aria-expanded", "true"));
    try { root.scrollIntoView({ behavior: "smooth", block: "start" }); } catch (e) { /* ignore */ }
  }
  function close() {
    if (busy) return;
    root.hidden = true;
    document.querySelectorAll("[data-cv-open]").forEach((b) => b.setAttribute("aria-expanded", "false"));
  }
  document.addEventListener("click", (e) => {
    const t = e.target.closest ? e.target.closest("[data-cv-open]") : null;
    if (t) { e.preventDefault(); open(); }
  });
  $("[data-cv-close]").addEventListener("click", close);
  if (location.hash === "#cv") open();
  window.addEventListener("hashchange", () => { if (location.hash === "#cv") open(); });

  /* ---------------- intake ---------------- */
  const drop = $("[data-cv-drop]");
  const fileInput = $("[data-cv-file]");
  const pasteBox = $("[data-cv-text]");
  const analyseBtn = $("[data-cv-analyse]");

  function refreshAnalyse() {
    analyseBtn.disabled = !(staged || (pasteBox.value || "").trim().length > 20);
  }
  function stageFile(file) {
    if (!file) return;
    if (file.size > MAX_BYTES) { say("Filen er for stor (maks 8 MB).", true); return; }
    staged = { type: "file", file: file };
    $("[data-cv-filename]").textContent = file.name;
    drop.classList.add("has-file");
    say("");
    refreshAnalyse();
  }
  drop.addEventListener("click", () => fileInput.click());
  drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); } });
  fileInput.addEventListener("change", () => stageFile(fileInput.files && fileInput.files[0]));
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("dragging"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("dragging"); }));
  drop.addEventListener("drop", (e) => stageFile(e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]));
  pasteBox.addEventListener("input", () => {
    if ((pasteBox.value || "").trim()) { staged = null; drop.classList.remove("has-file"); $("[data-cv-filename]").textContent = ""; }
    refreshAnalyse();
  });
  analyseBtn.addEventListener("click", startAnalysis);

  /* ---------------- parsing ---------------- */
  function newSessionId() {
    return (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : "cv-" + Date.now() + "-" + Math.random().toString(16).slice(2);
  }
  function setProgress(label, pct) {
    $("[data-cv-stage]").textContent = label;
    $("[data-cv-bar]").style.width = Math.round(pct * 100) + "%";
    $("[data-cv-progress]").setAttribute("aria-valuenow", String(Math.round(pct * 100)));
  }
  function closeStream() {
    if (es) { es.close(); es = null; }
    if (timer) { clearTimeout(timer); timer = null; }
  }
  function failParse(message) {
    closeStream();
    busy = false;
    track("cv_parse_failed");
    show("intake");
    say(message || "CV'et blev ikke analyseret. Prøv igen eller indsæt teksten direkte.", true);
  }
  function startAnalysis() {
    if (busy) return;
    const text = (pasteBox.value || "").trim();
    if (!staged && text.length <= 20) return;
    busy = true;
    sessionId = newSessionId();
    proposal = {};
    show("progress");
    setProgress(STAGES[0], 0.1);
    let failed = false;
    try {
      es = new EventSource("/api/cv/parse-stream?session_id=" + encodeURIComponent(sessionId));
      es.addEventListener("stage", (e) => {
        const idx = Math.max(0, STAGES.indexOf(e.data));
        setProgress(e.data + "  ·  " + (idx + 1) + "/" + STAGES.length, ((idx + 1) / STAGES.length) * 0.9);
      });
      es.addEventListener("result", (e) => {
        try {
          const d = JSON.parse(e.data);
          handleResult(d.proposal !== undefined ? d.proposal : d, d.hint || "");
        } catch (err) { failParse(); }
      });
      es.addEventListener("error", (e) => {
        if (failed) return;
        failed = true;
        failParse((e && e.data && e.data !== "timeout" && e.data) || "Forbindelsen til CV-analysen blev afbrudt.");
      });
      const fd = new FormData();
      fd.append("session_id", sessionId);
      if (staged && staged.type === "file") fd.append("cv", staged.file); else fd.append("cv_text", text);
      fetch("/api/cv/parse", { method: "POST", credentials: "same-origin", body: fd })
        .then((r) => r.json().catch(() => ({})).then((d) => ({ ok: r.ok, d: d })))
        .then((res) => { if (!res.ok || res.d.success === false) throw new Error(res.d.error || "CV'et kunne ikke sendes til analyse."); })
        .catch((err) => { if (!failed) { failed = true; failParse(err && err.message); } });
      timer = setTimeout(() => {
        if (!failed) { failed = true; failParse("Analysen tog for lang tid. Prøv igen eller indsæt teksten direkte."); }
      }, PARSE_TIMEOUT_MS);
    } catch (err) { failParse("CV-analysen kunne ikke startes."); }
  }

  function countItems(p) {
    return ["skills", "experience", "education", "courses", "certifications", "languages"]
      .reduce((n, k) => n + ((p && p[k]) || []).length, 0) + (p && p.summary ? 1 : 0);
  }
  function handleResult(p, hint) {
    closeStream();
    busy = false;
    proposal = p || {};
    if (!countItems(proposal)) {
      show("intake");
      say(hint || "Ingen CV-data fundet. Prøv en PDF-fil eller indsæt teksten direkte.", true);
      return;
    }
    track("cv_review", { items: countItems(proposal) });
    setProgress("Foreslår profil  ·  klar", 1);
    buildReview();
    show("review");
  }

  /* ---------------- review ---------------- */
  function field(item, key, label, opts) {
    opts = opts || {};
    const id = "cvf-" + Math.random().toString(36).slice(2, 9);
    const wrap = document.createElement("label");
    wrap.className = "cvi-field" + (opts.wide ? " wide" : "");
    wrap.setAttribute("for", id);
    const span = document.createElement("span");
    span.textContent = label;
    let input;
    if (opts.options) {
      input = document.createElement("select");
      const cur = item[key] == null ? "" : String(item[key]);
      const list = opts.options.slice();
      if (cur && list.indexOf(cur) < 0) list.unshift(cur);
      list.forEach((v) => {
        const o = document.createElement("option");
        o.value = v; o.textContent = v;
        if (v === cur) o.selected = true;
        input.appendChild(o);
      });
      if (!cur) item[key] = list[0];
    } else if (opts.area) {
      input = document.createElement("textarea");
      input.rows = 3;
      input.value = item[key] == null ? "" : item[key];
    } else {
      input = document.createElement("input");
      input.type = opts.type || "text";
      input.value = item[key] == null ? "" : item[key];
    }
    input.id = id;
    input.addEventListener("input", () => {
      item[key] = key === "is_current" ? input.value === "true" : input.value;
    });
    wrap.append(span, input);
    return wrap;
  }

  // Non-destructive coach: the suggestion is shown next to the person's own text and only
  // replaces it on an explicit "Brug forslag" (the server grounds it in the chosen facts).
  function coachButton(kind, item, fields) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "cvi-coach";
    b.textContent = kind === "summary" ? "Gør profilteksten skarpere med AI" : "Gør erfaringen skarpere med AI";
    b.addEventListener("click", () => {
      const old = fields.querySelector(".cvi-coach-result");
      if (old) old.remove();
      const out = document.createElement("div");
      out.className = "cvi-coach-result";
      out.textContent = "CV-coachen læser kun de valgte fakta…";
      fields.appendChild(out);
      b.disabled = true;
      fetch("/api/cv/improve", {
        method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ section: kind, content: kind === "summary" ? item.summary : item }),
      }).then((r) => r.json().catch(() => ({})).then((d) => ({ ok: r.ok, d: d }))).then((res) => {
        const d = res.d;
        if (!res.ok || !d.success) throw new Error(d.error || "CV-coachen kunne ikke svare.");
        out.textContent = "";
        const text = document.createElement("div");
        text.textContent = d.suggestion;
        out.appendChild(text);
        if (d.rationale) {
          const why = document.createElement("div");
          why.className = "cvi-coach-why";
          why.textContent = d.rationale;
          out.appendChild(why);
        }
        if ((d.missing_evidence || []).length) {
          const ul = document.createElement("ul");
          d.missing_evidence.forEach((q) => { const li = document.createElement("li"); li.textContent = q; ul.appendChild(li); });
          out.appendChild(ul);
        }
        const actions = document.createElement("div");
        actions.className = "cvi-coach-actions";
        const use = document.createElement("button");
        use.type = "button"; use.textContent = "Brug forslag";
        const keep = document.createElement("button");
        keep.type = "button"; keep.textContent = "Behold min tekst";
        use.addEventListener("click", () => {
          const key = kind === "summary" ? "summary" : "description";
          item[key] = d.suggestion;
          if (kind === "summary") proposal.summary = d.suggestion;
          const area = fields.querySelector("textarea");
          if (area) area.value = d.suggestion;
          out.remove();
        });
        keep.addEventListener("click", () => out.remove());
        actions.append(use, keep);
        out.appendChild(actions);
      }).catch((err) => { out.textContent = (err && err.message) || "CV-coachen er midlertidigt utilgængelig."; })
        .then(() => { b.disabled = false; });
    });
    return b;
  }

  function row(kind, item, specs, coach) {
    const r = document.createElement("div");
    r.className = "cvi-item";
    r.dataset.kind = kind;
    const check = document.createElement("input");
    check.type = "checkbox";
    check.checked = true;
    check.setAttribute("aria-label", "Gem dette forslag");
    check.addEventListener("change", updateCount);
    const fields = document.createElement("div");
    fields.className = "cvi-fields";
    specs.forEach((s) => fields.appendChild(field(item, s[0], s[1], s[2])));
    if (coach) fields.appendChild(coachButton(kind, item, fields));
    r.append(check, fields);
    r._item = item;
    return r;
  }

  function buildReview() {
    const box = $("[data-cv-items]");
    box.innerHTML = "";
    const group = (title, count) => {
      const g = document.createElement("section");
      g.className = "cvi-group";
      const h = document.createElement("h4");
      h.textContent = title + (count ? " (" + count + ")" : "");
      g.appendChild(h);
      box.appendChild(g);
      return g;
    };
    if (proposal.summary) {
      const item = { summary: proposal.summary };
      const r = row("summary", item, [["summary", "Profiltekst", { area: true, wide: true }]], true);
      r.addEventListener("input", () => { proposal.summary = item.summary; });
      group("Profiltekst").appendChild(r);
    }
    const lists = [
      ["skills", "skill", "Kompetencer", [["name", "Kompetence"], ["level", "Niveau", { options: LEVELS }]]],
      ["experience", "experience", "Erfaring", [
        ["title", "Titel"], ["company", "Virksomhed"], ["start_year", "Startår", { type: "number" }],
        ["end_year", "Slutår", { type: "number" }],
        ["is_current", "Nuværende", { options: ["false", "true"] }],
        ["description", "Ansvar og resultater", { area: true, wide: true }]], true],
      ["education", "education", "Uddannelse", [["degree", "Uddannelse"], ["institution", "Institution"], ["year", "År"]]],
      ["courses", "courses", "Kurser", [["title", "Kursus"], ["vendor", "Udbyder"], ["completed_date", "Gennemført"]]],
      ["certifications", "certifications", "Certificeringer", [
        ["name", "Certificering"], ["issuer", "Udsteder"], ["issue_date", "Udstedt"], ["expiry_date", "Udløber"]]],
      ["languages", "languages", "Sprog", [["language", "Sprog"], ["proficiency", "Niveau", { options: PROF }]]],
    ];
    lists.forEach((cfg) => {
      const items = proposal[cfg[0]] || [];
      if (!items.length) return;
      const g = group(cfg[2], items.length);
      items.forEach((it) => {
        // is_current is a boolean on the item; its select shows "false"/"true" (String(bool)).
        if (cfg[0] === "experience") it.is_current = !!it.is_current;
        g.appendChild(row(cfg[1], it, cfg[3], !!cfg[4]));
      });
    });
    updateCount();
  }

  function acceptedRows() {
    return Array.prototype.filter.call(root.querySelectorAll(".cvi-item"), (r) => r.querySelector("input[type=checkbox]").checked);
  }
  function updateCount() {
    const n = acceptedRows().length;
    $("[data-cv-count]").textContent = n + " valgt";
    $("[data-cv-save]").disabled = n === 0;
  }
  $("[data-cv-all]").addEventListener("click", () => setAll(true));
  $("[data-cv-none]").addEventListener("click", () => setAll(false));
  function setAll(on) {
    root.querySelectorAll(".cvi-item input[type=checkbox]").forEach((c) => { c.checked = on; });
    updateCount();
  }
  $("[data-cv-back]").addEventListener("click", () => { show("intake"); say(""); });

  /* ---------------- apply ---------------- */
  $("[data-cv-save]").addEventListener("click", () => {
    const rows = acceptedRows();
    if (!rows.length) return;
    const btn = $("[data-cv-save]");
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = "Gemmer…";
    let summary = "";
    const accepted = [];
    rows.forEach((r) => {
      if (r.dataset.kind === "summary") { summary = r._item.summary || ""; return; }
      accepted.push(Object.assign({ type: r.dataset.kind }, r._item));
    });
    fetch("/api/cv/apply", {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, conflict_mode: $("[data-cv-conflict]").value, summary: summary, accepted: accepted }),
    }).then((r) => r.json().catch(() => ({})).then((d) => ({ status: r.status, ok: r.ok, d: d })))
      .then((res) => {
        const d = res.d || {};
        if ((!res.ok && res.status !== 207) || (!d.success && !d.partial_success)) throw new Error(d.error || "apply failed");
        const out = d.outcomes || {};
        const changed = (out.created || 0) + (out.updated || 0) + (out.merged || 0);
        $("[data-cv-done-text]").textContent = changed + " oplysninger blev gemt" +
          (out.skipped ? ", " + out.skipped + " eksisterende blev beholdt" : "") +
          (out.failed ? ", og " + out.failed + " kunne ikke gemmes" : "") + ".";
        const action = d.career_action || {};
        const parts = [];
        const gaps = (action.top_gaps || []).map((g) => esc(g.skill || "")).filter(Boolean);
        const strong = (action.strongest_evidence || []).map(esc);
        if (strong.length) parts.push("<b>Stærkeste evidens:</b> " + strong.join(", "));
        if (gaps.length) parts.push("<b>Næste udviklingspunkter:</b> " + gaps.join(", "));
        const ca = $("[data-cv-career]");
        ca.innerHTML = parts.join("<br>");
        ca.hidden = !parts.length;
        track("cv_apply", { created: out.created || 0, updated: out.updated || 0, merged: out.merged || 0, failed: out.failed || 0 });
        show("done");
        document.dispatchEvent(new CustomEvent("fm:cv-applied", { detail: out }));
      })
      .catch(() => { say("Kunne ikke gemme. Dine forslag er stadig her. Prøv igen.", true); })
      .then(() => { btn.textContent = old; updateCount(); });
  });
  $("[data-cv-again]").addEventListener("click", () => {
    staged = null; pasteBox.value = ""; fileInput.value = "";
    drop.classList.remove("has-file"); $("[data-cv-filename]").textContent = "";
    refreshAnalyse(); say(""); show("intake");
  });

  show("intake");
  refreshAnalyse();
})();
