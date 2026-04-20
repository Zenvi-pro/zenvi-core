/* global window, document */
(function () {
  "use strict";

  function boostSearchUX() {
    var searchInput =
      document.querySelector("input[name='q']") ||
      document.querySelector(".wy-side-nav-search input");
    if (!searchInput) {
      return;
    }

    searchInput.setAttribute(
      "placeholder",
      "Search docs, workflows, and UI reference"
    );
    searchInput.setAttribute("aria-label", "Search Zenvi documentation");

    var form =
      searchInput.closest("form") || document.querySelector("form[action*='search']");
    if (form) {
      form.setAttribute("role", "search");
    }
  }

  function decorateTables() {
    var tables = document.querySelectorAll(".wy-table-responsive table, table.docutils");
    tables.forEach(function (table) {
      table.classList.add("zenvi-table");
    });
  }

  function tagAdmonitions() {
    var map = {
      note: "Note",
      tip: "Tip",
      warning: "Warning",
      important: "Important",
      caution: "Caution",
      danger: "Danger"
    };

    Object.keys(map).forEach(function (klass) {
      var blocks = document.querySelectorAll(".admonition." + klass);
      blocks.forEach(function (block) {
        var title = block.querySelector(".admonition-title");
        if (title && !title.textContent.trim()) {
          title.textContent = map[klass];
        }
        block.classList.add("zenvi-callout");
      });
    });
  }

  function normalizeThemeHeadingRoles() {
    var captions = document.querySelectorAll("p.caption[role='heading']");
    captions.forEach(function (caption) {
      // RTD theme outputs role=heading without aria-level in some blocks.
      // Convert to plain paragraph semantics to avoid invalid ARIA state.
      caption.removeAttribute("role");
    });
  }

  function run() {
    boostSearchUX();
    decorateTables();
    tagAdmonitions();
    normalizeThemeHeadingRoles();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run);
  } else {
    run();
  }
})();
