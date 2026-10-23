const assert = require('node:assert/strict');
const fs = require('node:fs');
const Catalog = require('../js/course-catalog.js');
const Construction = require('../js/construction-model.js');
const Covers = require('../js/course-cover-art.js');
const Graph = require('../js/graph-model.js');
const Atlas = require('../js/graph-atlas.js');
const course = require('../data/course-symplectic-math-physics.json');
const expected = '高等代数,数学分析I,数学分析II,数学分析III,抽象代数,解析几何,点集拓扑,常微分方程,复变函数,概率论,实变函数,泛函分析,偏微分方程,调和分析,辛几何与数学物理,同调论,微分几何,黎曼几何,微分流形,交换代数,数论基础,代数几何,同调代数,群与表示,数学模型,金融数学,运筹学,随机过程,数理统计,风险管理,随机分析,高维概率,数值线代,数值分析,机器学习,神经网络'.split(',');
const before = JSON.stringify(course), all = Catalog.merge([course]);
assert.deepEqual(all.map(item => item.title), expected);
assert.equal(new Set(all.map(item => item.id)).size, 36);
assert.deepEqual(Object.keys(Covers.designs).sort(), Catalog.entries.map(item => item.id).sort());
assert.equal(new Set(Object.values(Covers.designs).map(item => item[0])).size, 36);
Catalog.entries.forEach(item => {
  const graphic = Covers.render(item);
  assert(graphic.includes('<svg') && !/NaN|undefined/.test(graphic), `封面图形无效：${item.title}`);
});
// 按学科方向分组：一门课的方向写在课程上，不靠数组下标推
assert.equal(all.length, 36);
assert.deepEqual(Catalog.groups.map(group => group.id),
  ['analysis', 'algebra', 'geometry', 'pde', 'probability', 'computing']);
assert.equal(all.filter(item => item.groupId === 'analysis').length, 7);
assert.equal(all.filter(item => item.groupId === 'algebra').length, 6);
assert.equal(all.filter(item => item.groupId === 'geometry').length, 8);
assert.equal(all.filter(item => item.groupId === 'pde').length, 3);
assert.equal(all.filter(item => item.groupId === 'probability').length, 7);
assert.equal(all.filter(item => item.groupId === 'computing').length, 5);
assert(!all.some(item => /一期|二期/.test(`${item.subject}${item.categoryLabel}${item.direction}`)),
  '课程分类里还残留建设批次');
