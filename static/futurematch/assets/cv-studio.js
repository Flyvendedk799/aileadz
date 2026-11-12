/* Futurematch: the CV studio (/profil/cv, templates/fm/my_cv.html).
   - Draws the rail (quality checks, target-role fit, platform evidence) from the
     embedded #cvsData JSON and from GET /api/cv/document after every change.
   - Editor dialog for summary / headline / target role / one job's description, saved
     through the existing profile endpoints (POST /api/profile/summary,
     PUT /api/profile/experience).
   - AI coach (POST /api/cv/improve) is non-destructive: the suggestion is shown next to
     the person's text and only used on "Brug forslag"; nothing is saved until "Gem".
   - Layout prefs (template, sections, targeting, evidence) -> POST /api/cv/prefs.
   - Reloads when the CV import (profile-cv.js) emits `fm:cv-applied`. */
(function () {
  "use strict";
  const doc = document.getElementById("cvDoc");
  if (!doc) return;

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  let state = { cv: {}, quality: {} };
  try { state = JSON.parse(document.getElementById("cvsData").textContent) || state; } catch (e) { /* keep empty */ }

  function api(url, opts) {
    return fetch(url, Object.assign({ credentials: "same-origin" }, opts || {}))
      .then((r) => r.json().catch(() => ({})).then((d) => ({ ok: r.ok, d: d })));
  }
  function json(method, url, body) {
    return api(url, { method: method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  }
  function track(event, meta) {
    try { json("POST", "/api/learner/events", { event: event, meta: meta || {} }).catch(() => {}); } catch (e) { /* ignore */ }
  }
  function chatUrl(intent) {
    return "/chat?from=cv&intent=" + encodeURIComponent(intent || "Hjælp mig med mit CV");
  }
  function catalogUrl(q) { return "/catalog?q=" + encodeURIComponent(q || ""); }

  /* ---------------- refresh ---------------- */
  function refresh() {
    doc.classList.add("is-loading");
    const prefs = (state.cv || {}).prefs;
    return api("/api/cv/document" + (prefs ? "?prefs=" + encodeURIComponent(JSON.stringify(prefs)) : "")).then((res) => {
      if (!res.ok || !res.d.success) throw new Error("load");
      state = { cv: res.d.cv || {}, quality: res.d.quality || {} };
      doc.innerHTML = res.d.html || "";
      renderAll();
    }).catch(() => { /* keep the last good render */ })
      .then(() => doc.classList.remove("is-loading"));
  }

  /* ---------------- hero ---------------- */
  function renderHero() {
    const q = state.quality || {};
    const st = (state.cv || {}).stats || {};
    const ring = document.getElementById("cvsRing");
    if (ring) ring.style.setProperty("--p", q.score || 0);
    const pct = document.getElementById("cvsRingPct");
    if (pct) pct.textContent = (q.score || 0) + "%";
    const grade = document.getElementById("cvsGrade");
    if (grade) grade.textContent = q.grade || "";
    const stats = document.getElementById("cvsStats");
    if (stats) {
      const rows = [[st.years, "års erfaring"], [st.roles, "job"], [st.skills, "kompetencer"],
        [(st.verified_courses || 0) + (st.evidenced_skills || 0), "dokumenteret"], [st.in_progress, "i gang"]];
      stats.innerHTML = rows.map((r) => '<span><b class="tnum">' + esc(r[0] || 0) + "</b> " + esc(r[1]) + "</span>").join("");
    }
  }

  /* ---------------- checks ---------------- */
  let showDone = false;
  function actionButton(a) {
    if (!a) return "";
    const k = a.kind;
    if (k === "coach") return '<button type="button" class="cvs-check-btn" data-cv-coach="' + esc(a.section) + '"' +
      (a.id != null ? ' data-id="' + esc(a.id) + '"' : "") + '><i class="fa-solid fa-wand-magic-sparkles"></i> Forbedr med AI</button>';
    if (k === "edit") return '<button type="button" class="cvs-check-btn" data-cv-edit="' + esc(a.field) + '"><i class="fa-solid fa-pen"></i> Udfyld</button>';
    if (k === "import") return '<button type="button" class="cvs-check-btn" data-cv-open><i class="fa-solid fa-file-arrow-up"></i> Importér CV</button>';
    if (k === "chat") return '<a class="cvs-check-btn" href="' + esc(chatUrl(a.intent)) + '"><i class="fa-solid fa-comments"></i> Tag det med AI</a>';
    if (k === "catalog") return '<a class="cvs-check-btn" href="' + esc(catalogUrl(a.query)) + '"><i class="fa-solid fa-graduation-cap"></i> Find kurser</a>';
    if (k === "template") return '<button type="button" class="cvs-check-btn" data-cv-template="' + esc(a.template) + '"><i class="fa-solid fa-table-columns"></i> Prøv kompakt</button>';
    return "";
  }
  function renderChecks() {
    const box = document.getElementById("cvsChecks");
    if (!box) return;
    const checks = (state.quality || {}).checks || [];
    const todo = checks.filter((c) => !c.ok);
    const rank = { vigtig: 0, tip: 1 };
    todo.sort((a, b) => (rank[a.severity] - rank[b.severity]) || (b.weight - a.weight));
    const done = checks.filter((c) => c.ok);
    const item = (c) => '<li class="cvs-check is-' + esc(c.ok ? "ok" : c.severity) + '">' +
      '<i class="fa-solid ' + (c.ok ? "fa-circle-check" : (c.severity === "vigtig" ? "fa-circle-exclamation" : "fa-lightbulb")) + '"></i>' +
      "<div><div class=\"cvs-check-label\">" + esc(c.label) + "</div>" +
      (c.advice ? '<div class="cvs-check-advice">' + esc(c.advice) + "</div>" : "") + actionButton(c.action) + "</div></li>";
    let html = todo.length ? todo.map(item).join("")
      : '<li class="cvs-check is-ok"><i class="fa-solid fa-trophy"></i><div><div class="cvs-check-label">Dit CV er stærkt</div><div class="cvs-check-advice">Hold det ved lige: nye kurser og job kommer selv med.</div></div></li>';
    if (done.length) {
      html += '<li><button type="button" class="cvs-done-toggle" data-cv-toggle-done>' +
        (showDone ? "Skjul" : "Vis") + " " + done.length + " ting, der allerede er i orden</button></li>";
      if (showDone) html += done.map(item).join("");
    }
    box.innerHTML = html;
  }

  /* ---------------- target-role fit ---------------- */
  function renderFit() {
    const box = document.getElementById("cvsFit");
    if (!box) return;
    const cv = state.cv || {};
    const fit = cv.fit;
    if (!cv.target_role && !(fit && (fit.gaps || []).length)) {
      box.innerHTML = '<p class="cvs-muted" style="margin:0 0 8px">Hvilken rolle sigter du mod? Så målretter vi CV\'et og viser, hvad der mangler.</p>' +
        '<button type="button" class="cvs-check-btn" data-cv-edit="target_role"><i class="fa-solid fa-bullseye"></i> Sæt ønsket rolle</button>';
      return;
    }
    let html = cv.target_role ? '<div style="font-weight:700">' + esc(cv.target_role) + "</div>" : '<div style="font-weight:700">Dine mål</div>';
    if (fit && fit.pct != null) {
      html += '<div class="cvs-fit-bar" role="img" aria-label="' + esc(fit.pct) + ' procent dækket"><span style="width:' + Math.max(4, Math.min(100, fit.pct)) + '%"></span></div>' +
        '<div class="cvs-muted" style="font-size:12.5px;margin:-4px 0 8px">' + esc(fit.pct) + " % af det, rollen kræver, står allerede på dit CV</div>";
    }
    if (fit && (fit.matched || []).length) {
      html += '<div class="cvs-muted" style="font-size:12px">Det har du:</div><div class="cvs-chips">' +
        fit.matched.map((m) => '<span class="cvs-chip">' + esc(m) + "</span>").join("") + "</div>";
    }
    if (fit && (fit.gaps || []).length) {
      html += fit.gaps.map((g) => '<div class="cvs-gap"><div><b>' + esc(g.skill) + "</b><small>" +
        esc(g.current || "Ikke på CV'et") + " → " + esc(g.target || "") + "</small></div>" +
        '<a class="cvs-check-btn" href="' + esc(g.url || catalogUrl(g.skill)) + '">Kurser</a></div>').join("");
      html += '<div style="margin-top:10px"><a class="cvs-link" href="' + esc(chatUrl("Lav en læringssti, der lukker mine største gab mod " + (cv.target_role || "mine mål"))) + '">Lav en læringssti med AI →</a></div>';
    } else if (fit) {
      html += '<p class="cvs-muted" style="margin:6px 0 0">Ingen kendte gab mod rollen. Godt gået.</p>';
    }
    box.innerHTML = html;
  }

  /* ---------------- platform evidence ---------------- */
  function renderPlatform() {
    const box = document.getElementById("cvsPlatform");
    if (!box) return;
    const cv = state.cv || {};
    const st = cv.stats || {};
    const leader = (cv.skills || []).filter((s) => (s.evidence || []).some((e) => e.kind === "leder")).length;
    const course = (cv.skills || []).filter((s) => (s.evidence || []).some((e) => e.kind === "kursus")).length;
    const rows = [];
    rows.push(["fa-graduation-cap", st.verified_courses
      ? esc(st.verified_courses) + " kursus" + (st.verified_courses === 1 ? "" : "er") + " gennemført via Futurematch står på CV'et."
      : "Kurser du gennemfører via Futurematch, kommer automatisk på CV'et.", '<a href="/catalog">Find kurser</a>']);
    if (course) rows.push(["fa-circle-check", esc(course) + " kompetence" + (course === 1 ? "" : "r") + " er bevist via gennemførte kurser.", ""]);
    if (leader) rows.push(["fa-user-check", esc(leader) + " kompetence" + (leader === 1 ? "" : "r") + " er vurderet af din leder.", ""]);
    if ((cv.in_progress || []).length) rows.push(["fa-person-running", esc(cv.in_progress.length) + " forløb er i gang og vises under 'Under uddannelse'.", '<a href="/min-tidslinje">Se tidslinje</a>']);
    const expired = (cv.certifications || []).filter((c) => c.state === "udloebet");
    const soon = (cv.certifications || []).filter((c) => c.state === "udloeber");
    if (expired.length || soon.length) rows.push(["fa-certificate", (expired.length ? esc(expired.length) + " certificering(er) er udløbet" : "") +
      (expired.length && soon.length ? ", " : "") + (soon.length ? esc(soon.length) + " udløber snart" : "") + ".", '<a href="' + esc(catalogUrl((expired[0] || soon[0]).name)) + '">Forny</a>']);
    rows.push(["fa-bullseye", (cv.goals || []).length ? "Dine mål styrer, hvad CV'et fremhæver, når du målretter." : "Sæt mål, så ved vi, hvad CV'et skal pege mod.", '<a href="/mine-maal">Mine mål</a>']);
    rows.push(["fa-brain", "Det, AI'en har lært om dig, kan du se og rette i Mind-Map.", '<a href="/mind-map">Åbn Mind-Map</a>']);
    box.innerHTML = '<div class="cvs-plat">' + rows.map((r) => '<div class="cvs-plat-row"><i class="fa-solid ' + r[0] + '"></i><div>' + r[1] + (r[2] ? " " + r[2] : "") + "</div></div>").join("") + "</div>";
  }

  function renderPrefs() {
    const p = (state.cv || {}).prefs || {};
    document.querySelectorAll("[data-cv-template]").forEach((b) => {
      if (b.getAttribute("role") === "radio") b.setAttribute("aria-checked", String(b.getAttribute("data-cv-template") === p.template));
    });
    document.querySelectorAll("[data-cv-pref]").forEach((i) => { i.checked = !!p[i.getAttribute("data-cv-pref")]; });
    const pr = document.querySelector("[data-cv-print]");
    if (pr) pr.setAttribute("href", "/profil/cv/print?print=1&template=" + encodeURIComponent(p.template || "klassisk"));
    const hidden = p.hidden || [];
    document.querySelectorAll("[data-cv-section]").forEach((i) => { i.checked = hidden.indexOf(i.getAttribute("data-cv-section")) < 0; });
  }

  function renderAll() { renderHero(); renderChecks(); renderFit(); renderPlatform(); renderPrefs(); }

  /* ---------------- prefs ---------------- */
  function savePrefs(patch) {
    const prefs = Object.assign({}, (state.cv || {}).prefs || {}, patch);
    state.cv.prefs = prefs;
    renderPrefs();
    track("cv_prefs", { template: prefs.template, targeted: !!prefs.targeted });
    return Promise.all([
      json("POST", "/api/cv/prefs", prefs).then((res) => {
        if (!res.ok) notice("Dit valg vises, men kunne ikke gemmes til næste gang.");
      }).catch(() => notice("Dit valg vises, men kunne ikke gemmes til næste gang.")),
      refresh(),
    ]);
  }
  function notice(msg) {
    const el = document.querySelector(".cvs-hint");
    if (el) el.textContent = msg;
  }
  function currentSectionsHidden() {
    const out = [];
    document.querySelectorAll("[data-cv-section]").forEach((i) => { if (!i.checked) out.push(i.getAttribute("data-cv-section")); });
    return out;
  }

  /* ---------------- editor + coach ---------------- */
  const dlg = document.getElementById("cvsEditor");
  const $d = (sel) => dlg.querySelector(sel);
  let editing = null;
  const SPECS = {
    summary: { title: "Profiltekst", label: "Om dig", rows: 7, coach: true,
      hint: "3-5 sætninger: hvad du kan, hvad du har opnået, og hvor du vil hen." },
    headline: { title: "Overskrift", label: "Overskrift under dit navn", rows: 2, coach: true,
      hint: "Én linje, fx 'Projektleder med 8 års erfaring i byggeri'." },
    target_role: { title: "Ønsket rolle", label: "Rolle", rows: 1, coach: false,
      hint: "Den rolle du sigter mod. Vi bruger den til at målrette CV'et og finde dine gab." },
    experience: { title: "Erfaring", label: "Ansvar og resultater (én linje pr. punkt)", rows: 8, coach: true,
      hint: "Start hvert punkt med et udsagnsord, og vis effekten: tal, omfang, hvad der blev bedre." },
  };
  function findJob(id) {
    return ((state.cv || {}).experience || []).find((e) => String(e.id) === String(id)) || null;
  }
  function status(msg, bad) {
    const el = $d("[data-cv-editor-status]");
    el.textContent = msg || "";
    el.classList.toggle("is-bad", !!bad);
  }
  function openEditor(section, id, runCoach) {
    const spec = SPECS[section];
    if (!spec || !dlg) return;
    const cv = state.cv || {};
    let value = "";
    let title = spec.title;
    if (section === "summary") value = cv.summary || "";
    else if (section === "headline") value = cv.headline || "";
    else if (section === "target_role") value = cv.target_role || "";
    else if (section === "experience") {
      const job = findJob(id);
      if (!job) return;
      value = job.description || "";
      title = job.title + (job.company ? ", " + job.company : "");
    }
    editing = { section: section, id: id };
    $d("#cvsEditorTitle").textContent = title;
    $d("[data-cv-editor-label]").textContent = spec.label;
    $d("[data-cv-editor-hint]").textContent = spec.hint;
    const ta = $d("[data-cv-editor-text]");
    ta.value = value;
    ta.rows = spec.rows;
    $d("[data-cv-editor-coach]").hidden = !spec.coach;
    const out = $d("[data-cv-coach-out]");
    out.hidden = true; out.innerHTML = "";
    status("");
    if (typeof dlg.showModal === "function") dlg.showModal(); else dlg.setAttribute("open", "");
    ta.focus();
    if (runCoach && spec.coach) coach();
  }
  function closeEditor() {
    editing = null;
    if (typeof dlg.close === "function") dlg.close(); else dlg.removeAttribute("open");
  }
  function coach() {
    if (!editing) return;
    const draft = $d("[data-cv-editor-text]").value.trim();
    const body = { section: editing.section };
    if (editing.section === "experience") {
      body.id = editing.id;
      const job = findJob(editing.id);
      if (job && draft) body.content = { title: job.title, company: job.company, start_year: job.start_year,
        end_year: job.end_year, is_current: job.is_current, description: draft };
    } else if (editing.section === "summary" && draft) {
      body.content = draft;
    }
    const out = $d("[data-cv-coach-out]");
    const btn = $d("[data-cv-editor-coach]");
    out.hidden = false;
    out.textContent = "CV-coachen læser kun det, der står på din profil…";
    btn.disabled = true;
    track("cv_coach", { section: editing.section });
    json("POST", "/api/cv/improve", body).then((res) => {
      const d = res.d || {};
      if (!res.ok || !d.success) throw new Error(d.error || "CV-coachen kunne ikke svare.");
      let html = '<div class="cvs-coach-text">' + esc(d.suggestion) + "</div>";
      if (d.rationale) html += '<div class="cvs-coach-why">' + esc(d.rationale) + "</div>";
      if ((d.missing_evidence || []).length) {
        html += '<div class="cvs-coach-why">Det ville gøre det endnu stærkere, hvis du tilføjer:</div><ul>' +
          d.missing_evidence.map((q) => "<li>" + esc(q) + "</li>").join("") + "</ul>";
      }
      html += '<div class="cvs-coach-actions"><button type="button" class="btn btn-primary btn-sm" data-coach-use>Brug forslag</button>' +
        '<button type="button" class="btn btn-ghost btn-sm" data-coach-keep>Behold min tekst</button></div>';
      out.innerHTML = html;
      out.querySelector("[data-coach-use]").addEventListener("click", () => {
        $d("[data-cv-editor-text]").value = d.suggestion;
        out.hidden = true;
        status("Forslaget er sat ind. Ret det til, og klik Gem.");
      });
      out.querySelector("[data-coach-keep]").addEventListener("click", () => { out.hidden = true; });
    }).catch((err) => { out.textContent = (err && err.message) || "CV-coachen er midlertidigt utilgængelig."; })
      .then(() => { btn.disabled = false; });
  }
  function save() {
    if (!editing) return;
    const value = $d("[data-cv-editor-text]").value.trim();
    const btn = $d("[data-cv-editor-save]");
    btn.disabled = true;
    status("Gemmer…");
    let req;
    if (editing.section === "experience") {
      req = json("PUT", "/api/profile/experience", { id: editing.id, description: value });
    } else {
      const field = { summary: "bio", headline: "headline", target_role: "target_role" }[editing.section];
      const payload = {}; payload[field] = value;
      req = json("POST", "/api/profile/summary", payload);
    }
    req.then((res) => {
      if (!res.ok || res.d.success === false) throw new Error();
      track("cv_edit", { section: editing.section });
      closeEditor();
      return refresh();
    }).catch(() => status("Det blev ikke gemt. Prøv igen.", true))
      .then(() => { btn.disabled = false; });
  }
  if (dlg) {
    dlg.querySelectorAll("[data-cv-editor-close]").forEach((b) => b.addEventListener("click", closeEditor));
    $d("[data-cv-editor-save]").addEventListener("click", save);
    $d("[data-cv-editor-coach]").addEventListener("click", coach);
    $d("[data-cv-editor-form]").addEventListener("submit", (e) => { e.preventDefault(); save(); });
  }

  /* ---------------- delegated clicks ---------------- */
  document.addEventListener("click", (e) => {
    const t = e.target.closest ? e.target : null;
    if (!t) return;
    const ed = t.closest("[data-cv-edit]");
    if (ed) { e.preventDefault(); openEditor(ed.getAttribute("data-cv-edit"), ed.getAttribute("data-id"), false); return; }
    const co = t.closest("[data-cv-coach]");
    if (co) { e.preventDefault(); openEditor(co.getAttribute("data-cv-coach"), co.getAttribute("data-id"), true); return; }
    const tp = t.closest("[data-cv-template]");
    if (tp) { e.preventDefault(); savePrefs({ template: tp.getAttribute("data-cv-template") }); return; }
    if (t.closest("[data-cv-toggle-done]")) { showDone = !showDone; renderChecks(); return; }
    if (t.closest("[data-cv-copy]")) {
      e.preventDefault();
      fetch("/profil/cv/download.txt", { credentials: "same-origin" }).then((r) => r.text()).then((txt) => {
        if (navigator.clipboard) return navigator.clipboard.writeText(txt);
        throw new Error();
      }).then(() => { t.closest("[data-cv-copy]").textContent = "Kopieret ✓"; track("cv_export", { fmt: "copy" }); })
        .catch(() => { t.closest("[data-cv-copy]").textContent = "Kunne ikke kopiere"; });
      return;
    }
    const dl = t.closest(".cvs-menu-list a, [data-cv-print]");
    if (dl) track("cv_export", { fmt: dl.hasAttribute("data-cv-print") ? "print" : (dl.getAttribute("href") || "").split(".").pop() });
  });
  document.addEventListener("change", (e) => {
    const t = e.target;
    if (t.matches && t.matches("[data-cv-pref]")) { const p = {}; p[t.getAttribute("data-cv-pref")] = t.checked; savePrefs(p); }
    if (t.matches && t.matches("[data-cv-section]")) savePrefs({ hidden: currentSectionsHidden() });
  });
  document.addEventListener("fm:cv-applied", refresh);

  renderAll();
  track("cv_studio_open", { score: (state.quality || {}).score || 0 });
})();
