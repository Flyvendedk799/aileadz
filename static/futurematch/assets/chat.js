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
  let activeConvId = null;
  function trackLearner(event, meta) {
    try {
      fetch("/api/learner/events", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ event: event, meta: meta || {} }),
      }).catch(() => {});
    } catch (_) {}
  }

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

  /* ---------------- shared AI workspace ----------------
     One debounced fetch of /api/profile/workspace feeds every status widget on
     the AI surfaces: the chat's status bar here, and the profiler banner, which
     listens for the `fm:workspace` event instead of fetching on its own. A turn
     that saves several things asks for a refresh several times; the debounce
     turns that into one request. The number shown is the depth-aware
     weighted_pct (the same one the profile page and the Mind-Map show), and the
     text is the need-driven next_help, never a missing-fields checklist. */
  let profileLoaded = false;
  let wsTimer = null;
  function refreshWorkspace(delay) {
    clearTimeout(wsTimer);
    wsTimer = setTimeout(loadWorkspace, delay == null ? 250 : delay);
  }
  async function loadWorkspace() {
    const bar = $("#aiWorkspaceStatus");
    try {
      const resp = await fetch("/api/profile/workspace", {
        headers: { "X-Requested-With": "XMLHttpRequest" },
        credentials: "same-origin",
      });
      if (!resp.ok) throw new Error("workspace " + resp.status);
      const data = await resp.json();
      if (!data || data.success === false) throw new Error("workspace_shape");
      profileLoaded = true;
      window.fmWorkspace.last = data;
      if (bar) paintWorkspaceBar(data);
      document.dispatchEvent(new CustomEvent("fm:workspace", { detail: data }));
    } catch (e) {
      if (bar) bar.hidden = true;
    }
  }
  function paintWorkspaceBar(data) {
    const c = data.completeness || {};
    const counts = data.counts || {};
    const shown = c.weighted_pct != null ? c.weighted_pct : c.pct;
    const pct = $("#aiWsPct"), mem = $("#aiWsMem"), used = $("#aiWsUsed"), nodes = $("#aiWsNodes"), next = $("#aiWsNext");
    if (pct) pct.textContent = shown != null ? shown + "%" : "—";
    if (mem) mem.textContent = counts.memories != null ? counts.memories : "—";
    if (used) used.textContent = counts.used_memories != null ? counts.used_memories : "—";
    if (nodes) nodes.textContent = counts.leaves != null ? counts.leaves : "—";
    if (next) next.textContent = c.next_help || "";
    $("#aiWorkspaceStatus").hidden = false;
  }
  window.fmWorkspace = { refresh: refreshWorkspace, last: null };

  // Assistant prose never carries pictures: course cards own the imagery, and a pasted