assert.deepEqual(all.filter(item => item.status === 'ready').map(item => item.id), [course.id]);
assert.equal(all[14].groupId, 'geometry');  // 辛几何与数学物理
const alias = Catalog.merge([{ id: 'imported-analysis', title: '数学分析 Ⅰ', status: 'ready', subject: '错误旧分类' }]);
assert.equal(alias[1].id, 'imported-analysis');
assert.equal(alias[1].subject, '分析');
const extra = Catalog.merge([{ id: 'extra', title: '另一个已导入课程', status: 'ready' }]);
assert.equal(extra.length, 37); assert.equal(extra.at(-1).catalogPlanned, false);
assert.equal(extra.at(-1).groupId, 'imported');
const renamed = Catalog.merge([{ ...course, title: '辛几何与数学物理·新名称' }]);
assert.equal(renamed[14].title, '辛几何与数学物理·新名称');
const atlas = Atlas.render(course, value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;'));
assert.equal((atlas.match(/class="atlas-point"/g) || []).length, course.stats.knowledge);
assert.equal((atlas.match(/class="atlas-chapter"/g) || []).length, course.stats.sections);
const allowed = new Set(['a', 'b', 'c']);
const links = Graph.sharedEdges([{ id: 'q1', nodeIds: ['a', 'b'] }, { id: 'q2', nodeIds: ['b', 'c'] }, { id: 'q3', nodeIds: ['a', 'a', 'b', 'outside'] }], allowed);
assert.deepEqual(links, [{ source: 'a', target: 'b', type: 'shared_exercise', exerciseIds: ['q1', 'q3'] }, { source: 'b', target: 'c', type: 'shared_exercise', exerciseIds: ['q2'] }]);
const branch = Graph.subtree({ nodes: [
  { id: 'root', level: 1, children: ['parent'], exerciseIds: [] },
  { id: 'parent', level: 2, children: ['a', 'b'], exerciseIds: ['direct'] },
  { id: 'a', level: 3, children: [], exerciseIds: ['shared', 'only-a'] },
  { id: 'b', level: 3, children: [], exerciseIds: ['shared'] }
] }, 'parent');
assert.deepEqual(branch.leaves.map(node => node.id), ['a', 'b']);
assert.deepEqual(branch.exerciseIds, ['direct', 'shared', 'only-a']);
const graph = Graph.layout(Graph.build(course, 'chapter-08'));
assert(graph.nodes.every(node => Number.isFinite(node.x) && Number.isFinite(node.y)));
assert(graph.height > 620);
assert(graph.nodes.slice(1).every((node, index, nodes) => nodes.slice(index + 1).every(other => Math.hypot(node.x - other.x, node.y - other.y) >= 100)));
const fullGraph = Graph.layout(Graph.buildAll(course));
assert.equal(fullGraph.nodes.length, course.stats.nodes);
assert.equal(fullGraph.sharedCount, 55);
assert(fullGraph.nodes.every(node => Number.isFinite(node.x) && Number.isFinite(node.y)));
assert(fullGraph.nodes.slice(1).every((node, index, nodes) => nodes.slice(index + 1).every(other => Math.hypot(node.x - other.x, node.y - other.y) >= 40)));
const imported = fs.readdirSync('data').filter(name => /^course-.*\.json$/.test(name)).map(name => JSON.parse(fs.readFileSync(`data/${name}`, 'utf8')));
const active = Catalog.merge(imported);
const summary = Construction.summarize(active);
const requiredIds = ['course-symplectic-math-physics', 'pde', 'real-analysis', 'numerical-analysis', 'numerical-linear-algebra', 'machine-learning', 'functional-analysis', 'neural-networks', 'harmonic-analysis', 'high-dimensional-probability'];
assert(requiredIds.every(id => imported.some(item => item.id === id)));
assert.equal(new Set(imported.map(item => item.id)).size, imported.length);
assert.equal(active.length, 36 + active.filter(item => !item.catalogPlanned).length);
assert.equal(active.filter(item => item.status === 'ready').length, imported.length);
assert.equal(summary.plannedTotal, Catalog.entries.length);
assert.equal(summary.ready.length, imported.length);
assert.equal(summary.directions.reduce((sum, item) => sum + item.ready, 0) + summary.additionalReady, summary.ready.length);
assert.equal(summary.totals.knowledge, imported.flatMap(item => item.nodes.filter(node => node.kind === 'knowledge')).length);
assert.equal(summary.totals.exercises, imported.reduce((sum, item) => sum + item.exercises.length, 0));
assert(summary.totals.coveredKnowledge <= summary.totals.knowledge);
assert.equal(active.filter(item => item.groupId === 'analysis').length, 7);
assert.equal(active.filter(item => item.groupId === 'computing').length, 5);
imported.forEach(item => {
  const root = item.nodes.find(node => node.level === 1);
  const descendants = Graph.subtree(item, root.id);
  assert.equal(descendants.leaves.length, item.stats.knowledge);
  assert.equal(descendants.exerciseIds.length, item.stats.exercises);
  const network = Graph.layout(Graph.buildAll(item));
  assert.equal(network.nodes.length, item.stats.nodes);
  assert(network.nodes.every(node => Number.isFinite(node.x) && Number.isFinite(node.y)));
  assert(network.nodes.slice(1).every((node, index, nodes) => nodes.slice(index + 1).every(other => Math.hypot(node.x - other.x, node.y - other.y) >= 34)));
});
assert(graph.edges.every(edge => ['contains', 'shared_exercise'].includes(edge.type)));
graph.edges.filter(edge => edge.type === 'shared_exercise').forEach(edge => edge.exerciseIds.forEach(id => {
  const exercise = course.exercises.find(item => item.id === id);
  assert(exercise.nodeIds.includes(edge.source) && exercise.nodeIds.includes(edge.target));
}));
assert.equal(JSON.stringify(course), before, '目录或布局算法不得改写课程内容。');
const result = { plannedCourses: 36, firstPhase: 10, secondPhase: 26, readyCourses: imported.length, importedCourses: imported.length, totalKnowledge: summary.totals.knowledge, totalExercises: summary.totals.exercises, coveredKnowledge: summary.totals.coveredKnowledge, pendingReview: summary.totals.pendingReview, variantGroups: summary.totals.variantGroups, duplicatePaths: summary.totals.duplicatePaths, preservedOrder: true, fullCourseKnowledgePoints: course.stats.knowledge, fullCourseSections: course.stats.sections, fullGraphNodes: fullGraph.nodes.length, fullGraphSharedEdges: fullGraph.sharedCount, graphChapter: '第8章 辛流形', nodes: graph.nodes.length, sharedExerciseEdges: graph.sharedCount, inventedPrerequisiteEdges: 0, sourceDataUnchanged: true };
fs.writeFileSync('reports/catalog-graph-validation.json', JSON.stringify(result, null, 2) + '\n');
console.log(JSON.stringify(result, null, 2));
