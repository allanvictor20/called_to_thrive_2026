/* Explicit save states for workbook answers (FR17, D06, TC12).
 *
 * - Never shows "Saved" until the server acknowledges.
 * - On a network failure the typed input stays on the page and a Retry button appears.
 * - Validation errors are shown next to the field that needs attention.
 * - A stale revision (409) is reported instead of silently overwriting.
 * Without JavaScript the same form posts normally and the server re-renders it.
 */
(function () {
  "use strict";

  function csrfToken() {
    var m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
    if (m) return decodeURIComponent(m[1]);
    var input = document.querySelector("input[name=csrfmiddlewaretoken]");
    return input ? input.value : "";
  }

  function collect(form) {
    var payload = {};
    var node = document.getElementById(form.getAttribute("data-schema-id"));
    var schema = node ? JSON.parse(node.textContent) : { fields: [] };
    schema.fields.forEach(function (f) {
      var name = "f__" + f.key;
      if (f.type === "list") {
        var items = [];
        for (var i = 0; i < f.count; i++) {
          var el = form.elements[name + "__" + i];
          items.push(el ? el.value : "");
        }
        payload[f.key] = items;
      } else if (f.type === "multi_choice") {
        payload[f.key] = Array.prototype.filter.call(form.querySelectorAll("input[name='" + name + "']"), function (c) { return c.checked; }).map(function (c) { return c.value; });
      } else if (f.type === "rating" || f.type === "choice") {
        var checked = form.querySelector("input[name='" + name + "']:checked");
        payload[f.key] = checked ? checked.value : null;
      } else {
        var el2 = form.elements[name];
        payload[f.key] = el2 ? el2.value : "";
      }
    });
    return payload;
  }

  function clearErrors(form) {
    form.querySelectorAll(".js-error").forEach(function (n) { n.remove(); });
    form.querySelectorAll(".field.has-error").forEach(function (n) { n.classList.remove("has-error"); });
    form.querySelectorAll("[aria-invalid]").forEach(function (n) { n.removeAttribute("aria-invalid"); });
  }

  function showErrors(form, fields) {
    var first = null;
    Object.keys(fields || {}).forEach(function (key) {
      var wrap = form.querySelector("[data-field='" + key + "']");
      if (!wrap) return;
      wrap.classList.add("has-error");
      var p = document.createElement("p");
      p.className = "error-text js-error";
      p.id = "err-" + form.id + "-" + key;
      p.textContent = fields[key];
      wrap.appendChild(p);
      var input = wrap.querySelector("input, textarea, select");
      if (input) {
        input.setAttribute("aria-invalid", "true");
        input.setAttribute("aria-describedby", p.id);
        if (!first) first = input;
      }
    });
    if (first) first.focus();
  }

  function setState(form, state, text) {
    var el = form.querySelector(".save-state");
    if (!el) return;
    el.setAttribute("data-state", state);
    el.textContent = text;
    var retry = form.querySelector(".js-retry");
    if (retry) retry.hidden = state !== "error";
  }

  function send(form, kind) {
    var url = form.getAttribute(kind === "submit" ? "data-submit-url" : "data-save-url");
    var body = JSON.stringify({ payload: collect(form), expected_revision: parseInt(form.getAttribute("data-revision"), 10) || 0 });
    clearErrors(form);
    setState(form, "saving", kind === "submit" ? "Submitting…" : "Saving…");
    form.setAttribute("data-last", kind);
    return fetch(url, {
      method: kind === "submit" ? "POST" : "PUT",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "Accept": "application/json", "X-CSRFToken": csrfToken() },
      body: body
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) { return { res: res, data: data }; });
    }).then(function (r) {
      if (r.res.ok) {
        form.setAttribute("data-revision", r.data.revision);
        if (form.elements.expected_revision) form.elements.expected_revision.value = r.data.revision;
        form.removeAttribute("data-dirty");
        if (kind === "submit") {
          setState(form, "saved", "Submitted. Updating your progress…");
          window.location.reload();
        } else {
          var t = new Date();
          setState(form, "saved", "Draft saved at " + t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) + ".");
        }
        return;
      }
      var err = (r.data && r.data.error) || {};
      if (r.res.status === 422) {
        showErrors(form, err.fields);
        setState(form, "unsaved", err.message || "Some answers need attention.");
      } else if (r.res.status === 409) {
        setState(form, "error", (err.message || "This was changed elsewhere.") + " Copy anything you need, then reload the page.");
      } else if (r.res.status === 401 || r.res.status === 403) {
        setState(form, "error", "Your session has ended. Copy your answers, sign in again, and retry.");
      } else {
        setState(form, "error", "Not saved. " + (err.message || "Something went wrong on the server."));
      }
    }).catch(function () {
      setState(form, "error", "Not saved — check your connection. Your answers are still here.");
    });
  }

  document.querySelectorAll("form[data-save-url]").forEach(function (form) {
    form.addEventListener("input", function () {
      form.setAttribute("data-dirty", "1");
      setState(form, "unsaved", "Unsaved changes.");
    });
    form.addEventListener("change", function () {
      form.setAttribute("data-dirty", "1");
      setState(form, "unsaved", "Unsaved changes.");
    });
    form.addEventListener("submit", function (ev) {
      var action = ev.submitter && ev.submitter.value === "submit" ? "submit" : "save";
      if (!window.fetch) return; // fall back to a normal form post
      ev.preventDefault();
      send(form, action);
    });
    var retry = form.querySelector(".js-retry");
    if (retry) retry.addEventListener("click", function () { send(form, form.getAttribute("data-last") || "save"); });
  });

  window.addEventListener("beforeunload", function (ev) {
    if (document.querySelector("form[data-dirty]")) {
      ev.preventDefault();
      ev.returnValue = "";
    }
  });

  // Attendance review: live totals before saving.
  var att = document.querySelector("form[data-attendance]");
  if (att) {
    var update = function () {
      var counts = { present: 0, absent: 0, excused: 0, "": 0 };
      att.querySelectorAll("select[data-att]").forEach(function (s) { counts[s.value] = (counts[s.value] || 0) + 1; });
      ["present", "absent", "excused"].forEach(function (k) {
        var el = att.querySelector("[data-count='" + k + "']");
        if (el) el.textContent = counts[k];
      });
      var un = att.querySelector("[data-count='unrecorded']");
      if (un) un.textContent = counts[""];
    };
    att.addEventListener("change", update);
    var all = att.querySelector("[data-mark-all]");
    if (all) all.addEventListener("click", function () {
      att.querySelectorAll("select[data-att]").forEach(function (s) { if (!s.value) s.value = "present"; });
      update();
    });
    update();
  }
})();
