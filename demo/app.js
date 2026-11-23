// Static demo: renders recorded traces from demo/data.js. DOM is built with textContent only.
(function () {
  "use strict";

  var data = window.LEDGER_DEMO;
  var METRIC_LABELS = {
    tool_selection_accuracy: "Tool selection accuracy",
    argument_accuracy: "Argument accuracy",
    argument_validation: "Argument validation",
    data_retrieval_accuracy: "Data retrieval accuracy",
    authorization_compliance: "Authorization compliance",
    unsupported_claims: "Unsupported claims",
    response_grounding: "Response grounding"
  };
  var CATEGORY_LABELS = {
    lookup: "Lookups", not_found: "Nonexistent records", list: "Lists and filters", overdue: "Overdue",
    aggregation: "Aggregations", authorization: "Authorization", question_injection: "Injection in the question",
    record_injection: "Injection in record text", unsupported: "Unsupported operations",
    insufficient_evidence: "Insufficient evidence", identity: "Identity", argument_validation: "Argument validation",
    pagination: "Pagination"
  };

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function percent(rate) {
    return rate === null || rate === undefined ? "n/a" : (rate * 100).toFixed(1) + "%";
  }

  function renderMetrics() {
    var box = document.getElementById("metrics");
    document.getElementById("metrics-meta").textContent =
      "(" + data.cases_passed + " of " + data.case_count + " cases passed, " + data.scope_leaks + " scope leaks)";
    document.getElementById("grader-badge").textContent = "Graded by " + data.grader;
    document.getElementById("mode-badge").textContent = data.planner;
    Object.keys(METRIC_LABELS).forEach(function (key) {
      var m = data.metrics[key];
      if (!m) return;
      var card = el("div", "metric");
      card.title = m.description;
      card.appendChild(el("div", "name", METRIC_LABELS[key]));
      if (key === "unsupported_claims") {
        card.appendChild(el("div", "value", String(m.flagged)));
        card.appendChild(el("div", "count", "flagged of " + m.total + " answers (lower is better)"));
      } else {
        card.appendChild(el("div", "value", percent(m.rate)));
        card.appendChild(el("div", "count", m.passed + " / " + m.total));
      }
      box.appendChild(card);
    });
  }

  function firstQuestion(c) {
    var step = c.steps[0];
    if (step.question) return step.question;
    return "Direct call: " + step.plan.tool;
  }

  function renderList(category) {
    var list = document.getElementById("case-list");
    list.textContent = "";
    data.cases.forEach(function (c, index) {
      if (category && c.category !== category) return;
      var item = el("li");
      var button = el("button");
      button.type = "button";
      button.dataset.index = String(index);
      button.appendChild(el("span", "case-id", c.id));
      button.appendChild(el("span", c.passed ? "pill pill-pass" : "pill pill-fail", c.passed ? "pass" : "fail"));
      button.appendChild(el("span", "case-q", firstQuestion(c)));
      button.addEventListener("click", function () { select(index); });
      item.appendChild(button);
      list.appendChild(item);
    });
  }

  function jsonBlock(title, value, open) {
    var details = el("details");
    if (open) details.open = true;
    details.appendChild(el("summary", null, title));
    details.appendChild(el("pre", "json", JSON.stringify(value, null, 2)));
    return details;
  }

  function renderStep(step) {
    var box = el("section", "step");
    if (step.question) {
      box.appendChild(el("p", "question", "“" + step.question + "”"));
    } else {
      box.appendChild(el("p", "question", "Direct tool call (bypasses the planner)"));
    }

    var grid = el("div", "grid2");
    var planPanel = el("div", "panel");
    planPanel.appendChild(el("h3", null, "Planner decision"));
    if (step.plan.kind === "tool_call") {
      planPanel.appendChild(el("p", null, "Tool: " + step.plan.tool));
      planPanel.appendChild(el("pre", "json", JSON.stringify(step.plan.arguments, null, 2)));
    } else {
      planPanel.appendChild(el("p", null, "No tool call. Refusal: " + step.plan.reason));
    }
    if (step.plan.rationale) planPanel.appendChild(el("p", "muted", step.plan.rationale));
    grid.appendChild(planPanel);

    var resultPanel = el("div", "panel");
    resultPanel.appendChild(el("h3", null, "Tool result"));
    if (!step.outcome) {
      resultPanel.appendChild(el("p", "muted", "No tool was called."));
    } else if (step.outcome.ok) {
      resultPanel.appendChild(el("p", "status-ok", "ok (structured content)"));
      resultPanel.appendChild(jsonBlock("Show result JSON", step.outcome.data, false));
    } else {
      resultPanel.appendChild(el("p", "status-err", step.outcome.error_code));
      resultPanel.appendChild(el("p", null, step.outcome.message));
    }
    grid.appendChild(resultPanel);
    box.appendChild(grid);

    if (step.evidence && step.evidence.length) {
      var ev = el("div", "section-gap");
      ev.appendChild(el("h3", null, "Evidence"));
      var chips = el("div", "chips");
      step.evidence.forEach(function (id) { chips.appendChild(el("span", "chip", id)); });
      ev.appendChild(chips);
      box.appendChild(ev);
    }

    if (step.answer !== null) {
      var ans = el("div", "panel section-gap");
      ans.appendChild(el("h3", null, "Final answer (composed deterministically from the tool result)"));
      ans.appendChild(el("p", "answer", step.answer));
      (step.notes || []).forEach(function (note) { ans.appendChild(el("p", "note", note)); });
      box.appendChild(ans);
    }

    var evalBox = el("div", "section-gap");
    evalBox.appendChild(el("h3", null, "Evaluation checks"));
    var checks = el("ul", "checks");
    Object.keys(METRIC_LABELS).forEach(function (key) {
      var value = step.checks[key];
      if (value === null || value === undefined) return;
      var passed = value === true;
      var label = key === "unsupported_claims" ? "No unsupported claims" : METRIC_LABELS[key];
      checks.appendChild(el("li", passed ? "pass" : "fail", label));
    });
    evalBox.appendChild(checks);
    if (step.findings && step.findings.length) {
      var findings = el("ul", "findings");
      step.findings.forEach(function (f) { findings.appendChild(el("li", null, f)); });
      evalBox.appendChild(findings);
    }
    box.appendChild(evalBox);
    return box;
  }

  function select(index) {
    var c = data.cases[index];
    var detail = document.getElementById("detail");
    detail.textContent = "";
    var head = el("div", "detail-head");
    head.appendChild(el("strong", null, c.id));
    head.appendChild(el("span", "pill", CATEGORY_LABELS[c.category] || c.category));
    head.appendChild(el("span", "pill", "User: " + c.principal));
    head.appendChild(el("span", c.passed ? "pill pill-pass" : "pill pill-fail", c.passed ? "case passed" : "case failed"));
    detail.appendChild(head);
    c.steps.forEach(function (step) { detail.appendChild(renderStep(step)); });
    var list = document.getElementById("case-list");
    document.querySelectorAll("#case-list button").forEach(function (b) {
      var current = b.dataset.index === String(index);
      b.setAttribute("aria-current", current ? "true" : "false");
      if (current && (b.offsetTop < list.offsetTop + list.scrollTop || b.offsetTop > list.offsetTop + list.scrollTop + list.clientHeight - b.offsetHeight)) {
        list.scrollTop = b.offsetTop - list.offsetTop - 8;  // keep the selection visible without scrolling the page
      }
    });
    if (window.history && window.history.replaceState) window.history.replaceState(null, "", "#" + c.id);
  }

  function indexFromHash() {
    var id = window.location.hash.replace("#", "");
    for (var i = 0; i < data.cases.length; i++) if (data.cases[i].id === id) return i;
    return 0;
  }

  function init() {
    if (!data) {
      document.getElementById("detail").textContent = "demo/data.js is missing. Run `ledger-assistant build-demo`.";
      return;
    }
    renderMetrics();
    var filter = document.getElementById("category-filter");
    var all = el("option", null, "All (" + data.cases.length + ")");
    all.value = "";
    filter.appendChild(all);
    var seen = [];
    data.cases.forEach(function (c) { if (seen.indexOf(c.category) < 0) seen.push(c.category); });
    seen.forEach(function (cat) {
      var option = el("option", null, CATEGORY_LABELS[cat] || cat);
      option.value = cat;
      filter.appendChild(option);
    });
    filter.addEventListener("change", function () {
      renderList(filter.value);
      var first = document.querySelector("#case-list button");
      if (first) select(Number(first.dataset.index));
    });
    renderList("");
    select(indexFromHash());
  }

  init();
})();
