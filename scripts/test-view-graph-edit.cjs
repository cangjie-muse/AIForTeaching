#!/usr/bin/env node
/**
 * 知识图谱页的"结构工作台"部分：编辑入口、审核状态位、深链。
 *
 * 画布本身要在真浏览器里看（SVG 布局、缩放），这里只验页面骨架与动作分发——
 * 也就是"点编辑会不会带着正确的课程打开面板""学生会不会看到编辑入口"这类会错的地方。
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');

const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  URLSearchParams,
  location: { hash: '#/graph/analysis-ii' },
  fetch: () => Promise.reject(new Error('测试不请求后端')),
  localStorage: {
    store: new Map(),
    getItem(key) { return this.store.has(key) ? this.store.get(key) : null; },
    setItem(key, value) { this.store.set(key, String(value)); }
  },
  document: {
    getElementById: () => null,
    addEventListener() {},
    querySelectorAll: () => [],
    body: { classList: { add() {}, remove() {} } }
  }
};
sandbox.window = sandbox;
vm.createContext(sandbox);

const bundle = ['js/core.js', 'js/visual-identity.js', 'js/api-client.js',
  'js/graph-model.js', 'js/graph-view.js']
  .map(file => fs.readFileSync(path.join(root, file), 'utf8')).join('\n;\n');
vm.runInContext(bundle, sandbox, { filename: 'bundle.js' });

const App = sandbox.window.App;
App.render = () => {};
const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};

const chapter = {
  id: 'ch-1', title: '第一章 极限', level: 2, parentId: 'root', kind: 'container',
  path: ['数学分析 II', '第一章 极限'], children: [{ id: 'k-2' }], exerciseIds: []
};
const leaf = {
  id: 'k-2', title: '函数极限', level: 3, parentId: 'ch-1', kind: 'knowledge',
  path: ['数学分析 II', '第一章 极限', '函数极限'], children: [], exerciseIds: ['ex-1'],
  source: '教材 p.12', content: '函数极限的定义。'
};
const rootNode = {
  id: 'root', title: '数学分析 II', level: 1, parentId: null, kind: 'container',
  path: ['数学分析 II'], children: [chapter], exerciseIds: []
};
App.course = {
  id: 'analysis-ii', title: '数学分析 II', status: 'ready',
  nodes: [rootNode, chapter, leaf],
  stats: { knowledge: 1, exercises: 1, sections: 1 },
  exercises: [{ id: 'ex-1' }],
  relationships: []
};
App.courseMemory = () => ({});
App.node = id => App.course.nodes.find(node => node.id === id) || null;
App.state = App.state || {};
App.state.review = {
  nodePanel: '', nodeDetail: null, nodeDetailError: '', nodeCourse: '', filters: {}
};
App.route = { page: 'graph', params: new Map([['node', 'k-2']]), course: 'analysis-ii', query: {} };
App.actions['read-node'] = () => {};
App.actions['node-close'] = () => { App.state.review.nodePanel = ''; };
App.api.reviewGraph = async () => ({
  totals: { nodes: 3, verified: 1 },
  chapters: [{ chapter_id: 'ch-1', title: '第一章 极限', nodes: 2, verified: 1 }],
  nodes: [
    { id: 'ch-1', verified: true, exercise_count: 0 },
    { id: 'k-2', verified: true, exercise_count: 1 }
  ]
});

const calls = [];
App.reviewNode = {
  loadDetail(nodeId, course) {
    calls.push(['loadDetail', nodeId, course]);
    App.state.review.nodePanel = nodeId;
    App.state.review.nodeCourse = course;
  },
  panelFor(nodeId) {
    return App.state.review.nodePanel === nodeId
      ? `<div class="rv-node-panel">编辑面板：${nodeId}</div>` : '';
  }
};

async function shellTests() {
  console.log('图谱页骨架：');
  App.auth = { can: () => true };      // 教师 / 管理员
  let html = App.views.graph();
  check('有编辑面板的宿主', html.includes('id="network-edit"'));
  check('有"未审 N 个"的统计位', html.includes('id="network-review-summary"'));
  check('原有的浏览功能没被挤掉',
    html.includes('data-action="network-home"') && html.includes('data-action="network-view"'));

  App.auth = { can: () => false };     // 学生
  html = App.views.graph();
  check('学生看不到编辑宿主', !html.includes('id="network-edit"'));
  check('学生看不到审核统计位', !html.includes('id="network-review-summary"'));
}

async function actionTests() {
  console.log('\n编辑入口：');
  App.auth = { can: () => true };
  App.state.review.nodePanel = '';
  calls.length = 0;
  await App.actions['network-edit']({ dataset: { id: 'k-2' } });
  check('点编辑会带着图谱页的课程打开面板（不是审核页的筛选值）',
    calls.some(call => call[0] === 'loadDetail' && call[1] === 'k-2' && call[2] === 'analysis-ii'),
    JSON.stringify(calls[0] || null));
  await App.actions['network-edit']({ dataset: { id: 'k-2' } });
  check('再点一次同一个节点会收起面板（不重复打开）', App.state.review.nodePanel === '');

  App.auth = { can: () => false };
  calls.length = 0;
  await App.actions['network-edit']({ dataset: { id: 'k-2' } });
  check('学生点也打不开（前端闸门）', calls.length === 0, JSON.stringify(calls));
}

async function statusTests() {
  console.log('\n审核状态：');
  App.auth = { can: () => true };
  // G.open 要动真 DOM（画布、缩放），这里只验状态加载，所以把"画"这一步换成空函数。
  App.network.open = () => {};
  App.route.params = new Map();
  let asked = 0;
  App.api.reviewGraph = async courseId => {
    asked += 1;
    calls.push(['reviewGraph', courseId]);
    return { totals: { nodes: 3, verified: 1 }, chapters: [], nodes: [{ id: 'k-2', verified: true }] };
  };
  App.state.networkReview = null;
  App.state.networkReviewStale = true;
  App.after.graph();
  await new Promise(resolve => setTimeout(resolve, 10));
  check('取状态用图谱页的课程 id', calls.some(call => call[0] === 'reviewGraph' && call[1] === 'analysis-ii'));
  check('状态缓存在 A.state.networkReview 里',
    Boolean(App.state.networkReview && App.state.networkReview.byId.get('k-2')));
  App.state.networkReviewStale = false;
  const before = asked;
  App.after.graph();
  await new Promise(resolve => setTimeout(resolve, 10));
  check('同一门课不重复取（只在改过结构后重取）', asked === before, `${before} → ${asked}`);
}

(async () => {
  await shellTests();
  await actionTests();
  await statusTests();
  console.log();
  if (failures.length) {
    console.log(`失败 ${failures.length} 项：${failures.join('、')}`);
    process.exit(1);
  }
  console.log('图谱页结构工作台测试全部通过。');
})();
