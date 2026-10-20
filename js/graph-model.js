(function (root) {
  'use strict';
  function sharedEdges(exercises, allowed) {
    const pairs = new Map();
    exercises.forEach(exercise => {
      const ids = [...new Set(exercise.nodeIds)].filter(id => allowed.has(id)).sort();
      ids.forEach((source, i) => ids.slice(i + 1).forEach(target => {
        const key = JSON.stringify([source, target]);
        const edge = pairs.get(key) || { source, target, type: 'shared_exercise', exerciseIds: [] };
        edge.exerciseIds.push(exercise.id); pairs.set(key, edge);
      }));
    });
    return [...pairs.values()];
  }
  function build(course, focusId) {
    const focus = course.nodes.find(node => node.id === focusId) || course.nodes.find(node => node.level === 1);
    const children = course.nodes.filter(node => node.parentId === focus.id);
    const nodes = [focus, ...children].map((node, i) => ({ id: node.id, title: node.title, kind: node.kind, level: node.level, chapterId: node.chapterId, isRoot: i === 0, appendix: node.title.startsWith('附录'), exerciseCount: node.exerciseIds.length, x: 0, y: 0 }));
    const allowed = new Set(nodes.map(node => node.id));
    const contains = course.relationships.filter(edge => edge.type === 'contains' && allowed.has(edge.source) && allowed.has(edge.target)).map(edge => ({ ...edge, exerciseIds: [] }));
    const shared = sharedEdges(course.exercises, allowed);
    return { scope: 'local', focusId: focus.id, parentId: focus.parentId, nodes, edges: [...contains, ...shared], sharedCount: shared.length };
  }
  function buildAll(course) {
    const rootNode = course.nodes.find(node => node.level === 1);
    const nodes = course.nodes.map(node => ({ id: node.id, title: node.title, kind: node.kind, level: node.level, chapterId: node.chapterId, isRoot: node.id === rootNode.id, appendix: node.title.startsWith('附录'), exerciseCount: node.exerciseIds.length, x: 0, y: 0 }));
    const shared = sharedEdges(course.exercises, new Set(nodes.map(node => node.id)));
    return { scope: 'all', focusId: rootNode.id, parentId: null, nodes, edges: [...course.relationships.map(edge => ({ ...edge, exerciseIds: [] })), ...shared], sharedCount: shared.length };
  }
  function subtree(course, focusId) {
    const lookup = new Map(course.nodes.map(node => [node.id, node]));
    const focus = lookup.get(focusId) || course.nodes.find(node => node.level === 1);
    const nodes = [], leaves = [], exerciseIds = new Set(), visited = new Set(), stack = [focus.id];
    while (stack.length) {
      const node = lookup.get(stack.pop());
      if (!node || visited.has(node.id)) continue;
      visited.add(node.id); nodes.push(node);
      node.exerciseIds.forEach(id => exerciseIds.add(id));
      if (node.children.length) stack.push(...node.children.slice().reverse());
      else leaves.push(node);
    }
    return { focusId: focus.id, nodes, leaves, exerciseIds: [...exerciseIds] };
  }
  function layoutLocal(graph) {
    const width = 1100, height = 1100, center = 550, count = graph.nodes.length - 1;
    const rings = count > 26 ? [210, 335, 450] : count > 14 ? [270, 440] : [360];
    const total = rings.reduce((sum, radius) => sum + radius, 0);
    const allocations = rings.map((radius, index) => index === rings.length - 1 ? 0 : Math.round(count * radius / total));
    allocations[allocations.length - 1] = count - allocations.reduce((sum, amount) => sum + amount, 0);
    const nodes = [{ ...graph.nodes[0], x: center, y: center }]; let offset = 1;
    rings.forEach((radius, tier) => {
      const amount = allocations[tier];
      for (let index = 0; index < amount; index += 1) {
        const angle = -Math.PI / 2 + 2 * Math.PI * index / amount + tier * .17;
        nodes.push({ ...graph.nodes[offset + index], x: center + radius * Math.cos(angle), y: center + radius * Math.sin(angle) });
      }
      offset += amount;
    });
    return { ...graph, nodes, width, height, rings };
  }
  function layoutAll(graph) {
    const width = 3600, height = 3600, center = 1800, rings = [700, 1120, 1380, 1640];
    const nodes = graph.nodes.map(node => ({ ...node })), lookup = new Map(nodes.map(node => [node.id, node]));
    const chapters = nodes.filter(node => node.level === 2);
    const groups = chapters.map(chapter => ({ chapter, points: nodes.filter(node => node.chapterId === chapter.id && node.level > 2) }));
    const totalWeight = groups.reduce((sum, group) => sum + Math.max(7, group.points.length), 0);
    let progress = -Math.PI / 2;
    const rootNode = nodes.find(node => node.isRoot); rootNode.x = center; rootNode.y = center;
    groups.forEach(group => {
      const span = 2 * Math.PI * Math.max(7, group.points.length) / totalWeight;
      const angle = progress + span / 2, chapter = lookup.get(group.chapter.id);
      chapter.x = center + rings[0] * Math.cos(angle); chapter.y = center + rings[0] * Math.sin(angle);
      const slots = Math.ceil(group.points.length / 3);
      group.points.forEach((point, index) => {
        const tier = index % 3, slot = Math.floor(index / 3);
        const theta = progress + span * (slot + .5) / slots + (tier - 1) * .009;
        point.x = center + rings[tier + 1] * Math.cos(theta);
        point.y = center + rings[tier + 1] * Math.sin(theta);
      });
      progress += span;
    });
    return { ...graph, nodes, width, height, rings };
  }
  const layout = graph => graph.scope === 'all' ? layoutAll(graph) : layoutLocal(graph);
  const api = { build, buildAll, layout, sharedEdges, subtree };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.GraphModel = api;
})(typeof window === 'undefined' ? globalThis : window);
