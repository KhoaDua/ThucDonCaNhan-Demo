/* Đánh dấu riêng theo phiên bản thực đơn trong thẻ trình duyệt. Dữ liệu thực đơn chỉ dùng để hiển thị. */
(() => {
  const inputs = Array.from(document.querySelectorAll(".purchase-check input"));
  const filters = document.querySelector(".filters");
  const filterButtons = document.querySelectorAll("[data-filter]");
  const progress = document.querySelector("progress");
  const count = document.querySelector("[data-checked]");
  const progressStatus = document.querySelector("[data-progress-status]");
  const sessionNote = document.querySelector("[data-session-note]");
  const emptyFilter = document.querySelector("[data-empty-filter]");
  const storageKey = "shopping:checked:v1:" + document.body.dataset.shoppingVersion;
  let filter = "all";
  let sessionAvailable = true;

  try {
    const saved = JSON.parse(sessionStorage.getItem(storageKey) || "[]");
    if (Array.isArray(saved)) {
      inputs.forEach((input, index) => { input.checked = saved[index] === true; });
    }
  } catch {
    sessionAvailable = false;
  }

  function updateStorageNote() {
    if (!sessionAvailable) {
      sessionNote.textContent = "Dấu tích được giữ cho đến khi tải lại trang.";
    }
  }

  function refreshList() {
    const checked = inputs.filter(input => input.checked).length;
    count.textContent = String(checked);
    progress.value = checked;
    const remaining = inputs.length - checked;
    progressStatus.textContent = remaining
      ? "Còn " + remaining + " nguyên liệu cần mua"
      : inputs.length ? "Đã mua đủ nguyên liệu trong danh sách" : "Đã có đủ nguyên liệu";

    let visible = 0;
    for (const input of inputs) {
      const row = input.closest("[data-item]");
      row.classList.toggle("is-checked", input.checked);
      row.hidden = filter !== "all" && input.checked !== (filter === "done");
      if (!row.hidden) visible++;
    }
    if (emptyFilter) {
      emptyFilter.hidden = visible > 0;
      emptyFilter.textContent = visible > 0 ? "" : filter === "done"
        ? "Chưa đánh dấu nguyên liệu nào là đã mua."
        : "Đã mua đủ các nguyên liệu cần mua.";
    }
  }

  for (const input of inputs) {
    input.addEventListener("change", () => {
      const focused = input === document.activeElement;
      if (sessionAvailable) {
        try {
          sessionStorage.setItem(storageKey, JSON.stringify(inputs.map(item => item.checked)));
        } catch {
          sessionAvailable = false;
          updateStorageNote();
        }
      }
      refreshList();
      if (focused && input.closest("[data-item]").hidden) {
        const next = inputs.find(item => !item.closest("[data-item]").hidden);
        (next || filters.querySelector('[aria-pressed="true"]')).focus({ preventScroll: true });
      }
    });
  }
  for (const button of filterButtons) {
    button.addEventListener("click", () => {
      filter = button.dataset.filter;
      for (const option of filterButtons) {
        option.setAttribute("aria-pressed", String(option === button));
      }
      refreshList();
    });
  }
  if (filters) filters.hidden = false;
  updateStorageNote();
  refreshList();

  if ("IntersectionObserver" in window && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
    const observer = new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-entering");
          observer.unobserve(entry.target);
        }
      }
    });
    document.querySelectorAll("[data-reveal]").forEach(element => observer.observe(element));
  }
})();
