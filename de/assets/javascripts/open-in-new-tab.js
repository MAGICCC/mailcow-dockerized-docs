/* Opens external links in a new tab (replacement for mkdocs-open-in-new-tab). */
(function () {
  function update() {
    document.querySelectorAll(".md-content a[href]").forEach(function (a) {
      if (!/^https?:$/.test(a.protocol) || a.hostname === location.hostname) return;
      if (a.classList.contains("glightbox")) return;
      a.target = "_blank";
      a.rel = "noopener";
      if (!a.querySelector("img")) a.classList.add("external-link");
    });
  }
  if (typeof document$ !== "undefined") document$.subscribe(update);
  else if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", update);
  else update();
})();