// logo renders full width and pushes the real UI down.
const md = (t) => sanitizeHtml(window.marked ? window.marked.parse(t) : esc(t).replace(/\n/g, "<br>")).replace(/<img\b[^>]*>/gi, "");
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
        <a class="toast-link" href="${section === "memories" ? "/mind-map" : "/profile"}" target="_blank">${section === "memories" ? "Vis Mind-Map" : "Vis i profil"} <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg></a></div>
      <button class="toast-x" aria-label="Luk"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>`;
    t.querySelector(".toast-x").onclick = () => dismissToast(t);
    toastStack.appendChild(t);
    setTimeout(() => dismissToast(t), 5200);
    while (toastStack.children.length > 3) toastStack.firstChild.remove();
  }
  function dismissToast(t) { if (!t.parentNode) return; t.classList.add("out"); setTimeout(() => t.remove(), 300); }

  /* ---------------- message builders ---------------- */
  function addUser(text) {
    const r = document.createElement("div");
    r.className = "msg user";
    r.innerHTML = `<div class="bubble"></div>`;
    r.querySelector(".bubble").textContent = text;
    thread.appendChild(r); down(false, true);
  }
  // Every assistant turn has the same fixed zones, top to bottom, no matter in
  // which order the stream delivers things: what the AI did (activity), what it
  // said (text), what it found (rich cards), what it needs from you (ask),
  // quiet side notes (notes), and what to do next (foot). Renderers call
  // place(body, zone, el) instead of appending, so text and tool UI never
  // interleave by accident of timing.
  const CARDS_MARK = "<!--kort-->";   // keep in step with app1/card_text.py
  const ZONES = ["activity", "text", "rich", "tail", "ask", "notes", "foot"];
  function zoneEl(body, zone) {
    return body.querySelector(":scope > .tb-" + zone) || body;
  }
  function place(body, zone, el) {
    zoneEl(body, zone).appendChild(el);
    return el;
  }
  function addBot() {
    const r = document.createElement("div");
    r.className = "msg bot";
    r.innerHTML = `<div class="av-bot">${BOT}</div><div class="bot-body">${ZONES.map((z) => `<div class="tb tb-${z}"></div>`).join("")}</div>`;
    thread.appendChild(r);
    return r.querySelector(".bot-body");
  }
  function thinking(body) {
    const t = document.createElement("div");
    t.className = "think";
    // Accessible, labeled live region: screen readers announce the phase, and
    // the silent first-content window is no longer unexplained dead air. The
    // label updates as work progresses (see thinkStatus / tool_call below).
    t.setAttribute("role", "status");
    t.setAttribute("aria-live", "polite");
    t.innerHTML = `<span class="d" aria-hidden="true"></span><span class="d" aria-hidden="true"></span><span class="d" aria-hidden="true"></span>`;
    place(body, "activity", t); down();
    thinkStatus(body, "Arbejder…");
    return t;
  }
  // Map a tool category/name to a human phase label for the status placeholder.
  function phaseLabelFor(data) {
    const cat = (data && data.category_key) || "";
    if (cat === "catalog" || data.name === "catalog_search" || data.name === "search_courses") return "Søger i kataloget…";
    if (cat === "profile") return "Opdaterer din profil…";
    if (data && data.name === "suggest_learning_path") return "Bygger din læringssti…";
    if (data && data.name === "recommend_for_profile") return "Finder kurser til din profil…";
    return "Bruger værktøjer…";
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
          ${c.why ? `<div class="course-why"><i class="fa-solid fa-circle-check"></i><span>${esc(c.why)}</span></div>` : ""}
        </div>
        <div class="course-price">${c.old ? `<span class="old">${esc(c.old)}</span>` : ""}<span class="p">${esc(c.price)}</span>${c.agree ? '<span class="agree">Aftalepris</span>' : ""}</div>
        <div class="course-chev">${chevDown}</div>
      </div>
      <div class="course-meta">${meta}</div>
      <div class="course-exp"><div class="course-exp-in"><div class="course-exp-pad">
        <div class="course-summary">${esc(c.summary)}</div>
        ${variants ? `<div class="variants"><div class="variants-h">Kommende hold</div>${variants}</div>` : ""}
        <div class="course-actions">
          <button class="c-primary"><i class="fa-solid fa-cart-plus"></i> ${esc(chatCfg().primaryLabel)}</button>
          ${chatCfg().teamOrders ? '<button class="c-sec team"><i class="fa-solid fa-people-group"></i> Bestil til team</button>' : ""}
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
    // Role-aware (N-5.2): "Anmod om plads" is a request for yourself; "Bestil til
    // team" only exists when the company policy allows team orders.
    function sendOrderRequest(btn, forTeam) {
      if (!isLoggedIn()) { toast("Log ind for at bestille kurser", "courses"); return; }
      if (sending) { toast("Vent venligst — assistenten svarer stadig", "courses"); return; }
      const v = selectedVariant;
      const hold = v && v.date ? ` — holdet ${v.date}${v.loc ? " i " + v.loc : ""}` : "";
      ask(forTeam ? `Jeg vil gerne bestille "${c.title}"${hold} til mit team`
                  : `Jeg vil gerne anmode om en plads på "${c.title}"${hold}`);
      const old = btn.innerHTML;
      btn.classList.add("done"); btn.innerHTML = '<i class="fa-solid fa-check"></i> Sendt til rådgiveren';
      setTimeout(() => { btn.classList.remove("done"); btn.innerHTML = old; }, 2600);
    }
    card.querySelector(".c-primary").addEventListener("click", function (e) {
      e.stopPropagation(); sendOrderRequest(this, false);
    });
    const teamBtn = card.querySelector(".c-sec.team");
    if (teamBtn) teamBtn.addEventListener("click", function (e) { e.stopPropagation(); sendOrderRequest(this, true); });
    // "Vælg" stores the chosen variant so "Bestil til team" can compose a
    // precise order message (date + location) for the agent.
    card.querySelectorAll(".vbook").forEach((b, vi) => b.addEventListener("click", function (e) {
      e.stopPropagation();
      card.querySelectorAll(".vbook").forEach((x) => { x.textContent = "Vælg"; x.classList.remove("picked"); });
      this.textContent = "Valgt ✓"; this.classList.add("picked");
      selectedVariant = (c.variants || [])[vi] || null;
    }));
    // "Side" opens the real catalog product page when we have a handle.
    const det = card.querySelector(".det");
    if (det && c.handle) det.addEventListener("click", (e) => {
      e.stopPropagation(); trackLearner("recommendation_click", { source: "course_card" });
      window.open("/products/" + encodeURIComponent(c.handle), "_blank");
    });
    return card;
  }
  function addCourses(body, list) {
    const wrap = document.createElement("div");
    wrap.className = "cards";
    const cap = document.createElement("div");
    cap.className = "cards-cap";
    cap.textContent = list.length === 1 ? "Kursus til dig" : list.length + " kurser til dig";
    wrap.appendChild(cap);
    list.forEach((c, i) => wrap.appendChild(courseCard(c, i === 0)));
    place(body, "rich", wrap); down();
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
    place(body, "foot", c); down();
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
    place(body, "foot", row); place(body, "foot", details); down();
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

  // Server-provided chat config (role + team-order policy). Safe defaults when absent.
  function chatCfg() {
    const c = window.FM_CHAT_CFG || {};
    return { teamOrders: !!c.teamOrders, primaryLabel: c.primaryLabel || "Anmod om plads" };
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
    refreshWorkspace();
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
      card.querySelector(".pcard-msg").innerHTML = '<span class="q ok">✓ Gemt</span> ' + esc(opts.message);
      toast(opts.toast || "Profil opdateret", opts.section);
      // Re-sync the ring from the real profile rather than guessing a bump.
      refreshWorkspace();
      collapseToPill(card, opts.label || "Profil opdateret", false);
    };
    card.querySelector(".p-no").onclick = function () {
      card.classList.add("dim");
      card.querySelector(".pcard-actions").innerHTML = '<span class="pcard-state">Afvist</span>';
      collapseToPill(card, opts.label || "Opdatering", true);
    };
    card.querySelector(".p-chat").onclick = () => {
      card.classList.add("dim");
      card.querySelector(".pcard-actions").innerHTML = '<span class="pcard-state on">Svarer i chat…</span>';
      input.value = 'Ang. "' + opts.message.substring(0, 60) + '": '; input.focus(); resize(); toggleSend();
    };
    place(body, "ask", card); down();
  }

  /* ---------------- saved-at-once profile additions (N-5.1) ---------------- */
  // The AI already saved these; one card lists them, each with an inline Fortryd.
  function renderProfileSaved(body, items) {
    if (!items.length) return;
    const card = document.createElement("div");
    card.className = "pcard saved";
    const rows = items.map((it, i) => `
      <div class="psaved-row" data-i="${i}">
        <span class="psaved-label"><i class="fa-solid fa-check"></i> ${esc(it.label || "Noteret")}</span>
        ${it.undo ? '<button type="button" class="psaved-undo">Fortryd</button>' : ""}
      </div>`).join("");
    card.innerHTML = `
      <div class="pcard-ic">${ic[items[0].section] || ic.summary}</div>
      <div class="pcard-body"><div class="pcard-msg"><span class="q ok">Noteret på din profil</span></div>${rows}</div>`;
    card.querySelectorAll(".psaved-row").forEach((row) => {
      const it = items[+row.dataset.i];
      const btn = row.querySelector(".psaved-undo");
      if (!btn || !it.undo) return;
      btn.addEventListener("click", async () => {
        btn.disabled = true; btn.textContent = "…";
        try {
          await saveProfileUpdate(it.undo.action, it.undo.data || {});
          row.querySelector(".psaved-label").classList.add("undone");
          btn.replaceWith(Object.assign(document.createElement("span"), { textContent: "Fortrudt", className: "psaved-done" }));
          refreshWorkspace();
        } catch (e) { btn.disabled = false; btn.textContent = "Prøv igen"; }
      });
    });
    place(body, "notes", card); down();
  }

  // Several removals/edits proposed in one turn: one card, accept all or none.
  function renderProfileConfirmBatch(body, items) {
    if (!items.length) return;
    const card = document.createElement("div");
    card.className = "pcard";
    card.innerHTML = `
      <div class="pcard-ic">${ic[items[0].section] || ic.summary}</div>
      <div class="pcard-body"><div class="pcard-msg"><span class="q">Opdater profil?</span></div>
        <ul class="pbatch">${items.map((it) => `<li>${esc(it.message || "")}</li>`).join("")}</ul></div>
      <div class="pcard-actions"><button class="p-save">Gem alle</button><button class="p-no">Nej tak</button></div>`;
    card.querySelector(".p-save").onclick = async function () {
      this.disabled = true; this.textContent = "…";
      try {
        for (const it of items) { const c = it.confirm || {}; await saveProfileUpdate(c.action, c.data || {}); }
        this.textContent = "Gemt ✓"; card.querySelector(".p-no").remove(); refreshWorkspace();
      } catch (e) { this.disabled = false; this.textContent = "Prøv igen"; }
    };
    card.querySelector(".p-no").onclick = function () {
      card.classList.add("dim");
      card.querySelector(".pcard-actions").innerHTML = '<span class="pcard-state">Afvist</span>';
    };
    place(body, "ask", card); down();
  }

  /* ---------------- choice card (ui_type=choice) ---------------- */
  // The user picks one option; the pick goes back to the assistant as a normal
  // message (or straight to the save action when the card names one).
  function choiceCard(body, opts, onPick) {
    const card = document.createElement("div");
    card.className = "pcard ui choice";
    card.innerHTML = `
      <div class="pcard-ic">${ic[opts.section] || ic.summary}</div>
      <div class="pcard-body"><div class="pcard-msg">${esc(opts.message)}</div>
        <div class="pcard-tags">${opts.choices.map((c, i) => `<button type="button" class="pcard-tag pick" data-i="${i}">${esc(c.label || c.value)}</button>`).join("")}</div>
      </div>`;
    card.querySelectorAll(".pick").forEach((b) => b.addEventListener("click", () => {
      const c = opts.choices[+b.dataset.i];
      card.querySelectorAll(".pick").forEach((x) => { x.disabled = true; });
      b.classList.add("done");
      onPick(c);
    }));
    place(body, "ask", card); down();
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
      refreshWorkspace();
      collapseToPill(card, opts.label || "Tilføjet", false);
    };
    card.querySelector(".p-no").onclick = function () {
      card.classList.add("dim"); card.querySelector(".pcard-actions").innerHTML = '<span class="pcard-state">Afvist</span>';
      collapseToPill(card, opts.label || "Opdatering", true);
    };
    card.querySelector(".p-chat").onclick = () => { input.value = "Lad mig fortælle: "; input.focus(); resize(); toggleSend(); };
    place(body, "ask", card); down();
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
     event types: meta | chunk | product | course_cards | suggestions | notice |
                  thinking | ping | tool_call | tool_progress | confirm_card |
                  profile_confirm_request | profile_update | ui_card |
                  memory_used | memory_saved | profiler_progress |
                  ui_action | comparison_card | learning_path_card
     (canonical list: app1/sse_events.py)
     ============================================================ */
  const ASK_URL = "/app1/ask";

  function injectProductHtml(body, html) {
    if (!html) return;
    let cards = body.querySelector(".cards");
    if (!cards) { cards = document.createElement("div"); cards.className = "cards"; place(body, "rich", cards); }
    const wrap = document.createElement("div");
    wrap.innerHTML = sanitizeHtml(html);
    cards.appendChild(wrap);
    down();
  }

  // APPEND an error row instead of wiping the body — already-streamed content
  // (partial answer, cards, tool chips) must survive a dropped connection.
  function appendError(body, query, retryOpts) {
    const row = document.createElement("div");
    row.className = "err";
    row.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg><span>Forbindelsen blev afbrudt — Prøv igen.</span><button class="retry">Prøv igen</button>`;
    row.querySelector(".retry").onclick = () => { row.remove(); run(query, Object.assign({}, retryOpts || {}, { skipUser: true })); };
    place(body, "foot", row); down();
  }

  // Muted marker when the user stops generation — the feedback row still
  // renders afterwards so aborts are measurable.
  function addStopMarker(body) {
    const m = document.createElement("div");
    m.className = "note-line";
    m.textContent = "— stoppet —";
    place(body, "foot", m); down();
  }

  // Balance dangling markdown while streaming so half-arrived constructs don't
  // flash as broken markup: strip a trailing half-link, close an odd code fence.
  function balanceMarkdown(t) {
    let s = String(t || "");
    s = s.replace(/!?\[[^\]]*$/, "");            // "[Se kurset" (no closing ])
    s = s.replace(/!?\[[^\]]*\]\([^)]*$/, "");   // "[Se kurset](https://…" (no closing ))
    s = s.replace(/<!?-{0,2}[a-z]*-{0,2}$/, "");  // half-arrived CARDS_MARK
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
    save_learning_path: "Gem læringssti",
    get_learning_path: "Hent læringssti",
    open_in_app: "Åbn i appen",
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
    // Profile / memory / platform tools (the backend label wins; this is the offline fallback)
    show_cv_summary: "CV-oversigt",
    show_mindmap_preview: "Mind-Map",
    show_skill_gaps: "Kompetencegab",
    get_my_agenda: "Min agenda",
    get_my_compliance: "Mine krav",
    search_platform_help: "Hjælpeartikler",
    recall_about_user: "Husker tilbage",
    forget_about_user: "Glem hukommelse",
    update_learning_path: "Opdater læringssti",
    resolve_checkin: "Luk opfølgning",
    ask_user_questions: "Stiller spørgsmål",
    record_learning_outcome: "Gem kursusudbytte",
  };
  // What a finished chip says when the count alone would be silent or misleading.
  const TOOL_STATUS_NOTES = {
    empty: "ingen resultater",
    proposed: "afventer din bekræftelse",
    ui_card: "mangler dit svar",
    memory_saved: "gemt",
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
      box.className = "tool-run live";
      // One quiet line while the AI works; it folds itself into "Brugte n værktøjer" (see settleActivity).
      box.innerHTML = '<button type="button" class="tool-run-head" aria-expanded="false"><span class="tr-dot" aria-hidden="true"></span><span class="tr-label">Arbejder</span><span class="tool-count">0</span><i class="fa-solid fa-chevron-down tr-chev" aria-hidden="true"></i></button><div class="tool-run-list"></div>';
      box.querySelector(".tool-run-head").addEventListener("click", function () {
        const open = box.classList.toggle("open");
        this.setAttribute("aria-expanded", open ? "true" : "false");
      });
      place(body, "activity", box);
    }
    const list = box.querySelector(".tool-run-list");
    const running = data.phase === "start" || data.status === "running";
    if (running) {
      // New work after an earlier fold-up: the line goes live again and names what is happening now.
      box.classList.add("live");
      box.classList.remove("open", "has-error");
      box.querySelector(".tr-label").textContent = toolLabel(data);
      box.querySelector(".tool-run-head").setAttribute("aria-expanded", "false");
    }
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
    // Visible meta stays about the outcome; the technical details (category,
    // cache, latency) move to the tooltip so the line reads calmly.
    const meta = [];
    const tech = [];
    if (data.category) tech.push(data.category);
    const statusNote = !running && TOOL_STATUS_NOTES[data.status];
    if (statusNote) meta.push(statusNote);
    else if (Number(data.results_count) > 0) {
      const n = Number(data.results_count);
      meta.push(n === 1 ? "1 resultat" : n + " resultater");
    }
    if (data.cache_hit) {
      const ttl = data.cache_ttl ? Math.round(data.cache_ttl) + "s" : "";
      tech.push(ttl ? "cache " + ttl : "cache");
    }
    if (partialFail) meta.push("delvis fejl");
    if (data.side_effect) meta.push("ændrer data");
    if (Number(data.latency_ms) > 0) tech.push(Number(data.latency_ms) + "ms");
    const icon = data.ui_icon ? `<i class="fa-solid ${esc(data.ui_icon)}"></i>` : "";
    chip.innerHTML = `${icon}<span>${esc(toolLabel(data))}</span>${meta.length ? `<span class="meta">${esc(meta.join(" · "))}</span>` : ""}`;
    // Hover / screen reader: the server's one-line outcome ("Hukommelse gemt.", a safe error).
    const note = (data.status === "error" && data.safe_error) || data.message || "";
    if (note) { chip.title = note; chip.setAttribute("aria-label", toolLabel(data) + ": " + note); }
    else if (tech.length) chip.title = tech.join(" · ");
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
    settleActivity(body);
  }
  // The work is done (an answer or a card has started): fold the live tool list
  // into one muted line the user can open for the details.
  function settleActivity(body) {
    const box = body.querySelector(".tool-run.live");
    if (!box) return;
    box.classList.remove("live", "open");
    const n = box.querySelectorAll(".tool-chip").length;
    const failed = box.querySelectorAll(".tool-chip.error").length;
    box.querySelector(".tr-label").textContent = n === 1 ? "Brugte 1 værktøj" : "Brugte " + n + " værktøjer";
    box.classList.toggle("has-error", failed > 0 && failed === n);
    const head = box.querySelector(".tool-run-head");
    if (head) head.setAttribute("aria-expanded", "false");
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
    const isOrder = action === "create_course_order";
    const summaryDa = data.summary_da || "";
    // details is free text for some tools and a structured map for others; only
    // text is shown (the summary already names course, session and contact).
    const rawDetails = data.details;
    const details = typeof rawDetails === "string" ? rawDetails
      : (rawDetails && Array.isArray(rawDetails.participants) && rawDetails.participants.length
        ? "Deltagere: " + rawDetails.participants.join(", ") : "");
    const recipientCount = data.recipient_count != null ? data.recipient_count : null;
    const price = data.price != null ? data.price : null;

    const metaParts = [];
    if (recipientCount != null) metaParts.push(recipientCount + " modtagere");
    if (price != null) metaParts.push(Number(price).toLocaleString("da-DK") + " kr.");

    card.innerHTML = `
      <div class="confirm-card-head">
        <i class="fa-solid fa-triangle-exclamation"></i>
        <span>${isOrder ? "Bekræft bestilling" : "Bekræft handling"}</span>
      </div>
      <div class="confirm-card-body">${esc(summaryDa)}</div>
      ${details ? `<div class="confirm-card-details">${esc(details)}</div>` : ""}
      ${metaParts.length ? `<div class="confirm-card-meta">${esc(metaParts.join(" · "))}</div>` : ""}
      <div class="confirm-card-actions">
        <button class="confirm-card-ok" type="button">Bekræft</button>
        <button class="confirm-card-cancel" type="button">Afvis</button>
      </div>
    `;

    // The course being booked, in the same box as the suggestions. The card is
    // for deciding, so the pick-a-session / order actions are left out.
    if (data.course && data.course.title) {
      const media = courseCard(data.course, false);
      media.classList.add("in-confirm");
      media.querySelectorAll(".course-exp, .course-foot, .course-chev").forEach((n) => n.remove());
      card.insertBefore(media, card.querySelector(".confirm-card-body"));
    }

    const okBtn = card.querySelector(".confirm-card-ok");
    const cancelBtn = card.querySelector(".confirm-card-cancel");
    const showResult = (cls, msg, link) => {
      // The decision is made: the result replaces the buttons (they would
      // otherwise sit there, disabled, still reading "Bekræfter…").
      card.querySelector(".confirm-card-actions").hidden = true;
      const prev = card.querySelector(".confirm-card-result");
      if (prev) prev.remove();
      const res = document.createElement("div");
      res.className = "confirm-card-result " + cls;
      res.textContent = msg;
      if (link) {
        const a = document.createElement("a");
        a.href = link.href;
        a.textContent = link.label;
        a.className = "confirm-card-link";
        res.appendChild(document.createTextNode(" "));
        res.appendChild(a);
      }
      card.appendChild(res);
      down();
    };
    const OK_STATUSES = ["success", "order_created", "team_orders_created", "handed_off_to_hr"];

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
        if (result.status === "order_created") {
          // The order tool's message is a long markdown receipt meant for the
          // model; the card says it plainly and links to the order.
          const label = result.order_status_label ? " (" + result.order_status_label + ")" : "";
          showResult("ok", "Bestillingen er registreret" + label + ".",
            { href: result.order_url || "/min-tidslinje", label: "Se bestillingen" });
        } else if (OK_STATUSES.indexOf(result.status) >= 0) {
          showResult("ok", result.message_da || result.message || "Bekræftet");
        } else if (result.status === "already_confirmed") {
          showResult("ok", "Allerede bekræftet");
        } else {
          showResult("err", result.message_da || result.message || "Fejl");
        }
        // The assistant confirms in writing too, as it does after any other answer.
        if (result.confirmation_text) {
          try {
            const reply = document.createElement("div");
            reply.className = "md";
            reply.innerHTML = md(result.confirmation_text);
            place(addBot(), "text", reply);
            down();
          } catch (e) { /* the card already says it */ }
        }
      } catch (e) {
        // Nothing is known to have been booked: let the user try again.
        okBtn.disabled = false; cancelBtn.disabled = false;
        okBtn.textContent = "Bekræft";
        showResult("err", "Netværksfejl — prøv igen");
        card.querySelector(".confirm-card-actions").hidden = false;
      }
    });

    cancelBtn.addEventListener("click", () => {
      showResult("err", "Afvist");
    });

    place(body, "ask", card);
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
    wrap.className = "mem-used";
    const head = document.createElement("button");
    head.type = "button";
    head.className = "mem-used-head";
    head.setAttribute("aria-expanded", "false");
    const headLabel = document.createElement("span");
    headLabel.textContent = "Brugte hukommelse om dig (" + remaining + ")";
    head.innerHTML = '<i class="fa-solid fa-brain" aria-hidden="true"></i> ';
    head.appendChild(headLabel);
    const chips = document.createElement("div");
    chips.className = "mem-used-list";
    chips.hidden = true;
    memories.forEach((m) => {
      const c = document.createElement("span");
      c.className = "mem-used-item";
      const lbl = document.createElement("span");
      lbl.textContent = (m.category ? "[" + m.category + "] " : "") + (m.label || "");
      c.appendChild(lbl);
      if (m.id) {
        const del = document.createElement("button");
        del.type = "button";
        del.className = "mem-used-del";
        del.title = "Slet hukommelse";
        del.setAttribute("aria-label", "Slet hukommelse");
        del.innerHTML = "×";
        del.addEventListener("click", async (e) => {
          e.stopPropagation();
          try {
            const r = await fetch("/app1/memory/" + m.id, { method: "DELETE" });
            const res = await r.json();
            if (res.status === "ok") {
              c.classList.add("removed");
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
    head.onclick = () => {
      chips.hidden = !chips.hidden;
      head.setAttribute("aria-expanded", chips.hidden ? "false" : "true");
    };
    wrap.appendChild(head); wrap.appendChild(chips);
    place(body, "notes", wrap); down();
  }

  function renderMemorySaved(body, data) {
    // Inline "saved a memory" confirmation with a one-click correction: the AI
    // writes inferred facts, so the user must be able to delete a wrong one
    // right where it appears (not only on the Mind-Map).
    const wrap = document.createElement("div");
    wrap.className = "mem-chip mem-saved";
    const label = document.createElement("span");
    label.innerHTML = '<i class="fa-solid fa-brain" aria-hidden="true"></i> <span>' + esc(data.message || ("Husket: " + (data.label || ""))) + "</span>";
    wrap.appendChild(label);
    if (data.id) {
      const del = document.createElement("button");
      del.type = "button";
      del.className = "mem-chip-del";
      del.title = "Forkert? Slet";
      del.innerHTML = '<i class="fa-solid fa-xmark"></i> Forkert';
      del.addEventListener("click", async () => {
        del.disabled = true;
        try {
          const r = await fetch("/app1/memory/" + data.id, { method: "DELETE" });
          const res = await r.json();
          if (res.status === "ok") {
            wrap.classList.add("removed");
            label.innerHTML = '<i class="fa-solid fa-brain" aria-hidden="true"></i> <span class="mem-gone">Slettet</span>';
            del.remove();
            refreshWorkspace();
          } else { del.disabled = false; }
        } catch (_) { del.disabled = false; }
      });
      wrap.appendChild(del);
    }
    place(body, "notes", wrap); down();
  }

  /* ---------------- cross-surface action card ---------------- */
  // The agent can MOVE the user through the app via a ui_action directive. We
  // render an explicit action button (never an auto-redirect) so navigation is
  // always user-initiated; "view" actions open in a new tab, in-app flows
  // (start order / open profiler) navigate the current tab on click.
  const ACTION_ICONS = {
    view_product: "fa-up-right-from-square", open_compare: "fa-scale-balanced",
    open_profile: "fa-user-pen", open_mind_map: "fa-brain",
    open_cv_upload: "fa-file-arrow-up", open_my_cv: "fa-file-lines",
    open_learning_path: "fa-route", open_catalog: "fa-magnifying-glass",
    start_order: "fa-cart-plus", open_profiler: "fa-user-check", open_advisor: "fa-comments",
  };
  function renderActionCard(body, data) {
    const target = data.target || "";
    if (!target) return;
    const action = data.action || "";
    const label = data.label || "Åbn";
    const newTab = data.new_tab !== false && action !== "start_order" && action !== "open_profiler";
    const card = document.createElement("div");
    card.className = "ai-action-card";
    const ico = ACTION_ICONS[action] || "fa-arrow-right";
    card.innerHTML = `
      <div class="ai-action-ic"><i class="fa-solid ${icon(ico)}"></i></div>
      <div class="ai-action-body">
        <div class="ai-action-title">${esc(data.title || label)}</div>
        <div class="ai-action-sub">${esc(newTab ? "Åbner i ny fane" : "Åbner her")}</div>
      </div>
      <button class="ai-action-go" type="button">${esc(label)}</button>`;
    card.querySelector(".ai-action-go").addEventListener("click", () => {
      if (action === "start_order" && data.handle && !isLoggedIn()) {
        toast("Log ind for at tilmelde dig", "courses"); return;
      }
      if (action === "start_order") trackLearner("order_start", { source: "ai_action" });
      if (action === "open_learning_path") trackLearner("learning_path_open", { source: "ai_action" });
      if (newTab) window.open(target, "_blank");
      else window.location.href = target;
    });
    place(body, "rich", card); down();
  }

  /* ---------------- analytical comparison card ---------------- */
  function renderComparisonCard(body, data) {
    const rows = Array.isArray(data.comparison) ? data.comparison : [];
    if (rows.length < 2) return;
    const analysis = data.analysis || {};
    const winners = analysis.winners || {};
    const card = document.createElement("div");
    card.className = "compare-card";
    const badge = (handle, txt) => `<span class="compare-badge">${esc(txt)}</span>`;
    // Map handle -> array of "winner" labels for that course.
    const wins = {};
    const axisLabel = { cheapest: "Billigst", shortest: "Hurtigst", certification: "Certificering", soonest: "Tidligst" };
    Object.keys(winners).forEach((axis) => {
      const w = winners[axis];
      if (w && w.handle) { (wins[w.handle] = wins[w.handle] || []).push(axisLabel[axis] || axis); }
    });
    const cols = rows.map((c) => {
      const myWins = (wins[c.handle] || []).map((t) => badge(c.handle, t)).join("");
      const facts = [];
      if (c.price) facts.push(`<div class="cc-fact"><span>Pris</span><b>${esc(c.price)}</b></div>`);
      if (c.duration_days) facts.push(`<div class="cc-fact"><span>Varighed</span><b>${esc(c.duration_days)} dag(e)</b></div>`);
      if (c.certification) facts.push(`<div class="cc-fact"><span>Certificering</span><b>${esc(c.certification)}</b></div>`);
      if (c.soonest_date) facts.push(`<div class="cc-fact"><span>Næste hold</span><b>${esc(c.soonest_date)}</b></div>`);
      if (c.locations && c.locations.length) facts.push(`<div class="cc-fact"><span>Sted</span><b>${esc(c.locations.join(", "))}</b></div>`);
      return `<div class="compare-col">
        <div class="compare-col-h"><div class="cc-vendor">${esc(c.vendor || "")}</div><div class="cc-title">${esc(c.title || "")}</div>${myWins ? `<div class="compare-badges">${myWins}</div>` : ""}</div>
        <div class="cc-facts">${facts.join("")}</div>
        ${c.handle ? `<button class="cc-open" data-h="${esc(c.handle)}">Åbn kurset</button>` : ""}
      </div>`;
    }).join("");
    card.innerHTML = `
      <div class="compare-head"><i class="fa-solid fa-scale-balanced"></i><span>Sammenligning</span></div>
      <div class="compare-grid">${cols}</div>
      ${analysis.verdict ? `<div class="compare-verdict"><i class="fa-solid fa-lightbulb"></i><span>${esc(analysis.verdict)}</span></div>` : ""}`;
    card.querySelectorAll(".cc-open").forEach((b) =>
      b.addEventListener("click", () => {
        trackLearner("recommendation_click", { source: "comparison" });
        window.open("/products/" + encodeURIComponent(b.getAttribute("data-h")), "_blank");
      }));
    place(body, "rich", card); down();
  }

  /* ---------------- learning-path card ---------------- */
  function renderLearningPathCard(body, data) {
    const path = data.path || {};
    const steps = Array.isArray(path.steps) ? path.steps : [];
    if (!steps.length) return;
    const card = document.createElement("div");
    card.className = "lpath-card";
    const totals = [];
    if (path.total_duration_days) totals.push(`<span><i class="fa-solid fa-clock"></i> ${esc(path.total_duration_days)} dage</span>`);
    if (path.total_cost != null) totals.push(`<span><i class="fa-solid fa-tag"></i> ca. ${Number(path.total_cost).toLocaleString("da-DK")} kr</span>`);
    const stepsHtml = steps.map((s, i) => {
      const courses = (s.courses || []).map((c) =>
        `<button class="lp-course" data-h="${esc(c.handle || "")}"><i class="fa-solid fa-graduation-cap"></i> ${esc(c.title || "")}</button>`).join("");
      return `<div class="lp-step">
        <div class="lp-step-no">${i + 1}</div>
        <div class="lp-step-body">
          <div class="lp-step-top">${s.level ? `<span class="lp-level">${esc(s.level)}</span>` : ""}<span class="lp-topic">${esc(s.topic || "")}</span></div>
          ${s.reason ? `<div class="lp-reason">${esc(s.reason)}</div>` : ""}
          ${courses ? `<div class="lp-courses">${courses}</div>` : ""}
        </div></div>`;
    }).join("");
    card.innerHTML = `
      <div class="lpath-head"><i class="fa-solid fa-route"></i><span>${esc(path.title || "Din læringssti")}</span></div>
      ${totals.length ? `<div class="lpath-totals">${totals.join("")}</div>` : ""}
      <div class="lpath-steps">${stepsHtml}</div>`;
    card.querySelectorAll(".lp-course").forEach((b) => {
      const h = b.getAttribute("data-h");
      if (h) b.addEventListener("click", () => {
        trackLearner("recommendation_click", { source: "learning_path" });
        window.open("/products/" + encodeURIComponent(h), "_blank");
      });
    });
    place(body, "rich", card); down();
  }

  /* ---------------- CV summary card ---------------- */
  function renderCvSummaryCard(body, data) {
    const counts = data.counts || {};
    const sections = data.sections || {};
    const total = data.total || 0;
    const card = document.createElement("div");
    card.className = "cv-summary-card";
    const LABELS = { skills: "Kompetencer", experience: "Erfaring", education: "Uddannelse", certifications: "Certificeringer", languages: "Sprog" };
    const ICONS = { skills: "fa-bolt", experience: "fa-briefcase", education: "fa-graduation-cap", certifications: "fa-certificate", languages: "fa-language" };
    const pillsHtml = Object.keys(LABELS).map((k) => {
      const n = counts[k] || 0;
      return `<div class="csv-pill ${n > 0 ? "has" : ""}"><i class="fa-solid ${icon(ICONS[k])}"></i><span>${esc(LABELS[k])}</span><b>${n}</b></div>`;
    }).join("");
    const previewItems = [];
    (sections.skills || []).slice(0, 3).forEach((s) => previewItems.push(`<span class="csv-tag">${esc(s.name)}</span>`));
    (sections.experience || []).slice(0, 2).forEach((e) => previewItems.push(`<span class="csv-tag exp">${esc(e.title || e.company)}</span>`));
    const previewHtml = previewItems.length ? `<div class="csv-preview">${previewItems.join("")}</div>` : "";
    const emptyNote = !total ? `<div class="csv-empty">Ingen CV-data endnu — upload dit CV for at komme i gang.</div>` : "";
    card.innerHTML = `
      <div class="cv-summary-head"><i class="fa-solid ${icon("fa-id-card")}"></i><span>CV-oversigt</span>${total > 0 ? `<span class="csv-total">${total} elementer</span>` : ""}</div>
      <div class="csv-pills">${pillsHtml}</div>
      ${previewHtml}${emptyNote}
      <div class="csv-footer">
        <button class="csv-btn primary" data-url="/profil#cv"><i class="fa-solid ${icon("fa-file-arrow-up")}"></i> ${total > 0 ? "Opdater CV" : "Upload CV"}</button>
        ${total > 0 ? `<button class="csv-btn" data-url="/profile"><i class="fa-solid ${icon("fa-user")}"></i> Se profil</button>` : ""}
      </div>`;
    card.querySelectorAll("[data-url]").forEach((b) =>
      b.addEventListener("click", () => window.open(b.getAttribute("data-url"), "_blank")));
    place(body, "rich", card); down();
  }

  /* ---------------- Mind-map preview card ---------------- */
  function renderMindmapCard(body, data) {
    const cats = data.categories || {};
    const comp = data.completeness || {};
    // Depth-aware, like every other surface (profile page, profiler, Mind-Map).
    const pct = comp.weighted_pct != null ? comp.weighted_pct : (comp.pct != null ? comp.pct : "—");
    const memories = data.recent_memories || [];
    const CLABELS = { kompetencer: "Kompetencer", erfaring: "Erfaring", uddannelse: "Uddannelse", certificeringer: "Cert.", sprog: "Sprog", maal: "Mål", kurser: "Kurser", laeringsstier: "Stier", hukommelse: "Hukommelse" };
    const card = document.createElement("div");
    card.className = "mindmap-prev-card";
    const catsHtml = Object.entries(cats).filter(([, n]) => n > 0).map(([k, n]) =>
      `<div class="mmp-cat"><span>${esc(CLABELS[k] || k)}</span><b>${n}</b></div>`).join("");
    const memHtml = memories.length ? memories.map((m) =>
      `<button type="button" class="mmp-mem" data-node="${esc(m.node_id || "")}"><i class="fa-solid ${icon("fa-brain")}"></i>${esc(m.label)}</button>`).join("") : "";
    const pctNum = typeof pct === "number" ? pct : 0;
    card.innerHTML = `
      <div class="mmp-head"><i class="fa-solid ${icon("fa-brain")}"></i><span>AI Mind-Map</span><span class="mmp-pct">${esc(String(pct))}%</span></div>
      <div class="mmp-bar-wrap"><div class="mmp-bar" style="width:${Math.min(100, pctNum)}%"></div></div>
      <div class="mmp-cats">${catsHtml}</div>
      ${memHtml ? `<div class="mmp-mems-head">Seneste hukommelser</div><div class="mmp-mems">${memHtml}</div>` : ""}
      <div class="csv-footer">
        <button class="csv-btn primary" data-url="/mind-map"><i class="fa-solid ${icon("fa-brain")}"></i> Åbn 3D Mind-Map</button>
      </div>`;
    card.querySelector("[data-url]").addEventListener("click", () => window.open("/mind-map", "_blank"));
    card.querySelectorAll("[data-node]").forEach((b) => b.addEventListener("click", () => {
      const node = b.getAttribute("data-node");
      window.open("/mind-map" + (node ? "#n=" + encodeURIComponent(node) : ""), "_blank");
    }));
    place(body, "rich", card); down();
  }

  /* ---------------- Skill-gap card (current → target on the 1-5 scale) ------- */
  function renderSkillGapsCard(body, data) {
    const gaps = data.gaps || [];
    const card = document.createElement("div");
    card.className = "skill-gaps-card";
    if (!data.has_gaps || !gaps.length) {
      const msg = data.reason === "covered"
        ? "Du dækker dine nuværende kompetencemål — flot! 🎉"
        : "Sæt en <b>målrolle</b> på din profil, så beregner jeg dine kompetencegab og foreslår kurser.";
      card.innerHTML = `
        <div class="sgc-head"><i class="fa-solid ${icon("fa-bullseye")}"></i><span>Kompetencegab</span></div>
        <div class="sgc-empty">${msg}</div>
        <div class="csv-footer"><button class="csv-btn primary" data-url="/profile"><i class="fa-solid ${icon("fa-user")}"></i> Åbn profil</button></div>`;
      card.querySelectorAll("[data-url]").forEach((b) =>
        b.addEventListener("click", () => window.open(b.getAttribute("data-url"), "_blank")));
      place(body, "rich", card); down(); return;
    }
    const PRIO = { critical: "crit", high: "high", medium: "med", low: "low" };
    const rowsHtml = gaps.map((g) => {
      const cur = Math.max(0, Math.min(5, g.current_level || 0));
      const tgt = Math.max(0, Math.min(5, g.target_level || 0));
      const curPct = (cur / 5) * 100, tgtPct = (tgt / 5) * 100;
      return `
        <button type="button" class="sgc-row" data-node="${esc(g.node_id || "kompetencer")}">
          <div class="sgc-row-top">
            <span class="sgc-skill">${esc(g.skill)}</span>
            ${g.category ? `<span class="sgc-cat">${esc(g.category)}</span>` : ""}
            <span class="sgc-gap ${PRIO[g.priority] || "med"}">+${esc(String(g.gap))}</span>
          </div>
          <div class="sgc-track" title="${esc(g.current_label || "")} → ${esc(g.target_label || "")}">
            <div class="sgc-target" style="width:${tgtPct}%"></div>
            <div class="sgc-current" style="width:${curPct}%"></div>
          </div>
          <div class="sgc-levels"><span>${esc(g.current_label || "")}</span><span>mål: ${esc(g.target_label || "")}</span></div>
        </button>`;
    }).join("");
    const role = data.target_role ? `<span class="sgc-role">mod ${esc(data.target_role)}</span>` : "";
    card.innerHTML = `
      <div class="sgc-head"><i class="fa-solid ${icon("fa-bullseye")}"></i><span>Dine kompetencegab</span>${role}</div>
      <div class="sgc-rows">${rowsHtml}</div>
      <div class="csv-footer">
        <button class="csv-btn primary" data-ask="Anbefal kurser der lukker mine største kompetencegab"><i class="fa-solid ${icon("fa-wand-magic-sparkles")}"></i> Find kurser der lukker gabet</button>
      </div>`;
    card.querySelectorAll("[data-ask]").forEach((b) =>
      b.addEventListener("click", () => { try { if (typeof ask === "function") ask(b.getAttribute("data-ask")); } catch (e) {} }));
    card.querySelectorAll("[data-node]").forEach((b) =>
      b.addEventListener("click", () => window.open("/mind-map#n=" + encodeURIComponent(b.getAttribute("data-node")), "_blank")));
    place(body, "rich", card); down();
  }

  /* ---------------- Agenda card (deadlines, approvals, certs, goals) -------- */
  function renderAgendaCard(body, data) {
    const items = data.items || [];
    const card = document.createElement("div");
    card.className = "agenda-card";
    if (!items.length) {
      card.innerHTML = `
        <div class="agc-head"><i class="fa-solid ${icon("fa-list-check")}"></i><span>Din agenda</span></div>
        <div class="agc-empty">Der er ikke noget der haster for dig lige nu. 🎉</div>
        <div class="csv-footer"><button class="csv-btn" data-url="/min-laering"><i class="fa-solid ${icon("fa-graduation-cap")}"></i> Åbn min læring</button></div>`;
      card.querySelectorAll("[data-url]").forEach((b) =>
        b.addEventListener("click", () => window.open(b.getAttribute("data-url"), "_blank")));
      place(body, "rich", card); down(); return;
    }
    const KIND = {
      deadline: { label: "Frist", icon: "fa-hourglass-half" },
      approval: { label: "Godkendelse", icon: "fa-circle-check" },
      certification: { label: "Certificering", icon: "fa-certificate" },
      goal: { label: "Mål", icon: "fa-bullseye" },
    };
    const rowsHtml = items.map((it) => {
      const k = KIND[it.kind] || { label: it.kind || "", icon: "fa-circle-info" };
      const urgency = it.overdue ? "overdue" : (it.days_left != null && it.days_left <= 14 ? "soon" : "later");
      const when = it.days_left == null ? "" : (it.overdue ? `${Math.abs(it.days_left)} dage over` : `om ${it.days_left} dage`);
      return `
        <div class="agc-row ${urgency}">
          <i class="fa-solid ${icon(k.icon)}"></i>
          <div class="agc-main">
            <span class="agc-title">${esc(it.title || "")}</span>
            <span class="agc-detail">${esc(it.detail || "")}</span>
          </div>
          ${when ? `<span class="agc-when">${esc(when)}</span>` : ""}
        </div>`;
    }).join("");
    const urgent = data.urgent_count || 0;
    card.innerHTML = `
      <div class="agc-head">
        <i class="fa-solid ${icon("fa-list-check")}"></i><span>Din agenda</span>
        ${urgent ? `<span class="agc-badge">${urgent} haster</span>` : ""}
      </div>
      <div class="agc-rows">${rowsHtml}</div>
      <div class="csv-footer">
        <button class="csv-btn primary" data-url="/min-tidslinje"><i class="fa-solid ${icon("fa-timeline")}"></i> Se hele tidslinjen</button>
      </div>`;
    card.querySelectorAll("[data-url]").forEach((b) =>
      b.addEventListener("click", () => window.open(b.getAttribute("data-url"), "_blank")));
    place(body, "rich", card); down();
  }

  /* ---------------- My-compliance card (mandatory training, self-scoped) ---- */
  function renderComplianceCard(body, data) {
    const reqs = data.requirements || [];
    const card = document.createElement("div");
    card.className = "compliance-card";
    if (!data.has_requirements || !reqs.length) {
      card.innerHTML = `
        <div class="cmc-head"><i class="fa-solid ${icon("fa-shield-halved")}"></i><span>Obligatoriske kurser</span></div>
        <div class="cmc-empty">${esc(data.message || "Der er ingen obligatoriske krav registreret for dig.")}</div>`;
      place(body, "rich", card); down(); return;
    }
    const STATE = {
      compliant: { label: "Opfyldt", cls: "ok" },
      expiring: { label: "Skal fornys", cls: "warn" },
      overdue: { label: "Udløbet", cls: "bad" },
      missing: { label: "Mangler", cls: "bad" },
    };
    const rowsHtml = reqs.map((r) => {
      const st = STATE[r.state] || { label: r.state || "", cls: "warn" };
      const when = r.days_left != null && r.state !== "compliant"
        ? (r.days_left < 0 ? `${Math.abs(r.days_left)} dage over` : `om ${r.days_left} dage`) : "";
      return `
        <div class="cmc-row">
          <div class="cmc-main">
            <span class="cmc-title">${esc(r.title || "")}</span>
            ${r.is_statutory ? `<span class="cmc-tag">lovpligtig</span>` : ""}
          </div>
          <span class="cmc-state ${st.cls}">${esc(st.label)}${when ? ` · ${esc(when)}` : ""}</span>
        </div>`;
    }).join("");
    const need = data.action_needed || 0;
    card.innerHTML = `
      <div class="cmc-head">
        <i class="fa-solid ${icon("fa-shield-halved")}"></i><span>Obligatoriske kurser</span>
        <span class="cmc-badge ${need ? "bad" : "ok"}">${need ? `${need} mangler` : "alt opfyldt"}</span>
      </div>
      <div class="cmc-rows">${rowsHtml}</div>
      ${need ? `<div class="csv-footer">
        <button class="csv-btn primary" data-ask="Find kurserne der lukker mine manglende obligatoriske krav"><i class="fa-solid ${icon("fa-magnifying-glass")}"></i> Find kurserne</button>
      </div>` : ""}`;
    card.querySelectorAll("[data-ask]").forEach((b) =>
      b.addEventListener("click", () => { try { if (typeof ask === "function") ask(b.getAttribute("data-ask")); } catch (e) {} }));
    place(body, "rich", card); down();
  }

  async function streamFromBackend(body, actualQuery, kind, context) {
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
    let buffer = "", textEl = null, tailEl = null, fullText = "", suggestions = null, done = false;
    let messageIndex = null;              // from the meta event; used by the feedback POST
    let cardsSeen = 0, productSeen = 0;   // pair structured course_cards with fallback product HTML
    let questionsSeen = false;            // a question sheet already answers this turn's questions
    let awaiting = false;                 // a card is waiting for the user's decision: no generic follow-ups on top of it
    let eventsReceived = 0;               // meaningful events (excl. ping) — gates the silent retry

    // rAF-throttled rendering: buffer chunks and re-parse markdown at most once
    // per animation frame instead of on every chunk.
    let renderQueued = false, finalized = false;
    // The server leaves CARDS_MARK where it cut a prose listing that the course cards
    // already show: the text before it sits above the cards, the rest (the closing
    // remark or question) below them, so the answer reads as one flow.
    const paintAnswer = (balance) => {
      const parts = fullText.split(CARDS_MARK);
      const clean = (t) => (balance ? md(balanceMarkdown(t)) : md(t));
      textEl.innerHTML = clean(parts[0]);
      const rest = parts.slice(1).join("\n\n").trim();
      if (rest) {
        if (!tailEl) { tailEl = document.createElement("div"); tailEl.className = "md"; place(body, "tail", tailEl); }
        tailEl.innerHTML = clean(rest);
      }
    };
    const renderStream = () => {
      if (finalized || !textEl) return;
      paintAnswer(true);
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
      if (textEl) { paintAnswer(false); down(); }
    };

    try {
      armWatchdog();
      const resp = await fetch(ASK_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign(
          { query: actualQuery, mode: (window.CHAT_MODE || "default"), kind: kind || "message" },
          context ? { context: context } : {})),
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
          if (data.type !== "tool_call" && data.type !== "tool_progress") settleActivity(body);

          if (data.type === "chunk") {
            if (!textEl) { textEl = document.createElement("div"); textEl.className = "md"; place(body, "text", textEl); }
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
            note.className = "note-line";
            note.textContent = data.content || "";
            place(body, "notes", note); down();
          } else if (data.type === "profile_update") {
            const note = document.createElement("div");
            note.className = "note-line";
            note.innerHTML = md(data.message || "Profil opdateret");
            place(body, "notes", note); down();
            refreshWorkspace();
          } else if (data.type === "profile_saved") {
            renderProfileSaved(body, data.items || []);
            refreshWorkspace();
          } else if (data.type === "profile_confirm_batch") {
            awaiting = true;
            renderProfileConfirmBatch(body, data.items || []);
          } else if (data.type === "profile_confirm_request") {
            // Proposed profile change -> native confirm card, wired to the real save.
            awaiting = true;
            const conf = data.confirm || {};
            // Row ids are plumbing for the save call, not something to show.
            const tags = (conf.data && typeof conf.data === "object")
              ? Object.entries(conf.data).filter(([k, v]) => k !== "id" && v).map(([, v]) => String(v)) : [];
            const forgetting = data.section === "memories";
            profileConfirm(body, { section: data.section, message: data.message || "", tags: forgetting ? undefined : tags, section_label: data.section,
                                   toast: forgetting ? "Hukommelsen er glemt" : undefined,
                                   label: forgetting ? "Glemt" : undefined },
              conf.action ? () => saveProfileUpdate(conf.action, conf.data || {}) : null);
          } else if (data.type === "ui_card") {
            if (data.ui_type === "questions" && window.FmQuestionSheet) {
              // Several questions at once: one field per question, sent back as ONE message.
              questionsSeen = true;
              window.FmQuestionSheet.render(zoneEl(body, "ask"), { message: data.message || "", fields: data.fields || [] },
                { send: ask, after: () => down() });
              awaiting = true;
              continue;
            }
            const choices = (data.choices || []).filter((c) => c && (c.label || c.value));
            if ((data.ui_type === "choice" || (!data.fields || !data.fields.length)) && choices.length) {
              // Choice card: options render as buttons; the pick is sent on.
              choiceCard(body, { section: data.section, message: data.message || "", choices: choices },
                (c) => { if (data.save_action && data.section !== "summary") { saveProfileUpdate(data.save_action, Object.assign({}, data.prefilled || {}, { value: c.value })).catch(() => {}); } else { ask(c.label || c.value); } });
              awaiting = true;
              continue;
            }
            // Form card -> native uiCard design, wired to the real save endpoint.
            awaiting = true;
            const fields = (data.fields || []).map((f) => ({
              name: f.name, label: f.label, type: f.type,
              ph: f.placeholder || f.ph, hint: f.hint, options: f.options || [],
            }));
            const prefilled = data.prefilled || {};
            uiCard(body, { section: data.section, message: data.message || "", fields: fields, prefilled: prefilled },
              data.save_action ? (values) => saveProfileUpdate(data.save_action, Object.assign({}, prefilled, values)) : null);
          } else if (data.type === "memory_used") {
            renderMemoryUsed(body, data.memories || []);
            refreshWorkspace();
          } else if (data.type === "memory_saved") {
            renderMemorySaved(body, data);
            refreshWorkspace();
          } else if (data.type === "profiler_progress") {
            if (typeof window.onProfilerProgress === "function") window.onProfilerProgress(data.completeness);
            refreshWorkspace();
          } else if (data.type === "tool_progress") {
            // In-flight chip progress update from build_tool_progress_event (Phase 1/9)
            updateToolProgress(body, data);
          } else if (data.type === "confirm_card") {
            // Side-effect tool preview: render Bekræft/Afvis card (Phase 8/9)
            awaiting = true;
            renderConfirmCard(body, data);
          } else if (data.type === "ui_action") {
            // Cross-surface navigation directive → explicit action button.
            renderActionCard(body, data);
          } else if (data.type === "comparison_card") {
            // Analytical comparison with per-axis winners + verdict.
            renderComparisonCard(body, data);
          } else if (data.type === "learning_path_card") {
            // Sequenced, grounded learning path.
            renderLearningPathCard(body, data);
          } else if (data.type === "cv_summary_card") {
            // CV snapshot + link to 3D portal.
            renderCvSummaryCard(body, data);
          } else if (data.type === "mindmap_card") {
            // Mind-map stats + link to 3D globe.
            renderMindmapCard(body, data);
          } else if (data.type === "skill_gaps_card") {
            // Per-learner current→target skill gaps (1-5), CTA to gap-closing courses.
            renderSkillGapsCard(body, data);
          } else if (data.type === "agenda_card") {
            // Cross-silo "what's on my plate": deadlines, approvals, certs, goals.
            renderAgendaCard(body, data);
          } else if (data.type === "compliance_card") {
            // The learner's own mandatory-training status.
            renderComplianceCard(body, data);
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
    settleActivity(body);
    // The model wrote a list of questions as prose instead of using the sheet: give the list
    // fields, so nobody has to type "1: ... 2: ..." into the chat.
    if (!questionsSeen && textEl && window.FmQuestionSheet) {
      try { window.FmQuestionSheet.fromList(zoneEl(body, "ask"), textEl, { send: ask, after: () => down() }); } catch (e) { /* cosmetic */ }
    }
    // Render suggestion chips last, like the source UI. The server now
    // guarantees a set, but keep a client-side net so a turn never dead-ends
    // even if the suggestions event is dropped.
    if (awaiting) suggestions = [];
    else if (!suggestions || !suggestions.length) {
      suggestions = cardsSeen > 0
        ? ["Sammenlign de to bedste", "Vis billigere alternativer", "Fortæl mig mere"]
        : ["Vis populære kurser", "Hjælp mig med at vælge"];
    }
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
    // A handoff ({from, focus}) rides on the first message after arriving from
    // another surface only; a retry of that message re-sends it.
    const context = opts.context !== undefined ? opts.context : takeHandoff();
    input.value = ""; resize(); toggleSend();
    setSending(true);
    const body = addBot();
    const th = thinking(body);
    let result = null, lastErr = null;
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        result = await streamFromBackend(body, actualQuery, opts.kind, context);
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
      appendError(body, actualQuery, { kind: opts.kind, context: context });
    } else {
      // User Stop: mark the cut-off, but still render the feedback row so
      // aborted answers are measurable.
      if (aborted) { settleToolChips(body); addStopMarker(body); }
      addFeedback(body, query, result || {});
    }
    finish();
  }
  function finish() {
    sending = false; setSending(false); toggleSend(); input.focus();
    if (window.fmAiSidebar && typeof window.fmAiSidebar.refresh === "function") {
      window.fmAiSidebar.refresh({ selectNewestIfNone: !activeConvId });
    }
  }
  function ask(text) { run(text); }
  window.fmAsk = ask;
  // UI-generated openers (e.g. a handoff from another surface). The server skips
  // intent classification for a seed, so the opener's wording can never pull
  // in a playbook the user didn't ask for.
  window.fmSendSeed = (text) => run(text, { kind: "seed" });

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
  // "Pick up where you left off": the newest conversations on the welcome screen.
  // The history panel (ai-sidebar.js) owns the list; this only shows its top three.
  function paintRecents() {
    const box = document.getElementById("wRecent");
    if (!box) return;
    const items = (window.fmAiSidebar && window.fmAiSidebar.recent ? window.fmAiSidebar.recent(3) : [])
      .filter((c) => c && c.id != null && String(c.id) !== String(activeConvId));
    if (!items.length) { box.hidden = true; box.innerHTML = ""; return; }
    const when = (iso) => {
      const d = new Date(iso); if (!iso || isNaN(d.getTime())) return "";
      const days = Math.floor((Date.now() - d.getTime()) / 86400000);
      if (days < 1) return d.toLocaleTimeString("da-DK", { hour: "2-digit", minute: "2-digit" });
      if (days < 2) return "i går";
      return d.toLocaleDateString("da-DK", { day: "numeric", month: "short" });
    };
    box.innerHTML = '<div class="w-recent-h">Fortsæt hvor du slap</div>' + items.map((c) =>
      `<button type="button" class="w-recent-row" data-id="${esc(c.id)}"><span class="w-recent-t">${esc(c.title || "Samtale")}</span><span class="w-recent-w">${esc(when(c.updated_at))}</span></button>`).join("");
    box.hidden = false;
    box.querySelectorAll(".w-recent-row").forEach((b) => b.addEventListener("click", () => {
      if (typeof window.fmOpenConversation === "function") window.fmOpenConversation(b.getAttribute("data-id"));
    }));
  }
  document.addEventListener("fm:conversations", paintRecents);

  function welcome() {
    thread.innerHTML = `
      <div class="welcome">
        <div class="w-logo">${BOT}</div>
        <div class="w-eyebrow">Futurematch AI-assistent</div>
        <div class="w-title">Hvad vil du gerne hjælpes med?</div>
        <div class="w-sub">Fortæl, hvor du står og hvor du vil hen, eller beskriv et behov, en rolle eller en kompetence. Så finder jeg kurser, sammenligner muligheder og husker det vigtige på din profil.</div>
        <div class="w-hint">Anbefalinger tilpasses din profil</div>
        <div class="w-grid">
          <button class="w-card" data-q="Vis mig populære projektledelseskurser"><span class="ic"><i class="fa-solid fa-diagram-project"></i></span><span><div class="t">Populære kurser</div><div class="h">Se hvad andre vælger</div></span></button>
          <button class="w-card" data-q="Jeg vil gerne tale om, hvor jeg vil hen i min karriere"><span class="ic"><i class="fa-solid fa-compass"></i></span><span><div class="t">Min retning</div><div class="h">Sparring om næste skridt</div></span></button>
          <button class="w-card" data-q="Hvilke kompetencer mangler jeg for at nå mit mål?"><span class="ic"><i class="fa-solid fa-layer-group"></i></span><span><div class="t">Mine kompetencegab</div><div class="h">Se hvad der mangler</div></span></button>
          <button class="w-card" data-q="Opdater mit CV — jeg har erfaring med projektledelse og teamledelse"><span class="ic"><i class="fa-solid fa-id-card"></i></span><span><div class="t">Opdater dit CV</div><div class="h">Fortæl mig om din erfaring</div></span></button>
        </div>
        <div class="w-recent" id="wRecent" hidden></div>
      </div>`;
    thread.querySelectorAll(".w-card").forEach((c) => c.onclick = () => ask(c.dataset.q));
    paintRecents();
  }
  async function newChat() {
    attached = []; renderRef(); input.value = ""; toggleSend();
    // Honest reset: clear the server-side session (CHAT_MEMORY, shown products,
    // stage, rejections) BEFORE painting the welcome screen, so a "new" chat
    // never silently inherits the previous conversation's context. A failed
    // call still resets the UI — better a fresh screen than a stuck button.
    try {
      // Reset this surface's open conversation on the server.
      await fetch("/app1/new_session", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
        credentials: "same-origin",
        body: JSON.stringify({ mode: (window.CHAT_MODE || "default") }),
      });
    } catch (e) { /* offline / anonymous: still reset the UI */ }
    activeConvId = null;
    syncConvUrl(null);
    if (window.fmAiSidebar && typeof window.fmAiSidebar.setActive === "function") {
      window.fmAiSidebar.setActive(null);
    } else {
      // Sidebar not mounted yet: clear its remembered selection so that, after
      // this new chat's first message, it selects the new conversation.
      try { sessionStorage.removeItem("fm-ai-active-conv"); } catch (e) { /* ignore */ }
    }
    welcome();
    input.focus();
  }
  window.fmNewChat = newChat;
  // The conversation you are in is pinned in the URL (?c=<id>), so a reload or
  // a shared link reopens it — everything else opens a new chat.
  window.fmSetActiveConvId = (id) => { activeConvId = id || null; syncConvUrl(activeConvId); };

  /* ---------------- conversation restore ---------------- */

  function syncConvUrl(id) {
    if (!window.history || !history.replaceState) return;
    try {
      const u = new URL(location.href);
      if (id) u.searchParams.set("c", String(id));
      else u.searchParams.delete("c");
      u.searchParams.delete("new");
      history.replaceState({}, "", u.pathname + u.search + u.hash);
    } catch (e) { /* ignore */ }
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
          settleActivity(body);
        }
        const parts = String(content).split(CARDS_MARK);
        const el = document.createElement("div");
        el.className = "md";
        el.innerHTML = md(parts[0]);
        place(body, "text", el);
        const rest = parts.slice(1).join("\n\n").trim();
        if (rest) {
          const tail = document.createElement("div");
          tail.className = "md";
          tail.innerHTML = md(rest);
          place(body, "tail", tail);
        }
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
    syncConvUrl(id);
    if (window.fmAiSidebar && typeof window.fmAiSidebar.setActive === "function") {
      window.fmAiSidebar.setActive(id);
    }
    if (rail && window.innerWidth <= 860) rail.classList.remove("open");
    try {
      const resp = await fetch("/app1/conversations/" + encodeURIComponent(id) + "/resume", {
        method: "POST",
        headers: { "X-Requested-With": "XMLHttpRequest" },
        credentials: "same-origin",
      });
      if (resp.ok) {
        const data = await resp.json();
        if (data && data.status === "ok") { renderHistory(data.messages); input.focus(); return true; }
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
        if (conv && conv.messages) { renderHistory(conv.messages); return true; }
      }
    } catch (e) { /* leave current view untouched */ }
    return false;
  }
  window.fmOpenConversation = openConversation;

  /* ---------------- login state ----------------
     Positive signal: the workspace fetch succeeded (profileLoaded). Negative
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
  const nudgeX = $("#nudgeX");
  if (nudgeX) nudgeX.onclick = () => hideNudge();
  const nudgeLink = $("#nudgeLink");
  if (nudgeLink) nudgeLink.onclick = (e) => {
    e.preventDefault();
    hideNudge();
    const url = e.currentTarget.dataset.url;
    if (url) { window.location.href = url; return; }
    ask("Hvad mangler min profil?");
  };

  /* ---------------- init ----------------
     Like every mainstream AI chat: opening /chat starts a NEW
     conversation, and past ones live in the sidebar. The server session is
     reset too (newChat → /new_session for this surface), so a blank thread can
     never be quietly continuing an old one; what the AI should remember comes
     from the profile, memories and conversation digests, not the transcript.
     ?c=<id> reopens a specific conversation (sidebar links, reloads — the open
     conversation is pinned in the URL); ?intent= sends into a fresh chat. */
  // {from, focus} from the URL of a cross-surface handoff (mind-map node,
  // profile section, CV import); consumed by the first message (app1/surface_context.py).
  let pendingHandoff = null;
  function takeHandoff() { const h = pendingHandoff; pendingHandoff = null; return h; }
  function bootChat() {
    let params;
    try { params = new URLSearchParams(location.search); } catch (e) { params = new URLSearchParams(); }
    const cid = params.get("c");
    if (cid) return openConversation(cid);
    const intent = (params.get("intent") || "").trim();
    const from = (params.get("from") || "").trim();
    const focus = (params.get("focus") || "").trim();
    if (intent || from || focus) {
      try {
        ["intent", "from", "focus"].forEach((k) => params.delete(k));
        history.replaceState(null, "", location.pathname + (params.toString() ? "?" + params.toString() : "") + location.hash);
      } catch (e) { /* non-critical */ }
    }
    if (from || focus) pendingHandoff = { from: from, focus: focus };
    return newChat().then(() => {
      if (intent) {
        input.value = intent;
        input.dispatchEvent(new Event("input", { bubbles: true }));
        setTimeout(() => { if (!sending) send.click(); }, 80);
        return true;
      }
      if (pendingHandoff && pendingHandoff.focus) {
        // Arrived with something in focus but no words: open with a neutral
        // seed so the AI starts from what the user was looking at. A bare
        // `from` waits for the first message .
        run("Lad os tage udgangspunkt i det, jeg kiggede på", { kind: "seed" });
        return true;
      }
      return false;
    });
  }
  window.fmChatBoot = bootChat();
  renderRef();
  refreshWorkspace(0);
  loadNudges();
  input.focus();
})();
