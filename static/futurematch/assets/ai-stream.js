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
  async function ask(url, body, handlers, opts) {
    handlers = handlers || {};
    opts = opts || {};
    var controller = new AbortController(), reader, idleTimer, totalTimer;
    var finished = false, timedOut = false, hasAnswer = false;
    function complete(data) {
      if (finished) return;
      if (!hasAnswer && !(data && (data.failed || data.aborted))) {
        fail("Jeg fik ikke et svar denne gang. Prøv at sende din besked igen.");
        return;
      }
      finished = true;
      if (handlers.done) handlers.done(Object.assign({ type: "done" }, data || {}));
    }
    function fail(content, status) {
      if (finished) return;
      try {
        if (handlers.error) handlers.error({ type: "error", content: content, status: status });
      } finally { complete({ failed: true }); }
    }
    function timeout() { timedOut = true; controller.abort(); }
    function armIdle() {
      clearTimeout(idleTimer);
      idleTimer = setTimeout(timeout, opts.idleTimeoutMs || 90000);
    }
    function abort() { controller.abort(); }
    function dispatch(raw) {
      if (finished) return;
      if (raw.trim() === "[DONE]") { complete(); return; }
      var d = JSON.parse(raw);
      if (!d || typeof d !== "object" || typeof d.type !== "string") {
        throw new Error("Invalid assistant event");
      }
      if (d.chat_id && window.FMChatDebug) window.FMChatDebug.setId(d.chat_id, d.scope || "hr");
      if (handlers.event) handlers.event(d);
      var type = d.type === "chunk" ? "text" : d.type;
      if (type === "done") { complete(d); return; }
      if (type === "error") { fail(d.content || "Svaret kunne ikke færdiggøres. Prøv igen."); return; }
      if ((type === "text" && typeof d.content === "string" && d.content.trim()) ||
          ((type === "confirm_card" || type === "ui_action") && handlers[type])) hasAnswer = true;
      if (handlers[type]) handlers[type](d);
    }
    // Parse complete SSE frames, including multiline data and CRLF split across reads.
    var pending = "", dataLines = [];
    function line(value) {
      if (!value) {
        if (dataLines.length) dispatch(dataLines.join("\n"));
        dataLines = [];
      } else if (value === "data" || value.indexOf("data:") === 0) {
        dataLines.push(value === "data" ? "" : value.slice(5).replace(/^ /, ""));
      }
    }
    function consume(text, eof) {
      pending += text;
      var match;
      while (!finished && (match = /\r\n|\r|\n/.exec(pending))) {
        if (!eof && match[0] === "\r" && match.index === pending.length - 1) break;
        line(pending.slice(0, match.index));
        pending = pending.slice(match.index + match[0].length);
      }
      if (eof && !finished) {
        if (pending) line(pending);
        line("");
      }
    }
    try {
      if (opts.signal) {
        opts.signal.addEventListener("abort", abort, { once: true });
        if (opts.signal.aborted) abort();
      }
      armIdle();
      // Heartbeats keep an idle connection alive, but cannot keep the composer busy forever.
      totalTimer = setTimeout(timeout, opts.maxDurationMs || 180000);
      var headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
      var resp = await fetch(url, {
        method: "POST", headers: headers, body: JSON.stringify(body || {}),
        credentials: opts.credentials || "same-origin", signal: controller.signal,
      });
      if (window.FMChatDebug && resp.headers.get("X-Chat-ID")) {
        window.FMChatDebug.setId(resp.headers.get("X-Chat-ID"), resp.headers.get("X-Chat-Scope") || "hr");
      }
      var ct = resp.headers.get("content-type") || "";
      if (!resp.ok || ct.indexOf("text/event-stream") === -1) {
        var j = await resp.json().catch(function () { return {}; });
        if (controller.signal.aborted) throw new Error("Aborted assistant response");
        var msg = resp.status === 401 || resp.redirected ? "Din session er udløbet. Log ind igen for at fortsætte."
          : resp.status === 429 ? "Du sender beskeder lidt for hurtigt. Vent et øjeblik og prøv igen."
          : "Svaret kunne ikke hentes. Prøv igen om lidt.";
        if (j && typeof j.error === "string") msg = j.error;
        else if (j && j.answers && j.answers[0] && typeof j.answers[0].content === "string") msg = j.answers[0].content;
        fail(msg, resp.status);
        return;
      }
      if (!resp.body) throw new Error("Missing assistant stream");
      reader = resp.body.getReader();
      var dec = new TextDecoder();
      while (!finished) {
        var r = await reader.read();
        if (r.done) {
          consume(dec.decode(), true);
          if (!finished) fail("Forbindelsen blev afbrudt, før svaret var færdigt. Det modtagne svar er bevaret.");
          break;
        }
        armIdle();
        consume(dec.decode(r.value, { stream: true }), false);
      }
    } catch (err) {
      if (finished) return;
      if (controller.signal.aborted && !timedOut) { complete({ aborted: true }); return; }
      fail(timedOut
        ? "Svaret tog for lang tid. Det modtagne svar er bevaret. Prøv igen om lidt."
        : "Forbindelsen blev afbrudt. Det modtagne svar er bevaret. Prøv igen om lidt.");
    } finally {
      clearTimeout(idleTimer); clearTimeout(totalTimer);
      if (opts.signal) opts.signal.removeEventListener("abort", abort);
      if (reader) {
        // Do not wait for a server to close a response after its terminal event.
        reader.cancel().catch(function () {});
        reader.releaseLock();
      }
    }
  }

  /* ---- confirm card ---- */
  var OK_STATUSES = ["success", "already_confirmed", "order_created", "team_orders_created", "handed_off_to_hr"];
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
    function result(cls, msg, html) {
      // The decision is made: the result replaces the buttons, so none is left
      // disabled and still reading "Bekræfter…".
      card.querySelector(".fm-confirm-actions").hidden = true;
      var r = document.createElement("div");
      r.className = "fm-confirm-result " + cls;
      if (html) r.innerHTML = md(msg); else r.textContent = msg;
      card.appendChild(r);
      if (opts.onResult) opts.onResult(cls, msg);
    }
    ok.addEventListener("click", function () {
      ok.disabled = true; no.disabled = true; ok.textContent = "Bekræfter…";
      fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
        body: JSON.stringify({ token: data.token }) })
        .then(function (r) { return r.json().catch(function () { return {}; }); })
        .then(function (j) {
          if (OK_STATUSES.indexOf(j.status) >= 0 || j.success === true) {
            if (j.confirmation_text) result("ok", j.confirmation_text, true);
            else result("ok", j.message_da || j.message || "Bekræftet.");
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
      // A busy composer may decline a click; leave its suggestions available.
      b.addEventListener("click", function () { if (onPick(label) !== false) wrap.remove(); });
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
