/* ShiftCare shared motion layer — cursor glow, scroll reveal, nav state. */
(function () {
  "use strict";

  // ── Cursor glow (desktop pointer devices only) ──────────────────────────
  if (window.matchMedia("(hover: hover) and (pointer: fine)").matches) {
    var glow = document.createElement("div");
    glow.id = "cursor-glow";
    document.body.appendChild(glow);
    var gx = 0, gy = 0, tx = 0, ty = 0, active = false;

    document.addEventListener("mousemove", function (e) {
      tx = e.clientX; ty = e.clientY;
      if (!active) { active = true; glow.style.opacity = "1"; raf(); }
    });
    document.addEventListener("mouseleave", function () {
      glow.style.opacity = "0"; active = false;
    });

    function raf() {
      gx += (tx - gx) * 0.12;
      gy += (ty - gy) * 0.12;
      glow.style.left = gx + "px";
      glow.style.top = gy + "px";
      if (active) requestAnimationFrame(raf);
    }
  }

  // ── Scroll reveal ───────────────────────────────────────────────────────
  var revealEls = document.querySelectorAll(".reveal");
  if (revealEls.length && "IntersectionObserver" in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) { en.target.classList.add("in"); io.unobserve(en.target); }
      });
    }, { threshold: 0.12 });
    revealEls.forEach(function (el) { io.observe(el); });
  } else {
    revealEls.forEach(function (el) { el.classList.add("in"); });
  }

  // ── Nav scrolled state ──────────────────────────────────────────────────
  var nav = document.querySelector(".site-nav");
  if (nav) {
    var onScroll = function () {
      nav.classList.toggle("scrolled", window.scrollY > 10);
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
  }

  // ── Card tilt (subtle 3D on .card-tilt) ─────────────────────────────────
  if (window.matchMedia("(hover: hover) and (pointer: fine)").matches) {
    document.querySelectorAll(".card-tilt").forEach(function (card) {
      card.addEventListener("mousemove", function (e) {
        var r = card.getBoundingClientRect();
        var px = (e.clientX - r.left) / r.width - 0.5;
        var py = (e.clientY - r.top) / r.height - 0.5;
        card.style.transform = "perspective(800px) rotateY(" + (px * 4) + "deg) rotateX(" + (py * -4) + "deg) translateY(-2px)";
      });
      card.addEventListener("mouseleave", function () {
        card.style.transform = "";
      });
    });
  }
})();
