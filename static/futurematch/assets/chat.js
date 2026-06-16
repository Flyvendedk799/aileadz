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
  const isProfiler = window.CHAT_MODE === "profiler";

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

  async function refreshWorkspaceStatus() {
    const bar = $("#aiWorkspaceStatus");
    if (!bar) return;
    try {
      const resp = await fetch("/api/profile/mindmap", {
        headers: { "X-Requested-With": "XMLHttpRequest" },
        credentials: "same-origin",
      });
      if (!resp.ok) throw new Error("workspace " + resp.status);
      const data = await resp.json();
      if (!data || data.success === false) throw new Error("workspace_shape");
      const c = data.completeness || {};
      const counts = data.counts || {};
      const pct = $("#aiWsPct"), mem = $("#aiWsMem"), used = $("#aiWsUsed"), nodes = $("#aiWsNodes"), missing = $("#aiWsMissing");
      if (pct) pct.textContent = c.pct != null ? c.pct + "%" : "—";
      if (mem) mem.textContent = counts.memories != null ? counts.memories : "—";
      if (used) used.textContent = counts.used_memories != null ? counts.used_memories : "—";
      if (nodes) nodes.textContent = counts.leaves != null ? counts.leaves : "—";
      if (missing) {
        const missingItems = Array.isArray(c.missing) ? c.missing : [];
        missing.textContent = missingItems.length ? "Mangler: " + missingItems.slice(0, 3).join(", ") : "Profilen er komplet";
      }
      bar.hidden = false;
    } catch (e) {
      bar.hidden = true;
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
  let currentAbort = null;            // AbortController for the in-flight /ask stream
  let lastActualQuery = null;         // last query submitted (after course-ref expansion)
  // No-event watchdog. Once content streams, gaps are tiny (chunks/heartbeats),
  // so 25s safely detects a dead connection. Before the first content event the
  // backend may legitimately be silent for the whole tool loop (the live
  // tool-event heartbeat is env-gated server-side), so allow far longer there.
  const WATCHDOG_MS = 25000;
  const WATCHDOG_FIRST_MS = 90000;
  const BOT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';
  const ic = {
    skills:'<svg viewBox="0 0 24 24" fill="none" stroke="#2bb6a6" stroke-width="2"><polygon points="12 2 15.1 8.3 22 9.3 17 14.1 18.2 21 12 17.8 5.8 21 7 14.1 2 9.3 8.9 8.3 12 2"/></svg>',
    experience:'<svg viewBox="0 0 24 24" fill="none" stroke="#34c98a" stroke-width="2"><rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/></svg>',
    education:'<svg viewBox="0 0 24 24" fill="none" stroke="#e0b65a" stroke-width="2"><path d="M22 10v6M2 10l10-5 10 5-10 5z"/><path d="M6 12v5c0 2 4 3 6 3s6-1 6-3v-5"/></svg>',
    courses:'<svg viewBox="0 0 24 24" fill="none" stroke="#e0824f" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>',
    summary:'<svg viewBox="0 0 24 24" fill="none" stroke="#7d88ef" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>',
    certifications:'<svg viewBox="0 0 24 24" fill="none" stroke="#2bb6a6" stroke-width="2"><path d="M12 2l8 4v5c0 5-3.5 9-8 11-4.5-2-8-6-8-11V6z"/><path d="M9 12l2 2 4-4"/></svg>',
    languages:'<svg viewBox="0 0 24 24" fill="none" stroke="#7d88ef" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15 15 0 0 1 0 20M12 2a15 15 0 0 0 0 20"/></svg>',
    links:'<svg viewBox="0 0 24 24" fill="none" stroke="#e0824f" stroke-width="2"><path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1"/><path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"/></svg>',
  };
  const chevDown = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>';

  /* ---------------- scroll helpers ----------------
     Anchored autoscroll: only follow the stream while the user is (near) the
     bottom. Scrolling up to read detaches the anchor, so new chunks never yank
     the viewport; the fab (or a new question) re-attaches it. */
  let stickToBottom = true;
  function down(smooth, force) {
    if (force) stickToBottom = true;
    if (!stickToBottom) return;
    scroll.scrollTo({ top: scroll.scrollHeight, behavior: smooth ? "smooth" : "auto" });
  }
  scroll.addEventListener("scroll", () => {
    const d = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight;
    stickToBottom = d < 120;
    fab.classList.toggle("show", d > 160);
  });
  $("#fab").addEventListener("click", () => down(true, true));

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
  // Starts at 0 (neutral) until the real profile completeness loads. Never a fake number.
  let ringScore = 0;
  function setRing(score) {
    ringScore = Math.max(0, Math.min(100, Math.round(score) || 0));
    const C = 2 * Math.PI * 15.5;
    const fg = $("#ringFg"), pct = $("#ringPct");
    if (fg) fg.setAttribute("stroke-dasharray", (C * ringScore / 100) + " " + C);
    if (pct) pct.textContent = ringScore + "%";
  }

  /* ---------------- message builders ---------------- */
  function addUser(text) {
    const r = document.createElement("div");
    r.className = "msg user";
    r.innerHTML = `<div class="bubble"></div>`;
    r.querySelector(".bubble").textContent = text;
    thread.appendChild(r); down(false, true);
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
  // Backend 'thinking' events carry a status line ("Søger og analyserer…").
  // Show it next to the dots instead of dropping it on the floor.
  function thinkStatus(body, text) {
    const t = body.querySelector(".think");
    if (!t) return;
    let s = t.querySelector(".think-status");
    if (!s) {
      s = document.createElement("span");
      s.className = "think-status";
      s.style.cssText = "margin-left:8px;font-size:12px;color:var(--ink-3);font-style:italic;";
      t.appendChild(s);
    }
    s.textContent = String(text || "");
  }
  // First real content event makes the dots redundant — remove them.
  function clearThinking(body) {
    const t = body.querySelector(".think");
    if (t) t.remove();
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
    // "Bestil til team" hands the order off to the agent instead of dead-ending
    // in CSS: the composed message triggers the existing confirm-gated
    // check_course_readiness → prepare_course_order flow server-side, so no new
    // side-effect surface is opened here.
    let selectedVariant = null;
    card.querySelector(".c-primary").addEventListener("click", function (e) {
      e.stopPropagation();
      if (!isLoggedIn()) { toast("Log ind for at bestille kurser til dit team", "courses"); return; }
      if (sending) { toast("Vent venligst — assistenten svarer stadig", "courses"); return; }
      const v = selectedVariant;
      const hold = v && v.date ? ` — holdet ${v.date}${v.loc ? " i " + v.loc : ""}` : "";
      ask(`Jeg vil gerne bestille "${c.title}"${hold} til mit team`);
      const btn = this;
      btn.classList.add("done"); btn.innerHTML = '<i class="fa-solid fa-check"></i> Sendt til rådgiveren';
      setTimeout(() => { btn.classList.remove("done"); btn.innerHTML = '<i class="fa-solid fa-cart-plus"></i> Bestil til team'; }, 2600);
    });
    // "Vælg" stores the chosen variant so "Bestil til team" can compose a
    // precise order message (date + location) for the agent.
    card.querySelectorAll(".vbook").forEach((b, vi) => b.addEventListener("click", function (e) {
      e.stopPropagation();
      card.querySelectorAll(".vbook").forEach((x) => { x.textContent = "Vælg"; x.style.background = ""; x.style.color = ""; });
      this.textContent = "Valgt ✓"; this.style.background = "var(--teal)"; this.style.color = "#042320";
      selectedVariant = (c.variants || [])[vi] || null;
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
  // Fire-and-forget POST to the real app1 feedback endpoint. Never blocks or
  // breaks the UI — feedback is telemetry, not a user-visible transaction.
  function postFeedback(payload) {
    try {
      fetch("/app1/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
        credentials: "same-origin",
        body: JSON.stringify(payload),
      }).catch(() => {});
    } catch (e) { /* ignore */ }
  }

  // Copy with the modern clipboard API, falling back to execCommand for
  // non-secure contexts / older browsers.
  function execCopy(text) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0;";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    ta.remove();
    return ok;
  }
  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(() => true).catch(() => execCopy(text));
    }
    return Promise.resolve(execCopy(text));
  }

  function addFeedback(body, query, result) {
    const res = result || {};
    const answerText = String(res.fullText || "");
    const base = {
      message_index: res.messageIndex != null ? res.messageIndex : 0,
      query_text: query || "",
      assistant_response: answerText.slice(0, 300),
    };
    let lastRating = 0;
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
      sb.onclick = () => {
        sb.disabled = true; sb.textContent = "Tak for din feedback ✓";
        postFeedback(Object.assign({ rating: lastRating || -1, reason: sel, comment: (ta.value || "").trim() }, base));
      };
      details.append(wrap, ta, sb);
    }
    row.querySelector(".up").onclick = function () {
      row.querySelectorAll(".up,.down").forEach((b) => b.classList.add("voted")); this.classList.add("on");
      lastRating = 1;
      postFeedback(Object.assign({ rating: 1, reason: "", comment: "" }, base));
      toast("Tak for din feedback", "summary");
    };
    row.querySelector(".down").onclick = function () {
      row.querySelectorAll(".up,.down").forEach((b) => b.classList.add("voted")); this.classList.add("on");
      lastRating = -1;
      postFeedback(Object.assign({ rating: -1, reason: "", comment: "" }, base));
      showDown();
    };
    row.querySelector(".regen").onclick = () => { const r = body.closest(".msg"); const q = query; if (r) r.remove(); run(q, { skipUser: true }); };
    row.querySelector(".copy").onclick = function () {
      const btn = this;
      copyText(answerText).then(() => {
        btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="#34c98a" stroke-width="2.4"><polyline points="20 6 9 17 4 12"/></svg>';
        setTimeout(() => { btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>'; }, 1400);
      });
    };
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
    refreshWorkspaceStatus();
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
      // Re-sync the ring from the real profile rather than guessing a bump.
      refreshRing();
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
      // Re-sync the ring from the real profile rather than guessing a bump.
      refreshRing();
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
                  thinking | ping | tool_call | profile_confirm_request |
                  profile_update | ui_card | memory_used |
                  memory_saved | profiler_progress
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

  // APPEND an error row instead of wiping the body — already-streamed content
  // (partial answer, cards, tool chips) must survive a dropped connection.
  function appendError(body, query) {
    const row = document.createElement("div");
    row.className = "err";
    row.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg><span>Forbindelsen blev afbrudt — Prøv igen.</span><button class="retry">Prøv igen</button>`;
    row.querySelector(".retry").onclick = () => { row.remove(); run(query, { skipUser: true }); };
    body.appendChild(row); down();
  }

  // Muted marker when the user stops generation — the feedback row still
  // renders afterwards so aborts are measurable.
  function addStopMarker(body) {
    const m = document.createElement("div");
    m.className = "md";
    m.style.cssText = "opacity:.55;font-style:italic;font-size:12.5px;";
    m.textContent = "— stoppet —";
    body.appendChild(m); down();
  }

  // Balance dangling markdown while streaming so half-arrived constructs don't
  // flash as broken markup: strip a trailing half-link, close an odd code fence.
  function balanceMarkdown(t) {
    let s = String(t || "");
    s = s.replace(/!?\[[^\]]*$/, "");            // "[Se kurset" (no closing ])
    s = s.replace(/!?\[[^\]]*\]\([^)]*$/, "");   // "[Se kurset](https://…" (no closing ))
    const fences = (s.match(/```/g) || []).length;
    if (fences % 2 === 1) s += "\n```";
    return s;
  }

  /* ---------------- tool activity ---------------- */
  const TOOL_LABELS = {
    catalog_search: "Katalogsøgning",
    catalog_get_product: "Kursusdetaljer",
    catalog_get_category: "Kategoriopslag",
    catalog_get_vendor: "Leverandøropslag",
    catalog_compare_products: "Sammenligning",
    get_learning_context: "Læringskontekst",
    check_course_readiness: "Tilmeldingscheck",
    prepare_course_order: "Ordrekladde",
    create_course_order: "Opret ordre",
    check_order_approval_status: "Godkendelsesstatus",
    analyze_skill_gaps: "Kompetencegab",
    get_department_budget: "Budgetopslag",
    search_courses: "Kursussøgning",
    filter_courses: "Kursusfilter",
    get_course_details: "Kursusdetaljer",
    compare_courses: "Sammenligning",
    get_vendor_info: "Leverandørinfo",
    get_user_profile: "Hent profil",
    update_user_profile: "Opdater profil",
    request_user_input: "Profilkort",
    remember_about_user: "Gem hukommelse",
    recommend_for_profile: "Profilmatch",
    suggest_learning_path: "Læringssti",
    set_learning_goal: "Opret mål",
    get_learning_goals: "Hent mål",
    update_learning_goal: "Opdater mål",
    get_my_course_status: "Kursusstatus",
    get_negotiated_discount: "Aftalepris",
    check_course_prerequisites: "Forudsætninger",
    get_course_sequel: "Næste kursus",
    find_certification_path: "Certificeringsvej",
    track_goal_progress: "Målfremdrift",
    add_to_calendar: "Kalender",
    mark_course_complete: "Markér fuldført",
    // Phase 5-7 new tools
    schedule_recurring_report: "Planlæg rapport",
    recheck_compliance: "Tjek compliance",
    generate_fresh_insights: "Analyser samtaler",
    bulk_calendar_invites: "Kalenderinvitationer",
    send_company_email: "Send virksomhedsmail",
    send_deadline_reminders: "Send påmindelser",
    create_order_for_employee: "Opret medarbejderordre",
    save_course_for_later: "Gem til senere",
    set_course_reminder: "Sæt påmindelse",
    manage_my_order: "Administrer ordre",
    request_manager_approval: "Anmod om godkendelse",
  };
  function toolLabel(tool) {
    const name = typeof tool === "string" ? tool : tool && tool.name;
    const label = typeof tool === "object" && tool ? tool.label : "";
    return label || TOOL_LABELS[name] || String(name || "Værktøj").replace(/_/g, " ");
  }
  function renderToolCall(body, data) {
    if (!data || !data.name) return;
    let box = body.querySelector(".tool-run");
    if (!box) {
      box = document.createElement("div");
      box.className = "tool-run";
      box.innerHTML = '<div class="tool-run-head"><i class="fa-solid fa-wand-magic-sparkles"></i><span>AI-værktøjer</span><span class="tool-count">0</span></div><div class="tool-run-list"></div>';
      body.appendChild(box);
    }
    const list = box.querySelector(".tool-run-list");
    const running = data.phase === "start" || data.status === "running";
    // Live events carry a call id: phase:'start' creates a running chip, the
    // finish event upgrades that same chip in place (label/latency/results)
    // instead of appending a duplicate. Without an id (or without a prior
    // start event — backend may not emit live events) we just append.
    const callId = String(data.id || "");
    let chip = null;
    if (callId) {
      chip = Array.prototype.find.call(list.children, (el) => el.getAttribute("data-call-id") === callId) || null;
    }
    if (chip && running) return; // duplicate start for an already-rendered chip
    if (!chip) {
      chip = document.createElement("span");
      if (callId) chip.setAttribute("data-call-id", callId);
      list.appendChild(chip);
    }
    const partialFail = !running && data.partial_failure;
    chip.className = "tool-chip"
      + (data.status === "error" ? " error" : "")
      + (partialFail ? " partial-failure" : "")
      + (running ? " running" : "");
    const meta = [];
    if (data.category) meta.push(data.category);
    if (Number(data.results_count) > 0) meta.push(Number(data.results_count) + " resultater");
    if (data.cache_hit) {
      const ttl = data.cache_ttl ? Math.round(data.cache_ttl) + "s" : "";
      meta.push(ttl ? "cache " + ttl : "cache");
    }
    if (partialFail) meta.push("delvis fejl");
    if (data.side_effect) meta.push("ændrer data");
    if (Number(data.latency_ms) > 0) meta.push(Number(data.latency_ms) + "ms");
    const icon = data.ui_icon ? `<i class="fa-solid ${esc(data.ui_icon)}"></i>` : "";
    chip.innerHTML = `${icon}<span>${esc(toolLabel(data))}</span>${meta.length ? `<span class="meta">${esc(meta.join(" · "))}</span>` : ""}`;
    // Progress bar for running chips (shown when progress_label is set or always for running)
    if (running) {
      const progWrap = document.createElement("div");
      progWrap.className = "tool-chip-progress";
      if (data.progress_label) {
        const lbl = document.createElement("div");
        lbl.className = "tool-chip-progress-label";
        lbl.textContent = data.progress_label;
        progWrap.appendChild(lbl);
      }
      const barWrap = document.createElement("div");
      barWrap.className = "tool-chip-progress-bar-wrap";
      const bar = document.createElement("div");
      bar.className = "tool-chip-progress-bar";
      if (data.percent != null) bar.setAttribute("data-pct", "1");
      bar.style.width = (data.percent != null ? Math.min(100, data.percent) : 0) + "%";
      barWrap.appendChild(bar);
      progWrap.appendChild(barWrap);
      chip.appendChild(progWrap);
    }
    // Error detail + retry: shown when safe_error is present on finished chips.
    if (!running && data.status === "error" && data.safe_error) {
      const errToggle = document.createElement("button");
      errToggle.type = "button";
      errToggle.className = "tool-chip-err-toggle";
      errToggle.title = "Vis fejldetalje";
      errToggle.innerHTML = '<i class="fa-solid fa-circle-info"></i>';
      const errDetail = document.createElement("span");
      errDetail.className = "tool-chip-err-detail";
      errDetail.style.display = "none";
      errDetail.textContent = data.safe_error;
      errToggle.addEventListener("click", (e) => {
        e.stopPropagation();
        errDetail.style.display = errDetail.style.display === "none" ? "inline" : "none";
      });
      chip.appendChild(errToggle);
      chip.appendChild(errDetail);

      const retryBtn = document.createElement("button");
      retryBtn.type = "button";
      retryBtn.className = "tool-chip-retry";
      retryBtn.title = "Prøv igen";
      retryBtn.innerHTML = '<i class="fa-solid fa-rotate-right"></i> Prøv igen';
      retryBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (lastActualQuery && !sending) run(lastActualQuery, { skipUser: true });
      });
      chip.appendChild(retryBtn);
    }
    box.querySelector(".tool-count").textContent = String(list.children.length);
    down();
  }
  // A turn that ends mid-tool (error / user Stop) must not leave chips in the
  // muted "running" state — settle them so the UI doesn't imply ongoing work.
  function settleToolChips(body) {
    body.querySelectorAll(".tool-chip.running").forEach((c) => c.classList.remove("running"));
  }

  // Update an in-flight chip's progress bar from a tool_progress SSE event.
  function updateToolProgress(body, data) {
    const callId = String(data.id || "");
    if (!callId) return;
    const list = body.querySelector(".tool-run-list");
    if (!list) return;
    const chip = Array.prototype.find.call(list.children,
      (el) => el.getAttribute("data-call-id") === callId) || null;
    if (!chip) return;
    const bar = chip.querySelector(".tool-chip-progress-bar");
    if (bar && data.percent != null) {
      bar.setAttribute("data-pct", "1");
      bar.style.width = Math.min(100, data.percent) + "%";
    }
    const lbl = chip.querySelector(".tool-chip-progress-label");
    if (lbl && data.note) lbl.textContent = data.note;
    down();
  }

  // Render a confirm card for a side-effect tool and wire Bekræft/Afvis.
  function renderConfirmCard(body, data) {
    const card = document.createElement("div");
    card.className = "confirm-card";
    const action = data.action || "";
    const summaryDa = data.summary_da || "";
    const details = data.details || "";
    const recipientCount = data.recipient_count != null ? data.recipient_count : null;
    const price = data.price != null ? data.price : null;

    const metaParts = [];
    if (recipientCount != null) metaParts.push(recipientCount + " modtagere");
    if (price != null) metaParts.push(Number(price).toLocaleString("da-DK") + " kr.");

    card.innerHTML = `
      <div class="confirm-card-head">
        <i class="fa-solid fa-triangle-exclamation"></i>
        <span>Bekræft handling</span>
      </div>
      <div class="confirm-card-body">${esc(summaryDa)}</div>
      ${details ? `<div class="confirm-card-details">${esc(details)}</div>` : ""}
      ${metaParts.length ? `<div class="confirm-card-meta">${esc(metaParts.join(" · "))}</div>` : ""}
      <div class="confirm-card-actions">
        <button class="confirm-card-ok" type="button">Bekræft</button>
        <button class="confirm-card-cancel" type="button">Afvis</button>
      </div>
    `;

    const okBtn = card.querySelector(".confirm-card-ok");
    const cancelBtn = card.querySelector(".confirm-card-cancel");
    const showResult = (cls, msg) => {
      okBtn.disabled = true; cancelBtn.disabled = true;
      const res = document.createElement("div");
      res.className = "confirm-card-result " + cls;
      res.textContent = msg;
      card.appendChild(res);
      down();
    };

    okBtn.addEventListener("click", async () => {
      okBtn.disabled = true; cancelBtn.disabled = true;
      okBtn.textContent = "Bekræfter…";
      try {
        const resp = await fetch("/app1/confirm_tool_action", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token: data.token }),
        });
        const result = await resp.json();
        if (result.status === "success" || result.status === "already_confirmed") {
          showResult("ok", result.message_da || result.message || "Bekræftet");
        } else if (result.status === "already_confirmed") {
          showResult("ok", "Allerede bekræftet");
        } else {
          showResult("err", result.message_da || result.message || "Fejl");
        }
      } catch (e) {
        showResult("err", "Netværksfejl — prøv igen");
      }
    });

    cancelBtn.addEventListener("click", () => {
      showResult("err", "Afvist");
    });

    body.appendChild(card);
    down();
  }

  /* ---------------- memory transparency ---------------- */
  // Shows which stored memories the AI used this turn, and confirms new ones it
  // saved — so the "what does it know about me" loop is visible in realtime.
  // Phase 10: each memory chip has a × delete button that removes it from the store.
  function renderMemoryUsed(body, memories) {
    if (!memories || !memories.length) return;
    let remaining = memories.length;
    const wrap = document.createElement("div");
    wrap.style.cssText = "margin:8px 0 2px;";
    const head = document.createElement("button");
    head.type = "button";
    head.style.cssText = "display:inline-flex;align-items:center;gap:6px;background:color-mix(in srgb,var(--fm-primary) 9%,transparent);color:var(--fm-primary);border:1px solid color-mix(in srgb,var(--fm-primary) 22%,transparent);border-radius:999px;padding:4px 11px;font-size:11.5px;font-weight:650;cursor:pointer;";
    const headLabel = document.createElement("span");
    headLabel.textContent = "Brugte hukommelse om dig (" + remaining + ")";
    head.innerHTML = "🧠 ";
    head.appendChild(headLabel);
    const chips = document.createElement("div");
    chips.style.cssText = "display:none;flex-wrap:wrap;gap:6px;margin-top:8px;";
    memories.forEach((m) => {
      const c = document.createElement("span");
      c.style.cssText = "display:inline-flex;align-items:center;gap:5px;font-size:11.5px;padding:3px 9px;border-radius:8px;background:var(--fm-surface-2);border:1px solid var(--fm-line);color:var(--fm-ink-2);";
      const lbl = document.createElement("span");
      lbl.textContent = (m.category ? "[" + m.category + "] " : "") + (m.label || "");
      c.appendChild(lbl);
      if (m.id) {
        const del = document.createElement("button");
        del.type = "button";
        del.title = "Slet hukommelse";
        del.style.cssText = "border:none;background:none;color:var(--fm-ink-3);cursor:pointer;font-size:10px;padding:0 2px;line-height:1;";
        del.innerHTML = "×";
        del.addEventListener("click", async (e) => {
          e.stopPropagation();
          try {
            const r = await fetch("/app1/memory/" + m.id, { method: "DELETE" });
            const res = await r.json();
            if (res.status === "ok") {
              c.style.opacity = ".35";
              del.disabled = true;
              remaining = Math.max(0, remaining - 1);
              headLabel.textContent = "Brugte hukommelse om dig (" + remaining + ")";
            }
          } catch (_) {}
        });
        c.appendChild(del);
      }
      chips.appendChild(c);
    });
    head.onclick = () => { chips.style.display = chips.style.display === "none" ? "flex" : "none"; };
    wrap.appendChild(head); wrap.appendChild(chips);
    body.appendChild(wrap); down();
  }

  function renderMemorySaved(body, data) {
    const wrap = document.createElement("div");
    wrap.style.cssText = "display:inline-flex;align-items:center;gap:7px;margin:8px 0 2px;font-size:12px;font-weight:600;color:var(--fm-primary);background:color-mix(in srgb,var(--fm-primary) 9%,transparent);border:1px solid color-mix(in srgb,var(--fm-primary) 22%,transparent);border-radius:10px;padding:6px 11px;";
    wrap.innerHTML = "🧠 <span>" + esc(data.message || ("Husket: " + (data.label || ""))) + "</span>";
    body.appendChild(wrap); down();
  }

  async function streamFromBackend(body, actualQuery) {
    // Abort plumbing: the Stop button aborts via currentAbort; a no-event
    // watchdog aborts a silently dead connection (backend emits an initial
    // ping and heartbeats far below this threshold).
    const controller = new AbortController();
    currentAbort = controller;
    let timedOut = false, watchdog = null, sawContent = false;
    const armWatchdog = () => {
      if (watchdog) clearTimeout(watchdog);
      watchdog = setTimeout(() => { timedOut = true; try { controller.abort(); } catch (e) {} },
        sawContent ? WATCHDOG_MS : WATCHDOG_FIRST_MS);
    };

    const decoder = new TextDecoder("utf-8");
    let buffer = "", textEl = null, fullText = "", suggestions = null, done = false;
    let messageIndex = null;              // from the meta event; used by the feedback POST
    let cardsSeen = 0, productSeen = 0;   // pair structured course_cards with fallback product HTML
    let eventsReceived = 0;               // meaningful events (excl. ping) — gates the silent retry

    // rAF-throttled rendering: buffer chunks and re-parse markdown at most once
    // per animation frame instead of on every chunk.
    let renderQueued = false, finalized = false;
    const renderStream = () => {
      if (finalized || !textEl) return;
      textEl.innerHTML = md(balanceMarkdown(fullText));
      down();
    };
    const queueRender = () => {
      if (renderQueued) return;
      renderQueued = true;
      requestAnimationFrame(() => { renderQueued = false; renderStream(); });
    };
    const renderFinal = () => {
      if (finalized) return;
      finalized = true;
      if (textEl) { textEl.innerHTML = md(fullText); down(); }
    };

    try {
      armWatchdog();
      const resp = await fetch(ASK_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: actualQuery, mode: (window.CHAT_MODE || "default") }),
        signal: controller.signal,
      });
      if (!resp.ok || !resp.body) throw new Error("HTTP " + resp.status);

      const reader = resp.body.getReader();

      while (!done) {
        if (aborted) { try { reader.cancel(); } catch (e) {} break; }
        const r = await reader.read();
        if (r.done) break;
        armWatchdog();
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

          if (data.type === "ping") continue;
          eventsReceived++;
          if (data.type === "meta") {
            if (data.message_index != null) messageIndex = data.message_index;
            continue;
          }
          if (data.type === "thinking") {
            // Status line next to the dots ("Søger og analyserer…") instead of dead air.
            thinkStatus(body, data.content);
            continue;
          }
          // Any real content event makes the dots placeholder redundant.
          sawContent = true;
          clearThinking(body);

          if (data.type === "chunk") {
            if (!textEl) { textEl = document.createElement("div"); textEl.className = "md"; body.appendChild(textEl); }
            fullText += (data.content || "");
            queueRender();
          } else if (data.type === "tool_call") {
            renderToolCall(body, data);
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
            refreshWorkspaceStatus();
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
          } else if (data.type === "memory_used") {
            renderMemoryUsed(body, data.memories || []);
            refreshWorkspaceStatus();
          } else if (data.type === "memory_saved") {
            renderMemorySaved(body, data);
            refreshWorkspaceStatus();
          } else if (data.type === "profiler_progress") {
            if (typeof window.onProfilerProgress === "function") window.onProfilerProgress(data.completeness);
            refreshWorkspaceStatus();
          } else if (data.type === "tool_progress") {
            // In-flight chip progress update from build_tool_progress_event (Phase 1/9)
            updateToolProgress(body, data);
          } else if (data.type === "confirm_card") {
            // Side-effect tool preview: render Bekræft/Afvis card (Phase 8/9)
            renderConfirmCard(body, data);
          }
        }
      }
    } catch (e) {
      // User Stop aborts the fetch — that is a graceful end, return the partial
      // result. Watchdog timeouts and network errors propagate to run(), but
      // the already-streamed DOM content stays untouched.
      if (!(aborted && !timedOut)) {
        renderFinal();
        const err = e instanceof Error ? e : new Error(String(e));
        err.eventsReceived = eventsReceived;
        throw err;
      }
    } finally {
      if (watchdog) clearTimeout(watchdog);
      if (currentAbort === controller) currentAbort = null;
    }
    renderFinal();
    // Render suggestion chips last, like the source UI.
    if (suggestions && suggestions.length) addChips(body, suggestions);
    return { fullText: fullText, messageIndex: messageIndex, eventsReceived: eventsReceived };
  }

  /* ---------------- send / run ---------------- */
  async function run(query, opts = {}) {
    if (sending) return;
    sending = true; aborted = false;
    document.querySelector(".welcome")?.remove();
    if (!opts.skipUser) addUser(query);
    // Reference any attached products the same way the real app1 UI does.
    // Composed ONCE per turn (before the retry loop) so the silent auto-retry
    // and the manual "Prøv igen" resend the same effective query; refs are
    // consumed by this send so stale course attachments stop steering
    // retrieval on the next question.
    let actualQuery = query;
    if (attached.length) {
      const refs = attached.map((t) => `[VEDHÆFTET KURSUS: "${t}"]`).join(" ");
      actualQuery = refs + "\n" + query;
      attached = []; renderRef();
    }
    lastActualQuery = actualQuery;
    input.value = ""; resize(); toggleSend();
    setSending(true);
    const body = addBot();
    const th = thinking(body);
    let result = null, lastErr = null;
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        result = await streamFromBackend(body, actualQuery);
        lastErr = null;
        break;
      } catch (e) {
        lastErr = e;
        // One silent auto-retry, only when the stream died before delivering
        // anything (zero events) and the user didn't stop it themselves.
        if (attempt === 0 && !aborted && !(e && e.eventsReceived > 0)) continue;
        break;
      }
    }
    th.remove();
    if (lastErr) {
      settleToolChips(body);
      appendError(body, actualQuery);
    } else {
      // User Stop: mark the cut-off, but still render the feedback row so
      // aborted answers are measurable.
      if (aborted) { settleToolChips(body); addStopMarker(body); }
      addFeedback(body, query, result || {});
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
  stop.onclick = () => {
    aborted = true;
    // Abort the in-flight fetch immediately instead of waiting for the next chunk.
    if (currentAbort) { try { currentAbort.abort(); } catch (e) {} }
  };

  /* ---------------- input ---------------- */
  function resize() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 150) + "px"; }
  function toggleSend() { send.classList.toggle("on", input.value.trim().length > 0); }
  input.addEventListener("input", () => { resize(); toggleSend(); });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); if (input.value.trim()) run(input.value.trim()); } });
  send.onclick = () => { if (input.value.trim()) run(input.value.trim()); };

  /* ---------------- welcome ---------------- */
  function welcome() {
    if (isProfiler) {
      thread.innerHTML = `
        <div class="welcome profiler-welcome">
          <div class="w-logo">${BOT}</div>
          <div class="w-eyebrow">AI Profiler</div>
          <div class="w-title">Gør din profil komplet</div>
          <div class="w-sub">Fortæl om dine kompetencer, erfaringer og mål — så bygger jeg en profil, der kan bruges til bedre anbefalinger.</div>
          <div class="w-hint">Profilmode · svar gemmes som profilforslag</div>
          <div class="w-grid">
            <button class="w-card" data-q="Hjælp mig med at gøre min profil komplet. Stil mig det første spørgsmål."><span class="ic"><i class="fa-solid fa-user-check"></i></span><span><div class="t">Start profiler</div><div class="h">Svar på målrettede spørgsmål</div></span></button>
            <button class="w-card" data-q="Jeg vil opdatere mine kompetencer og niveauer"><span class="ic"><i class="fa-solid fa-layer-group"></i></span><span><div class="t">Kompetencer</div><div class="h">Tilføj skills og niveauer</div></span></button>
            <button class="w-card" data-q="Jeg vil fortælle om min erfaring og tidligere roller"><span class="ic"><i class="fa-solid fa-briefcase"></i></span><span><div class="t">Erfaring</div><div class="h">Gem roller og resultater</div></span></button>
            <button class="w-card" data-q="Jeg vil sætte mine læringsmål"><span class="ic"><i class="fa-solid fa-bullseye"></i></span><span><div class="t">Læringsmål</div><div class="h">Definer hvad du vil opnå</div></span></button>
          </div>
        </div>`;
      thread.querySelectorAll(".w-card").forEach((c) => c.onclick = () => ask(c.dataset.q));
      return;
    }
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
  async function newChat() {
    attached = []; renderRef(); input.value = ""; toggleSend();
    // Honest reset: clear the server-side session (CHAT_MEMORY, shown products,
    // stage, rejections) BEFORE painting the welcome screen, so a "new" chat
    // never silently inherits the previous conversation's context. A failed
    // call still resets the UI — better a fresh screen than a stuck button.
    try {
      await fetch("/app1/new_session", {
        method: "POST",
        headers: { "X-Requested-With": "XMLHttpRequest" },
        credentials: "same-origin",
      });
    } catch (e) { /* offline / anonymous: still reset the UI */ }
    activeConvId = null;
    welcome();
    refreshConv();
    input.focus();
  }
  window.fmNewChat = newChat;

  /* ---------------- conversation list ----------------
     Real history from GET /app1/conversations. No fabricated rows: an empty or
     failed fetch renders a neutral empty state, never placeholder samples. */
  const convIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';
  const delIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>';
  let CONVS = [];
  let activeConvId = null;

  // Group a conversation by its updated_at into "today" / "yesterday" / "older".
  function convGroup(iso) {
    if (!iso) return "older";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "older";
    const now = new Date();
    const startOfDay = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const dayMs = 86400000;
    const diff = startOfDay(now) - startOfDay(d);
    if (diff <= 0) return "today";
    if (diff <= dayMs) return "yesterday";
    return "older";
  }

  function renderConv() {
    const list = $("#convList");
    if (!list) return;
    if (!CONVS.length) {
      // Neutral empty state — no fake conversations. Styled inline (no chat.css dep)
      // to match the muted rail labels.
      list.innerHTML = `<div class="conv-empty" style="padding:12px 9px;font-size:12px;color:var(--ink-4)">Ingen samtaler endnu</div>`;
      return;
    }
    const groups = [["today", "I dag"], ["yesterday", "I går"], ["older", "Ældre"]];
    list.innerHTML = groups.map(([k, lab]) => {
      const items = CONVS.filter((c) => convGroup(c.updated_at) === k);
      if (!items.length) return "";
      return `<div class="conv-date">${esc(lab)}</div>` + items.map((c) => `
        <div class="conv${c.id == activeConvId ? " active" : ""}" data-id="${esc(c.id)}">
          ${convIcon}
          <span>${esc(c.title || "Samtale")}</span>
          <button class="conv-del" data-id="${esc(c.id)}" title="Slet">${delIcon}</button>
        </div>`).join("");
    }).join("");
    list.querySelectorAll(".conv").forEach((el) => el.onclick = () => {
      openConversation(el.dataset.id);
    });
    list.querySelectorAll(".conv-del").forEach((b) => b.onclick = (e) => {
      e.stopPropagation();
      const id = b.dataset.id;
      // Optimistic removal; persist deletion to the real backend.
      CONVS = CONVS.filter((c) => String(c.id) !== String(id));
      if (String(activeConvId) === String(id)) activeConvId = null;
      renderConv();
      fetch("/app1/conversations/" + encodeURIComponent(id), {
        method: "DELETE",
        headers: { "X-Requested-With": "XMLHttpRequest" },
        credentials: "same-origin",
      }).catch(() => {});
    });
  }

  async function refreshConv() {
    try {
      const resp = await fetch("/app1/conversations", {
        headers: { "X-Requested-With": "XMLHttpRequest" },
        credentials: "same-origin",
      });
      if (!resp.ok) { CONVS = []; renderConv(); return; }
      const data = await resp.json();
      CONVS = Array.isArray(data && data.conversations) ? data.conversations : [];
    } catch (e) {
      CONVS = [];
    }
    renderConv();
  }

  // Paint a stored conversation's messages into the thread. Stored history only
  // keeps user + assistant turns (see save_conversation_history); assistant text
  // is rendered as markdown, the same as a live answer.
  function renderHistory(messages) {
    document.querySelector(".welcome")?.remove();
    thread.innerHTML = "";
    (messages || []).forEach((m) => {
      const role = m && m.role, content = (m && m.content) || "";
      if (!content) return;
      if (role === "user") {
        addUser(content);
      } else if (role === "assistant") {
        const body = addBot();
        // Replay the same rich UI the user saw live: tool chips at the top, then
        // the answer text, then course cards (matching the live stream order).
        if (Array.isArray(m._tools) && m._tools.length) {
          m._tools.forEach((t) => renderToolCall(body, t));
        }
        const el = document.createElement("div");
        el.className = "md";
        el.innerHTML = md(content);
        body.appendChild(el);
        if (Array.isArray(m._cards) && m._cards.length) addCourses(body, m._cards);
      }
    });
    down(false, true);
  }

  // Open a past conversation: resume it server-side (so the next /ask continues
  // it) and render its transcript. Falls back to read-only render if resume fails.
  async function openConversation(id) {
    if (!id || sending) return;
    activeConvId = id;
    renderConv();
    if (rail && window.innerWidth <= 860) rail.classList.remove("open");
    try {
      const resp = await fetch("/app1/conversations/" + encodeURIComponent(id) + "/resume", {
        method: "POST",
        headers: { "X-Requested-With": "XMLHttpRequest" },
        credentials: "same-origin",
      });
      if (resp.ok) {
        const data = await resp.json();
        if (data && data.status === "ok") { renderHistory(data.messages); input.focus(); return; }
      }
    } catch (e) { /* fall through to read-only load */ }
    // Resume unavailable (offline/older backend): still show the transcript.
    try {
      const r2 = await fetch("/app1/conversations/" + encodeURIComponent(id), {
        headers: { "X-Requested-With": "XMLHttpRequest" }, credentials: "same-origin",
      });
      if (r2.ok) {
        const d2 = await r2.json();
        const conv = d2 && (d2.conversation || d2);
        if (conv && conv.messages) renderHistory(conv.messages);
      }
    } catch (e) { /* leave current view untouched */ }
  }

  /* ---------------- real profile completeness ----------------
     Drives the ring from GET /api/profile/full instead of a hardcoded number.
     Completeness = fraction of the five learner-profile sections that have any
     real content: skills, experience, education, completed courses, goals.
     Anonymous / 401 / fetch failure -> ring stays hidden, never a fake %. */
  let profileLoaded = false;
  function computeCompleteness(p) {
    if (!p || typeof p !== "object") return 0;
    const arr = (x) => (Array.isArray(x) ? x.length : 0);
    const sections = [
      arr(p.skills) > 0,
      arr(p.experience) > 0,
      arr(p.education) > 0,
      arr(p.completed_courses) > 0,
      // "Goals" is populated if there are learning goals OR a free-text goal/bio.
      (arr(p.learning_goals) > 0) || !!(p.goals && String(p.goals).trim()),
    ];
    const filled = sections.filter(Boolean).length;
    return Math.round((filled / sections.length) * 100);
  }

  async function loadProfile() {
    const widget = $(".ring-widget");
    try {
      const resp = await fetch("/api/profile/full", {
        headers: { "Accept": "application/json", "X-Requested-With": "XMLHttpRequest" },
        credentials: "same-origin",
      });
      if (!resp.ok) throw new Error("profile " + resp.status);
      const data = await resp.json();
      if (!data || !data.success || !data.profile) throw new Error("profile_shape");
      profileLoaded = true;
      setRing(computeCompleteness(data.profile));
      if (widget) widget.style.display = "";
    } catch (e) {
      // Anonymous / failed: hide the ring widget rather than show a fake number.
      profileLoaded = false;
      if (widget) widget.style.display = "none";
    }
  }

  // Re-sync the ring from the live profile after a save (used by profile cards).
  // Only re-fetches when the profile loaded successfully in the first place.
  function refreshRing() { if (profileLoaded) loadProfile(); }

  /* ---------------- login state ----------------
     Positive signal: the profile fetch succeeded (profileLoaded). Negative
     signal: the fm_base shell renders a "Log ind" userchip for anonymous
     sessions. Unknown (standalone layout, fetch race) -> assume logged in;
     order creation is still confirm-gated server-side either way. */
  function isLoggedIn() {
    if (profileLoaded) return true;
    return !document.querySelector('.fm-userchip[href*="login"]');
  }

  /* ---------------- real nudge banner ----------------
     Banner text/link come from the first item of GET /app1/nudges. No nudges
     (or anonymous / failure) -> banner stays hidden, never a fabricated nudge. */
  function showNudge(text, url) {
    const nudge = $("#nudge"), link = $("#nudgeLink");
    if (!nudge || !link) return;
    link.textContent = text;
    const safeUrl = (url && /^(?:https?:|\/)/i.test(String(url))) ? String(url) : "";
    link.dataset.url = safeUrl;
    nudge.classList.add("show");
  }
  function hideNudge() { const n = $("#nudge"); if (n) n.classList.remove("show"); }

  async function loadNudges() {
    try {
      const resp = await fetch("/app1/nudges", {
        headers: { "X-Requested-With": "XMLHttpRequest" },
        credentials: "same-origin",
      });
      if (!resp.ok) { hideNudge(); return; }
      const data = await resp.json();
      const list = (data && Array.isArray(data.nudges)) ? data.nudges : [];
      const first = list[0];
      const text = first && (first.text || first.message);
      if (!text) { hideNudge(); return; }
      showNudge(String(text), first.action_url || first.link || "");
    } catch (e) {
      hideNudge();
    }
  }

  /* ---------------- sidebar / nudge / misc ---------------- */
  // Rail chrome (toggle/menu/overlay) only exists on the standalone chat layout.
  // When the chat is embedded in the shared fm_base shell these are absent, so
  // guard every binding.
  const railToggleEl = $("#railToggle");
  if (railToggleEl && rail) railToggleEl.onclick = () => { if (window.innerWidth <= 860) rail.classList.toggle("open"); else rail.classList.toggle("collapsed"); };
  const menuBtnEl = $("#menuBtn");
  if (menuBtnEl && rail) menuBtnEl.onclick = () => rail.classList.add("open");
  const overlayEl = $("#overlay");
  if (overlayEl && rail) overlayEl.onclick = () => rail.classList.remove("open");
  document.querySelectorAll("[data-new]").forEach((b) => b.onclick = newChat);
  $("#nudgeX").onclick = () => hideNudge();
  $("#nudgeLink").onclick = (e) => {
    e.preventDefault();
    hideNudge();
    const url = e.currentTarget.dataset.url;
    if (url) { window.location.href = url; return; }
    ask("Hvad mangler min profil?");
  };

  /* ---------------- init ---------------- */
  refreshConv();
  welcome();
  renderRef();
  refreshWorkspaceStatus();
  loadProfile();
  loadNudges();
  input.focus();
})();
