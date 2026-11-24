"use strict";

(() => {
  let receipt = document.getElementById("basket-receipt");
  if (!receipt) return;
  let controller;
  let requestId = 0;

  function prepareControls() {
    receipt.dataset.live = "true";
    const selects = [...receipt.querySelectorAll("select[name^=shop]")];
    selects.forEach((select) => {
      [...select.options].forEach((option) => {
        option.disabled =
          Boolean(option.value) &&
          selects.some(
            (other) => other !== select && other.value === option.value,
          );
      });
    });
  }

  function updateLinks(params) {
    const filter = receipt.querySelector("#receipt-filter-form");
    const address = new URL(filter.action);
    address.search = params.toString();
    history.replaceState(null, "", address);
    document.querySelectorAll("a.basket-choice").forEach((link) => {
      const target = new URL(link.href);
      ["date", "shop1", "shop2", "shop3"].forEach((name) =>
        target.searchParams.set(name, params.get(name) || ""),
      );
      link.href = target;
    });
  }

  async function refreshReceipt() {
    const form = receipt.querySelector("#receipt-filter-form");
    const status = receipt.querySelector(".receipt-status");
    controller?.abort();
    const currentRequest = ++requestId;
    if (!form.checkValidity()) {
      receipt.removeAttribute("aria-busy");
      receipt.dataset.state = "error";
      status.textContent = "Укажите корректную дату цен.";
      return;
    }
    controller = new AbortController();
    const focusedName = document.activeElement?.name;
    const params = new URLSearchParams(new FormData(form));
    updateLinks(params);
    receipt.setAttribute("aria-busy", "true");
    receipt.dataset.state = "loading";
    status.textContent = "Обновление цен…";
    try {
      const response = await fetch(receipt.dataset.receiptUrl + "?" + params, {
        signal: controller.signal,
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!response.ok) throw new Error("Receipt request failed");
      const fragment = new DOMParser().parseFromString(
        await response.text(),
        "text/html",
      );
      const updated = fragment.getElementById("basket-receipt");
      if (!updated) throw new Error("Invalid receipt response");
      if (currentRequest !== requestId) return;
      const scrollLeft = receipt.querySelector(".table-scroll").scrollLeft;
      receipt.replaceWith(updated);
      receipt = updated;
      receipt.querySelector(".table-scroll").scrollLeft = scrollLeft;
      prepareControls();
      const selected = new URLSearchParams(
        new FormData(receipt.querySelector("#receipt-filter-form")),
      );
      updateLinks(selected);
      receipt.querySelector(".receipt-status").textContent = "Цены обновлены.";
      if (focusedName) {
        [...receipt.querySelectorAll("input, select")]
          .find((field) => field.name === focusedName)
          ?.focus({ preventScroll: true });
      }
    } catch (error) {
      if (error.name === "AbortError" || currentRequest !== requestId) return;
      receipt.dataset.live = "false";
      receipt.dataset.state = "error";
      receipt.querySelector(".receipt-status").textContent =
        "Не удалось обновить цены. Нажмите «Показать цены» или обновите страницу.";
    } finally {
      if (currentRequest === requestId) receipt.removeAttribute("aria-busy");
    }
  }

  document.addEventListener("change", (event) => {
    if (
      receipt.contains(event.target) &&
      event.target.form?.id === "receipt-filter-form"
    ) {
      refreshReceipt();
    }
  });
  document.addEventListener("submit", (event) => {
    if (
      event.target.id === "receipt-filter-form" &&
      receipt.dataset.live === "true"
    ) {
      event.preventDefault();
      refreshReceipt();
    }
  });
  prepareControls();
})();
