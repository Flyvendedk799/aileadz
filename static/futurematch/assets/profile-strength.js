/* Shared profile-strength component (N-5.1).
   One place that turns the server's completeness payload into a NEED-DRIVEN
   message: what the AI can do for the user now, and one soft next step. Used by
   the AI Profiler banner and the profile page, so the two can never drift and
   neither shows "x/8 felter", "Mangler: ..." or a "complete me" checklist.
   The percentage stays as context (the ring), not as a goal. */
(function () {
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function pct(c) {
    if (!c) return 0;
    return c.weighted_pct != null ? c.weighted_pct : (c.pct || 0);
  }
  function html(c) {
    var un = (c && c.unlocked) || [];
    var items = un.slice(0, 4).map(function (t) {
      return '<li><i class="fa-solid fa-check"></i> ' + esc(t) + "</li>";
    }).join("");
    var next = c && c.next_help ? '<div class="ps-next">' + esc(c.next_help) + "</div>" : "";
    return '<div class="ps-title">Det kan jeg hjælpe dig med nu</div><ul class="ps-list">' + items + "</ul>" + next;
  }
  function paint(opts, c) {
    if (opts.ring) opts.ring.style.setProperty("--p", pct(c));
    if (opts.pct) opts.pct.textContent = pct(c) + "%";
    if (opts.body) opts.body.innerHTML = html(c);
  }
  window.fmProfileStrength = { html: html, paint: paint, pct: pct };
})();
