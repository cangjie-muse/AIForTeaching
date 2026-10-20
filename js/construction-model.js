(function (root) {
  'use strict';
  function duplicatePathGroups(nodes) {
    const seen = new Set(), repeated = new Set();
    nodes.forEach(node => {
      const path = JSON.stringify(node.path);
      if (seen.has(path)) repeated.add(path);
      else seen.add(path);
    });
    return repeated.size;
  }
  function summarize(courses) {
    const planned = courses.filter(course => course.catalogPlanned);
    const ready = courses.filter(course => course.status === 'ready' && Array.isArray(course.nodes));
    const knowledge = ready.flatMap(course => course.nodes.filter(node => node.kind === 'knowledge'));
    const totals = {
      chapters: ready.reduce((sum, course) => sum + (course.stats?.sections || 0), 0),
      knowledge: knowledge.length,
      exercises: ready.reduce((sum, course) => sum + (course.stats?.exercises || 0), 0),
      associations: ready.reduce((sum, course) => sum + (course.stats?.associations || 0), 0),
      coveredKnowledge: knowledge.filter(node => node.exerciseIds.length).length,
      pendingReview: ready.filter(course => course.reviewStatus === '待教师审核').length,
      variantGroups: ready.reduce((sum, course) => sum + (course.stats?.variantGroups || 0), 0),
      duplicatePaths: ready.reduce((sum, course) => sum + duplicatePathGroups(course.nodes), 0)
    };
    const phase = ['phase-1', 'phase-2'].map(id => ({
      id, total: planned.filter(course => course.groupId === id).length,
      ready: ready.filter(course => course.catalogPlanned && course.groupId === id).length
    }));
    return { plannedTotal: planned.length, plannedReady: ready.filter(course => course.catalogPlanned).length, ready, pending: planned.filter(course => course.status !== 'ready'), additionalReady: ready.filter(course => !course.catalogPlanned).length, phase, totals };
  }
  const api = { summarize };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.ConstructionModel = api;
})(typeof window === 'undefined' ? globalThis : window);
