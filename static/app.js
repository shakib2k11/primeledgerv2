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
    newRow?.querySelector("select, input")?.focus();
  });
});
