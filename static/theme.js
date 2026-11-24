"use strict";

(() => {
  const root = document.documentElement;
  let savedTheme;
  try {
    savedTheme = localStorage.getItem("basket-theme");
  } catch {
    // The toggle also works when browser storage is unavailable.
  }
  root.dataset.theme = ["light", "dark"].includes(savedTheme)
    ? savedTheme
    : "dark";

  document.addEventListener("DOMContentLoaded", () => {
    const toggle = document.getElementById("theme-toggle");
    const label = document.getElementById("theme-label");
    if (!toggle) return;
    function updateButton() {
      const dark = root.dataset.theme === "dark";
      label.textContent = dark ? "Тёмная" : "Светлая";
      toggle.setAttribute("aria-pressed", String(dark));
      const action = dark ? "Включить светлую тему" : "Включить тёмную тему";
      toggle.setAttribute("aria-label", action);
      toggle.title = action;
    }
    updateButton();
    toggle.addEventListener("click", () => {
      root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
      try {
        localStorage.setItem("basket-theme", root.dataset.theme);
      } catch {
        // Keep the selected theme for this page without persistent storage.
      }
      updateButton();
      window.dispatchEvent(new Event("themechange"));
    });
  });
})();
