/* Shared assistant UI kit (N-5.3 / N-5.4 / N-5.6).
 *
 * The HR panel, the HR full page, the vendor assistant and the embeddable widget
 * all talk to an SSE endpoint that speaks the same event vocabulary as the employee
 * chat (text/chunk, suggestions, confirm_card, ui_action, tool_call, meta, error,
 * done). This file renders that vocabulary once so none of them drops an event.
 *
 *   FMAI.md(text)                      sanitised markdown -> HTML (suggestion tag removed)
 *   FMAI.ask(url, body, handlers, o)   POST + robust SSE parsing across chunk boundaries
 *   FMAI.confirmCard(el, data, o)      Bekræft / Afvis card -> POST o.confirmUrl {token}
 *   FMAI.chips(el, items, onPick)      suggestion chips
 *   FMAI.action(el, data)              ui_action -> same-origin link button
 *   FMAI.feedback(el, ctx)             thumbs up / down -> POST o.feedbackUrl
 */
(function () {
  "use strict";

  var esc = function (s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  };

  /* ---- sanitising (model output is untrusted) ---- */
  var FORBIDDEN = { script: 1, style: 1, iframe: 1, object: 1, embed: 1, link: 1, meta: 1, base: 1,
    form: 1, noscript: 1, template: 1, frame: 1, frameset: 1, applet: 1 };
  var URL_ATTRS = ["href", "src", "xlink:href", "action", "formaction", "background", "poster"];
  var SAFE_URL = /^(?:https?:|mailto:|tel:|\/(?!\/)|#|\.)/i;

  function sanitize(html) {
    var str = String(html == null ? "" : html);
    if (!str) return "";
    if (window.DOMPurify && typeof window.DOMPurify.sanitize === "function") {
      try { return window.DOMPurify.sanitize(str, { FORBID_TAGS: ["style"] }); } catch (e) { /* fall through */ }
    }
    try {
      var tpl = document.createElement("template");
      tpl.innerHTML = str;
      var all = tpl.content.querySelectorAll("*");
      for (var i = all.length - 1; i >= 0; i--) {
        var el = all[i];
        if (FORBIDDEN[(el.tagName || "").toLowerCase()]) { el.remove(); continue; }
        Array.prototype.slice.call(el.attributes || []).forEach(function (a) {
          var name = (a.name || "").toLowerCase();
          if (name.indexOf("on") === 0 || name === "style" || name === "srcdoc") { el.removeAttribute(a.name); return; }
          if (URL_ATTRS.indexOf(name) !== -1) {
            var v = (a.value || "").replace(/[\x00-\x1f\x7f\s]+/g, "");
            if (v && !SAFE_URL.test(v)) el.removeAttribute(a.name);
          }
        });
      }
      Array.prototype.forEach.call(tpl.content.querySelectorAll("a[href]"), function (a) {
        a.setAttribute("rel", "noopener noreferrer");
        if (/^https?:/i.test(a.getAttribute("href") || "")) a.setAttribute("target", "_blank");
      });
      return tpl.innerHTML;
    } catch (e) {
      return esc(str);
    }
  }

  function stripSuggestions(text) {
    // Complete tag, or a half-streamed one at the end.
    return String(text || "").replace(/\s*<suggestions>[\s\S]*?(?:<\/suggestions>|$)\s*/g, "");
  }

  function md(text) {
    var t = stripSuggestions(text);
    if (!t) return "";
    if (window.marked && typeof window.marked.parse === "function") {
      try { return sanitize(window.marked.parse(t)); } catch (e) { /* fall through */ }
    }
    return sanitize(esc(t)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/^[-*] (.+)$/gm, "<li>$1</li>")
      .replace(/(<li>[\s\S]*?<\/li>)(?!\s*<li>)/g, "<ul>$1</ul>")
      .replace(/\n/g, "<br>"));
  }

  /* ---- streaming ---- */
  // handlers: { event(d) } called for every parsed event, plus per-type callbacks
  // (text, suggestions, confirm_card, ui_action, tool_call, meta, error, done).
  function ask(url, body, handlers, opts) {
    handlers = handlers || {};
    opts = opts || {};
    var headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    return fetch(url, {
      method: "POST", headers: headers, body: JSON.stringify(body || {}),
      credentials: opts.credentials || "same-origin", signal: opts.signal,
    }).then(function (resp) {
      var ct = resp.headers.get("content-type") || "";
      if (!resp.ok || ct.indexOf("text/event-stream") === -1) {
        // Guard responses (credits paused, forbidden, empty) are JSON, not a stream.
        return resp.json().catch(function () { return {}; }).then(function (j) {
          var msg = j.error || (j.answers && j.answers[0] && j.answers[0].content) || "Der opstod en fejl. Prøv igen.";
          if (handlers.error) handlers.error({ type: "error", content: msg, status: resp.status });
          if (handlers.done) handlers.done({ type: "done", failed: true });
        });
      }
      var reader = resp.body.getReader(), dec = new TextDecoder(), buf = "", finished = false;
      function dispatch(line) {
        if (line.indexOf("data: ") !== 0) return;
        var raw = line.substring(6).trim();
        if (raw === "[DONE]") { if (!finished) { finished = true; if (handlers.done) handlers.done({ type: "done" }); } return; }
        var d;
        try { d = JSON.parse(raw); } catch (e) { return; }
        if (handlers.event) handlers.event(d);
        var type = d.type === "chunk" ? "text" : d.type;
        if (type === "done") { if (!finished) { finished = true; if (handlers.done) handlers.done(d); } return; }
        if (handlers[type]) handlers[type](d);
      }
      function pump() {
        return reader.read().then(function (r) {
          if (r.done) {
            if (buf) dispatch(buf);
            if (!finished && handlers.done) { finished = true; handlers.done({ type: "done" }); }
            return;
          }
          buf += dec.decode(r.value, { stream: true });
          var lines = buf.split("\n");
          buf = lines.pop();
          lines.forEach(dispatch);
          return pump();
        });
      }
      return pump();
    }).catch(function (err) {
      if (err && err.name === "AbortError") { if (handlers.done) handlers.done({ type: "done", aborted: true }); return; }
      if (handlers.error) handlers.error({ type: "error", content: "Netværksfejl – prøv igen." });
      if (handlers.done) handlers.done({ type: "done", failed: true });
    });
  }

  /* ---- confirm card ---- */
  function confirmCard(container, data, opts) {
    opts = opts || {};
    var url = opts.confirmUrl || "/app1/confirm_tool_action";
    var card = document.createElement("div");
    card.className = "fm-confirm-card";
    var meta = [];
    if (data.recipient_count != null) meta.push(data.recipient_count + " modtagere");
    if (data.price != null) meta.push(Number(data.price).toLocaleString("da-DK") + " kr.");
    card.innerHTML =
      '<div class="fm-confirm-head"><i class="fa-solid fa-triangle-exclamation"></i> Bekræft handling</div>' +
      '<div class="fm-confirm-body">' + esc(data.summary_da || "") + "</div>" +
      (data.details ? '<div class="fm-confirm-details">' + esc(typeof data.details === "string" ? data.details : JSON.stringify(data.details)) + "</div>" : "") +
      (meta.length ? '<div class="fm-confirm-meta">' + esc(meta.join(" · ")) + "</div>" : "") +
      '<div class="fm-confirm-actions"><button type="button" class="fm-confirm-ok">Bekræft</button>' +
      '<button type="button" class="fm-confirm-cancel">Afvis</button></div>';
    var ok = card.querySelector(".fm-confirm-ok"), no = card.querySelector(".fm-confirm-cancel");
    function result(cls, msg) {
      ok.disabled = true; no.disabled = true;
      var r = document.createElement("div");
      r.className = "fm-confirm-result " + cls; r.textContent = msg;
      card.appendChild(r);
      if (opts.onResult) opts.onResult(cls, msg);
    }
    ok.addEventListener("click", function () {
      ok.disabled = true; no.disabled = true; ok.textContent = "Bekræfter…";
      fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
        body: JSON.stringify({ token: data.token }) })
        .then(function (r) { return r.json().catch(function () { return {}; }); })
        .then(function (j) {
          if (j.status === "success" || j.status === "already_confirmed" || j.success === true) {
            result("ok", j.message_da || j.message || "Bekræftet.");
          } else {
            result("err", j.message_da || j.message || "Handlingen kunne ikke gennemføres.");
          }
        })
        .catch(function () { result("err", "Netværksfejl – prøv igen."); });
    });
    no.addEventListener("click", function () { result("err", "Afvist – der er ikke ændret noget."); });
    container.appendChild(card);
    return card;
  }

  /* ---- chips, actions, feedback ---- */
  function chips(container, items, onPick) {
    var old = container.querySelector(".fm-chips");
    if (old) old.remove();
    if (!items || !items.length) return null;
    var wrap = document.createElement("div");
    wrap.className = "fm-chips";
    items.forEach(function (label) {
      var b = document.createElement("button");
      b.type = "button"; b.className = "fm-chip"; b.textContent = label;
      b.addEventListener("click", function () { wrap.remove(); onPick(label); });
      wrap.appendChild(b);
    });
    container.appendChild(wrap);
    return wrap;
  }

  function action(container, d) {
    var target = String(d.target || "");
    if (!/^\/[^\/]/.test(target)) return null;     // same-origin absolute paths only
    var a = document.createElement("a");
    a.className = "fm-action-link";
    a.href = target;
    if (d.new_tab) { a.target = "_blank"; a.rel = "noopener"; }
    a.textContent = d.label || "Åbn";
    container.appendChild(a);
    return a;
  }

  function feedback(container, ctx, opts) {
    opts = opts || {};
    var url = opts.feedbackUrl || "/app1/feedback";
    var row = document.createElement("div");
    row.className = "fm-feedback";
    row.innerHTML = '<button type="button" class="up" title="Godt svar" aria-label="Godt svar"><i class="fa-regular fa-thumbs-up"></i></button>' +
      '<button type="button" class="down" title="Dårligt svar" aria-label="Dårligt svar"><i class="fa-regular fa-thumbs-down"></i></button>';
    var current = 0;
    function send(rating) {
      var next = current === rating ? 0 : rating;       // clicking again clears it
      current = next;
      row.querySelector(".up").classList.toggle("on", next === 1);
      row.querySelector(".down").classList.toggle("on", next === -1);
      try {
        fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
          body: JSON.stringify(Object.assign({ rating: next }, ctx || {})) }).catch(function () {});
      } catch (e) { /* telemetry only */ }
    }
    row.querySelector(".up").addEventListener("click", function () { send(1); });
    row.querySelector(".down").addEventListener("click", function () { send(-1); });
    container.appendChild(row);
    return row;
  }

  window.FMAI = { esc: esc, sanitize: sanitize, md: md, stripSuggestions: stripSuggestions, ask: ask,
    confirmCard: confirmCard, chips: chips, action: action, feedback: feedback };
})();
