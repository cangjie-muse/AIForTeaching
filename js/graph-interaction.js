(function () {
  const A = App, G = A.network;
  function position(svg, event) {
    return new DOMPoint(event.clientX, event.clientY).matrixTransform(svg.getScreenCTM().inverse());
  }
  G.zoom = function (factor, pivot) {
    const scale = Math.max(G.fitScale, Math.min(G.fitScale * 12, G.camera.scale * factor));
    const ratio = scale / G.camera.scale;
    G.camera.x = pivot.x - (pivot.x - G.camera.x) * ratio;
    G.camera.y = pivot.y - (pivot.y - G.camera.y) * ratio;
    G.camera.scale = scale; G.applyCamera();
  };
  function startDrag(svg, event) {
    if (event.button !== 0) return;
    const nodeId = event.target.closest('[data-node-id]')?.dataset.nodeId;
    const node = G.current.nodes.find(item => item.id === nodeId);
    const point = position(svg, event);
    const edgeIndex = event.target.closest('[data-edge-index]')?.dataset.edgeIndex;
    G.drag = { pointer: event.pointerId, point, moved: false, nodeId, edgeIndex, node: node && !node.isRoot ? node : null, original: node && !node.isRoot ? { x: node.x, y: node.y } : { x: G.camera.x, y: G.camera.y } };
    svg.setPointerCapture(event.pointerId);
  }
  function moveDrag(svg, event) {
    if (!G.drag || G.drag.pointer !== event.pointerId) return;
    const point = position(svg, event), dx = point.x - G.drag.point.x, dy = point.y - G.drag.point.y;
    if (Math.hypot(dx, dy) < 4 && !G.drag.moved) return;
    G.drag.moved = true; svg.classList.add('is-dragging');
    if (G.drag.node) {
      G.drag.node.x = Math.max(80, Math.min(G.current.width - 80, G.drag.original.x + dx / G.camera.scale));
      G.drag.node.y = Math.max(80, Math.min(G.current.height - 80, G.drag.original.y + dy / G.camera.scale));
      G.updateGeometry();
    } else {
      G.camera.x = G.drag.original.x + dx; G.camera.y = G.drag.original.y + dy; G.applyCamera();
    }
  }
  function finishDrag(svg, event) {
    if (!G.drag || G.drag.pointer !== event.pointerId) return;
    const drag = G.drag;
    G.suppressClick = Date.now() + 250;
    G.drag = null; svg.classList.remove('is-dragging');
    if (svg.hasPointerCapture(event.pointerId)) svg.releasePointerCapture(event.pointerId);
    if (drag.moved || event.type !== 'pointerup') return;
    if (drag.nodeId) G.select(drag.nodeId);
    else if (drag.edgeIndex != null) G.inspectEdge(Number(drag.edgeIndex));
  }
  function activate(event) {
    if (Date.now() < (G.suppressClick || 0)) return;
    const nodeId = event.target.closest('[data-node-id]')?.dataset.nodeId;
    if (nodeId) { G.select(nodeId); return; }
    const edgeIndex = event.target.closest('[data-edge-index]')?.dataset.edgeIndex;
    if (edgeIndex != null) G.inspectEdge(Number(edgeIndex));
  }
  function keys(event) {
    const id = event.target.closest('[data-node-id]')?.dataset.nodeId;
    if (id && ['Enter', ' '].includes(event.key)) { event.preventDefault(); G.select(id); return; }
    if (['+', '='].includes(event.key)) { event.preventDefault(); G.zoom(1.25, { x: G.current.width / 2, y: G.current.height / 2 }); }
    if (event.key === '-') { event.preventDefault(); G.zoom(.8, { x: G.current.width / 2, y: G.current.height / 2 }); }
    if (event.key === 'Escape') { G.focusOnly = false; G.highlight(null); }
  }
  G.bindInteractions = function () {
    const svg = document.getElementById('network-svg');
    G.drag = null;
    svg.addEventListener('pointerdown', event => startDrag(svg, event));
    svg.addEventListener('pointermove', event => moveDrag(svg, event));
    svg.addEventListener('pointerup', event => finishDrag(svg, event));
    svg.addEventListener('pointercancel', event => finishDrag(svg, event));
    svg.addEventListener('click', activate);
    svg.addEventListener('dblclick', event => {
      const id = event.target.closest('[data-node-id]')?.dataset.nodeId;
      if (id && !A.node(id).children.length) A.actions['read-node']({ dataset: { id } });
    });
    svg.addEventListener('pointerover', event => {
      const id = event.target.closest('[data-node-id]')?.dataset.nodeId;
      if (!G.drag) G.highlight(id === G.current.focusId ? null : id || null);
    });
    svg.addEventListener('pointerleave', () => { if (!G.drag) G.highlight(null); });
    svg.addEventListener('wheel', event => {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault(); G.zoom(Math.exp(-Math.max(-100, Math.min(100, event.deltaY)) * .003), position(svg, event));
    }, { passive: false });
    svg.addEventListener('keydown', keys);
  };
})();
