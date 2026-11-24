/* Xem trước từ cùng cấu hình với máy chủ; máy chủ luôn tính lại khi lưu. */
(function () {
  const module = document.querySelector('[data-body-module]');
  if (!module) return;
  const policy = JSON.parse(document.getElementById('body-policy-data').textContent);
  const disease = JSON.parse(document.getElementById('disease-policy-data').textContent);
  const scopeScenarios = JSON.parse(document.getElementById('body-scope-scenarios').textContent);
  const scenarioPanel = document.querySelector('[data-disease-scenario]');
  const scopeQuestion = module.querySelector('[data-body-scope-question]');
  const form = module.closest('form'), preview = module.querySelector('[data-body-preview]');
  const fmt = n => n.toLocaleString('vi-VN', {maximumFractionDigits: 2});
  const names = {breakfast:'Bữa sáng', lunch:'Bữa trưa', dinner:'Bữa tối'};
  function number(key, range) {
    const raw = form.elements['body_' + key].value.trim().replace(',', '.');
    if (!/^[0-9]+(?:\.[0-9]+)?$/.test(raw)) return null;
    const n = Number(raw);
    return Number.isFinite(n) && n >= range[0] && n <= range[1] ? n : null;
  }
  function update() {
    const conditions = Array.from(form.querySelectorAll('[name=health_conditions]:checked')).map(c=>c.value).filter(v=>v!=='none');
    const hasDisease = conditions.length > 0;
    const personalGoal = !!form.querySelector('[name=goals][value=energy]:checked');
    const applied = hasDisease || personalGoal;
    scenarioPanel.hidden = !hasDisease;
    for (const input of scenarioPanel.querySelectorAll('input,select')) input.disabled = !hasDisease;
    if (!hasDisease) { form.elements.health_scenario.value = ''; form.elements.health_simulation_scope.checked = false; }
    const scenario = form.elements.health_scenario.value;
    const simulationConfirmed = form.elements.health_simulation_scope.checked;
    const simulatedScope = hasDisease && simulationConfirmed && scopeScenarios.includes(scenario);
    const needsScopeAnswer = applied && !simulatedScope;
    scopeQuestion.hidden = !needsScopeAnswer;
    for (const input of scopeQuestion.querySelectorAll('input')) input.disabled = !needsScopeAnswer;
    const scopeAnswer = needsScopeAnswer ? form.elements.body_pregnancy_or_breastfeeding.value : '';
    preview.replaceChildren();
    const line = text => { const p = document.createElement('p'); p.textContent = text; preview.append(p); };
    const h = number('height_cm', policy.height_range_cm), w = number('weight_kg', policy.weight_range_kg);
    if (h === null || w === null) { line('Nhập chiều cao (cm) và cân nặng (kg) hợp lệ để xem BMI.'); return; }
    const bmi = w / Math.pow(h / 100, 2);
    const age = number('age_years', [policy.age_min, policy.age_max_input]);
    const adult = age !== null && Number.isInteger(age);
    const eligible = adult && (simulatedScope || scopeAnswer === 'no');
    let group = bmi < policy.bmi.underweight_below ? 'Thiếu cân' : bmi < policy.bmi.overweight_from ? 'Bình thường' : 'Thừa cân';
    if (bmi >= policy.bmi.obesity_from) group += ' (BMI từ 30: thuộc ngưỡng béo phì)';
    line('BMI: ' + fmt(bmi) + (eligible ? ' · ' + group : ' · Chưa phân loại'));
    if (!applied) return;
    if (!adult) { line('Nhập tuổi hợp lệ từ 18 tuổi để tính calo.'); return; }
    if (!eligible) {
      line(scopeAnswer === 'yes'
        ? 'Mục tiêu calo cá nhân chưa áp dụng khi đang mang thai hoặc cho con bú.'
        : 'Trả lời câu hỏi Có hoặc Không để tính calo.');
      return;
    }
    const sex = form.elements.body_sex.value, activity = form.elements.body_activity.value;
    if (!policy.sex_labels[sex] || !policy.activity[activity]) {
      line('Chọn giới tính và mức vận động để ước tính calo duy trì.'); return;
    }
    const band = policy.age_bands.find(b => age >= b.min_age && (b.max_age_exclusive === null || age < b.max_age_exclusive));
    let day = (band[sex][0] * w + band[sex][1]) * policy.activity[activity].factor;
    line('Calo duy trì ước tính cả ngày: ' + fmt(day) + ' kcal.');
    if (hasDisease) {
      if (!scenario || !simulationConfirmed) { line('Chọn mục tiêu và xác nhận phạm vi kịch bản bệnh để tính calo có hiệu lực.'); return; }
      if (bmi < disease.minimum_bmi) { line('Hồ sơ thiếu cân nằm ngoài phạm vi mô phỏng bệnh này.'); return; }
      if (scenario === 'weight_loss') {
        if (bmi < disease.weight_loss_bmi) { line('Giảm cân theo bệnh chỉ áp dụng khi BMI từ 25.'); return; }
        day -= disease.deficit_kcal;
        if (day * (1-policy.tolerance) < disease.energy_floor[sex]) { line('Cận dưới sau giảm thấp hơn ngưỡng chặn DEMO. Kịch bản không phù hợp.'); return; }
      }
      line('Mục tiêu có hiệu lực theo bệnh: ' + fmt(day) + ' kcal/ngày · ' + disease.scenarios[scenario] + '.');
      line('Bộ quy tắc mô phỏng tự áp dụng dù chưa chọn mục tiêu Kiểm soát năng lượng.');
    }
    let share = 0;
    for (const meal of form.querySelectorAll('[name=meals]:checked')) {
      const ratio = policy.meal_ratios[meal.value], target = day * ratio; share += ratio;
      line(names[meal.value] + ' (' + fmt(ratio * 100) + '%): ' + fmt(target) + ' kcal; khoảng ' + fmt(target * (1-policy.tolerance)) + ' – ' + fmt(target * (1+policy.tolerance)) + ' kcal.');
    }
    if (!share) { line('Chọn ít nhất một bữa để phân bổ calo.'); return; }
    line('Tổng các bữa đã chọn: ' + fmt(day * share) + ' kcal/ngày (' + fmt(share * 100) + '% cả ngày).');
    line('Khoảng ±10% áp dụng cho tổng các món trong từng bữa. Bữa chưa chọn không được dồn calo vào các bữa còn lại.');
  }
  form.addEventListener('input', update); form.addEventListener('change', update); update();
})();
