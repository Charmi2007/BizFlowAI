// BizFlow AI – sidebar toggle for mobile (drawer under 768px).
(function () {
  "use strict";

  var MOBILE_QUERY = "(max-width: 767px)";

  function init() {
    var sidebar = document.getElementById("sidebar");
    var toggle = document.getElementById("menu-toggle");
    var overlay = document.getElementById("sidebar-overlay");
    if (!sidebar || !toggle) {
      return; // No sidebar on this page (logged-out layout).
    }

    function setOpen(open) {
      sidebar.classList.toggle("open", open);
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      if (overlay) {
        overlay.hidden = !open;
      }
    }

    function isMobile() {
      return window.matchMedia(MOBILE_QUERY).matches;
    }

    toggle.addEventListener("click", function () {
      setOpen(!sidebar.classList.contains("open"));
    });

    if (overlay) {
      overlay.addEventListener("click", function () {
        setOpen(false);
      });
    }

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && sidebar.classList.contains("open")) {
        setOpen(false);
      }
    });

    // Reset drawer state when resizing to desktop so the sidebar is always visible there.
    window.matchMedia(MOBILE_QUERY).addEventListener("change", function (event) {
      if (!event.matches) {
        setOpen(false);
      }
    });

    // Close the drawer after navigating via a sidebar link on mobile.
    sidebar.querySelectorAll("a").forEach(function (link) {
      link.addEventListener("click", function () {
        if (isMobile()) {
          setOpen(false);
        }
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

// BizFlow AI – invoice form: add/remove line items and show a live total preview.
// The server recomputes every total on save; this is display only.
(function () {
  "use strict";

  var MAX_ITEMS = 10;

  function init() {
    var body = document.getElementById("item-rows");
    var template = document.getElementById("item-row-template");
    var addButton = document.getElementById("add-item");
    var totalEl = document.getElementById("invoice-total");
    if (!body || !template || !addButton) {
      return; // Not on the invoice form.
    }

    function rows() {
      return body.querySelectorAll(".item-row");
    }

    function formatRupees(paise) {
      return "\u20b9" + (paise / 100).toLocaleString("en-IN", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2
      });
    }

    function rowAmountPaise(row) {
      var qty = parseInt(row.querySelector(".item-qty").value, 10);
      var price = parseFloat(row.querySelector(".item-price").value);
      if (!(qty >= 1) || !(price >= 0)) {
        return 0;
      }
      return qty * Math.round(price * 100);
    }

    function refresh() {
      var total = 0;
      rows().forEach(function (row) {
        var amount = rowAmountPaise(row);
        row.querySelector(".item-amount").textContent = formatRupees(amount);
        total += amount;
      });
      if (totalEl) {
        totalEl.textContent = formatRupees(total);
      }
      addButton.disabled = rows().length >= MAX_ITEMS;
    }

    addButton.addEventListener("click", function () {
      if (rows().length >= MAX_ITEMS) {
        return;
      }
      body.appendChild(template.content.cloneNode(true));
      refresh();
    });

    body.addEventListener("click", function (event) {
      var button = event.target.closest ? event.target.closest(".item-remove") : null;
      if (!button) {
        return;
      }
      var row = button.closest(".item-row");
      if (rows().length > 1) {
        row.remove();
      } else {
        // Keep at least one row: clear it instead of removing it.
        row.querySelector(".item-desc").value = "";
        row.querySelector(".item-qty").value = "1";
        row.querySelector(".item-price").value = "";
      }
      refresh();
    });

    body.addEventListener("input", refresh);
    refresh();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

// BizFlow AI – payment follow-up: copy a reminder message to the clipboard.
(function () {
  "use strict";

  var COPIED_MS = 2000;

  function fallbackCopy(textarea) {
    textarea.focus();
    textarea.select();
    try {
      return document.execCommand("copy");
    } catch (err) {
      return false;
    }
  }

  function init() {
    var buttons = document.querySelectorAll(".js-copy-reminder");
    if (!buttons.length) {
      return; // Not on the follow-up page.
    }

    buttons.forEach(function (button) {
      button.addEventListener("click", function () {
        var target = document.getElementById(button.getAttribute("data-target"));
        if (!target) {
          return;
        }
        if (!button.dataset.label) {
          button.dataset.label = button.textContent;
        }

        function showCopied() {
          button.textContent = "Copied!";
          window.setTimeout(function () {
            button.textContent = button.dataset.label;
          }, COPIED_MS);
        }

        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(target.value).then(showCopied, function () {
            if (fallbackCopy(target)) {
              showCopied();
            }
          });
        } else if (fallbackCopy(target)) {
          showCopied();
        }
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

