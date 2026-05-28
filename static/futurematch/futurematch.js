(function () {
  "use strict";

  const root = document.documentElement;
  const body = document.body;
  const storedTheme = localStorage.getItem("futurematch-theme") || localStorage.getItem("mode");
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function applyTheme(mode) {
    const normalized = mode === "light" ? "light" : "dark";
    root.dataset.theme = normalized;
    body.classList.toggle("light-mode", normalized === "light");
    body.classList.toggle("dark-mode", normalized === "dark");
    document.querySelectorAll("#mode-toggle, .fm-theme-toggle").forEach((toggle) => {
      toggle.classList.toggle("active", normalized === "light");
      toggle.setAttribute("aria-pressed", normalized === "light" ? "true" : "false");
    });
  }

  if (storedTheme) {
    applyTheme(storedTheme === "light" ? "light" : "dark");
  } else {
    applyTheme("light");
  }

  function toggleTheme() {
    const next = root.dataset.theme === "light" ? "dark" : "light";
    applyTheme(next);
    localStorage.setItem("futurematch-theme", next);
    localStorage.setItem("mode", next);
  }

  window.futurematchToggleTheme = toggleTheme;

  const FuturematchMotion = {
    initStagger() {
      if (reducedMotion) return;
      document.querySelectorAll("[data-fm-stagger]").forEach(function (container) {
        container.querySelectorAll("[data-fm-animate]").forEach(function (node, index) {
          node.style.setProperty("--fm-stagger-index", String(Math.min(index, 8)));
        });
      });
      document.querySelectorAll("[data-fm-animate]:not([data-fm-stagger] [data-fm-animate])").forEach(function (node, index) {
        if (!node.closest("[data-fm-stagger]")) {
          node.style.setProperty("--fm-stagger-index", String(Math.min(index, 6)));
        }
      });
    },

    initTabTransitions() {
      document.querySelectorAll('.tab-pane, [role="tabpanel"]').forEach(function (pane) {
        pane.addEventListener("shown.bs.tab", function () {
          if (reducedMotion) return;
          pane.classList.remove("fm-tab-pane-enter");
          void pane.offsetWidth;
          pane.classList.add("fm-tab-pane-enter");
        });
      });
      document.querySelectorAll('[data-bs-toggle="tab"]').forEach(function (trigger) {
        trigger.addEventListener("shown.bs.tab", function (event) {
          if (reducedMotion) return;
          const target = document.querySelector(event.target.getAttribute("data-bs-target"));
          if (!target) return;
          target.classList.remove("fm-tab-pane-enter");
          void target.offsetWidth;
          target.classList.add("fm-tab-pane-enter");
        });
      });
    },

    initSidebar() {
      const sidebar = document.getElementById("sidebar");
      if (!sidebar || reducedMotion) return;
      sidebar.classList.add("fm-sidebar-animated");
    },

    animateCountUp(node) {
      const target = Number(node.dataset.count || "0");
      if (!Number.isFinite(target) || reducedMotion) {
        node.textContent = target.toLocaleString("da-DK");
        return;
      }
      const duration = 650;
      const start = performance.now();
      function step(now) {
        const progress = Math.min((now - start) / duration, 1);
        const eased = 1 - Math.pow(1 - progress, 3);
        node.textContent = Math.round(target * eased).toLocaleString("da-DK");
        if (progress < 1) requestAnimationFrame(step);
      }
      requestAnimationFrame(step);
    }
  };

  window.FuturematchMotion = FuturematchMotion;

  function ready(fn) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", fn);
    } else {
      fn();
    }
  }

  ready(function () {
    FuturematchMotion.initStagger();
    FuturematchMotion.initTabTransitions();
    FuturematchMotion.initSidebar();

    const sidebar = document.getElementById("sidebar");
    const sidebarToggle = document.getElementById("sidebar-toggle");
    const modeToggle = document.getElementById("mode-toggle");

    if (sidebar && localStorage.getItem("futurematch-sidebar") === "expanded") {
      sidebar.classList.remove("collapsed");
    }

    if (sidebar && sidebarToggle) {
      sidebarToggle.addEventListener("click", function () {
        sidebar.classList.toggle("collapsed");
        localStorage.setItem("futurematch-sidebar", sidebar.classList.contains("collapsed") ? "collapsed" : "expanded");
      });
    }

    if (modeToggle) {
      modeToggle.addEventListener("click", toggleTheme);
    }

    document.querySelectorAll("[data-nav-link]").forEach(function (link) {
      const linkPath = new URL(link.getAttribute("href"), window.location.origin).pathname.replace(/\/+$/, "") || "/";
      const currentPath = window.location.pathname.replace(/\/+$/, "") || "/";
      if (currentPath === linkPath || (linkPath !== "/" && currentPath.startsWith(linkPath + "/"))) {
        link.classList.add("active");
        link.setAttribute("aria-current", "page");
      } else {
        link.classList.remove("active");
        link.removeAttribute("aria-current");
      }
    });

    const creditsDisplay = document.getElementById("credits-display");
    if (creditsDisplay && localStorage.getItem("credits")) {
      creditsDisplay.innerHTML = '<i class="fa-solid fa-coins"></i> ' + localStorage.getItem("credits");
    }

    function updateCredits() {
      if (!creditsDisplay) return;
      fetch("/api/credits", { credentials: "same-origin" })
        .then((response) => response.ok ? response.json() : Promise.reject(response))
        .then((data) => {
          creditsDisplay.innerHTML = '<i class="fa-solid fa-coins"></i> ' + data.credits;
          localStorage.setItem("credits", data.credits);
        })
        .catch(() => {});
    }

    updateCredits();
    if (creditsDisplay) setInterval(updateCredits, 15000);

    const badge = document.getElementById("notif-badge");
    const headerNotif = document.getElementById("header-notifications");
    const notifDropdown = document.getElementById("notif-dropdown");
    const notifList = document.getElementById("notif-list");

    function updateNotifBadge() {
      if (!badge) return;
      fetch("/api/notifications/unread_count", { credentials: "same-origin" })
        .then((response) => response.ok ? response.json() : Promise.reject(response))
        .then((data) => {
          badge.textContent = data.unread_count || 0;
          if (!reducedMotion) {
            badge.animate([{ transform: "scale(1)" }, { transform: "scale(1.12)" }, { transform: "scale(1)" }], {
              duration: 220,
              easing: "ease-out"
            });
          }
        })
        .catch(() => {});
    }

    function updateDropdown() {
      if (!notifList) return;
      fetch("/api/notifications/unread_list", { credentials: "same-origin" })
        .then((response) => response.ok ? response.json() : Promise.reject(response))
        .then((data) => {
          notifList.innerHTML = "";
          if (data.notifications && data.notifications.length) {
            data.notifications.forEach((notif) => {
              const item = document.createElement("div");
              item.className = "dropdown-item";
              item.id = "dropdown-notif-" + notif.id;
              const imageHTML = notif.image_url ? `<img src="${notif.image_url}" class="dropdown-image" alt="">` : "";
              item.innerHTML = `
                ${imageHTML}
                <a href="/notifications#notif-${notif.id}" class="notif-link">
                  <strong>${notif.title}</strong><br>
                  <span style="font-size:0.8rem;color:var(--fm-text-muted);">${notif.timestamp || ""}</span>
                </a>
                <button type="button" onclick="markDropdownRead(${notif.id})">Læs</button>
              `;
              notifList.appendChild(item);
            });
          } else {
            notifList.innerHTML = '<div class="dropdown-item" style="justify-content:center;">Ingen nye notifikationer</div>';
          }
        })
        .catch(() => {
          notifList.innerHTML = '<div class="dropdown-item" style="justify-content:center;">Notifikationer kunne ikke hentes</div>';
        });
    }

    if (badge) {
      updateNotifBadge();
      setInterval(updateNotifBadge, 15000);
    }

    if (headerNotif && notifDropdown) {
      headerNotif.addEventListener("click", function (event) {
        event.stopPropagation();
        notifDropdown.classList.toggle("active");
        if (notifDropdown.classList.contains("active")) updateDropdown();
      });
      document.addEventListener("click", function (event) {
        if (!notifDropdown.contains(event.target) && !headerNotif.contains(event.target)) {
          notifDropdown.classList.remove("active");
        }
      });
    }

    window.markDropdownRead = function (notifId) {
      fetch("/api/notifications/" + notifId + "/mark_read", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" }
      })
        .then((response) => response.ok ? response.json() : Promise.reject(response))
        .then((data) => {
          if (!data.success) return;
          const item = document.getElementById("dropdown-notif-" + notifId);
          if (item) item.remove();
          updateNotifBadge();
        })
        .catch(() => {});
    };

    const markAllBtn = document.getElementById("mark-all-btn");
    if (markAllBtn) {
      markAllBtn.addEventListener("click", function () {
        fetch("/api/mark-notifications-read", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mark_all: true })
        })
          .then((response) => response.ok ? response.json() : Promise.reject(response))
          .then((data) => {
            if (!data.success) return;
            if (notifDropdown) notifDropdown.classList.remove("active");
            updateNotifBadge();
          })
          .catch(() => {});
      });
    }

    const dashboardSearch = document.querySelector("[data-dashboard-search]");
    if (dashboardSearch) {
      dashboardSearch.addEventListener("input", function () {
        const filter = dashboardSearch.value.toLowerCase().trim();
        document.querySelectorAll("[data-search-card]").forEach(function (card) {
          const matches = card.textContent.toLowerCase().includes(filter);
          card.style.display = matches ? "" : "none";
        });
      });
    }

    const backToTop = document.getElementById("back-to-top");
    if (backToTop) {
      window.addEventListener("scroll", function () {
        backToTop.classList.toggle("visible", window.scrollY > 320);
      });
      backToTop.addEventListener("click", function () {
        window.scrollTo({ top: 0, behavior: reducedMotion ? "auto" : "smooth" });
      });
    }

    document.querySelectorAll(".fm-stat-value[data-count]").forEach(function (node) {
      FuturematchMotion.animateCountUp(node);
    });
  });
})();
