/* CSRF helper (S-2.1). Loaded on every HTML page by the server-side injector.
 *  - adds X-CSRFToken to same-origin fetch()/XMLHttpRequest writes
 *  - adds a hidden csrf_token input to POST forms (also ones built later)
 * The token lives in <meta name="csrf-token">. Never logs or exposes it. */
(function () {
  "use strict";
  var SAFE = /^(GET|HEAD|OPTIONS|TRACE)$/i;

  function token() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute("content") : "";
  }

  function sameOrigin(url) {
    try {
      return new URL(url, window.location.href).origin === window.location.origin;
    } catch (e) { return false; }
  }

  if (window.fetch && !window.fetch.__csrfPatched) {
    var origFetch = window.fetch;
    var patched = function (input, init) {
      try {
        var url = typeof input === "string" ? input : (input && input.url) || "";
        var method = (init && init.method) || (input && input.method) || "GET";
        if (!SAFE.test(method) && sameOrigin(url)) {
          init = init || {};
          var headers = new Headers(init.headers || (input && input.headers) || {});
          if (!headers.has("X-CSRFToken")) headers.set("X-CSRFToken", token());
          init.headers = headers;
        }
      } catch (e) { /* never block a request over this */ }
      return origFetch.call(this, input, init);
    };
    patched.__csrfPatched = true;
    window.fetch = patched;
  }

  if (window.XMLHttpRequest && !XMLHttpRequest.prototype.__csrfPatched) {
    var origOpen = XMLHttpRequest.prototype.open;
    var origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (method, url) {
      this.__csrf = { m: method, u: url };
      return origOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function () {
      try {
        if (this.__csrf && !SAFE.test(this.__csrf.m) && sameOrigin(this.__csrf.u)) {
          this.setRequestHeader("X-CSRFToken", token());
        }
      } catch (e) { /* ignore */ }
      return origSend.apply(this, arguments);
    };
    XMLHttpRequest.prototype.__csrfPatched = true;
  }

  function decorateForms(root) {
    var t = token();
    if (!t) return;
    var forms = (root || document).querySelectorAll('form[method="post" i], form[method="POST"]');
    for (var i = 0; i < forms.length; i++) {
      var f = forms[i];
      if (f.querySelector('input[name="csrf_token"]')) continue;
      var action = f.getAttribute("action");
      if (action && /^https?:\/\//i.test(action) && !sameOrigin(action)) continue;
      var inp = document.createElement("input");
      inp.type = "hidden";
      inp.name = "csrf_token";
      inp.value = t;
      f.appendChild(inp);
    }
  }

  function start() {
    decorateForms(document);
    if (window.MutationObserver) {
      new MutationObserver(function () { decorateForms(document); })
        .observe(document.documentElement, { childList: true, subtree: true });
    }
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
