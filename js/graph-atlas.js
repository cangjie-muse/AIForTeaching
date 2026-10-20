(function (root) {
  'use strict';
  function render(course, escape) {
    const chapters = course.nodes.filter(node => node.level === 2);
    const jump = chapters.map((chapter, index) => `<button data-action="atlas-jump" data-id="${escape(chapter.id)}">${String(index + 1).padStart(2, '0')} ${escape(chapter.title)}</button>`).join('');
    const cards = chapters.map((chapter, index) => {
      const points = course.nodes.filter(node => node.kind === 'knowledge' && node.chapterId === chapter.id);
      const leaves = points.map(node => `<button class="atlas-point" data-action="read-node" data-id="${escape(node.id)}"><span class="atlas-point-name">${escape(node.title)}</span><small>${node.exerciseIds.length ? `${node.exerciseIds.length} 题` : '暂无题'}</small></button>`).join('');
      return `<section class="atlas-chapter" id="atlas-${escape(chapter.id)}"><button class="atlas-chapter-head" data-action="atlas-chapter" data-id="${escape(chapter.id)}"><span class="atlas-chapter-index">${String(index + 1).padStart(2, '0')}</span><span><b>${escape(chapter.title)}</b><small>${points.length} 个知识点 · 点击进入章节关系图</small></span><span class="atlas-open">↗</span></button><div class="atlas-points">${leaves}</div></section>`;
    }).join('');
    return `<div class="atlas-map"><div class="atlas-intro"><span class="atlas-orbit" aria-hidden="true">✦</span><div><strong>${escape(course.title)}</strong><p>${chapters.length} 个章节与附录 · ${course.stats.knowledge} 个知识点。沿章节主线逐层浏览，点选名称可直接阅读。</p></div></div><div class="atlas-jump"><span>快速定位</span>${jump}</div><div class="atlas-grid">${cards}</div></div>`;
  }
  const api = { render };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.GraphAtlas = api;
})(typeof window === 'undefined' ? globalThis : window);
