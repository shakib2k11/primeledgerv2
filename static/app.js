const normalizeSearchText = (value) => String(value || "")
  .normalize("NFKD")
  .replace(/[\u0300-\u036f]/g, "")
  .toLocaleLowerCase();

const enhanceSelect = (select) => {
  if (
    !(select instanceof HTMLSelectElement)
    || select.dataset.autocompleteReady === "true"
    || select.multiple
  ) return;

  select.dataset.autocompleteReady = "true";
  select.classList.add("autocomplete-native");
  select.tabIndex = -1;
  select.setAttribute("aria-hidden", "true");

  const wrapper = document.createElement("div");
  wrapper.className = "select-autocomplete";
  const input = document.createElement("input");
  input.type = "text";
  input.className = "select-autocomplete-input";
  input.autocomplete = "off";
  input.spellcheck = false;
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-expanded", "false");
  input.setAttribute("aria-label", select.getAttribute("aria-label") || "Search options");
  const list = document.createElement("div");
  list.className = "select-autocomplete-list";
  list.id = `autocomplete-${select.id || Math.random().toString(36).slice(2)}`;
  list.setAttribute("role", "listbox");
  list.hidden = true;
  input.setAttribute("aria-controls", list.id);
  wrapper.append(input);
  select.insertAdjacentElement("afterend", wrapper);
  document.body.append(list);

  let visibleOptions = [];
  let activeIndex = -1;
  let lastCommittedLabel = "";

  const selectedLabel = () => select.selectedOptions[0]?.textContent.trim() || "";
  const setActive = (nextIndex) => {
    if (!visibleOptions.length) return;
    activeIndex = Math.max(0, Math.min(nextIndex, visibleOptions.length - 1));
    list.querySelectorAll("[role='option']").forEach((item, index) => {
      const active = index === activeIndex;
      item.classList.toggle("active", active);
      item.setAttribute("aria-selected", String(active));
      if (active) {
      input.setAttribute("aria-activedescendant", item.id);
        if (item.offsetTop < list.scrollTop) list.scrollTop = item.offsetTop;
        else if (item.offsetTop + item.offsetHeight > list.scrollTop + list.clientHeight) {
          list.scrollTop = item.offsetTop + item.offsetHeight - list.clientHeight;
        }
      }
    });
  };
  const close = ({restore = false} = {}) => {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    activeIndex = -1;
    if (restore) input.value = selectedLabel();
  };
  const positionList = () => {
    const rect = input.getBoundingClientRect();
    const viewportGap = 8;
    const roomBelow = window.innerHeight - rect.bottom - viewportGap;
    const roomAbove = rect.top - viewportGap;
    const opensAbove = roomBelow < 180 && roomAbove > roomBelow;
    const available = Math.max(100, Math.min(240, opensAbove ? roomAbove : roomBelow));
    list.style.left = `${rect.left}px`;
    list.style.width = `${rect.width}px`;
    list.style.maxHeight = `${available}px`;
    if (opensAbove) {
      list.style.top = "auto";
      list.style.bottom = `${window.innerHeight - rect.top + 4}px`;
    } else {
      list.style.top = `${rect.bottom + 4}px`;
      list.style.bottom = "auto";
    }
  };
  const commit = (option) => {
    select.value = option.value;
    input.value = option.textContent.trim();
    lastCommittedLabel = input.value;
    select.dispatchEvent(new Event("change", {bubbles: true}));
    close();
  };
  const render = (query = "") => {
    activeIndex = -1;
    const needle = normalizeSearchText(query.trim());
    visibleOptions = [...select.options].filter((option) => {
      if (option.hidden) return false;
      return !needle || normalizeSearchText(option.textContent).includes(needle);
    });
    list.replaceChildren();
    visibleOptions.forEach((option, index) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "select-autocomplete-option";
      item.id = `${list.id}-option-${index}`;
      item.setAttribute("role", "option");
      item.setAttribute("aria-selected", "false");
      item.textContent = option.textContent.trim() || "Clear selection";
      if (!option.value) item.classList.add("placeholder");
      item.addEventListener("pointerdown", (event) => {
        event.preventDefault();
        commit(option);
        input.focus();
      });
      list.append(item);
    });
    if (!visibleOptions.length) {
      const empty = document.createElement("div");
      empty.className = "select-autocomplete-empty";
      empty.textContent = "No matching options";
      list.append(empty);
    }
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    positionList();
    const selectedIndex = visibleOptions.findIndex((option) => option.selected);
    if (selectedIndex >= 0) setActive(selectedIndex);
    else if (visibleOptions.length) setActive(0);
  };
  const synchronize = () => {
    lastCommittedLabel = selectedLabel();
    if (document.activeElement !== input) input.value = lastCommittedLabel;
    const disabled = select.disabled;
    input.disabled = disabled;
    wrapper.classList.toggle("disabled", disabled);
  };

  input.addEventListener("focus", () => {
    if (input.disabled) return;
    input.select();
    render("");
  });
  input.addEventListener("input", () => render(input.value));
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (list.hidden) render(input.value);
      setActive(activeIndex + 1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      if (list.hidden) render(input.value);
      setActive(activeIndex < 0 ? visibleOptions.length - 1 : activeIndex - 1);
    } else if (event.key === "Enter" && !list.hidden && activeIndex >= 0) {
      event.preventDefault();
      commit(visibleOptions[activeIndex]);
    } else if (event.key === "Escape") {
      event.preventDefault();
      close({restore: true});
    } else if (event.key === "Tab") {
      close({restore: true});
    }
  });
  input.addEventListener("blur", () => {
    window.setTimeout(() => close({restore: true}), 100);
  });
  select.addEventListener("change", synchronize);
  select.addEventListener("focus", () => input.focus());
  new MutationObserver(synchronize).observe(select, {
    attributes: true,
    attributeFilter: ["disabled"],
  });
  select.form?.addEventListener("reset", () => window.setTimeout(synchronize));
  window.addEventListener("resize", () => {
    if (!list.hidden) positionList();
  });
  window.addEventListener("scroll", (event) => {
    if (event.target === list || list.contains(event.target)) return;
    close({restore: true});
  }, true);
  synchronize();
};

