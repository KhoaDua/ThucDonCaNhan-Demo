/* Chỉ xử lý trình bày và chuyển động; biểu mẫu gửi theo hành động gốc. */
(function () {
  const reduced=window.matchMedia('(prefers-reduced-motion: reduce)');
  const running=new Set();
  function motion(element,frames) {
    if (reduced.matches || !element.animate) return;
    const animation=element.animate(frames,{duration:220,easing:'ease-out'});
    running.add(animation);
    animation.finished.then(()=>running.delete(animation),()=>running.delete(animation));
  }
  function applyPreference() {
    document.body.classList.toggle('motion-ready',!reduced.matches);
    if (reduced.matches) for (const animation of running) animation.cancel();
  }
  applyPreference();
  reduced.addEventListener('change',applyPreference);
  if ('IntersectionObserver' in window) {
    const observer=new IntersectionObserver(entries=>{
      for (const entry of entries) if (entry.isIntersecting) {
        if (!reduced.matches) entry.target.classList.add('is-entering');
        entry.target.dataset.revealed='true';
        observer.unobserve(entry.target);
      }
    },{threshold:.06});
    document.querySelectorAll('.page-heading > div:first-child,.review-art,.review-status,.profile-snapshot,.review-content > .card,.energy-summary').forEach(el=>observer.observe(el));
  }
  document.querySelectorAll('details').forEach(details=>{
    details.addEventListener('toggle',()=>{
      if (details.open) for (const child of details.children) if (child.tagName!=='SUMMARY') motion(child,[{opacity:.6,transform:'translateY(-4px)'},{opacity:1,transform:'translateY(0)'}]);
    });
  });
})();
