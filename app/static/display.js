/* Chỉ làm gọn chữ hiển thị, kể cả kết quả cập nhật động. Không sửa giá trị biểu mẫu. */
(function () {
  function clean(text) {
    let previous;
    do { previous = text; text = text.replace(/\s*\([^()]*\)/g, ''); } while (text !== previous);
    return text;
  }
  const excluded = 'script,style,textarea';
  function cleanNode(node) {
    if (node.nodeType === Node.TEXT_NODE) {
      if (node.parentElement && !node.parentElement.closest(excluded)) {
        const text = clean(node.nodeValue);
        if (text !== node.nodeValue) node.nodeValue = text;
      }
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE || node.matches(excluded)) return;
    for (const attr of ['placeholder','title','aria-label','alt']) {
      if (node.hasAttribute(attr)) {
        const current = node.getAttribute(attr), text = clean(current);
        if (text !== current) node.setAttribute(attr, text);
      }
    }
    for (const child of node.childNodes) cleanNode(child);
  }
  cleanNode(document.body);
  new MutationObserver(records => {
    for (const record of records) {
      if (record.type === 'characterData') cleanNode(record.target);
      else if (record.type === 'attributes') cleanNode(record.target);
      else for (const node of record.addedNodes) cleanNode(node);
    }
  }).observe(document.body, {childList:true,subtree:true,characterData:true,attributes:true,
                            attributeFilter:['placeholder','title','aria-label','alt']});
})();
