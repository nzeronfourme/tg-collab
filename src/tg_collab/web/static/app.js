// Small progressive-enhancement helpers; no external dependencies.
(function () {
  "use strict";

  function copyText(elementId) {
    var element = document.getElementById(elementId);
    if (!element) {
      return;
    }
    var text = typeof element.value === "string" ? element.value : element.textContent || "";
    var button = document.querySelector('[data-copy-target="' + elementId + '"]');

    function done() {
      if (!button) {
        return;
      }
      var original = button.textContent;
      button.textContent = "OK";
      window.setTimeout(function () {
        button.textContent = original;
      }, 1200);
    }

    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () {
        fallbackCopy(text, done);
      });
      return;
    }
    fallbackCopy(text, done);
  }

  function fallbackCopy(text, done) {
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "readonly");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    try {
      document.execCommand("copy");
      done();
    } finally {
      document.body.removeChild(area);
    }
  }

  window.copyText = copyText;

  document.addEventListener("click", function (event) {
    var target = event.target.closest("[data-copy-target]");
    if (target) {
      copyText(target.getAttribute("data-copy-target"));
    }
  });

  document.addEventListener("submit", function (event) {
    var form = event.target;
    var message = form && form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
    }
  });
})();
