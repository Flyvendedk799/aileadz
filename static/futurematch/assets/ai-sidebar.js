/* Futurematch — shared AI conversation history for /chat, /mind-map.
   Fills #aiConvPanel, a floating panel opened from the header button
   [data-ai-hist-toggle] (on wide chat screens it can be pinned as a column).
   Chat pages expose window.fmOpenConversation / window.fmNewChat; Mind-Map
   navigates to the matching surface. */
(function () {
  "use strict";

  const panel = document.getElementById("aiConvPanel");
  if (!panel) return;

  const listEl = document.getElementById("aiConvList");
  const searchEl = document.getElementById("aiConvSearch");
  const page = (panel.getAttribute("data-ai-page") || document.body.dataset.page || "").trim();
  const STORE_KEY = "fm-ai-active-conv";
  const FILTER_KEY = "fm-ai-conv-filter";
  const PIN_KEY = "fm-ai-hist-pinned";
  const hist = document.getElementById("aiHist") || panel;
  const WIDE = window.matchMedia ? window.matchMedia("(min-width: 1180px)") : { matches: false, addEventListener: function () {} };

  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

  let CONVS = [];
  let activeId = null;
  let filter = "all";
  let query = "";

  try { filter = localStorage.getItem(FILTER_KEY) || "all"; } catch (e) { /* ignore */ }
  filter = "all";
  try {
    const stored = sessionStorage.getItem(STORE_KEY);
    if (stored) activeId = stored;
  } catch (e) { /* ignore */ }

  // One assistant: every conversation, including ones that began in the former
  // AI Profiler, opens in /chat.
  function modeOf() {
    return "chat";
  }
  function hrefFor(c) {
    return "/chat?c=" + encodeURIComponent(c.id);
  }
  function persistActive(id) {
    activeId = id ? String(id) : null;
    try {
      if (activeId) sessionStorage.setItem(STORE_KEY, activeId);
      else sessionStorage.removeItem(STORE_KEY);
    } catch (e) { /* ignore */ }
    if (typeof window.fmSetActiveConvId === "function") window.fmSetActiveConvId(activeId);
  }

  function convGroup(iso) {
    if (!iso) return "older";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "older";
    const now = new Date();
    const startOfDay = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const diff = startOfDay(now) - startOfDay(d);
    if (diff <= 0) return "today";
    if (diff <= 86400000) return "yesterday";
    return "older";
  }

  function relTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    const diff = Date.now() - d.getTime();
    if (diff < 45000) return "lige nu";
    if (diff < 3600000) return Math.max(1, Math.round(diff / 60000)) + " min";
    if (diff < 86400000 && convGroup(iso) === "today") {
      return d.toLocaleTimeString("da-DK", { hour: "2-digit", minute: "2-digit" });
    }
    return d.toLocaleDateString("da-DK", { day: "numeric", month: "short" });
  }

  function visibleConvs() {
    const q = query.trim().toLowerCase();
    return CONVS.filter((c) => {
      if (filter !== "all" && modeOf(c) !== filter) return false;
      if (q && String(c.title || "").toLowerCase().indexOf(q) === -1) return false;
      return true;
    });
  }

  function render() {
    if (!listEl) return;
    const items = visibleConvs();
    if (!CONVS.length) {
      listEl.innerHTML = '<div class="ai-conv-empty">Ingen samtaler endnu. Start en samtale med AI-assistenten — de vises her på alle AI-siderne.</div>';
      return;
    }
    if (!items.length) {
      listEl.innerHTML = '<div class="ai-conv-empty">Ingen samtaler matcher.</div>';
      return;
    }
    const groups = [["today", "I dag"], ["yesterday", "I går"], ["older", "Ældre"]];
    listEl.innerHTML = groups.map(([k, lab]) => {
      const rows = items.filter((c) => convGroup(c.updated_at) === k);
      if (!rows.length) return "";
      return '<div class="ai-conv-date">' + esc(lab) + "</div>" + rows.map((c) => {
        const mode = modeOf(c);
        const on = String(c.id) === String(activeId);
        const badge = "Assistent";
        const icon = "fa-comment-dots";
        const when = relTime(c.updated_at);
        return (
          '<div class="ai-conv-item' + (on ? " is-on" : "") + '" role="listitem" tabindex="0"' +
            ' data-id="' + esc(c.id) + '" data-mode="' + mode + '"' +
            (on ? ' aria-current="true"' : "") +
            ' title="' + esc(c.title || "Samtale") + '">' +
            '<span class="ai-conv-ico"><i class="fa-solid ' + icon + '"></i></span>' +
            '<span class="ai-conv-body">' +
              '<span class="ai-conv-title">' + esc(c.title || "Samtale") + "</span>" +
              '<span class="ai-conv-meta">' +
                '<span class="ai-conv-badge">' + badge + "</span>" +
                (when ? "<span>" + esc(when) + "</span>" : "") +
              "</span>" +
            "</span>" +
            '<button type="button" class="ai-conv-del" data-id="' + esc(c.id) + '" title="Slet" aria-label="Slet samtale">' +
              '<i class="fa-solid fa-trash"></i>' +
            "</button>" +
          "</div>"
        );
      }).join("");
    }).join("");

    listEl.querySelectorAll(".ai-conv-item").forEach((el) => {
      const open = () => openConv(el.getAttribute("data-id"), el.getAttribute("data-mode"));
      el.addEventListener("click", (e) => {
        if (e.target.closest(".ai-conv-del")) return;
        open();
      });
      el.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
      });
    });
    listEl.querySelectorAll(".ai-conv-del").forEach((b) => {
      b.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        deleteConv(b.getAttribute("data-id"));
      });
    });
  }

  function paintFilters() {
    panel.querySelectorAll("[data-ai-filter]").forEach((b) => {
      const on = b.getAttribute("data-ai-filter") === filter;
      b.classList.toggle("is-on", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
  }

  function openConv(id, mode) {
    if (!id) return;
    persistActive(id);
    render();
    closeFloating();
    const conv = CONVS.find((c) => String(c.id) === String(id));
    if (page === "chat" && typeof window.fmOpenConversation === "function") {
      window.fmOpenConversation(id);
      return;
    }
    window.location.href = conv ? hrefFor(conv) : "/chat?c=" + encodeURIComponent(id);
  }

  async function deleteConv(id) {
    const conv = CONVS.find((c) => String(c.id) === String(id));
    const title = (conv && conv.title) || "samtalen";
    if (!window.confirm('Slet "' + title + '"?')) return;
    CONVS = CONVS.filter((c) => String(c.id) !== String(id));
    const wasActive = String(activeId) === String(id);
    if (wasActive) persistActive(null);
    render();
    try {
      await fetch("/app1/conversations/" + encodeURIComponent(id), {
        method: "DELETE",
        headers: { "X-Requested-With": "XMLHttpRequest" },
        credentials: "same-origin",
      });
    } catch (e) { /* optimistic */ }
    if (wasActive && typeof window.fmNewChat === "function") window.fmNewChat();
  }

  async function refresh(opts) {
    opts = opts || {};
    try {
      const resp = await fetch("/app1/conversations", {
        headers: { "X-Requested-With": "XMLHttpRequest" },
        credentials: "same-origin",
      });
      if (!resp.ok) { CONVS = []; render(); return CONVS; }
      const data = await resp.json();
      CONVS = Array.isArray(data && data.conversations) ? data.conversations : [];
    } catch (e) {
      CONVS = [];
    }
    if (opts.activeId != null) persistActive(opts.activeId);
    else if (opts.selectNewestIfNone && !activeId && CONVS.length) persistActive(CONVS[0].id);
    render();
    document.dispatchEvent(new CustomEvent("fm:conversations", { detail: { items: CONVS } }));
    return CONVS;
  }

  function newChat() {
    persistActive(null);
    render();
    closeFloating();
    if (typeof window.fmNewChat === "function") {
      window.fmNewChat();
      return;
    }
    const btn = panel.querySelector("[data-ai-new]");
    const href = (btn && btn.getAttribute("data-ai-new-href")) || "/chat?new=1";
    window.location.href = href;
  }

  // the header carries a "new chat" button too, so look beyond the panel
  document.querySelectorAll("[data-ai-new]").forEach((b) => {
    b.addEventListener("click", (e) => { e.preventDefault(); newChat(); });
  });
  panel.querySelectorAll("[data-ai-filter]").forEach((b) => {
    b.addEventListener("click", () => {
      filter = b.getAttribute("data-ai-filter") || "all";
      try { localStorage.setItem(FILTER_KEY, filter); } catch (e) { /* ignore */ }
      paintFilters();
      render();
    });
  });
  if (searchEl) {
    searchEl.addEventListener("input", () => { query = searchEl.value || ""; render(); });
  }

  /* ---- panel: floating (Esc / click outside closes) or pinned as a column ---- */
  const toggles = document.querySelectorAll("[data-ai-hist-toggle]");
  const pinBtn = panel.querySelector("[data-ai-hist-pin]");
  const canPin = () => page === "chat" && WIDE.matches;
  const isPinned = () => document.body.classList.contains("ai-hist-pinned");
  const isOpen = () => hist.classList.contains("open") || isPinned();

  function syncButtons() {
    const open = isOpen();
    toggles.forEach((b) => b.setAttribute("aria-expanded", open ? "true" : "false"));
    if (pinBtn) {
      pinBtn.classList.toggle("on", isPinned());
      pinBtn.setAttribute("aria-pressed", isPinned() ? "true" : "false");
      pinBtn.title = isPinned() ? "Frigør fra siden" : "Fastgør til siden";
    }
  }
  function openFloating() {
    hist.classList.add("open");
    syncButtons();
    if (searchEl) { try { searchEl.focus({ preventScroll: true }); } catch (e) { /* ignore */ } }
  }
  function closeFloating() {
    hist.classList.remove("open");
    syncButtons();
  }
  function setPinned(on) {
    document.body.classList.toggle("ai-hist-pinned", !!on && canPin());
    try { localStorage.setItem(PIN_KEY, on ? "1" : "0"); } catch (e) { /* ignore */ }
    if (isPinned()) hist.classList.remove("open");
    syncButtons();
  }
  function togglePanel() {
    if (isPinned()) { setPinned(false); return; }
    if (hist.classList.contains("open")) closeFloating(); else openFloating();
  }

  toggles.forEach((b) => b.addEventListener("click", (e) => { e.preventDefault(); togglePanel(); }));
  if (pinBtn) pinBtn.addEventListener("click", () => { if (isPinned()) { setPinned(false); openFloating(); } else setPinned(true); });
  panel.querySelectorAll("[data-ai-hist-close]").forEach((b) => b.addEventListener("click", () => { if (isPinned()) setPinned(false); else closeFloating(); }));
  hist.querySelectorAll(".ai-hist-scrim[data-ai-hist-close]").forEach((b) => b.addEventListener("click", closeFloating));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && hist.classList.contains("open")) { closeFloating(); }
  });
  try { if (localStorage.getItem(PIN_KEY) === "1" && canPin()) document.body.classList.add("ai-hist-pinned"); } catch (e) { /* ignore */ }
  if (WIDE.addEventListener) {
    WIDE.addEventListener("change", () => {
      if (!WIDE.matches) document.body.classList.remove("ai-hist-pinned");
      else { try { if (localStorage.getItem(PIN_KEY) === "1" && canPin()) document.body.classList.add("ai-hist-pinned"); } catch (e) { /* ignore */ } }
      syncButtons();
    });
  }
  syncButtons();

  window.fmAiSidebar = {
    // the newest conversations, for the welcome screen ("pick up where you left off")
    recent: function (n) { return CONVS.slice(0, n || 3); },
    open: openFloating,
    close: closeFloating,
    refresh: refresh,
    setActive: function (id) { persistActive(id); render(); },
    getActive: function () { return activeId; },
    newChat: newChat,
  };

  paintFilters();
  refresh();
})();
