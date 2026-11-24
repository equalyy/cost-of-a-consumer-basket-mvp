"use strict";

(() => {
  const dialog = document.getElementById("reset-dialog");
  if (!dialog) return;
  const openButton = document.getElementById("reset-open");
  const cancelButton = document.getElementById("reset-cancel");
  const deleteButton = document.getElementById("reset-delete");
  const form = document.getElementById("reset-form");
  const confirmation = document.getElementById("reset-confirmation");
  const status = document.getElementById("reset-status");
  let interval;
  let controller;
  let requestId = 0;
  let deadline = Infinity;

  function resetState() {
    clearInterval(interval);
    controller?.abort();
    requestId += 1;
    deadline = Infinity;
    confirmation.value = "";
    deleteButton.disabled = true;
    deleteButton.textContent = "Удалить (3)";
  }

  async function openDialog() {
    resetState();
    const currentRequest = requestId;
    status.textContent = "Подготовка подтверждения…";
    dialog.showModal();
    cancelButton.focus();
    controller = new AbortController();
    try {
      const body = new FormData();
      body.set("csrf", form.elements.csrf.value);
      const response = await fetch(form.dataset.prepareUrl, {
        method: "POST",
        body,
        signal: controller.signal,
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error("Confirmation request failed");
      const data = await response.json();
      if (typeof data.confirmation !== "string" || data.wait_seconds !== 3) {
        throw new Error("Invalid confirmation response");
      }
      if (currentRequest !== requestId || !dialog.open) return;
      confirmation.value = data.confirmation;
      deadline = performance.now() + data.wait_seconds * 1000;
      function tick() {
        const remaining = Math.max(
          0,
          Math.ceil((deadline - performance.now()) / 1000),
        );
        deleteButton.disabled = remaining > 0;
        deleteButton.textContent = remaining
          ? `Удалить (${remaining})`
          : "Удалить";
        status.textContent = remaining
          ? `Пожалуйста, проверьте действие. Удаление станет доступно через ${remaining} с.`
          : "Удаление доступно. Нажмите «Отмена», если хотите сохранить данные.";
        if (!remaining) clearInterval(interval);
      }
      tick();
      interval = setInterval(tick, 1000);
    } catch (error) {
      if (error.name === "AbortError" || currentRequest !== requestId) return;
      status.textContent =
        "Не удалось подготовить очистку. Закройте окно, обновите страницу и повторите попытку.";
    }
  }

  openButton.addEventListener("click", openDialog);
  cancelButton.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", resetState);
  form.addEventListener("submit", (event) => {
    if (
      deleteButton.disabled ||
      !confirmation.value ||
      performance.now() < deadline
    ) {
      event.preventDefault();
      return;
    }
    deleteButton.disabled = true;
    deleteButton.textContent = "Удаление…";
  });
})();
