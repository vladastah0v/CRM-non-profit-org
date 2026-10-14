/*
 * Компоненты Alpine.js. Используется CSP-сборка Alpine: выражения в разметке
 * не вычисляются через eval, поэтому вся логика регистрируется здесь.
 */
document.addEventListener("alpine:init", () => {
  // Выпадающее меню / мобильная навигация
  window.Alpine.data("toggle", () => ({
    open: false,
    toggle() {
      this.open = !this.open;
    },
    close() {
      this.open = false;
    },
  }));

  // Сообщение, которое можно закрыть
  window.Alpine.data("dismissible", () => ({
    visible: true,
    dismiss() {
      this.visible = false;
    },
  }));
});

// CSRF-токен для всех HTMX-запросов
document.addEventListener("htmx:configRequest", (event) => {
  const token = document.querySelector('meta[name="csrf-token"]');
  if (token) {
    event.detail.headers["X-CSRFToken"] = token.getAttribute("content");
  }
});

// Подтверждение действий: <form data-confirm="Текст вопроса">
document.addEventListener("submit", (event) => {
  const form = event.target;
  const message = form.getAttribute && form.getAttribute("data-confirm");
  if (message && !window.confirm(message)) {
    event.preventDefault();
  }
});

// «Отметить всех» / «Снять отметки»: <button type="button" data-check-all="имя поля" data-value="1|0">
document.addEventListener("click", (event) => {
  const button = event.target.closest && event.target.closest("[data-check-all]");
  if (!button || !button.form) {
    return;
  }
  const name = button.getAttribute("data-check-all");
  const checked = button.getAttribute("data-value") !== "0";
  button.form.querySelectorAll('input[type="checkbox"]').forEach((box) => {
    if (box.name === name) {
      box.checked = checked;
    }
  });
});
