#!/usr/bin/env node
/**
 * 选课入口的冒烟测试（node:vm，不启浏览器）。
 *
 * 起因：侧边栏「知识图谱 / 习题与测验」以前直接带一门默认课程进去，
 * 点进去人还没选课，平台已经替他选了。这一版改成先进 #/pick/<page>。
 * 这里盯三件事：
 *   1. 没选课程时，课程相关页面的链接必须是 #/pick/<page>，不能带课程 id；
 *   2. 选了课程后，仍是原来的 #/course/<id>/<page>，深链不断；
 *   3. 路由解析：#/pick/exercises 要能解析成 page=pick, want=exercises。
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');

const store = new Map();
const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  setInterval: () => 1,
  clearInterval: () => {},
  URLSearchParams,
  location: { hash: '#/catalog' },
  localStorage: {
    getItem: key => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => store.set(key, String(value)),
    removeItem: key => store.delete(key)
  },
  document: { querySelectorAll: () => [], getElementById: () => null }
};
sandbox.window = sandbox;
sandbox.window.COURSE_DATA = [
  {
    id: 'analysis-ii', title: '数学分析II', status: 'ready', subject: '专业课程',
    categoryLabel: '基础课程', direction: '分析', description: '不定积分与级数',
    stats: { sections: 10, knowledge: 90, exercises: 349 },
    nodes: [], exercises: [], files: []
  },
  {
    id: 'analysis', title: '数学分析I', status: 'ready', subject: '专业课程',
    categoryLabel: '基础课程', direction: '分析', description: '极限与连续',
    stats: { sections: 8, knowledge: 70, exercises: 300 },
    nodes: [], exercises: [], files: []
  }
];
vm.createContext(sandbox);

const bundle = ['js/course-catalog.js', 'js/core.js', 'js/visual-identity.js', 'js/pick-view.js']
  .map(file => fs.readFileSync(path.join(root, file), 'utf8')).join('\n;\n');
vm.runInContext(bundle, sandbox, { filename: 'bundle.js' });

const App = sandbox.window.App;
const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};
const eq = (name, actual, expected) => check(name, actual === expected, `${actual}（期望 ${expected}）`);

console.log('链接规则：');

eq('没选课程时，知识图谱指向选课页', App.url('graph', null), '#/pick/graph');
eq('没选课程时，习题指向选课页', App.url('exercises', null), '#/pick/exercises');
eq('没选课程时，AI 助手指向选课页', App.url('ai', null), '#/pick/ai');
eq('没选课程时，课程大纲指向选课页', App.url('outline', null), '#/pick/outline');
eq('课程中心仍是一级入口', App.url('catalog', null), '#/catalog');
eq('我的学习不受影响', App.url('study', null), '#/study');
eq('课程内容不受影响', App.url('content', null), '#/content');
eq('选了课程就是深链', App.url('graph', 'analysis-ii'), '#/course/analysis-ii/graph');
eq('深链带参数', App.url('outline', 'analysis-ii', { node: 'n-1' }), '#/course/analysis-ii/outline?node=n-1');
eq('课程 id 会被转义', App.url('graph', 'x y'), '#/course/x%20y/graph');

console.log('\n路由解析：');

let route = App.parseHash('#/pick/exercises');
eq('选课页的 page', route.page, 'pick');
eq('选课页记住想去的页面', route.want, 'exercises');
eq('选课页没有课程 id', route.courseId, '');

route = App.parseHash('#/pick');
eq('只写 #/pick 默认去知识图谱', route.want, 'graph');

route = App.parseHash('#/course/analysis-ii/graph');
eq('深链识别为课程路由', route.isCourse, true);
eq('深链的课程 id', route.courseId, 'analysis-ii');
eq('深链的页面', route.page, 'graph');

route = App.parseHash('#/course/analysis-ii');
eq('课程根路径默认课程概览', route.page, 'overview');

route = App.parseHash('');
eq('空 hash 回课程中心', route.page, 'catalog');

route = App.parseHash('#/graph');
eq('老书签 #/graph 解析成课程页', route.page, 'graph');
eq('老书签转成选课页并记住功能', App.normalizeRoute(route).want, 'graph');
eq('老书签不会带课程 id', App.normalizeRoute(route).page, 'pick');

route = App.parseHash('#/exercises');
eq('老书签 #/exercises 也进选课页', App.normalizeRoute(route).want, 'exercises');

route = App.parseHash('#/course/analysis-ii/graph');
eq('有课程的深链保持原样', App.normalizeRoute(route).page, 'graph');
eq('有课程时不改成选课页', App.normalizeRoute(route).want, undefined);

route = App.parseHash('#/catalog');
eq('一级页面不受影响', App.normalizeRoute(route).page, 'catalog');

route = App.parseHash('#/catalog?x=1');
eq('带查询参数的一级页', route.page, 'catalog');
eq('查询参数保留', route.params.get('x'), '1');

console.log('\n选课页内容：');

App.route = { page: 'pick', want: 'graph', params: new URLSearchParams() };
let html = App.views.pick();
check('标题写明要进入哪个功能', html.includes('选择课程 · 知识图谱'), '');
check('说明先选课程', html.includes('先选一门课程'));
check('卡片链到该课程的图谱', html.includes('#/course/analysis-ii/graph'), '');
check('已接入分组直接展示', html.includes('已接入'));
check('待接入课程收进 details', html.includes('待接入') && html.includes('pick-more'));
check('待接入课程不给假入口（指向课程概览）', html.includes('#/course/algebra/overview'));
check('带课程规模（章/知识点/习题）', html.includes('349'), '');
check('有搜索框', html.includes('data-input="pick-filter"'));
check('没有把默认课程写进链接', !html.includes('#/course/undefined') && !html.includes('#/course//'), '');

App.route = { page: 'pick', want: 'exercises', params: new URLSearchParams() };
html = App.views.pick();
check('习题入口的标题随之变化', html.includes('选择课程 · 习题与测验'));
check('标题说明作答后才展开解答', html.includes('作答后可展开完整解答'));
check('卡片链到习题页', html.includes('#/course/analysis-ii/exercises'));
check('所有卡片都指向同一个功能页',
  (html.match(/#\/course\/[^"]+\/(graph|exercises)/g) || []).every(href => href.endsWith('/exercises')));

console.log('\n当前课程条：');

check('没有课程时不渲染课程条', App.courseBar() === '', App.courseBar().slice(0, 40));
App.course = { id: 'analysis-ii', title: '数学分析II', status: 'ready',
  stats: { sections: 10, knowledge: 90, exercises: 349 } };
App.route = { page: 'graph', params: new URLSearchParams() };
let bar = App.courseBar();
check('有课程时渲染课程条', bar.includes('course-bar') && bar.includes('数学分析II'));
check('课程条写明规模', bar.includes('10 章') && bar.includes('349 道习题'));
check('课程条的本课链接带课程 id', bar.includes('href="#/course/analysis-ii/outline"'));
check('课程条高亮当前页', bar.includes('course-bar-link active'));
check('换课程回选课页并记住当前功能', bar.includes('href="#/pick/graph"'), '');
App.route = { page: 'exercises', params: new URLSearchParams() };
check('换课程跟随当前功能', App.courseBar().includes('href="#/pick/exercises"'));
App.course = { id: 'homology', title: '同调论', status: 'planned', stats: {} };
check('待接入课程标注内容待接入', App.courseBar().includes('内容待接入'));
App.course = null;

console.log('\n上次浏览：');

App.rememberCourse('analysis');
eq('记住课程', App.lastCourse(), 'analysis');
App.route = { page: 'pick', want: 'graph', params: new URLSearchParams() };
html = App.views.pick();
check('显示上次浏览', html.includes('上次浏览') && html.includes('数学分析I'), '');
check('提供继续入口', html.includes('#/course/analysis/graph'));
check('不自动跳转（模块加载不改变 hash）', sandbox.location.hash === '#/catalog', sandbox.location.hash);

App.rememberCourse('');
eq('空 id 不覆盖记忆', App.lastCourse(), 'analysis');

console.log('');
if (failures.length) {
  console.log(`失败 ${failures.length} 项：${failures.join(', ')}`);
  process.exit(1);
}
console.log('选课入口测试全部通过。');
