/* Futurematch: question sheet for the AI assistant.

   When the assistant needs several answers at once, a numbered list in the chat forces the
   person to type "1: ... 2: ..." themselves. The sheet gives each question its own field
   (or tappable answers) and turns the filled-in sheet into ONE well-formed chat message.

   Two ways in:
   - the `ask_user_questions` tool -> `ui_card` event with ui_type "questions" -> render()
   - fallback: the model wrote a list of questions as prose anyway -> fromList() reads the
     rendered list and renders the same sheet under it.

   Pure helpers (compose, parseList) are exposed for tests; the DOM part only needs a `send`
   callback (chat.js passes its own `ask`). No dependencies. */
(function (root) {
  "use strict";

  var MIN_LIST_QUESTIONS = 3;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  /* The chat message for a filled-in sheet: "Label: svar" per answered question, then which
     ones were left blank, so the model can tell "skipped" from "forgotten". */
  function compose(fields, answers) {
    var done = [];
    var skipped = [];
    (fields || []).forEach(function (f, i) {
      var a = String((answers && answers[i]) || "").trim();
      var label = f.label || "Spørgsmål " + (i + 1);
      if (a) done.push(label + ": " + a);
      else skipped.push(label);
    });
    if (!done.length) return "";
    if (skipped.length) done.push("Springer over: " + skipped.join(", ") + ".");
    return done.join("\n");
  }

  /* Questions out of a rendered answer: a list (ol/ul) with at least MIN_LIST_QUESTIONS items
     that are questions. Label = the bold lead ("Kompetencer:"), question = the rest. */
  function parseList(el) {
    if (!el || !el.querySelectorAll) return null;
    var lists = el.querySelectorAll("ol, ul");
    for (var i = lists.length - 1; i >= 0; i--) {
      var items = Array.prototype.filter.call(lists[i].children, function (n) { return n.tagName === "LI"; });
      var qs = items.filter(function (li) { return (li.textContent || "").indexOf("?") !== -1; });
      if (qs.length < MIN_LIST_QUESTIONS || qs.length < items.length * 0.75) continue;
      var fields = qs.map(function (li, n) {
        var strong = li.querySelector("strong, b");
        var label = strong ? strong.textContent.replace(/[:：\s]+$/, "").trim() : "";
        var text = (li.textContent || "").replace(/\s+/g, " ").trim();
        var question = text;
        if (label && text.toLowerCase().indexOf(label.toLowerCase()) === 0) {
          question = text.slice(label.length).replace(/^[\s:：\-–—]+/, "");
        }
        return {
          name: "q" + (n + 1),
          label: (label || "Spørgsmål " + (n + 1)).slice(0, 40),
          question: question.slice(0, 240),
        };
      });
      return {
        message: "Svar herunder, så sender jeg dem samlet. Du kan springe over, hvad du vil.",
        fields: fields,
      };
    }
    return null;
  }

  function render(body, spec, opts) {
    opts = opts || {};
    var fields = (spec.fields || []).filter(function (f) { return f && (f.question || f.label); });
    if (!fields.length) return null;
    var send = typeof opts.send === "function" ? opts.send : function () {};
    var uid = "qs" + Math.random().toString(36).slice(2, 8);

    var card = document.createElement("section");
    card.className = "pcard ui qsheet";
    card.setAttribute("aria-label", "Spørgsmål til dig");
    var items = fields.map(function (f, i) {
      var id = uid + "-" + i;
      var choices = (f.choices || []).filter(Boolean).slice(0, 6);
      return '<li class="qs-item" data-i="' + i + '">' +
        '<label class="qs-q" for="' + id + '">' +
          (f.label ? '<b>' + esc(f.label) + '</b> ' : "") + (f.question ? esc(f.question) : "") +
        "</label>" +
        (choices.length
          ? '<div class="qs-choices" role="group" aria-label="Forslag til svar">' +
            choices.map(function (c) { return '<button type="button" class="qs-choice" aria-pressed="false">' + esc(c) + "</button>"; }).join("") +
            "</div>"
          : "") +
        '<textarea id="' + id + '" rows="1" placeholder="' + esc(f.placeholder || "Skriv dit svar…") + '"></textarea>' +
      "</li>";
    }).join("");
    card.innerHTML =
      '<div class="qs-msg">' + esc(spec.message || "Et par ting, jeg gerne vil vide:") + "</div>" +
      '<ol class="qs-list">' + items + "</ol>" +
      '<div class="qs-actions">' +
        '<span class="qs-count" aria-live="polite">0 af ' + fields.length + " besvaret</span>" +
        '<button type="button" class="qs-skip">Spring over</button>' +
        '<button type="button" class="qs-send" disabled>Send svar</button>' +
      "</div>";

    var areas = Array.prototype.slice.call(card.querySelectorAll("textarea"));
    var sendBtn = card.querySelector(".qs-send");
    var count = card.querySelector(".qs-count");
    var locked = false;

    function values() { return areas.map(function (t) { return t.value; }); }
    function refresh() {
      var n = values().filter(function (v) { return v.trim(); }).length;
      count.textContent = n + " af " + fields.length + " besvaret";
      sendBtn.disabled = locked || n === 0;
    }
    function grow(t) { t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 140) + "px"; }
    function lock(label) {
      locked = true;
      card.classList.add("is-sent");
      areas.forEach(function (t) { t.disabled = true; });
      card.querySelectorAll("button").forEach(function (b) { b.disabled = true; });
      var note = document.createElement("div");
      note.className = "qs-sent";
      note.textContent = label;
      card.querySelector(".qs-actions").replaceWith(note);
    }
    function submit() {
      if (locked) return;
      var text = compose(fields, values());
      if (!text) return;
      lock("Svar sendt ✓");
      send(text);
    }

    areas.forEach(function (t, i) {
      t.addEventListener("input", function () {
        grow(t);
        // typing over a suggested answer un-selects it
        card.querySelectorAll('.qs-item[data-i="' + i + '"] .qs-choice').forEach(function (b) {
          var on = b.textContent === t.value;
          b.classList.toggle("on", on);
          b.setAttribute("aria-pressed", on ? "true" : "false");
        });
        refresh();
      });
      t.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submit(); }
      });
    });
    card.querySelectorAll(".qs-choice").forEach(function (b) {
      b.addEventListener("click", function () {
        var li = b.closest(".qs-item");
        var t = li.querySelector("textarea");
        var was = b.classList.contains("on");
        li.querySelectorAll(".qs-choice").forEach(function (x) { x.classList.remove("on"); x.setAttribute("aria-pressed", "false"); });
        if (was) { t.value = ""; } else { b.classList.add("on"); b.setAttribute("aria-pressed", "true"); t.value = b.textContent; }
        grow(t);
        refresh();
      });
    });
    sendBtn.addEventListener("click", submit);
    card.querySelector(".qs-skip").addEventListener("click", function () {
      if (locked) return;
      lock("Sprunget over");
      send("Lad os springe de spørgsmål over lige nu.");
    });

    body.appendChild(card);
    if (typeof opts.after === "function") opts.after(card);
    return card;
  }

  /* Fallback: read the answer the model wrote and put a sheet under it. */
  function fromList(body, textEl, opts) {
    var spec = parseList(textEl);
    return spec ? render(body, spec, opts) : null;
  }

  root.FmQuestionSheet = { compose: compose, parseList: parseList, render: render, fromList: fromList };
})(typeof window !== "undefined" ? window : this);
