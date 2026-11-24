/* Lớp trình bày riêng: các giá trị đều đọc từ đoạn kết quả của body.js gốc. */
(function () {
  const form = document.getElementById('profile-checklist');
  if (!form) return;
  const raw = form.querySelector('[data-body-preview]');
  const views = document.querySelectorAll('[data-summary-view]');
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const running = new Set(), latest = new WeakMap();
  let previous = '', framePending = false;
  // Hiệu ứng chỉ thay đổi trình bày; không can thiệp giá trị, lựa chọn hoặc phép tính.
  function playMotion(element, frames, options = {}) {
    if (reducedMotion.matches || !element.animate || !element.getBoundingClientRect().width) return;
    latest.get(element)?.cancel();
    const animation = element.animate(frames, {duration: 280, easing: 'ease-out', ...options});
    latest.set(element, animation);
    running.add(animation);
    animation.finished.then(() => running.delete(animation), () => running.delete(animation));
  }
  function applyMotionPreference() {
    document.body.classList.toggle('motion-ready', !reducedMotion.matches);
    if (reducedMotion.matches) for (const animation of running) animation.cancel();
  }
  applyMotionPreference();
  reducedMotion.addEventListener('change', applyMotionPreference);
  const icon = name => {
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
    svg.setAttribute('aria-hidden','true');
    const use=document.createElementNS('http://www.w3.org/2000/svg','use');
    use.setAttribute('href','#'+name);svg.append(use);
    return svg;
  };
  const afterColon = text => text.slice(text.indexOf(':')+1).trim().replace(/\.$/,'');
  const node = (tag, className, text) => {
    const element=document.createElement(tag);
    element.className=className;
    element.textContent=text;
    return element;
  };
  function present() {
    framePending=false;
    const lines=Array.from(raw.querySelectorAll('p'),p=>p.textContent.trim());
    const signature=lines.join('\n');
    if (signature===previous) return;
    const hadResults=previous!=='';
    previous=signature;
    const bmi=lines.find(t=>t.startsWith('BMI:'));
    const base=lines.find(t=>t.startsWith('Calo duy trì ước tính cả ngày:'));
    const effective=lines.find(t=>t.startsWith('Mục tiêu có hiệu lực theo bệnh:'));
    const planned=lines.find(t=>t.startsWith('Tổng các bữa đã chọn:'));
    const meals=lines.filter(t=>/^(Bữa sáng|Bữa trưa|Bữa tối)[\s(:]/.test(t));
    const messages=lines.filter(t=>!(/^(BMI:|Calo duy trì ước tính cả ngày:|Mục tiêu có hiệu lực theo bệnh:|Bộ quy tắc mô phỏng tự áp dụng|Bữa sáng|Bữa trưa|Bữa tối|Tổng các bữa đã chọn:|Khoảng ±10%)/.test(t)));
    for (const view of views) {
      const oldBmi=view.querySelector('[data-bmi-value]').textContent;
      const oldEnergy=view.querySelector('[data-energy-value]').textContent;
      view.querySelector('[data-bmi-value]').textContent=bmi ? afterColon(bmi).split(' · ')[0] : '—';
      view.querySelector('[data-bmi-status]').textContent=bmi?.includes(' · ') ? bmi.split(' · ').slice(1).join(' · ') : 'Chưa đủ thông tin';
      view.querySelector('[data-energy-label]').textContent=effective ? 'Mục tiêu theo bệnh cả ngày' : 'Calo duy trì cả ngày';
      view.querySelector('[data-energy-value]').textContent=effective || base ? afterColon(effective || base).split(' · ')[0] : '—';
      const list=view.querySelector('[data-meal-targets]');
      list.replaceChildren();
      for (const text of meals) {
        const row=node('div','meal-target','');
        const name=node('span','meal-name',text.slice(0,text.indexOf(':')).trim());
        name.prepend(icon(text.startsWith('Bữa sáng') ? 'sunrise' : text.startsWith('Bữa trưa') ? 'sun' : 'moon'));
        row.append(name);
        const [value,range]=afterColon(text).split('; khoảng ');
        row.append(node('strong','meal-value',value));
        if (range) row.append(node('span','meal-range','Khoảng '+range));
        list.append(row);
      }
      const total=view.querySelector('[data-planned-total]');
      total.hidden=!planned;
      total.replaceChildren();
      if (planned) total.append(node('span','','Tổng các bữa đã chọn'),node('strong','',afterColon(planned)));
      const message=view.querySelector('[data-summary-message]');
      message.hidden=messages.length===0;
      message.textContent=messages.join(' ');
      if (hadResults) {
        const glow=[{boxShadow:'0 0 0 3px rgb(18 107 94 / 18%)'},{boxShadow:'0 0 0 0 rgb(18 107 94 / 0%)'}];
        if (oldBmi!==view.querySelector('[data-bmi-value]').textContent) playMotion(view.querySelector('.bmi-stat'),glow,{duration:420});
        if (oldEnergy!==view.querySelector('[data-energy-value]').textContent) playMotion(view.querySelector('.energy-stat'),glow,{duration:420});
      }
    }
  }
  new MutationObserver(() => {
    if (!framePending) { framePending=true; requestAnimationFrame(present); }
  }).observe(raw,{childList:true,subtree:true,characterData:true});
  present();
  const optionLegends = form.querySelectorAll('.profile-extra > fieldset > legend');
  ['plate','clock','wallet','basket'].forEach((name,index)=>{
    const legend=optionLegends[index];
    if (legend && !legend.querySelector('svg')) {
      const glyph=icon(name);glyph.classList.add('extra-icon');legend.prepend(glyph);
    }
  });
  // Mỗi khối chỉ xuất hiện nhẹ một lần khi vào vùng nhìn; không ẩn nội dung khi JS lỗi.
  if ('IntersectionObserver' in window) {
    const reveal=new IntersectionObserver(entries=>{
      for (const entry of entries) if (entry.isIntersecting) {
        if (!reducedMotion.matches && !entry.target.contains(document.activeElement)) entry.target.classList.add('is-entering');
        entry.target.dataset.revealed='true';
        reveal.unobserve(entry.target);
      }
    },{threshold:0.06});
    document.querySelectorAll('.intro-copy,.checklist-group,.profile-extra,.desktop-summary .summary-view').forEach(element=>reveal.observe(element));
  }
  form.addEventListener('change',event=>{
    const field=event.target;
    if (field.matches('input[type="checkbox"],input[type="radio"]') && field.checked) {
      const label=field.closest('.choice-list label');
      if (label) playMotion(label,[{transform:'scale(.98)'},{transform:'scale(1)'}],{duration:180});
    }
  });
  document.querySelectorAll('details').forEach(details=>{
    details.addEventListener('toggle',()=>{
      if (details.open) for (const child of details.children) if (child.tagName!=='SUMMARY') {
        playMotion(child,[{opacity:.4,transform:'translateY(-4px)'},{opacity:1,transform:'translateY(0)'}],{duration:220});
      }
    });
  });
  document.querySelectorAll('[data-disease-scenario],[data-food-choices]').forEach(panel=>{
    new MutationObserver(()=>{
      if (!panel.hidden) playMotion(panel,[{opacity:.4,transform:'translateY(-5px)'},{opacity:1,transform:'translateY(0)'}],{duration:240});
    }).observe(panel,{attributes:true,attributeFilter:['hidden']});
  });
})();
