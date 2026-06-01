/* ============================================================
   FUTUREMATCH · AI Assistant (app1) — chat.js
   Simulated AI + all AI-driven UI components.
   ============================================================ */
(function () {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const esc = (s) => String(s == null ? "" : s).replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const md = (t) => (window.marked ? window.marked.parse(t) : esc(t).replace(/\n/g, "<br>"));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const thread = $("#thread");
  const scroll = $("#scroll");
  const input = $("#input");
  const send = $("#send");
  const stop = $("#stop");
  const fab = $("#fab");
  const refBar = $("#refBar");
  const rail = $("#rail");

  let sending = false, aborted = false;
  const BOT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';
  const ic = {
    skills:'<svg viewBox="0 0 24 24" fill="none" stroke="#2bb6a6" stroke-width="2"><polygon points="12 2 15.1 8.3 22 9.3 17 14.1 18.2 21 12 17.8 5.8 21 7 14.1 2 9.3 8.9 8.3 12 2"/></svg>',
    experience:'<svg viewBox="0 0 24 24" fill="none" stroke="#34c98a" stroke-width="2"><rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/></svg>',
    education:'<svg viewBox="0 0 24 24" fill="none" stroke="#e0b65a" stroke-width="2"><path d="M22 10v6M2 10l10-5 10 5-10 5z"/><path d="M6 12v5c0 2 4 3 6 3s6-1 6-3v-5"/></svg>',
    courses:'<svg viewBox="0 0 24 24" fill="none" stroke="#e0824f" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>',
    summary:'<svg viewBox="0 0 24 24" fill="none" stroke="#7d88ef" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>',
  };
  const chevDown = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>';

  /* ---------------- scroll helpers ---------------- */
  function down(smooth) { scroll.scrollTo({ top: scroll.scrollHeight, behavior: smooth ? "smooth" : "auto" }); }
  scroll.addEventListener("scroll", () => {
    const d = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight;
    fab.classList.toggle("show", d > 160);
  });
  $("#fab").addEventListener("click", () => down(true));

  /* ---------------- toast ---------------- */
  let toastStack;
  function toast(msg, section) {
    if (!toastStack) { toastStack = document.createElement("div"); toastStack.className = "toast-stack"; document.body.appendChild(toastStack); }
    const t = document.createElement("div");
    t.className = "toast";
    t.innerHTML = `<div class="toast-ic">${ic[section] || ic.summary}</div>
      <div class="toast-body"><div class="toast-msg">${esc(msg)}</div>
        <a class="toast-link" href="profile.html" target="_blank">Vis i profil <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg></a></div>
      <button class="toast-x" aria-label="Luk"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>`;
    t.querySelector(".toast-x").onclick = () => dismissToast(t);
    toastStack.appendChild(t);
    setTimeout(() => dismissToast(t), 5200);
    while (toastStack.children.length > 3) toastStack.firstChild.remove();
  }
  function dismissToast(t) { if (!t.parentNode) return; t.classList.add("out"); setTimeout(() => t.remove(), 300); }

  /* ---------------- profile ring ---------------- */
  let ringScore = 62;
  function setRing(score) {
    ringScore = Math.min(100, score);
    const C = 2 * Math.PI * 15.5;
    $("#ringFg").setAttribute("stroke-dasharray", (C * ringScore / 100) + " " + C);
    $("#ringPct").textContent = ringScore + "%";
  }

  /* ---------------- message builders ---------------- */
  function addUser(text) {
    const r = document.createElement("div");
    r.className = "msg user";
    r.innerHTML = `<div class="bubble"></div>`;
    r.querySelector(".bubble").textContent = text;
    thread.appendChild(r); down();
  }
  function addBot() {
    const r = document.createElement("div");
    r.className = "msg";
    r.innerHTML = `<div class="av-bot">${BOT}</div><div class="bot-body"></div>`;
    thread.appendChild(r);
    return r.querySelector(".bot-body");
  }
  function thinking(body) {
    const t = document.createElement("div");
    t.className = "think";
    t.innerHTML = `<span class="d"></span><span class="d"></span><span class="d"></span>`;
    body.appendChild(t); down();
    return t;
  }
  async function streamMd(body, text) {
    const el = document.createElement("div");
    el.className = "md";
    body.appendChild(el);
    const tokens = text.split(/(\s+)/);
    let acc = "";
    for (let i = 0; i < tokens.length; i++) {
      if (aborted) break;
      acc += tokens[i];
      el.innerHTML = md(acc);
      if (i % 2 === 0) { down(); await sleep(16 + Math.random() * 22); }
    }
    el.innerHTML = md(text);
    down();
  }

  /* ---------------- course cards ---------------- */
  function courseCard(c, featured) {
    const card = document.createElement("div");
    card.className = "course" + (featured ? " featured" : "");
    const meta = c.meta.map((m) => `<span class="cpill${m[2] ? " rating" : ""}"><i class="fa-solid ${m[0]}"></i>${m[1]}</span>`).join("");
    const variants = (c.variants || []).map((v) => `
      <div class="variant">
        <div class="vdate"><i class="fa-solid fa-calendar-day"></i>${v.date}</div>
        <div class="vloc">${v.loc}</div>
        <div class="vseats${v.seats <= 3 ? " low" : ""}">${v.seats <= 3 ? v.seats + " pladser" : "Ledig"}</div>
        <button class="vbook">Vælg</button>
      </div>`).join("");
    card.innerHTML = `
      <div class="course-h">
        <div class="course-thumb"><i class="fa-solid ${c.icon}"></i></div>
        <div class="course-main">
          ${featured ? '<div class="course-featured-tag"><i class="fa-solid fa-wand-magic-sparkles"></i> Bedste match</div>' : ""}
          <div class="course-kick">${esc(c.vendor)}</div>
          <div class="course-title">${esc(c.title)}</div>
        </div>
        <div class="course-price">${c.old ? `<span class="old">${c.old}</span>` : ""}<span class="p">${c.price}</span>${c.agree ? '<span class="agree">Aftalepris</span>' : ""}</div>
        <div class="course-chev">${chevDown}</div>
      </div>
      <div class="course-meta">${meta}</div>
      <div class="course-exp"><div class="course-exp-in"><div class="course-exp-pad">
        <div class="course-summary">${esc(c.summary)}</div>
        ${variants ? `<div class="variants"><div class="variants-h">Kommende hold</div>${variants}</div>` : ""}
        <div class="course-actions">
          <button class="c-primary"><i class="fa-solid fa-cart-plus"></i> Bestil til team</button>
          <button class="c-sec attach"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg> Vedhæft</button>
          <button class="c-sec det"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg> Side</button>
        </div>
      </div></div></div>
      <div class="course-foot"><span class="cpill"><i class="fa-solid fa-layer-group"></i>${(c.variants || []).length} hold</span><span class="expand-hint">Se hold &amp; detaljer ${chevDown}</span></div>`;

    const head = card.querySelector(".course-h");
    const foot = card.querySelector(".expand-hint");
    const toggle = () => {
      const open = card.classList.toggle("open");
      foot.innerHTML = (open ? "Skjul detaljer " : "Se hold &amp; detaljer ") + chevDown;
    };
    head.addEventListener("click", toggle);
    foot.addEventListener("click", (e) => { e.stopPropagation(); toggle(); });
    card.querySelector(".attach").addEventListener("click", (e) => {
      e.stopPropagation();
      attachProduct(c.title);
      const b = e.currentTarget; b.classList.add("attached"); b.innerHTML = '<i class="fa-solid fa-check"></i> Vedhæftet';
      setTimeout(() => { b.classList.remove("attached"); b.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg> Vedhæft'; }, 1600);
    });
    card.querySelector(".c-primary").addEventListener("click", function (e) {
      e.stopPropagation();
      this.classList.add("done"); this.innerHTML = '<i class="fa-solid fa-check"></i> Tilføjet til ordre';
      toast("Tilføjet til ordrekurv · " + c.title, "courses");
    });
    card.querySelectorAll(".vbook").forEach((b) => b.addEventListener("click", function (e) {
      e.stopPropagation();
      card.querySelectorAll(".vbook").forEach((x) => { x.textContent = "Vælg"; x.style.background = ""; x.style.color = ""; });
      this.textContent = "Valgt ✓"; this.style.background = "var(--teal)"; this.style.color = "#042320";
    }));
    return card;
  }
  function addCourses(body, list) {
    const wrap = document.createElement("div");
    wrap.className = "cards";
    list.forEach((c, i) => wrap.appendChild(courseCard(c, i === 0)));
    body.appendChild(wrap); down();
  }

  /* ---------------- suggestion chips ---------------- */
  function addChips(body, items) {
    if (!items || !items.length) return;
    const c = document.createElement("div");
    c.className = "chips";
    items.forEach((it) => {
      const b = document.createElement("button");
      b.className = "chip";
      b.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg><span>${esc(it)}</span>`;
      b.onclick = () => ask(it);
      c.appendChild(b);
    });
    body.appendChild(c); down();
  }

  /* ---------------- feedback ---------------- */
  function addFeedback(body, query) {
    const row = document.createElement("div");
    row.className = "fb";
    row.innerHTML = `
      <button class="up" title="Godt svar"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/></svg></button>
      <button class="down" title="Dårligt svar"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3zm7-13h2.67A2.31 2.31 0 0 1 22 4v7a2.31 2.31 0 0 1-2.33 2H17"/></svg></button>
      <button class="regen" title="Regenerér"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg> Regenerér</button>
      <button class="copy" title="Kopiér"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button>`;
    const details = document.createElement("div");
    details.className = "fb-details";
    function showDown() {
      details.classList.add("show");
      details.innerHTML = "";
      const reasons = ["Ikke relevant", "Manglende detaljer", "Forkert svar", "For langt"];
      const wrap = document.createElement("div"); wrap.className = "fb-reasons";
      let sel = "";
      reasons.forEach((r) => {
        const b = document.createElement("button"); b.className = "fb-reason"; b.textContent = r;
        b.onclick = () => { sel = r; wrap.querySelectorAll(".fb-reason").forEach((x) => x.classList.remove("sel")); b.classList.add("sel"); };
        wrap.appendChild(b);
      });
      const ta = document.createElement("textarea"); ta.className = "fb-comment"; ta.placeholder = "Valgfri kommentar — hjælper os med at forbedre svarene";
      const sb = document.createElement("button"); sb.className = "fb-send"; sb.textContent = "Send feedback";
      sb.onclick = () => { sb.disabled = true; sb.textContent = "Tak for din feedback ✓"; };
      details.append(wrap, ta, sb);
    }
    row.querySelector(".up").onclick = function () { row.querySelectorAll(".up,.down").forEach((b) => b.classList.add("voted")); this.classList.add("on"); toast("Tak for din feedback", "summary"); };
    row.querySelector(".down").onclick = function () { row.querySelectorAll(".up,.down").forEach((b) => b.classList.add("voted")); this.classList.add("on"); showDown(); };
    row.querySelector(".regen").onclick = () => { const r = body.closest(".msg"); const q = query; if (r) r.remove(); run(q, { skipUser: true }); };
    row.querySelector(".copy").onclick = function () { this.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="#34c98a" stroke-width="2.4"><polyline points="20 6 9 17 4 12"/></svg>'; setTimeout(() => { this.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>'; }, 1400); };
    body.appendChild(row); body.appendChild(details); down();
  }

  /* ---------------- collapse → pill ---------------- */
  function collapseToPill(card, label, dismiss) {
    const wrap = document.createElement("div"); wrap.className = "pcard-collapse";
    const inner = document.createElement("div");
    card.parentNode.insertBefore(wrap, card); inner.appendChild(card); wrap.appendChild(inner);
    const pill = document.createElement("button");
    pill.className = "done-pill " + (dismiss ? "dismiss" : "ok");
    pill.innerHTML = `<span class="pc">${dismiss ? "✕" : "✓"}</span><span>${esc(label)}</span><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><polyline points="9 18 15 12 9 6"/></svg>`;
    setTimeout(() => {
      wrap.classList.add("collapsed");
      setTimeout(() => { wrap.replaceWith(pill); }, 360);
    }, dismiss ? 500 : 950);
    pill.onclick = () => {
      const w2 = document.createElement("div"); w2.className = "pcard-collapse collapsed";
      const i2 = document.createElement("div"); i2.appendChild(card); w2.appendChild(i2);
      pill.replaceWith(w2); requestAnimationFrame(() => w2.classList.remove("collapsed"));
      card.classList.remove("dim");
    };
  }

  /* ---------------- profile confirm card ---------------- */
  function profileConfirm(body, opts) {
    const card = document.createElement("div");
    card.className = "pcard";
    card.innerHTML = `
      <div class="pcard-ic">${ic[opts.section] || ic.summary}</div>
      <div class="pcard-body"><div class="pcard-msg"><span class="q">Opdater profil?</span> ${esc(opts.message)}</div>
        ${opts.tags ? `<div class="pcard-tags">${opts.tags.map((t) => `<span class="pcard-tag">${esc(t)}</span>`).join("")}</div>` : ""}
      </div>
      <div class="pcard-actions">
        <button class="p-save">Gem</button>
        <button class="p-chat">${BOT}Svar i chat</button>
        <button class="p-no">Nej tak</button>
      </div>`;
    card.querySelector(".p-save").onclick = function () {
      this.classList.add("done"); this.textContent = "Gemt ✓"; this.disabled = true;
      card.querySelector(".p-no").remove();
      card.querySelector(".pcard-msg").innerHTML = '<span class="q" style="color:var(--green)">✓ Gemt</span> ' + esc(opts.message);
      toast(opts.toast || "Profil opdateret", opts.section);
      setRing(Math.min(100, ringScore + (opts.bump || 8)));
      collapseToPill(card, opts.label || "Profil opdateret", false);
    };
    card.querySelector(".p-no").onclick = function () {
      card.classList.add("dim");
      card.querySelector(".pcard-actions").innerHTML = '<span style="font-size:12px;color:var(--ink-3)">Afvist</span>';
      collapseToPill(card, opts.label || "Opdatering", true);
    };
    card.querySelector(".p-chat").onclick = () => {
      card.classList.add("dim");
      card.querySelector(".pcard-actions").innerHTML = '<span style="font-size:12px;color:var(--teal)">Svarer i chat…</span>';
      input.value = 'Ang. "' + opts.message.substring(0, 60) + '": '; input.focus(); resize(); toggleSend();
    };
    body.appendChild(card); down();
  }

  /* ---------------- UI card (form) ---------------- */
  function uiCard(body, opts) {
    const card = document.createElement("div");
    card.className = "pcard ui";
    const fields = opts.fields.map((f) => {
      if (f.type === "select") {
        return `<div class="pfield"><label>${esc(f.label)}</label><select data-name="${f.name}"><option value="">${esc(f.label)}</option>${f.options.map((o) => `<option>${esc(o)}</option>`).join("")}</select></div>`;
      }
      const hint = f.hint ? `<span class="hint">${esc(f.hint)}</span>` : "";
      return `<div class="pfield"><label>${esc(f.label)}</label><input data-name="${f.name}" type="${f.type || "text"}" placeholder="${esc(f.ph || f.label)}">${hint}</div>`;
    }).join("");
    card.innerHTML = `
      <div class="pcard-ic">${ic[opts.section] || ic.summary}</div>
      <div class="pcard-body"><div class="pcard-msg">${esc(opts.message)}</div>
        ${opts.tags ? `<div class="pcard-tags">${opts.tags.map((t) => `<span class="pcard-tag">${esc(t)}</span>`).join("")}</div>` : ""}
        <div class="pcard-fields">${fields}</div>
      </div>
      <div class="pcard-actions">
        <button class="p-save">Gem</button>
        <button class="p-chat">${BOT}Svar i chat</button>
        <button class="p-no">Nej tak</button>
      </div>`;
    card.querySelector(".p-save").onclick = function () {
      this.classList.add("done"); this.textContent = "Gemt ✓"; this.disabled = true;
      card.querySelector(".p-no").remove();
      card.querySelectorAll("input,select").forEach((i) => { i.disabled = true; i.style.opacity = ".6"; });
      toast(opts.toast || "Profil opdateret", opts.section);
      setRing(Math.min(100, ringScore + (opts.bump || 10)));
      collapseToPill(card, opts.label || "Tilføjet", false);
    };
    card.querySelector(".p-no").onclick = function () {
      card.classList.add("dim"); card.querySelector(".pcard-actions").innerHTML = '<span style="font-size:12px;color:var(--ink-3)">Afvist</span>';
      collapseToPill(card, opts.label || "Opdatering", true);
    };
    card.querySelector(".p-chat").onclick = () => { input.value = "Lad mig fortælle: "; input.focus(); resize(); toggleSend(); };
    body.appendChild(card); down();
  }

  /* ---------------- product reference bar ---------------- */
  let attached = [];
  function attachProduct(title) {
    if (attached.includes(title)) return;
    attached.push(title); renderRef(); input.focus();
  }
  function renderRef() {
    refBar.innerHTML = "";
    refBar.classList.toggle("show", attached.length > 0);
    attached.forEach((t) => {
      const c = document.createElement("div"); c.className = "ref-chip";
      c.innerHTML = `<svg class="thumb" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg><span>${esc(t)}</span><button class="ref-x"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>`;
      c.querySelector(".ref-x").onclick = () => { attached = attached.filter((x) => x !== t); renderRef(); };
      refBar.appendChild(c);
    });
  }

  /* ============================================================
     SIMULATED AI — scenario router
     ============================================================ */
  const COURSES = {
    projekt: [
      { vendor: "Teknologisk Institut", icon: "fa-diagram-project", title: "Projektledelse Praktisk", price: "8.900 kr", old: "10.500 kr", agree: true, summary: "Et intensivt 3-dages forløb der giver projektledere konkrete værktøjer til planlægning, interessenthåndtering og eksekvering — med øvelser baseret på egne projekter.", meta: [["fa-clock", "3 dage"], ["fa-location-dot", "København"], ["fa-certificate", "Certifikat"], ["fa-star", "4,8", true]], variants: [{ date: "12. jun 2026", loc: "København K", seats: 8 }, { date: "3. sep 2026", loc: "Aarhus C", seats: 2 }, { date: "21. okt 2026", loc: "Online live", seats: 12 }] },
      { vendor: "IBC Innovationsfabrikken", icon: "fa-bolt", title: "Agil Projektledelse & Scrum", price: "6.400 kr", summary: "Lær at arbejde agilt med backlog, sprints og ceremonier der faktisk fungerer i praksis. Velegnet til teams der vil levere hurtigere.", meta: [["fa-clock", "2 dage"], ["fa-display", "Online"], ["fa-users", "Holdundervisning"]], variants: [{ date: "18. jun 2026", loc: "Online live", seats: 15 }, { date: "9. sep 2026", loc: "Kolding", seats: 6 }] },
      { vendor: "Mannaz", icon: "fa-toolbox", title: "Projektlederens Værktøjskasse", price: "11.200 kr", summary: "Det komplette forløb for erfarne projektledere der vil skærpe metoder, ledelse og forretningsforståelse.", meta: [["fa-clock", "4 dage"], ["fa-location-dot", "Frederiksberg"], ["fa-star", "4,6", true]], variants: [{ date: "25. aug 2026", loc: "Frederiksberg", seats: 4 }] },
    ],
    excel: [
      { vendor: "Mannaz", icon: "fa-table-cells", title: "Excel for Analytikere", price: "5.400 kr", agree: true, summary: "Pivot-tabeller, Power Query og dashboards til datadrevne beslutninger. Hands-on med realistiske datasæt.", meta: [["fa-clock", "2 dage"], ["fa-display", "Online"], ["fa-certificate", "Bevis"]], variants: [{ date: "16. jun 2026", loc: "Online live", seats: 20 }, { date: "2. sep 2026", loc: "København", seats: 7 }] },
      { vendor: "Smart Learning", icon: "fa-chart-line", title: "Dataanalyse med Power BI", price: "7.800 kr", summary: "Byg interaktive rapporter og modeller i Power BI fra grunden — perfekt opfølgning på Excel.", meta: [["fa-clock", "3 dage"], ["fa-display", "Online"], ["fa-star", "4,7", true]], variants: [{ date: "23. jun 2026", loc: "Online live", seats: 11 }] },
    ],
    gratis: [
      { vendor: "Microsoft Learning", icon: "fa-cloud", title: "Azure Fundamentals (intro)", price: "Gratis", summary: "Gratis introduktionsmodul til cloud-begreber og Azure-services. Selvstudie i eget tempo.", meta: [["fa-clock", "4 timer"], ["fa-display", "Online"], ["fa-infinity", "Selvstudie"]], variants: [{ date: "Når som helst", loc: "Online · selvstudie", seats: 99 }] },
      { vendor: "Futurematch Academy", icon: "fa-shield-halved", title: "GDPR Grundkursus", price: "Gratis", summary: "Få styr på de vigtigste GDPR-principper for medarbejdere. Inkluderer quiz og bevis.", meta: [["fa-clock", "1,5 time"], ["fa-display", "Online"], ["fa-certificate", "Bevis"]], variants: [{ date: "Når som helst", loc: "Online · selvstudie", seats: 99 }] },
    ],
    ledelse: [
      { vendor: "CBS Executive", icon: "fa-users-gear", title: "Leadership Essentials", price: "12.500 kr", agree: true, summary: "Grundlæggende ledelse for nye og kommende ledere — selvindsigt, feedback og teamudvikling.", meta: [["fa-clock", "4 dage"], ["fa-location-dot", "Frederiksberg"], ["fa-star", "4,8", true]], variants: [{ date: "1. sep 2026", loc: "Frederiksberg", seats: 5 }, { date: "10. nov 2026", loc: "Aarhus", seats: 9 }] },
      { vendor: "Mannaz", icon: "fa-people-arrows", title: "Forandringsledelse", price: "10.900 kr", summary: "Led mennesker gennem forandring med tillid og tempo. Praktiske modeller og cases.", meta: [["fa-clock", "3 dage"], ["fa-location-dot", "København"]], variants: [{ date: "15. sep 2026", loc: "København K", seats: 3 }] },
    ],
  };

  const SCN = [
    { test: /sammenlign|forskel|versus|\bvs\b/i, run: scnCompare },
    { test: /tilføj.*(erfaring|uddannelse|stilling)|jobtitel|certificer|min stilling/i, run: scnUiCard },
    { test: /opdater.*cv|min baggrund|jeg arbejder|har erfaring|erfaring med|min profil/i, run: scnProfile },
    { test: /gratis|free/i, run: (b, q) => scnCourses(b, q, "gratis") },
    { test: /excel|power ?bi|analytiker|dataanalyse/i, run: (b, q) => scnCourses(b, q, "excel") },
    { test: /projekt|scrum|agil/i, run: (b, q) => scnCourses(b, q, "projekt") },
    { test: /ledelse|leder|leadership|forandring/i, run: (b, q) => scnCourses(b, q, "ledelse") },
    { test: /hej|hjælp|kan du|hvad kan|guide|kom i gang/i, run: scnHelp },
  ];

  async function scnCourses(body, q, key) {
    const list = COURSES[key] || COURSES.projekt;
    const intro = {
      projekt: "Jeg har fundet **3 stærke match** til projektledelse, der passer til et hold og holder sig inden for et typisk afdelingsbudget. Teknologisk Institut er det mest komplette valg:",
      excel: "Her er **2 oplagte forløb** til datadrevne roller. Start med Excel for Analytikere — den dækker det meste — og byg videre med Power BI:",
      gratis: "Disse kurser er **helt gratis** og kan tages i eget tempo. Gode til onboarding og bred opkvalificering:",
      ledelse: "Til lederudvikling anbefaler jeg disse **2 forløb**. Leadership Essentials passer bedst til nye ledere:",
    }[key];
    await streamMd(body, intro);
    if (aborted) return;
    await sleep(180);
    addCourses(body, list);
    addChips(body, ["Sammenlign de to billigste", "Kun online hold", "Tjek budget for Udvikling", "Hvad indgår i prisen?"]);
  }

  async function scnCompare(body) {
    await streamMd(body, "Her er en **sammenligning** af de to mest populære projektledelseskurser:\n\n| | Projektledelse Praktisk | Agil Projektledelse |\n|---|---|---|\n| **Pris** | 8.900 kr | 6.400 kr |\n| **Varighed** | 3 dage | 2 dage |\n| **Format** | Fysisk + online | Online |\n| **Niveau** | Begynder–øvet | Alle |\n| **Certifikat** | Ja | Bevis |\n\nVælg **Praktisk** hvis I vil have et anerkendt certifikat og fysisk netværk — eller **Agil** hvis pris og fleksibilitet vejer tungest.");
    if (aborted) return;
    addChips(body, ["Vis begge kurser", "Hvilket passer til 4 personer?", "Book et infomøde"]);
  }

  async function scnProfile(body) {
    await streamMd(body, "Tak — det hjælper mig med at give bedre anbefalinger. Det lyder som om du har erfaring med **projektledelse og teamledelse**. Vil du gemme det på din profil, så jeg kan tilpasse fremtidige forslag?");
    if (aborted) return;
    await sleep(150);
    profileConfirm(body, {
      section: "experience",
      message: "Tilføj “Projektledelse” og “Teamledelse” til dine kompetencer.",
      tags: ["Projektledelse", "Teamledelse"],
      toast: "2 kompetencer tilføjet til din profil",
      label: "Kompetencer tilføjet",
      bump: 10,
    });
    addChips(body, ["Anbefal kurser ud fra min profil", "Tilføj min uddannelse", "Hvad mangler min profil?"]);
  }

  async function scnUiCard(body) {
    await streamMd(body, "Godt — lad os tilføje det til din profil. Udfyld de felter du kender, så gemmer jeg det med det samme:");
    if (aborted) return;
    await sleep(150);
    uiCard(body, {
      section: "experience",
      message: "Tilføj erhvervserfaring",
      tags: ["Forudfyldt fra samtale"],
      fields: [
        { name: "title", label: "Stilling", ph: "fx Projektleder" },
        { name: "company", label: "Virksomhed", ph: "fx Nordi A/S" },
        { name: "start", label: "Startår", type: "number", ph: "2021" },
        { name: "end", label: "Slutår", type: "number", ph: "Nu", hint: "Tom = stadig ansat" },
        { name: "type", label: "Ansættelse", type: "select", options: ["Fuldtid", "Deltid", "Freelance", "Praktik"] },
      ],
      toast: "Erhvervserfaring gemt på din profil",
      label: "Erfaring tilføjet",
      bump: 12,
    });
  }

  async function scnHelp(body) {
    await streamMd(body, "Hej! Jeg er **Futurematch Kursusrådgiver**. Jeg kan hjælpe dig med at:\n\n- **Finde kurser** ud fra rolle, behov eller budget\n- **Sammenligne** muligheder side om side\n- **Bestille** til dig selv eller hele teamet\n- **Holde din profil opdateret**, så anbefalinger bliver skarpere\n\nHvad vil du starte med?");
    if (aborted) return;
    addChips(body, ["Vis populære kurser", "Projektledelse til 4 personer", "Gratis kurser", "Opdater mit CV"]);
  }

  function route(q) { const m = SCN.find((s) => s.test.test(q)); return m ? m.run : (b) => scnCourses(b, q, "projekt"); }

  /* ---------------- send / run ---------------- */
  async function run(query, opts = {}) {
    if (sending) return;
    sending = true; aborted = false;
    document.querySelector(".welcome")?.remove();
    if (!opts.skipUser) addUser(query);
    input.value = ""; resize(); toggleSend();
    setSending(true);
    const body = addBot();
    const th = thinking(body);
    await sleep(620 + Math.random() * 350);
    if (aborted) { th.remove(); body.closest(".msg").remove(); finish(); return; }
    th.remove();
    try {
      await route(query)(body, query);
      if (!aborted) addFeedback(body, query);
    } catch (e) {
      body.innerHTML = `<div class="err"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg><span>Der opstod en fejl. Prøv igen.</span><button class="retry">Prøv igen</button></div>`;
      body.querySelector(".retry").onclick = () => { body.closest(".msg").remove(); run(query, { skipUser: true }); };
    }
    finish();
  }
  function finish() { sending = false; setSending(false); toggleSend(); input.focus(); }
  function ask(text) { run(text); }
  window.fmAsk = ask;

  function setSending(on) {
    send.style.display = on ? "none" : "grid";
    stop.classList.toggle("show", on);
  }
  stop.onclick = () => { aborted = true; };

  /* ---------------- input ---------------- */
  function resize() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 150) + "px"; }
  function toggleSend() { send.classList.toggle("on", input.value.trim().length > 0); }
  input.addEventListener("input", () => { resize(); toggleSend(); });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); if (input.value.trim()) run(input.value.trim()); } });
  send.onclick = () => { if (input.value.trim()) run(input.value.trim()); };

  /* ---------------- welcome ---------------- */
  function welcome() {
    thread.innerHTML = `
      <div class="welcome">
        <div class="w-logo">${BOT}</div>
        <div class="w-eyebrow">Futurematch kursusrådgiver</div>
        <div class="w-title">Hvad skal dit team lære?</div>
        <div class="w-sub">Beskriv et behov, en rolle eller en kompetence — så finder jeg relevante kurser, sammenligner muligheder og foreslår hold.</div>
        <div class="w-hint">Logget ind som <b>Mette Krogh</b> — anbefalinger tilpasses din profil</div>
        <div class="w-grid">
          <button class="w-card" data-q="Vis mig populære projektledelseskurser"><span class="ic"><i class="fa-solid fa-diagram-project"></i></span><span><div class="t">Populære kurser</div><div class="h">Se hvad andre vælger</div></span></button>
          <button class="w-card" data-q="Hvilke kurser er gratis?"><span class="ic"><i class="fa-solid fa-gift"></i></span><span><div class="t">Gratis kurser</div><div class="h">Kom i gang uden omkostninger</div></span></button>
          <button class="w-card" data-q="Vis ledelseskurser til mellemledere"><span class="ic"><i class="fa-solid fa-users-gear"></i></span><span><div class="t">Ledelseskurser</div><div class="h">Udvikl dine lederevner</div></span></button>
          <button class="w-card" data-q="Opdater mit CV — jeg har erfaring med projektledelse og teamledelse"><span class="ic"><i class="fa-solid fa-id-card"></i></span><span><div class="t">Opdater dit CV</div><div class="h">Fortæl mig om din erfaring</div></span></button>
        </div>
      </div>`;
    thread.querySelectorAll(".w-card").forEach((c) => c.onclick = () => ask(c.dataset.q));
  }
  function newChat() { attached = []; renderRef(); welcome(); refreshConv(); input.value = ""; toggleSend(); }
  window.fmNewChat = newChat;

  /* ---------------- conversation list ---------------- */
  const CONVS = [
    { id: 1, t: "Projektledelse til 4 medarbejdere", g: "today", active: true },
    { id: 2, t: "Excel-kurser for analytikere", g: "today" },
    { id: 3, t: "Leadership-forløb Q3", g: "today" },
    { id: 4, t: "GDPR compliance hold", g: "yesterday" },
    { id: 5, t: "Scrum Master certificering", g: "yesterday" },
    { id: 6, t: "Onboarding-pakke til nye", g: "older" },
  ];
  function refreshConv() {
    const list = $("#convList");
    const groups = [["today", "I dag"], ["yesterday", "I går"], ["older", "Ældre"]];
    list.innerHTML = groups.map(([k, lab]) => {
      const items = CONVS.filter((c) => c.g === k);
      if (!items.length) return "";
      return `<div class="conv-date">${lab}</div>` + items.map((c) => `
        <div class="conv${c.active ? " active" : ""}" data-id="${c.id}">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
          <span>${esc(c.t)}</span>
          <button class="conv-del" data-id="${c.id}" title="Slet"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg></button>
        </div>`).join("");
    }).join("");
    list.querySelectorAll(".conv").forEach((el) => el.onclick = () => {
      CONVS.forEach((c) => c.active = false);
      const c = CONVS.find((x) => x.id == el.dataset.id); if (c) c.active = true;
      refreshConv(); if (window.innerWidth <= 860) rail.classList.remove("open");
    });
    list.querySelectorAll(".conv-del").forEach((b) => b.onclick = (e) => {
      e.stopPropagation();
      const i = CONVS.findIndex((c) => c.id == b.dataset.id);
      if (i > -1) CONVS.splice(i, 1); refreshConv();
    });
  }

  /* ---------------- sidebar / nudge / misc ---------------- */
  $("#railToggle").onclick = () => { if (window.innerWidth <= 860) rail.classList.toggle("open"); else rail.classList.toggle("collapsed"); };
  $("#menuBtn").onclick = () => rail.classList.add("open");
  $("#overlay").onclick = () => rail.classList.remove("open");
  document.querySelectorAll("[data-new]").forEach((b) => b.onclick = newChat);
  $("#nudgeX").onclick = () => $("#nudge").classList.remove("show");
  $("#nudgeLink").onclick = (e) => { e.preventDefault(); $("#nudge").classList.remove("show"); ask("Hvad mangler min profil?"); };

  /* ---------------- init ---------------- */
  setRing(62);
  refreshConv();
  welcome();
  renderRef();
  setTimeout(() => $("#nudge").classList.add("show"), 1400);
  input.focus();
})();
