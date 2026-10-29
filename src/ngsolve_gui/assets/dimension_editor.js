(() => {
  if (window.__mmfemDimensionEditor) return;
  const style = document.createElement('style');
  style.textContent = `
    .solve-sketch-dimension-editor { touch-action:none; }
    .solve-sketch-dimension-editor .dimension-editor-header {
      cursor:move; user-select:none; padding:4px 2px 8px; touch-action:none;
    }
    .solve-sketch-dimension-editor[data-editor-mode="inline"] {
      width:auto !important; padding:0 3px !important; border-radius:3px !important;
      box-shadow:0 1px 5px #14263825 !important;
    }
    .solve-sketch-dimension-editor[data-editor-mode="inline"] .dimension-editor-header,
    .solve-sketch-dimension-editor[data-editor-mode="inline"] .dimension-editor-actions { display:none !important; }
    .solve-sketch-dimension-editor[data-editor-mode="inline"] .q-field { width:132px !important; }
    .solve-sketch-dimension-editor[data-editor-mode="inline"] .q-field__control,
    .solve-sketch-dimension-editor[data-editor-mode="inline"] .q-field__marginal { min-height:28px; height:28px; }
    .solve-sketch-dimension-editor[data-editor-mode="popup"] .dimension-editor-value > button { display:none; }
  `;
  document.head.appendChild(style);
  let labelId = null, drag = null, frame = 0, observer = null, resizeObserver = null;
  const editor = () => document.querySelector('.solve-sketch-dimension-editor');
  const clamp = (node, x, y) => {
    const host = node.parentElement;
    node.style.left = Math.max(4, Math.min(x, host.clientWidth - node.offsetWidth - 4)) + 'px';
    node.style.top = Math.max(4, Math.min(y, host.clientHeight - node.offsetHeight - 4)) + 'px';
  };
  const position = () => {
    frame = 0;
    const node = editor();
    if (!node || node.style.display === 'none' || drag) return;
    const label = labelId && document.getElementById(labelId);
    if (node.dataset.editorMode === 'inline' && label) {
      const bounds = (label.querySelector('text') || label).getBoundingClientRect();
      const host = node.parentElement.getBoundingClientRect();
      clamp(node, bounds.left - host.left - 4, bounds.top - host.top - 5);
      label.style.visibility = 'hidden';
    } else clamp(node, parseFloat(node.style.left) || 4, parseFloat(node.style.top) || 4);
  };
  const schedule = () => { if (!frame) frame = requestAnimationFrame(position); };
  const restore = () => {
    const label = labelId && document.getElementById(labelId);
    if (label) label.style.visibility = '';
  };
  window.__mmfemDimensionEditor = {
    close() {
      restore();
      const node = editor();
      if (node) node.style.display = 'none';
      if (observer) observer.disconnect();
      if (resizeObserver) resizeObserver.disconnect();
      drag = null;
      labelId = null;
    },
    open(id, point, inline) {
      this.close();
      labelId = id;
      const node = editor();
      if (!node) return;
      node.dataset.editorMode = inline ? 'inline' : 'popup';
      node.style.display = 'flex';
      node.style.flexDirection = 'column';
      node.style.gap = inline ? '0' : '7px';
      node.style.width = '190px';
      clamp(node, point ? point[0] : node.parentElement.clientWidth / 2 - 95,
                  point ? point[1] : node.parentElement.clientHeight / 2 - 50);
      const svg = node.parentElement.querySelector('svg');
      if (svg) {
        observer = new MutationObserver(schedule);
        observer.observe(svg, {subtree:true, childList:true, attributes:true, attributeFilter:['transform', 'viewBox', 'x', 'y']});
      }
      resizeObserver = new ResizeObserver(schedule);
      resizeObserver.observe(node.parentElement);
      position();
      requestAnimationFrame(() => {
        const input = node.querySelector('input');
        if (input) { input.focus(); input.select(); }
      });
    },
    expand() {
      const node = editor();
      if (!node) return;
      restore();
      node.dataset.editorMode = 'popup';
      node.style.gap = '7px';
      position();
      const input = node.querySelector('input');
      if (input) input.focus();
    }
  };
  document.addEventListener('pointerdown', event => {
    const header = event.target.closest && event.target.closest('.dimension-editor-header');
    if (!header || event.button !== 0) return;
    const node = editor();
    drag = {id:event.pointerId, x:event.clientX, y:event.clientY,
            left:parseFloat(node.style.left) || 0, top:parseFloat(node.style.top) || 0};
    header.setPointerCapture(event.pointerId);
    event.preventDefault();
    event.stopPropagation();
  }, true);
  document.addEventListener('pointermove', event => {
    if (!drag || drag.id !== event.pointerId) return;
    clamp(editor(), drag.left + event.clientX - drag.x, drag.top + event.clientY - drag.y);
    event.preventDefault();
    event.stopPropagation();
  }, true);
  for (const name of ['pointerup', 'pointercancel']) document.addEventListener(name, event => {
    if (!drag || drag.id !== event.pointerId) return;
    drag = null;
    event.stopPropagation();
  }, true);
  window.addEventListener('resize', schedule);
  document.addEventListener('keydown', event => {
    const node = editor();
    if (!node || !node.contains(event.target) || !event.target.matches('input') || event.isComposing) return;
    if (event.key !== 'Enter' && event.key !== 'Escape') return;
    const actions = node.querySelectorAll('.dimension-editor-actions button');
    const action = event.key === 'Enter' ? actions[0] : actions[actions.length - 1];
    if (!action) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    action.click();
  }, true);
})();
