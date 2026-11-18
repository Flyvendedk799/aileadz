/* Futurematch shell — shared sidebar, theme + collapse behaviour.
   Each page sets <body data-page="..."> to mark the active nav item. */
(function () {
  /* ---- White-label brand layer ---- */
  const DEFAULT_BRAND = { name: "Futurematch", tagline: "Læring & HR", mark: "F", primary: "", accent: "", poweredBy: true };
  function getBrand() {
    try { return Object.assign({}, DEFAULT_BRAND, JSON.parse(localStorage.getItem("fm-brand") || "{}")); }
    catch (e) { return Object.assign({}, DEFAULT_BRAND); }
  }
  function setBrand(cfg) {
    const merged = Object.assign(getBrand(), cfg);
    localStorage.setItem("fm-brand", JSON.stringify(merged));
    applyBrand(true);
    return merged;
  }
  function resetBrand() { localStorage.removeItem("fm-brand"); applyBrand(true); }

  /* The server renders the real company name, logo and colours (white-label). Those
     win: applyBrand() only repaints when the branding editor asks for a live preview
     (force = true). Before, it overwrote the server-rendered name/logo with whatever
     stale localStorage held. */
  function applyBrand(force) {
    if (document.body && document.body.dataset.brandServer === "1" && !force) return;
    const b = getBrand();
    // --- colors ---
    let st = document.getElementById("fm-brand-vars");
    if (b.primary) {
      if (!st) { st = document.createElement("style"); st.id = "fm-brand-vars"; document.head.appendChild(st); }
      const X = b.primary, Y = b.accent || b.primary;
      st.textContent =
        `:root{--fm-primary:${X};--fm-primary-700:color-mix(in srgb,${X},#000 26%);--fm-primary-600:color-mix(in srgb,${X},#000 12%);--fm-primary-300:color-mix(in srgb,${X},#fff 32%);--fm-primary-tint:color-mix(in srgb,${X} 12%,#fff);--fm-ring:color-mix(in srgb,${X} 22%,transparent);--fm-clay:${Y};--fm-clay-tint:color-mix(in srgb,${Y} 14%,#fff);}` +
        `[data-theme="dark"]{--fm-primary:color-mix(in srgb,${X},#fff 12%);--fm-primary-700:color-mix(in srgb,${X},#fff 2%);--fm-primary-600:${X};--fm-primary-300:color-mix(in srgb,${X},#000 16%);--fm-primary-tint:color-mix(in srgb,${X} 16%,transparent);--fm-ring:color-mix(in srgb,${X} 30%,transparent);--fm-clay:color-mix(in srgb,${Y},#fff 10%);--fm-clay-tint:color-mix(in srgb,${Y} 16%,transparent);}`;
    } else if (st) { st.remove(); }
    // --- name / tagline / mark across all brand surfaces ---
    document.querySelectorAll(".fm-brand-name, .ab-name, [data-brand-name]").forEach(e => e.textContent = b.name);
    document.querySelectorAll(".fm-brand-sub, .ab-sub, [data-brand-tagline]").forEach(e => e.textContent = b.tagline);
    document.querySelectorAll(".fm-mark, .ab-mark, [data-brand-mark]").forEach(e => e.textContent = b.mark);
    // --- footer brand word + powered-by ---
    document.querySelectorAll(".fm-foot").forEach(f => {
      const first = f.querySelector("span");
      if (first && b.name !== "Futurematch") first.innerHTML = first.innerHTML.replace(/Futurematch/g, b.name);
      let pb = f.querySelector(".fm-powered");
      if (b.name !== "Futurematch" && b.poweredBy) {
        if (!pb) { pb = document.createElement("span"); pb.className = "fm-powered"; pb.style.cssText = "opacity:.7"; f.insertBefore(pb, f.firstChild.nextSibling); }
        pb.textContent = "Drevet af Futurematch";
      } else if (pb) { pb.remove(); }
    });
    document.title = document.title.replace(/Futurematch/g, b.name);
  }
  window.fmGetBrand = getBrand;
  window.fmSetBrand = setBrand;
  window.fmResetBrand = resetBrand;
  window.fmApplyBrand = applyBrand;

  function init() {
    initSidebar();

    // theme — smart default: stored pref → else system preference
    const stored = localStorage.getItem("fm-theme");
    const sysDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    applyTheme(stored || (sysDark ? "dark" : "light"));
    document.addEventListener("click", (e) => {
      const t = e.target.closest("[data-theme-toggle]");
      if (!t) return;
      const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
      // brief cross-fade
      const root = document.documentElement;
      root.classList.add("fm-theming");
      setTimeout(() => root.classList.remove("fm-theming"), 400);
      applyTheme(next);
      localStorage.setItem("fm-theme", next);
    });

    initMotion();
    initNavSections();
    initCmdK();
    initTooltips();
    initNotifBadge();
    applyBrand();
  }

  function initSidebar() {
    const side = document.getElementById("fmSide");
    if (!side) return;
    const toggle = document.getElementById("fmSideToggle");
    const opener = document.querySelector("[data-side-open]");
    const main = document.querySelector(".fm-main");
    const mobile = window.matchMedia("(max-width: 1080px)");
    const scrim = document.createElement("div");
    scrim.className = "fm-scrim";
    document.body.appendChild(scrim);

    let collapsed = localStorage.getItem("fm-collapsed") === "1";
    function paint(open) {
      const drawer = mobile.matches && open;
      const compact = !mobile.matches && collapsed;
      side.classList.toggle("open", drawer);
      side.classList.toggle("collapsed", compact);
      scrim.classList.toggle("show", drawer);
      side.inert = mobile.matches && !drawer;
      main.inert = drawer;
      opener.setAttribute("aria-expanded", String(drawer));
      toggle.setAttribute("aria-expanded", String(mobile.matches ? drawer : !compact));
      toggle.setAttribute("aria-label", mobile.matches ? "Luk menu" : compact ? "Udvid menu" : "Fold menu sammen");
      toggle.firstElementChild.className = "fa-solid " + (mobile.matches ? "fa-xmark" : "fa-bars");
    }
    function closeDrawer() {
      paint(false);
      opener.focus();
    }
    opener.addEventListener("click", () => { paint(true); toggle.focus(); });
    toggle.addEventListener("click", () => {
      if (mobile.matches) { closeDrawer(); return; }
      collapsed = !collapsed;
      localStorage.setItem("fm-collapsed", collapsed ? "1" : "0");
      paint(false);
    });
    scrim.addEventListener("click", closeDrawer);
    document.addEventListener("keydown", e => {
      if (e.key === "Escape" && !document.querySelector("#cmdkBack[open]") && side.classList.contains("open")) closeDrawer();
    });
    side.addEventListener("click", e => {
      if (mobile.matches && e.target.closest("a[href]")) closeDrawer();
    });
    side.querySelectorAll(".fm-nav-link").forEach(link => {
      const name = link.querySelector("span")?.textContent;
      if (name) { link.setAttribute("aria-label", name); link.title = name; }
      if (link.classList.contains("active")) link.setAttribute("aria-current", "location");
    });
    mobile.addEventListener("change", () => paint(false));
    paint(false);
  }

  /* ---- Collapsible sidebar sections ----
     A label with data-section folds the list that follows it. The state is
     remembered per section (localStorage, best effort); data-default="closed"
     is the starting state for a first visit, and the section that holds the
     current page always stays open so the active link is never hidden. */
  function initNavSections() {
    document.querySelectorAll("#fmSide .fm-nav-label[data-section]").forEach(label => {
      const list = label.nextElementSibling;
      if (!list || !list.classList.contains("fm-nav-list")) return;
      const key = "fm-nav-" + label.dataset.section;
      let open = label.dataset.default !== "closed";
      try {
        const saved = localStorage.getItem(key);
        if (saved === "1") open = true;
        else if (saved === "0") open = false;
      } catch (e) { /* storage blocked: use the default */ }
      if (list.querySelector("a.active")) open = true;
      label.classList.add("is-foldable");
      label.setAttribute("role", "button");
      label.setAttribute("tabindex", "0");
      function paint() {
        label.setAttribute("aria-expanded", open ? "true" : "false");
        list.classList.toggle("is-folded", !open);
      }
      function toggle() {
        open = !open;
        paint();
        try { localStorage.setItem(key, open ? "1" : "0"); } catch (e) { /* ignore */ }
      }
      label.addEventListener("click", toggle);
      label.addEventListener("keydown", e => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
      });
      paint();
    });
  }

  /* ---- Unread notification badge (bell + nav) ----
     Sourced from a single cheap poll of the company-scoped unread count.
     Fully guarded: any error leaves the shell untouched (badges stay hidden),
     so users without notifications / outside a company are never affected. */
  function initNotifBadge() {
    const dots = document.querySelectorAll("[data-notif-dot]");
    const counts = document.querySelectorAll("[data-notif-count]");
    const navBadges = document.querySelectorAll("[data-notif-badge]");
    if (!dots.length && !counts.length && !navBadges.length) return;
    if (!window.fetch) return;
    // Anonymous visitors have no notifications; the endpoint would answer 401.
    if (document.body.dataset.auth === "0") return;

    function paint(n) {
      const has = n > 0;
      const label = n > 99 ? "99+" : String(n);
      dots.forEach(d => { d.hidden = !has; });
      counts.forEach(c => { c.hidden = !has; if (has) c.textContent = label; });
      navBadges.forEach(b => { b.hidden = !has; if (has) b.textContent = label; });
    }

    function refresh() {
      fetch("/api/notifications/company/unread_count", {
        credentials: "same-origin", headers: { "Accept": "application/json" }
      })
        .then(r => r.ok ? r.json() : null)
        .then(d => { if (d && typeof d.unread_count === "number") paint(d.unread_count); })
        .catch(() => {});
    }

    refresh();
    // Light poll so the badge stays fresh without hammering the server.
    window.fmRefreshNotifBadge = refresh;
    setInterval(refresh, 60000);
  }

  /* Universal scroll-reveal. Adds html.fm-motion (so CSS hidden-state
     only applies when JS is live), then reveals [data-reveal] as they
     enter the viewport. Safety net reveals everything after 1.5s. */
  function initMotion() {
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce || !("IntersectionObserver" in window)) { countAllNow(); return; }
    const root = document.documentElement;
    root.classList.add("fm-motion");

    const io = new IntersectionObserver((entries) => {
      entries.forEach((en) => {
        if (en.isIntersecting) {
          en.target.classList.add("fm-seen");
          countUpIn(en.target);
          io.unobserve(en.target);
        }
      });
    }, { threshold: 0.08, rootMargin: "0px 0px -6% 0px" });

    const scan = () => document.querySelectorAll("[data-reveal]:not(.fm-seen)").forEach((el) => io.observe(el));
    scan();
    // safety: never leave anything hidden
    setTimeout(() => document.querySelectorAll("[data-reveal]:not(.fm-seen)").forEach((el) => { el.classList.add("fm-seen"); countUpIn(el); }), 1500);
    window.fmRescanReveal = scan;
    drawRings();
  }

  /* conic-ring draw-in: reset to 0 then animate to inline target via @property --p */
  function drawRings() {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    document.querySelectorAll(".fm-ring, .learn-ring").forEach((r) => {
      const target = (r.getAttribute("style") || "").match(/--p:\s*([\d.]+)/);
      if (!target) return;
      r.style.setProperty("--p", "0");
      requestAnimationFrame(() => requestAnimationFrame(() => { r.style.setProperty("--p", target[1]); }));
    });
  }

  /* ---- Number count-up ---- (Danish number format aware) */
  function parseNum(txt) {
    // returns {prefix, value, decimals, suffix} or null
    const m = txt.match(/^(\D*?)([\d.]+(?:,\d+)?)(\D*)$/);
    if (!m) return null;
    const raw = m[2];
    if (!/\d/.test(raw)) return null;
    const decimals = raw.includes(",") ? raw.split(",")[1].length : 0;
    const value = parseFloat(raw.replace(/\./g, "").replace(",", "."));
    if (!isFinite(value)) return null;
    return { prefix: m[1], value, decimals, suffix: m[3] };
  }
  function fmtNum(v, decimals) {
    return v.toLocaleString("da-DK", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
  }
  function countUpIn(scope) {
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const els = scope.querySelectorAll(".kpi-val, .kpi-tile .kv, .cv-stat .v, .an-kpi .val, .streak-card .v, .signal .val, .hr-kpi .val");
    els.forEach((el) => {
      if (el.dataset.counted) return;
      const p = parseNum(el.textContent.trim());
      if (!p || p.value === 0 || p.value > 100000) { el.dataset.counted = "1"; return; }
      el.dataset.counted = "1";
      if (reduce) return;
      el.classList.add("fm-counting");
      const dur = 1000, t0 = performance.now();
      const tick = (now) => {
        const k = Math.min(1, (now - t0) / dur);
        const e = 1 - Math.pow(1 - k, 3); // easeOutCubic
        el.textContent = p.prefix + fmtNum(p.value * e, p.decimals) + p.suffix;
        if (k < 1) requestAnimationFrame(tick);
        else { el.textContent = p.prefix + fmtNum(p.value, p.decimals) + p.suffix; el.classList.remove("fm-counting"); }
      };
      requestAnimationFrame(tick);
    });
  }
  function countAllNow() {
    document.querySelectorAll("[data-reveal]").forEach((el) => countUpIn(el));
    countUpIn(document.body);
  }

  function applyTheme(mode) {
    document.documentElement.setAttribute("data-theme", mode);
    document.querySelectorAll("[data-theme-toggle] i").forEach(i => {
      i.className = mode === "dark" ? "fa-solid fa-sun" : "fa-solid fa-moon";
    });
  }

  /* ---- Command palette (⌘K) ---- */
  function initCmdK() {
    // Navigation entries are read from the server-rendered sidebar, so the palette
    // always matches what this role may actually reach (no second, drifting list).
    const items = [];
    let groupLabel = "Naviger";
    document.querySelectorAll("#fmSide .fm-side-scroll > *").forEach(node => {
      if (node.classList.contains("fm-nav-label")) { groupLabel = node.textContent.trim(); return; }
      node.querySelectorAll("a.fm-nav-link").forEach(a => {
        const t = a.querySelector("span");
        const icon = a.querySelector("i");
        items.push({
          icon: icon ? (icon.className.split(" ").find(c => c.startsWith("fa-") && c !== "fa-solid") || "fa-circle") : "fa-circle",
          title: t ? t.textContent.trim() : a.textContent.trim(),
          sub: groupLabel, href: a.getAttribute("href"), group: "Naviger",
        });
      });
    });
    // Action hrefs use real Flask paths so they navigate correctly in the
    // server-rendered shell (the legacy *.html paths only ever applied to the
    // standalone prototype). Role-awareness mirrors the NAV entries above:
    // these learner/account actions are available to both roles.
    const actions = [
      { icon: "fa-comment-dots", title: "Spørg AI-assistenten", sub: "Kurser og karriere", href: "/chat", group: "Handlinger" },
      { icon: "fa-bullseye", title: "Opret udviklingsmål", sub: "Udviklingsmål", href: "/mine-maal", group: "Handlinger" },
      { icon: "fa-bell", title: "Mine notifikationer", sub: "Konto", href: "/notifications", group: "Handlinger" },
      { icon: "fa-graduation-cap", title: "Åbn kursuskataloget", sub: "Katalog", href: "/catalog", group: "Handlinger" },
      { icon: "fa-receipt", title: "Mine bestillinger", sub: "Min tidslinje", href: "/min-tidslinje", group: "Handlinger" },
      { icon: "fa-circle-half-stroke", title: "Skift tema (lys/mørk)", sub: "Udseende", act: "theme", group: "Handlinger" },
      { icon: "fa-circle-question", title: "Support & hjælp", sub: "Konto", href: "/support", group: "Handlinger" },
    ];
    const all = items.concat(actions);

    const back = document.createElement("dialog");
    back.setAttribute("aria-label", "Søg i arbejdsområdet");
    back.className = "cmdk-back"; back.id = "cmdkBack";
    back.innerHTML = `
      <div class="cmdk">
        <div class="cmdk-in"><i class="fa-solid fa-magnifying-glass"></i><input id="cmdkInput" type="text" role="combobox" aria-label="Søg sider og handlinger" aria-controls="cmdkList" aria-expanded="true" aria-autocomplete="list" placeholder="Søg sider, handlinger…" autocomplete="off" /><button class="fm-kbd" type="button" data-command-close aria-label="Luk søgning">Esc</button></div>
        <div class="cmdk-list" id="cmdkList" role="listbox" aria-label="Søgeresultater"></div>
        <div class="cmdk-foot"><span><span class="fm-kbd">↑</span><span class="fm-kbd">↓</span> Naviger</span><span><span class="fm-kbd">↵</span> Vælg</span><span><span class="fm-kbd">⌘K</span> Åbn</span></div>
      </div>`;
    document.body.appendChild(back);
    const input = back.querySelector("#cmdkInput");
    const list = back.querySelector("#cmdkList");
    let results = [], active = 0, searchSeq = 0, debounceT = null;
    const SEARCH_URL = "/api/search";

    function staticMatches(ql) {
      return all.filter(it => !ql || it.title.toLowerCase().includes(ql) || it.sub.toLowerCase().includes(ql));
    }
    function drawList(q) {
      list.replaceChildren();
      input.removeAttribute("aria-activedescendant");
      if (!results.length) {
        const empty = document.createElement("div");
        empty.className = "cmdk-empty";
        empty.setAttribute("role", "status");
        empty.textContent = "Ingen resultater for “" + q + "”";
        list.append(empty);
        return;
      }
      let previousGroup;
      results.forEach((result, index) => {
        if (result.group !== previousGroup) {
          const heading = document.createElement("div");
          heading.className = "cmdk-group";
          heading.textContent = result.group;
          list.append(heading);
          previousGroup = result.group;
        }
        const option = document.createElement("div");
        option.className = "cmdk-item";
        option.id = "cmdk-option-" + index;
        option.dataset.i = index;
        option.setAttribute("role", "option");
        const icon = document.createElement("i");
        icon.className = "fa-solid " + result.icon;
        const title = document.createElement("span");
        title.className = "cmdk-t";
        title.textContent = result.title;
        const detail = document.createElement("span");
        detail.className = "cmdk-s";
        detail.textContent = result.sub;
        option.append(icon, title, detail);
        option.addEventListener("mouseenter", () => { active = index; paintActive(); });
        option.addEventListener("click", () => choose(index));
        list.append(option);
      });
      paintActive();
    }

    function render(q) {
      const ql = q.trim().toLowerCase();
      results = staticMatches(ql);
      active = 0;
      drawList(q);
      // Live entity search (employees / companies / users) via the backend,
      // debounced; live hits are shown first, static nav/actions after.
      if (debounceT) clearTimeout(debounceT);
      const seq = ++searchSeq;
      if (ql.length >= 2) {
        debounceT = setTimeout(() => {
          fetch(SEARCH_URL + "?q=" + encodeURIComponent(q.trim()), { headers: { "Accept": "application/json" } })
            .then(r => r.ok ? r.json() : null)
            .then(data => {
              if (!data || seq !== searchSeq) return;
              const live = [];
              (data.groups || []).forEach(g => (g.items || []).forEach(it =>
                live.push({ icon: it.icon || "fa-circle", title: it.title, sub: it.sub || "", href: it.href, group: g.label })));
              if (!live.length) return;
              results = live.concat(staticMatches(ql));
              active = 0;
              drawList(q);
            }).catch(() => {});
        }, 180);
      }
    }
    function paintActive() {
      list.querySelectorAll(".cmdk-item").forEach(el => {
        const selected = +el.dataset.i === active;
        el.classList.toggle("active", selected);
        el.setAttribute("aria-selected", String(selected));
      });
      const el = list.querySelector(".cmdk-item.active");
      if (el) { input.setAttribute("aria-activedescendant", el.id); el.scrollIntoView({ block: "nearest" }); }
    }
    function choose(i) {
      const r = results[i]; if (!r) return;
      close();
      if (r.act === "theme") { document.querySelector("[data-theme-toggle]")?.click(); return; }
      if (r.href) window.location.href = r.href;
    }
    let returnFocus;
    function open(event) {
      returnFocus = event?.currentTarget || document.activeElement;
      back.showModal();
      input.value = "";
      render("");
      input.focus();
    }
    back.addEventListener("close", () => returnFocus?.focus());
    function close() { back.close(); }
    back.querySelector("[data-command-close]").addEventListener("click", close);
    document.querySelector("[data-command-open]")?.addEventListener("click", open);
    window.fmCmdK = open;

    input.addEventListener("input", () => render(input.value));
    input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") { e.preventDefault(); active = Math.min(results.length - 1, active + 1); paintActive(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); active = Math.max(0, active - 1); paintActive(); }
      else if (e.key === "Enter") { e.preventDefault(); choose(active); }
    });
    back.addEventListener("click", (e) => { if (e.target === back) close(); });
    document.addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); back.open ? close() : open(); }
    });
    // ONLY the global topbar search opens the palette; in-page filters stay functional
    document.querySelectorAll(".fm-top .fm-search input").forEach(inp => {
      inp.readOnly = true; inp.style.cursor = "pointer";
      inp.closest(".fm-search")?.addEventListener("click", open);
    });
    document.querySelectorAll(".fm-top .fm-search").forEach(s => {
      if (!s.querySelector(".fm-kbd")) { const k = document.createElement("span"); k.className = "fm-kbd"; k.textContent = "⌘K"; s.appendChild(k); }
    });
  }

  /* ---- Tooltips from aria-label on icon-only controls ---- */
  function initTooltips() {
    let tip;
    const sel = ".fm-icon-btn[aria-label], .fm-theme[aria-label], .fm-side-toggle[aria-label]";
    function show(el) {
      const label = el.getAttribute("aria-label"); if (!label) return;
      tip = tip || (() => { const t = document.createElement("div"); t.className = "fm-tip"; document.body.appendChild(t); return t; })();
      tip.textContent = label;
      const r = el.getBoundingClientRect();
      tip.style.left = Math.round(r.left + r.width / 2) + "px";
      tip.style.top = Math.round(r.bottom + 8) + "px";
      tip.style.transform = "translateX(-50%)";
      requestAnimationFrame(() => tip.classList.add("show"));
    }
    function hide() { if (tip) tip.classList.remove("show"); }
    document.addEventListener("mouseover", (e) => { const el = e.target.closest(sel); if (el) show(el); });
    document.addEventListener("mouseout", (e) => { if (e.target.closest(sel)) hide(); });
    document.addEventListener("click", (e) => { if (e.target.closest(sel)) hide(); });
  }

  window.fmApplyTheme = applyTheme;
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
