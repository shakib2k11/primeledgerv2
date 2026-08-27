document.addEventListener("DOMContentLoaded", () => {
  const editor = document.querySelector("[data-formset]");
  if (!editor) return;

  const container = editor.querySelector("[data-formset-lines]");
  const template = editor.querySelector("template");
  const total = editor.querySelector("input[name$='-TOTAL_FORMS']");
  const addButton = editor.querySelector("[data-add-line]");

  addButton?.addEventListener("click", () => {
    const index = Number(total.value);
    const markup = template.innerHTML.replaceAll("__prefix__", String(index));
    container.insertAdjacentHTML("beforeend", markup);
    total.value = String(index + 1);
    container.lastElementChild?.querySelector("select, input")?.focus();
  });
});
