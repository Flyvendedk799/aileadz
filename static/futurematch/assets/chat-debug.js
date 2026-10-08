/* The displayed ID is always the server's session_id, never the sidebar row id. */
(function () {
  "use strict";
  const ids = Object.create(null);
  function setId(id, scope) {
    scope = scope || "employee";
    ids[scope] = typeof id === "string" && /^[A-Za-z0-9_-]{1,255}$/.test(id) ? id : "";
    document.querySelectorAll('[data-chat-id-copy]').forEach(button => {
      if ((button.dataset.chatScope || "employee") !== scope) return;
      clearTimeout(button._copyTimer);
      const label = button.querySelector('[data-chat-id-label]');
      if (label) label.textContent = "Kopiér chat-id";
      button.disabled = !ids[scope];
      button.title = ids[scope] ? "Chat-id: " + ids[scope] : "Chat-id bliver tilgængeligt, når samtalen starter";
    });
  }
  async function copyId(button) {
    const scope = button.dataset.chatScope || "employee", id = ids[scope];
    if (!id) return;
    let copied = false;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(id); copied = true;
      }
    } catch (_) { /* HTTP or clipboard permission denied: try a selectable field. */ }
    if (!copied) {
      const field = document.createElement("textarea");
      field.value = id; field.readOnly = true;
      field.style.position = "fixed"; field.style.opacity = "0";
      document.body.appendChild(field); field.select();
      try { copied = document.execCommand("copy"); } catch (_) { /* manual fallback below */ }
      field.remove(); button.focus();
      if (!copied) window.prompt("Kopiér chat-id og send det sammen med din fejlbeskrivelse:", id);
    }
    if (copied && ids[scope] === id) {
      const label = button.querySelector('[data-chat-id-label]');
      if (label) {
        label.textContent = "Kopieret";
        clearTimeout(button._copyTimer);
        button._copyTimer = setTimeout(() => { label.textContent = "Kopiér chat-id"; }, 2000);
      }
    }
  }
  document.addEventListener("click", event => {
    const button = event.target.closest('[data-chat-id-copy]');
    if (button) copyId(button);
  });
  window.FMChatDebug = { setId: setId, copyId: copyId };
})();
