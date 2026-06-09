/* Mind-Map — visualises everything the AI has stored about the user as a
   root -> category -> fact graph (cytoscape). The "normal" precursor to the
   planned 3D view. Reads /api/profile/mindmap; lets the user add/remove the
   free-form memories. Colours resolve from the live --fm-* theme so it follows
   light/dark. */
(function () {
  "use strict";
  if (typeof cytoscape === "undefined") {
    console.warn("cytoscape not loaded");
    return;
  }

  var root = document.documentElement;
  function cssVar(name, fallback) {
    var v = getComputedStyle(root).getPropertyValue(name).trim();
    return v || fallback || "#888888";
  }

  // Branch/category -> colour. Resolved once (re-resolved on reload to catch theme flips).
  function palette() {
    return {
      root: cssVar("--fm-primary", "#0c6b62"),
      om: cssVar("--fm-indigo", "#5b63d3"),
      kompetencer: cssVar("--fm-primary", "#0c6b62"),
      erfaring: cssVar("--fm-clay", "#c9603a"),
      uddannelse: cssVar("--fm-gold", "#c8973a"),
      certificeringer: cssVar("--fm-success", "#1f9d6b"),
      sprog: cssVar("--fm-indigo", "#5b63d3"),
      maal: cssVar("--fm-clay", "#c9603a"),
      hukommelse: "#8b5cf6",
      samtale: cssVar("--fm-ink-3", "#8a8a8a")
    };
  }

  var SOURCE_LABEL = { ai: "AI lærte det", profiler: "AI Profiler", user: "Du tilføjede det", cv: "Fra dit CV", profil: "Din profil", samtale: "Samtale" };
  var CAT_DOT;
  var cy = null;

  function esc(s) {
    var d = document.createElement("div");
    d.appendChild(document.createTextNode(s == null ? "" : String(s)));
    return d.innerHTML;
  }

  function fmtDate(iso) {
    if (!iso) return "—";
    try {
      var d = new Date(iso);
      if (isNaN(d.getTime())) return String(iso).slice(0, 10);
      return d.toLocaleDateString("da-DK", { day: "numeric", month: "short", year: "numeric" });
    } catch (e) { return String(iso).slice(0, 10); }
  }

  function api(url, opts) {
    return fetch(url, Object.assign({ headers: { "Content-Type": "application/json" }, credentials: "same-origin" }, opts || {})).then(function (r) { return r.json(); });
  }

  function setStats(data) {
    var c = data.completeness || {};
    var counts = data.counts || {};
    document.getElementById("mmPct").textContent = (c.pct != null ? c.pct + "%" : "—");
    document.getElementById("mmNodes").textContent = (counts.leaves != null ? counts.leaves : "—");
    document.getElementById("mmMem").textContent = (counts.memories != null ? counts.memories : "—");
  }

  function buildLegend(nodes) {
    var seen = {};
    nodes.forEach(function (n) { if (n.type === "branch") seen[n.category] = n.label; });
    var pal = CAT_DOT;
    var html = Object.keys(seen).map(function (cat) {
      return '<span class="mm-leg"><i style="background:' + (pal[cat] || pal.root) + '"></i>' + esc(seen[cat]) + "</span>";
    }).join("");
    document.getElementById("mmLegend").innerHTML = html;
  }

  function showPanel(d) {
    var panel = document.getElementById("mmPanel");
    if (!d || d.type === "root") {
      panel.innerHTML = '<div class="ph">Klik på en node for at se hvad der er gemt — og hvornår AI’en sidst brugte det.</div>';
      return;
    }
    var color = CAT_DOT[d.category] || CAT_DOT.root;
    var meta = d.meta || {};
    var kind = meta.kind || (d.type === "branch" ? "Kategori" : "Data");
    var rows = "";
    function row(k, v) { if (v == null || v === "") return; rows += '<div class="mm-prow"><span class="k">' + esc(k) + '</span><span class="val">' + esc(v) + "</span></div>"; }

    row("Kilde", SOURCE_LABEL[meta.source] || meta.source);
    if (meta.level) row("Niveau", meta.level);
    if (meta.expiry) row("Udløber", meta.expiry);
    if (meta.status) row("Status", meta.status);
    if (meta.confidence != null) row("Sikkerhed", Math.round(meta.confidence * 100) + "%");
    if (meta.created_at) row("Tilføjet", fmtDate(meta.created_at));

    var usedBlock = "";
    if (meta.used_count != null) {
      usedBlock = '<div class="mm-used">🧠 Brugt i samtaler <b>' + (meta.used_count || 0) + "</b> gang" + (meta.used_count === 1 ? "" : "e") +
        (meta.last_used_at ? " · sidst " + fmtDate(meta.last_used_at) : " · endnu ikke brugt") + "</div>";
    }
    var delBtn = (meta.memory_id ? '<button class="mm-pdel" data-del="' + meta.memory_id + '">Slet denne hukommelse</button>' : "");

    panel.innerHTML =
      '<span class="mm-pk"><span class="mm-pdot" style="background:' + color + '"></span>' + esc(kind) + "</span>" +
      '<div class="mm-ptitle">' + esc(d.label) + "</div>" +
      (meta.detail ? '<div class="mm-pdetail">' + esc(meta.detail) + "</div>" : "") +
      usedBlock + rows + delBtn;

    var del = panel.querySelector("[data-del]");
    if (del) del.addEventListener("click", function () {
      if (!confirm("Slet denne hukommelse?")) return;
      api("/api/profile/memories", { method: "DELETE", body: JSON.stringify({ id: Number(del.getAttribute("data-del")) }) })
        .then(function () { loadGraph(); })
        .catch(function () { });
    });
  }

  function loadGraph() {
    CAT_DOT = palette();
    return api("/api/profile/mindmap").then(function (data) {
      setStats(data);
      var nodes = data.nodes || [];
      var edges = data.edges || [];
      buildLegend(nodes);

      var leafCount = nodes.filter(function (n) { return n.type === "leaf"; }).length;
      document.getElementById("mmEmpty").hidden = leafCount > 0;

      var elements = [];
      nodes.forEach(function (n) {
        var meta = n.meta || {};
        var used = meta.used_count || 0;
        elements.push({ data: { id: n.id, label: n.label, type: n.type, category: n.category, color: CAT_DOT[n.category] || CAT_DOT.root, meta: meta, used: used } });
      });
      edges.forEach(function (e) { elements.push({ data: { source: e.source, target: e.target } }); });

      if (cy) { try { cy.destroy(); } catch (e) {} cy = null; }

      cy = cytoscape({
        container: document.getElementById("cy"),
        elements: elements,
        wheelSensitivity: 0.2,
        style: [
          { selector: "node", style: {
            "label": "data(label)", "color": cssVar("--fm-ink", "#222"), "font-size": "10px",
            "font-family": "inherit", "text-wrap": "wrap", "text-max-width": "92px",
            "text-valign": "bottom", "text-margin-y": 4, "background-color": cssVar("--fm-surface", "#fff"),
            "border-width": 2, "border-color": "data(color)", "width": 20, "height": 20,
            "transition-property": "border-width, width, height", "transition-duration": "0.15s"
          }},
          { selector: 'node[type="root"]', style: {
            "width": 66, "height": 66, "background-color": CAT_DOT.root, "border-color": CAT_DOT.root,
            "color": "#ffffff", "font-size": "13px", "font-weight": "bold", "text-valign": "center", "text-margin-y": 0
          }},
          { selector: 'node[type="branch"]', style: {
            "width": 40, "height": 40, "background-color": "data(color)", "border-color": "data(color)",
            "font-size": "11px", "font-weight": "bold"
          }},
          // Memory leaves grow subtly with how often they've been used.
          { selector: 'node[type="leaf"][category="hukommelse"]', style: {
            "width": "mapData(used, 0, 10, 20, 40)", "height": "mapData(used, 0, 10, 20, 40)"
          }},
          { selector: "edge", style: {
            "width": 1.4, "line-color": cssVar("--fm-line-2", "#ddd"), "curve-style": "bezier", "opacity": 0.65
          }},
          { selector: "node:selected", style: { "border-width": 4, "border-color": "data(color)" }},
          { selector: "node.dim", style: { "opacity": 0.25 }},
          { selector: "edge.dim", style: { "opacity": 0.1 }}
        ],
        layout: {
          name: "cose", animate: true, animationDuration: 600, padding: 40,
          nodeRepulsion: 9000, idealEdgeLength: 90, gravity: 0.3, nestingFactor: 0.9
        }
      });

      cy.on("tap", "node", function (evt) {
        var n = evt.target;
        showPanel(n.data());
        cy.elements().addClass("dim");
        n.removeClass("dim");
        n.neighborhood().removeClass("dim");
        n.connectedEdges().removeClass("dim");
      });
      cy.on("tap", function (evt) {
        if (evt.target === cy) { cy.elements().removeClass("dim"); showPanel(null); }
      });
      return data;
    }).catch(function (e) {
      console.warn("mindmap load failed", e);
      document.getElementById("mmEmpty").hidden = false;
    });
  }

  // ── Add-memory form ──
  function wireAddForm() {
    var addBtn = document.getElementById("mmAddBtn");
    var form = document.getElementById("mmAdd");
    var save = document.getElementById("mmSave");
    var cancel = document.getElementById("mmCancel");
    if (addBtn) addBtn.addEventListener("click", function () {
      form.classList.toggle("show");
      if (form.classList.contains("show")) document.getElementById("mmLabel").focus();
    });
    if (cancel) cancel.addEventListener("click", function () { form.classList.remove("show"); });
    if (save) save.addEventListener("click", function () {
      var label = (document.getElementById("mmLabel").value || "").trim();
      var category = document.getElementById("mmCat").value;
      if (!label) { document.getElementById("mmLabel").focus(); return; }
      api("/api/profile/memories", { method: "POST", body: JSON.stringify({ label: label, category: category, source: "user" }) })
        .then(function (d) {
          if (d && d.success !== false) {
            document.getElementById("mmLabel").value = "";
            form.classList.remove("show");
            loadGraph();
          }
        }).catch(function () { });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    wireAddForm();
    loadGraph();
  });
})();
