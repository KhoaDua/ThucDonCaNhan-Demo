// Tối thiểu: chống bấm gửi hai lần và cuộn tới ô lỗi đầu tiên.
document.querySelectorAll("form").forEach(function (f) {
  f.addEventListener("submit", function (ev) {
    if (f.dataset.sent) { ev.preventDefault(); return; }
    f.dataset.sent = "1";
    setTimeout(function () { delete f.dataset.sent; }, 4000);
  });
});
var firstErr = document.querySelector(".err");
if (firstErr) { firstErr.scrollIntoView({ block: "center" }); }

// A panel's choices belong to the saved plan version and this exact day/meal/position.
document.querySelectorAll('[data-swap-panel]').forEach(panel => {
  const form = panel.querySelector('[data-swap-form]');
  const recipe = form.elements.recipe_id;
  const servings = form.elements.servings;
  const save = form.querySelector('button');
  const message = panel.querySelector('[data-swap-message]');
  const retry = panel.querySelector('[data-swap-retry]');
  let loaded = false, loading = false, choices = [];
  const selectServings = () => {
    servings.replaceChildren();
    const selected = choices.find(choice => choice.recipe_id === recipe.value);
    for (const level of selected?.servings || []) {
      servings.add(new Option(Number(level).toLocaleString('vi-VN') + ' khẩu phần', String(level)));
    }
    servings.disabled = !selected;
    save.disabled = !selected || !servings.options.length;
  };
  recipe.addEventListener('change', selectServings);
  const load = async () => {
    if (loading || loaded) return;
    loading = true;
    recipe.disabled = servings.disabled = save.disabled = true;
    retry.hidden = true;
    message.textContent = 'Đang kiểm tra món và khẩu phần phù hợp…';
    const params = new URLSearchParams();
    for (const key of ['base_version', 'day', 'meal', 'pos']) params.set(key, form.elements[key].value);
    try {
      const response = await fetch(panel.dataset.optionsUrl + '?' + params, {cache:'no-store'});
      const result = await response.json();
      if (!response.ok || result.status !== 'ok') {
        message.textContent = (result.reasons || ['Không tải được lựa chọn đổi món.']).join(' ');
        retry.hidden = result.status === 'stale_plan';
        if (result.status === 'stale_plan') {
          const link = document.createElement('a');
          link.href = location.pathname + '?day=' + form.elements.day.value;
          link.textContent = ' Tải lại thực đơn';
          message.append(link);
        }
        return;
      }
      choices = result.alternatives;
      recipe.replaceChildren();
      for (const choice of choices) recipe.add(new Option(choice.name, choice.recipe_id));
      if (result.portion_change) {
        recipe.add(new Option('Giữ món, đổi khẩu phần: ' + result.portion_change.name, result.portion_change.recipe_id));
        choices = [...choices, result.portion_change];
      }
      loaded = true;
      recipe.disabled = !choices.length;
      selectServings();
      message.textContent = result.alternatives.length
        ? 'Chỉ hiển thị món và khẩu phần đáp ứng các điều kiện của thực đơn hiện tại.'
        : 'Chưa có món thay thế đáp ứng các điều kiện hiện tại.' + (result.portion_change ? ' Bạn có thể đổi khẩu phần của món đang dùng.' : '');
    } catch (error) {
      message.textContent = 'Chưa tải được lựa chọn đổi món. Vui lòng thử lại.';
      retry.hidden = false;
    } finally {
      loading = false;
    }
  };
  panel.addEventListener('toggle', () => { if (panel.open) load(); });
  retry.addEventListener('click', load);
});

const healthChecklist = document.querySelector("[data-health-checklist]");
if (healthChecklist) {
  healthChecklist.addEventListener("change", event => {
    if (event.target.name === "health_conditions" && event.target.checked) {
      for (const choice of healthChecklist.querySelectorAll("[name=health_conditions]")) {
        if (choice !== event.target && (choice.value === "none" || event.target.value === "none")) choice.checked = false;
      }
    }
  });
}
const allergenChecklist = document.querySelector("[data-allergen-checklist]");
if (allergenChecklist) {
  allergenChecklist.addEventListener("change", event => {
    if (event.target.checked) {
      for (const choice of allergenChecklist.querySelectorAll("[name=allergen_choices]")) {
        if (choice !== event.target && (choice.value === "none" || event.target.value === "none")) choice.checked = false;
      }
    }
  });
}
const foodRestrictions = document.querySelector("[data-food-restrictions]");
if (foodRestrictions) {
  const panel = foodRestrictions.querySelector("[data-food-choices]");
  const update = () => {
    const show = !!foodRestrictions.querySelector("[name=food_restrictions][value=yes]:checked");
    panel.hidden = !show;
    for (const choice of panel.querySelectorAll("input[type=checkbox]")) {
      choice.disabled = !show;
      if (!show) choice.checked = false;
    }
  };
  foodRestrictions.addEventListener("change", update);
  update();
}

const dishPicker = document.getElementById("dish-picker");
if (dishPicker) {
  const selectedDishes = document.getElementById("avoid-dishes");
  const pickerError = document.getElementById("dish-picker-error");
  const catalog = new Set(Array.from(dishPicker.list.options, option => option.value));
  const addDish = () => {
    const value = dishPicker.value.trim();
    if (!catalog.has(value)) {
      pickerError.textContent = "Hãy chọn một món trong danh mục gợi ý.";
      pickerError.hidden = false;
      return;
    }
    pickerError.hidden = true;
    const dishes = new Set(selectedDishes.value.trim().split("\n").filter(Boolean));
    dishes.add(value);
    selectedDishes.value = Array.from(dishes).join("\n");
    dishPicker.value = "";
    dishPicker.focus();
  };
  document.querySelector("[data-add-dish]").addEventListener("click", addDish);
  dishPicker.addEventListener("keydown", event => {
    if (event.key === "Enter") {
      event.preventDefault();
      addDish();
    }
  });
}
