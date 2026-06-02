/* ============================================================
   FUTUREMATCH · AI Assistant (app1) — chat.js
   Simulated AI + all AI-driven UI components.
   ============================================================ */
(function () {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  // Icon/CSS-class values are also interpolated into an inline onerror JS string,
  // so they must be restricted to a safe token charset (no quotes/brackets) to
  // prevent breakout. Falls back to a neutral icon when empty/invalid.
  const icon = (s) => (String(s == null ? "" : s).replace(/[^a-z0-9 _-]/gi, "").slice(0, 40) || "fa-graduation-cap");

  /* ---------------- HTML sanitizer ----------------
     Agent/assistant content (markdown-rendered chunks, profile_update messages,
     and the legacy pre-rendered product HTML) is inserted via innerHTML. Since
     that content originates from the model, sanitize it to neutralise stored
     XSS: drop dangerous elements (script/style/iframe/object/embed/...), strip
     on* event-handler attributes and javascript:/data-executable URLs, while
     preserving the normal formatting (headings, lists, links, code, images,
     tables) that marked produces. Dependency-free and offline-safe; if a
     trusted sanitizer (DOMPurify) is already loaded we prefer it. */
  const FORBIDDEN_TAGS = new Set([
    "script", "style", "iframe", "object", "embed", "link", "meta", "base",
    "form", "noscript", "template", "frame", "frameset", "applet",
  ]);
  const URL_ATTRS = ["href", "src", "xlink:href", "action", "formaction", "background", "poster"];
  const SAFE_URL = /^(?:https?:|mailto:|tel:|ftp:|\/|#|\.|data:image\/(?:png|jpe?g|gif|webp|svg\+xml|avif))/i;

  function sanitizeNode(root) {
    // Walk the whole subtree; collect nodes first so removal during iteration is safe.
    const all = root.querySelectorAll("*");
    for (let i = all.length - 1; i >= 0; i--) {
      const el = all[i];
      const tag = (el.tagName || "").toLowerCase();
      if (FORBIDDEN_TAGS.has(tag)) { el.remove(); continue; }
      // Copy attribute list because we mutate it while iterating.
      const attrs = Array.prototype.slice.call(el.attributes || []);
      for (const attr of attrs) {
        const name = (attr.name || "").toLowerCase();
        const val = attr.value || "";
        // Strip all inline event handlers (onclick, onerror, onload, ...).
        if (name.startsWith("on")) { el.removeAttribute(attr.name); continue; }
        // Block style attributes to avoid expression()/url(javascript:) vectors.
        if (name === "style") { el.removeAttribute(attr.name); continue; }
        // srcdoc on a (forbidden) iframe would be gone, but strip defensively.
        if (name === "srcdoc") { el.removeAttribute(attr.name); continue; }
        // Validate URL-bearing attributes; remove anything that isn't an allowlisted scheme.
        if (URL_ATTRS.indexOf(name) !== -1) {
          // Strip whitespace + control chars (NUL..0x1F, 0x7F) so tricks like
          // "java\tscript:" / "java\nscript:" can't smuggle a bad scheme past the check.
          const normalized = val.replace(/[\x00-\x1f\x7f\s]+/g, "").toLowerCase();
          if (/^(?:javascript|vbscript):/i.test(normalized) || /^data:text\/html/i.test(normalized)) {
            el.removeAttribute(attr.name);
            continue;
          }
          // If there's a value and it's neither an allowlisted scheme nor a safe
          // relative/anchor form, drop the attribute.
          if (normalized && !SAFE_URL.test(normalized) && !SAFE_URL.test(val.trim())) {
            el.removeAttribute(attr.name);
          }
        }
      }
    }
    return root;
  }

  function sanitizeHtml(html) {
    const str = String(html == null ? "" : html);
    if (!str) return "";
    // Prefer a trusted sanitizer if one is present on the page.
    if (window.DOMPurify && typeof window.DOMPurify.sanitize === "function") {
      try { return window.DOMPurify.sanitize(str, { FORBID_TAGS: ["style"] }); } catch (e) { /* fall through */ }
    }
    let tpl;
    try {
      tpl = document.createElement("template");
      tpl.innerHTML = str;                 // inert parse: no scripts run, no resources load
      sanitizeNode(tpl.content);
      return tpl.innerHTML;
    } catch (e) {
      // As a last resort, fall back to text-only (escaped) rendering.
      return esc(str);
    }
  }

  const md = (t) => sanitizeHtml(window.marked ? window.marked.parse(t) : esc(t).replace(/\n/g, "<br>"));
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
        <a class="toast-link" href="/profile" target="_blank">Vis i profil <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg></a></div>
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

  /* ---------------- course cards ---------------- */
  function courseCard(c, featured) {
    const card = document.createElement("div");
    card.className = "course" + (featured ? " featured" : "");
    const meta = c.meta.map((m) => `<span class="cpill${m[2] ? " rating" : ""}"><i class="fa-solid ${icon(m[0])}"></i>${esc(m[1])}</span>`).join("");
    const variants = (c.variants || []).map((v) => `
      <div class="variant">
        <div class="vdate"><i class="fa-solid fa-calendar-day"></i>${esc(v.date)}</div>
        <div class="vloc">${esc(v.loc)}</div>
        <div class="vseats${v.seats <= 3 ? " low" : ""}">${v.seats <= 3 ? v.seats + " pladser" : "Ledig"}</div>
        <button class="vbook">Vælg</button>
      </div>`).join("");
    card.innerHTML = `
      <div class="course-h">
        <div class="course-thumb${c.image ? " has-img" : ""}">${c.image ? `<img src="${esc(c.image)}" alt="${esc(c.title)}" loading="lazy" onerror="this.parentNode.classList.remove('has-img');this.replaceWith(Object.assign(document.createElement('i'),{className:'fa-solid ${icon(c.icon)}'}))">` : `<i class="fa-solid ${icon(c.icon)}"></i>`}</div>
        <div class="course-main">
          ${featured ? '<div class="course-featured-tag"><i class="fa-solid fa-wand-magic-sparkles"></i> Bedste match</div>' : ""}
          <div class="course-kick">${esc(c.vendor)}</div>
          <div class="course-title">${esc(c.title)}</div>
        </div>
        <div class="course-price">${c.old ? `<span class="old">${esc(c.old)}</span>` : ""}<span class="p">${esc(c.price)}</span>${c.agree ? '<span class="agree">Aftalepris</span>' : ""}</div>
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
    // "Side" opens the real catalog product page when we have a handle.
    const det = card.querySelector(".det");
    if (det && c.handle) det.addEventListener("click", (e) => { e.stopPropagation(); window.open("/products/" + encodeURIComponent(c.handle), "_blank"); });
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
  // Persist a proposed profile update to the real app1 backend.
  async function saveProfileUpdate(action, data) {
    const resp = await fetch("/app1/confirm_profile_update", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: action, data: data || {} }),
    });
    if (!resp.ok) throw new Error("save_failed");
    const r = await resp.json();
    if (!r || r.status !== "success") throw new Error((r && r.message) || "save_failed");
    return r;
  }

  function profileConfirm(body, opts, onSave) {
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
    card.querySelector(".p-save").onclick = async function () {
      if (onSave) {
        const old = this.textContent; this.disabled = true; this.textContent = "…";
        try { await onSave(); }
        catch (e) { this.disabled = false; this.textContent = "Prøv igen"; setTimeout(() => { this.textContent = old; }, 1800); return; }
      }
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
  function uiCard(body, opts, onSave) {
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
    // Show the agent's pre-filled values so the user can see and edit them.
    if (opts.prefilled) {
      card.querySelectorAll("[data-name]").forEach((el) => {
        const pv = opts.prefilled[el.getAttribute("data-name")];
        if (pv == null || pv === "") return;
        if (el.tagName === "SELECT") {
          let matched = false;
          el.querySelectorAll("option").forEach((o) => { if (o.value === String(pv) || o.textContent === String(pv)) { o.selected = true; matched = true; } });
          if (!matched) { const o = document.createElement("option"); o.textContent = String(pv); o.selected = true; el.appendChild(o); }
        } else { el.value = String(pv); }
      });
    }
    card.querySelector(".p-save").onclick = async function () {
      const values = {};
      card.querySelectorAll("[data-name]").forEach((i) => { const v = (i.value || "").trim(); if (v) values[i.getAttribute("data-name")] = v; });
      if (onSave) {
        const old = this.textContent; this.disabled = true; this.textContent = "…";
        try { await onSave(values); }
        catch (e) { this.disabled = false; this.textContent = "Prøv igen"; setTimeout(() => { this.textContent = old; }, 1800); return; }
      }
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
  // Injected backend product cards call this from their "Spørg om" button.
  window.attachProductToChat = function (handle, title) { attachProduct(title || handle); };
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
     REAL AI — talks to app1's streaming /ask endpoint (SSE)
     POST /app1/ask  {query}  →  data: {json}\n\n ... data: [DONE]
     event types: meta | chunk | product | suggestions | notice |
                  thinking | ping | profile_confirm_request |
                  profile_update | ui_card
     ============================================================ */
  const ASK_URL = "/app1/ask";

  function injectProductHtml(body, html) {
    if (!html) return;
    let cards = body.querySelector(".cards");
    if (!cards) { cards = document.createElement("div"); cards.className = "cards"; body.appendChild(cards); }
    const wrap = document.createElement("div");
    wrap.innerHTML = sanitizeHtml(html);
    cards.appendChild(wrap);
    down();
  }

  function showError(body, query) {
    body.innerHTML = `<div class="err"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg><span>Der opstod en fejl. Prøv igen.</span><button class="retry">Prøv igen</button></div>`;
    body.querySelector(".retry").onclick = () => { body.closest(".msg").remove(); run(query, { skipUser: true }); };
  }

  async function streamFromBackend(body, query) {
    // Reference any attached products the same way the real app1 UI does.
    let actualQuery = query;
    if (attached.length) {
      const refs = attached.map((t) => `[VEDHÆFTET KURSUS: "${t}"]`).join(" ");
      actualQuery = refs + "\n" + query;
    }

    const resp = await fetch(ASK_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: actualQuery }),
    });
    if (!resp.ok || !resp.body) throw new Error("HTTP " + resp.status);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "", textEl = null, fullText = "", suggestions = null, done = false;
    let cardsSeen = 0, productSeen = 0;   // pair structured course_cards with fallback product HTML

    while (!done) {
      if (aborted) { try { reader.cancel(); } catch (e) {} break; }
      const r = await reader.read();
      if (r.done) break;
      buffer += decoder.decode(r.value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop();
      for (const part of parts) {
        const line = part.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        const raw = line.slice(6).trim();
        if (raw === "[DONE]") { done = true; break; }
        let data;
        try { data = JSON.parse(raw); } catch (e) { continue; }

        if (data.type === "ping" || data.type === "meta" || data.type === "thinking") continue;

        if (data.type === "chunk") {
          if (!textEl) { textEl = document.createElement("div"); textEl.className = "md"; body.appendChild(textEl); }
          fullText += (data.content || "");
          textEl.innerHTML = md(fullText);
          down();
        } else if (data.type === "course_cards") {
          // Structured course data -> render with the native Futurematch courseCard design.
          if (data.items && data.items.length) { cardsSeen++; addCourses(body, data.items); }
        } else if (data.type === "product") {
          // Fallback: only inject the legacy pre-rendered HTML if this batch had no course_cards.
          productSeen++;
          if (productSeen > cardsSeen) injectProductHtml(body, data.html);
        } else if (data.type === "suggestions") {
          suggestions = data.items || [];
        } else if (data.type === "notice") {
          const note = document.createElement("div");
          note.className = "md"; note.style.opacity = ".7"; note.style.fontStyle = "italic";
          note.textContent = data.content || "";
          body.appendChild(note); down();
        } else if (data.type === "profile_update") {
          const note = document.createElement("div");
          note.className = "md";
          note.innerHTML = md(data.message || "Profil opdateret");
          body.appendChild(note); down();
        } else if (data.type === "profile_confirm_request") {
          // Proposed profile change -> native confirm card, wired to the real save.
          const conf = data.confirm || {};
          const tags = (conf.data && typeof conf.data === "object")
            ? Object.values(conf.data).filter(Boolean).map(String) : [];
          profileConfirm(body, { section: data.section, message: data.message || "", tags: tags, section_label: data.section },
            conf.action ? () => saveProfileUpdate(conf.action, conf.data || {}) : null);
        } else if (data.type === "ui_card") {
          // Form card -> native uiCard design, wired to the real save endpoint.
          const fields = (data.fields || []).map((f) => ({
            name: f.name, label: f.label, type: f.type,
            ph: f.placeholder || f.ph, hint: f.hint, options: f.options || [],
          }));
          const prefilled = data.prefilled || {};
          uiCard(body, { section: data.section, message: data.message || "", fields: fields, prefilled: prefilled },
            data.save_action ? (values) => saveProfileUpdate(data.save_action, Object.assign({}, prefilled, values)) : null);
        }
      }
    }
    // Render suggestion chips last, like the source UI.
    if (suggestions && suggestions.length) addChips(body, suggestions);
    return fullText;
  }

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
    try {
      await streamFromBackend(body, query);
      th.remove();
      if (!aborted) addFeedback(body, query);
    } catch (e) {
      th.remove();
      showError(body, query);
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
        <div class="w-hint">Anbefalinger tilpasses din profil</div>
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