const enhanceSelects = (root = document) => {
  if (root instanceof HTMLSelectElement) enhanceSelect(root);
  root.querySelectorAll?.("select").forEach(enhanceSelect);
};

document.addEventListener("DOMContentLoaded", () => {
  const expenseForm = document.querySelector("[data-expense-form]");
  if (expenseForm) {
    const settlement = expenseForm.querySelector('[name="settlement"]');
    const paymentField = expenseForm.querySelector("[data-payment-account-field]");
    const paymentAccount = expenseForm.querySelector('[name="payment_account"]');
    const credit = expenseForm.querySelector("[data-expense-credit]");
    const updateExpenseSettlement = () => {
      const paidNow = settlement.value === "paid";
      paymentField.hidden = !paidNow;
      paymentAccount.disabled = !paidNow;
      credit.textContent = paidNow
        ? "Credit the selected Cash, Bank, or Mobile Financial Services account"
        : "Credit Accounts Payable; allocate cash payments later";
    };
    settlement.addEventListener("change", updateExpenseSettlement);
    updateExpenseSettlement();
  }
  enhanceSelects();

  new MutationObserver((mutations) => {
    mutations.forEach((mutation) => mutation.addedNodes.forEach((node) => {
      if (node instanceof Element) enhanceSelects(node);
    }));
  }).observe(document.body, {childList: true, subtree: true});
  const setoffForm = document.querySelector("[data-setoff-form]");
  if (setoffForm) {
    const saleInputs = [...setoffForm.querySelectorAll('input[name^="sale_"]')];
    const purchaseInputs = [...setoffForm.querySelectorAll('input[name^="purchase_"]')];
    const saleOutput = setoffForm.querySelector("[data-setoff-sale-total]");
    const purchaseOutput = setoffForm.querySelector("[data-setoff-purchase-total]");
    const status = setoffForm.querySelector("[data-setoff-status]");
    const total = (inputs) => inputs.reduce((sum, input) => sum + (Number.parseFloat(input.value) || 0), 0);
    const updateSetoff = () => {
      const sales = total(saleInputs);
      const purchases = total(purchaseInputs);
      saleOutput.textContent = sales.toFixed(2);
      purchaseOutput.textContent = purchases.toFixed(2);
      const balanced = sales > 0 && Math.abs(sales - purchases) < 0.005;
      status.textContent = balanced ? `Ready to set off ${sales.toFixed(2)}` : `Difference: ${Math.abs(sales - purchases).toFixed(2)}`;
      status.classList.toggle("positive", balanced);
    };
    [...saleInputs, ...purchaseInputs].forEach((input) => input.addEventListener("input", updateSetoff));
    updateSetoff();
  }
  const editor = document.querySelector("[data-formset]");
  if (!editor) return;

  const container = editor.querySelector("[data-formset-lines]");
  const template = editor.querySelector("template");
  const total = editor.querySelector("input[name$='-TOTAL_FORMS']");
  const addButton = editor.querySelector("[data-add-line]");

  const updateLineAmount = (row) => {
    if (!row) return;
    const quantity = row.querySelector("input[name$='-quantity']");
    const unitPrice = row.querySelector("input[name$='-unit_price']");
    const output = row.querySelector("[data-line-amount]");
    if (!quantity || !unitPrice || !output) return;
    const quantityValue = Number.parseFloat(quantity.value);
    const priceValue = Number.parseFloat(unitPrice.value);
    const amount = (
      Number.isFinite(quantityValue) && Number.isFinite(priceValue)
        ? quantityValue * priceValue
        : 0
    );
    output.textContent = amount.toFixed(2);
  };

  container.querySelectorAll(".trade-line-grid").forEach(updateLineAmount);
  container.addEventListener("input", (event) => {
    if (!event.target.matches("input[name$='-quantity'], input[name$='-unit_price']")) return;
    updateLineAmount(event.target.closest(".trade-line-grid"));
  });

  addButton?.addEventListener("click", () => {
    const index = Number(total.value);
    const markup = template.innerHTML.replaceAll("__prefix__", String(index));
    container.insertAdjacentHTML("beforeend", markup);
    total.value = String(index + 1);
    const newRow = container.lastElementChild;
    updateLineAmount(newRow);
    enhanceSelects(newRow);
    newRow?.querySelector(".select-autocomplete-input, input")?.focus();
  });
});
