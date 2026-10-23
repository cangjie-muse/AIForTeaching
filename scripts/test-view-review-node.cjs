#!/usr/bin/env node
/**
 * 节点管理 + 习题侧挂载：改标题、挂题、解绑、删节点，以及"挂载一变就退回未核"的后果提示。
 *
 * 这一组测的是四个写动作真的打到哪个接口、带什么参数，以及删除时那一步确认有没有被跳过——
 * 跳过确认就等于悄悄删掉一棵子树。
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');

const confirms = [];
let confirmAnswer = true;
let queryValue = '';

const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  URLSearchParams,
  location: { hash: '#/review' },
  fetch: () => Promise.reject(new Error('测试不请求后端')),
  localStorage: {
    store: new Map(),
    getItem(key) { return this.store.has(key) ? this.store.get(key) : null; },
    setItem(key, value) { this.store.set(key, String(value)); }
  },
  document: {
    getElementById: () => null,
    addEventListener() {},
    body: { classList: { add() {}, remove() {} } },
    querySelector: () => ({ value: queryValue })
  }
};
sandbox.window = sandbox;
sandbox.window.confirm = message => { confirms.push(message); return confirmAnswer; };
vm.createContext(sandbox);

const bundle = ['js/core.js', 'js/visual-identity.js', 'js/api-client.js',
  'js/review-scope.js', 'js/review-node.js', 'js/publish-panel.js', 'js/review.js']
  .map(file => fs.readFileSync(path.join(root, file), 'utf8')).join('\n;\n');
vm.runInContext(bundle, sandbox, { filename: 'bundle.js' });

const App = sandbox.window.App;
App.markdown = source => `<div class="prose">${source}</div>`;
App.render = () => {};
const toasts = [];
App.toast = message => toasts.push(message);
const calls = [];
App.api.get = async () => ({ items: [], counts: [], task_states: [] });
App.api.reviewGraph = async () => ({ totals: { nodes: 0, verified: 0 }, chapters: [], nodes: [] });
App.api.reviewLedger = async () => ({ items: [], counts: [], total: 0 });
App.api.nodeDetail = async (course, node) => {
  calls.push(['nodeDetail', course, node]);
  return {
    node: { id: node, title: '函数极限', kind: 'knowledge', level: 3, verified: false },
    parent: null, chapter: { id: 'ch-1', title: '第一章 极限' },
    children: [], exercises: [],
    move_targets: [
      { id: 'course-root', title: '数学分析 II', level: 1, path: ['数学分析 II'] },
      { id: 'ch-2', title: '第二章 微分', level: 2, path: ['数学分析 II', '第二章 微分'] }
    ],
    counts: { children: 0, descendants: 0, exercises: 0 }
  };
};

const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};

function reset(overrides = {}) {
  App.state.review = {
    status: 'ready', error: '',
    summary: { tiers: [], task_states: [], open_issues: [] },
    items: [], courses: [], checked: {}, diffs: {}, busy: false,
    tab: 'graph',
    filters: { course_id: 'analysis-ii', category: '', priority: '' },
    graph: { totals: { nodes: 1, verified: 0 }, chapters: [], nodes: [] },
    graphOpen: {}, nodePanel: '', nodeDetail: null, nodeDetailError: '',
    nodeTitle: '', nodePicker: { query: '', items: [], loading: false, error: '' },
    ledger: null, ledgerTier: '', ledgerQuery: '',
    verification: null, lookupId: '', open: null, nodeOptions: null,
    ...overrides
  };
  calls.length = 0;
  toasts.length = 0;
  confirms.length = 0;
  confirmAnswer = true;
  queryValue = '';
}

async function titleTests() {
  console.log('改标题：');
  reset();
  App.api.patchNode = async (course, node, body) => {
    calls.push(['patchNode', course, node, body]);
    return { exercises_reset: 2 };
  };
  App.state.review.nodeTitle = '  函数极限（新） ';
  await App.actions['node-save-title']({ dataset: { node: 'k-2' }, disabled: false });
  const patch = calls.find(call => call[0] === 'patchNode');
  check('提交去掉空白的标题并带上课程', patch && patch[2] === 'k-2'
    && patch[3].title === '函数极限（新）', JSON.stringify(patch || null));
  check('改标题后提示有几道题退回未核',
    toasts.some(text => text.includes('2 道题退回未核')), String(toasts[0] || ''));
  check('改完重新读一次节点（面板要显示新标题与新状态）',
    calls.some(call => call[0] === 'nodeDetail' && call[2] === 'k-2'));
}

async function mountTests() {
  console.log('\n挂题与解绑：');
  reset();
  App.api.exerciseOptions = async (course, query) => {
    calls.push(['exerciseOptions', course, query]);
    return { items: [{ id: 'ex-1', prompt_md: '求极限', tier: 'none', node_ids: [] }] };
  };
  App.state.review.nodePicker.query = 'ex-1';
  await App.actions['node-mount-search']({ dataset: { node: 'k-2' }, disabled: false });
  check('搜索候选题带上课程与关键词',
    calls.some(call => call[0] === 'exerciseOptions' && call[2] === 'ex-1'),
    JSON.stringify(calls.find(call => call[0] === 'exerciseOptions') || null));
  check('候选写进状态', App.state.review.nodePicker.items.length === 1);

  App.api.mountExercises = async (course, node, ids) => {
    calls.push(['mountExercises', course, node, ids]);
    return { attached: ids, reset: { exercises: 1, cleared: 1 } };
  };
  await App.actions['node-mount']({ dataset: { node: 'k-2', exercise: 'ex-1' }, disabled: false });
  check('挂题提交到节点侧接口',
    calls.some(call => call[0] === 'mountExercises' && call[2] === 'k-2'
      && call[3][0] === 'ex-1'), JSON.stringify(calls.filter(call => call[0] === 'mountExercises')));
  check('挂题后说明人工审核已撤销',
    toasts.some(text => text.includes('撤销')), String(toasts[0] || ''));

  reset();
  App.api.unmountExercise = async (course, node, exercise) => {
    calls.push(['unmountExercise', course, node, exercise]);
    return { detached: true, reset: { cleared: 1 } };
  };
  await App.actions['node-unmount']({ dataset: { node: 'k-2', exercise: 'ex-1' }, disabled: false });
  check('解绑先问一句（挂载没了，题也退回未核）', confirms.length === 1, String(confirms[0] || ''));
  check('确认后走解绑接口', calls.some(call => call[0] === 'unmountExercise'));

  reset();
  confirmAnswer = false;
  App.api.unmountExercise = async () => { calls.push(['unmountExercise']); return {}; };
  await App.actions['node-unmount']({ dataset: { node: 'k-2', exercise: 'ex-1' }, disabled: false });
  check('取消确认就不发请求', !calls.some(call => call[0] === 'unmountExercise'));
}

async function deleteTests() {
  console.log('\n删节点：');
  reset();
  const conflict = Object.assign(new Error('这个节点还有子节点或挂载的题'), {
    status: 409,
    detail: { title: '函数极限', nodes: 3, exercises: 5 }
  });
  let attempts = 0;
  App.api.deleteNode = async (course, node, cascade) => {
    attempts += 1;
    calls.push(['deleteNode', course, node, cascade]);
    if (!cascade) throw conflict;
    return { deleted: 3, detached_exercises: 5, reset: { cleared: 5 } };
  };
  await App.actions['node-delete']({ dataset: { node: 'k-2' }, disabled: false });
  check('有连带时先问，并把节点数/题数摆出来',
    confirms.length === 1 && confirms[0].includes('3 个节点') && confirms[0].includes('5 道'),
    String(confirms[0] || ''));
  check('确认后带 cascade 重发',
    calls.some(call => call[0] === 'deleteNode' && call[3] === true), JSON.stringify(calls));
  check('删完报连带范围',
    toasts.some(text => text.includes('3 个节点')), String(toasts[0] || ''));
  check('删完关掉面板', App.state.review.nodePanel === '');

  reset();
  confirmAnswer = false;
  attempts = 0;
  App.api.deleteNode = async (course, node, cascade) => {
    attempts += 1;
    calls.push(['deleteNode', cascade]);
    if (!cascade) throw conflict;
    return { deleted: 3, detached_exercises: 0, reset: { cleared: 0 } };
  };
  await App.actions['node-delete']({ dataset: { node: 'k-2' }, disabled: false });
  check('不确认就不删（只试了一次）', attempts === 1, String(attempts));
}

async function moveTests() {
  console.log('\n移动到别的章：');
  reset();
  App.api.patchNode = async (course, node, body) => {
    calls.push(['patchNode', course, node, body]);
    return {
      id: node, title: '函数极限', parent_id: body.parent_id, level: 2,
      exercises_reset: 3,
      move: { from_parent: 'ch-1', parent_id: body.parent_id, level: 2, nodes: 2, descendants: 1 }
    };
  };
  await App.reviewNode.loadDetail('k-2', 'analysis-ii');
  let html = App.reviewNode.panelFor('k-2');
  check('面板给「移到」下拉与移动按钮',
    html.includes('data-input="node-move-target"') && html.includes('移动到这里'));
  check('候选里列出别的容器节点',
    html.includes('第二章 微分') && html.includes('value="ch-2"'));
  check('候选里不含自己（挪到自己下面会成环）',
    App.state.review.nodeDetail.move_targets.every(row => row.id !== 'k-2'));

  App.state.review.nodeMoveTarget = '';
  calls.length = 0;
  await App.actions['node-move']({ dataset: { node: 'k-2' }, disabled: false });
  check('没选去处就不发请求', calls.length === 0, JSON.stringify(calls));
  check('并且说清为什么没动', toasts.some(text => text.includes('先选一个去处')));

  App.state.review.nodeMoveTarget = 'ch-2';
  calls.length = 0;
  toasts.length = 0;
  await App.actions['node-move']({ dataset: { node: 'k-2' }, disabled: false });
  const patch = calls.find(call => call[0] === 'patchNode');
  check('移动只提交 parent_id（不是整份节点）',
    patch && patch[3].parent_id === 'ch-2' && Object.keys(patch[3]).length === 1,
    JSON.stringify(patch || null));
  check('移动后说明整棵子树的审核被撤销',
    toasts.some(text => text.includes('2 个节点的审核已撤销')), String(toasts[0] || ''));
  check('移动后重新读一次节点', calls.some(call => call[0] === 'nodeDetail'));

  reset();
  App.api.patchNode = async () => { throw new Error('最多 6 层'); };
  await App.reviewNode.loadDetail('k-2', 'analysis-ii');
  App.state.review.nodeMoveTarget = 'ch-2';
  toasts.length = 0;
  await App.actions['node-move']({ dataset: { node: 'k-2' }, disabled: false });
  check('后端拒绝时把原因显示出来', toasts.some(text => text.includes('最多 6 层')));
}

async function exerciseSideTests() {
  console.log('\n习题侧挂载：');
  reset({
    tab: 'exercises',
    items: [{ task_id: 9, course_id: 'analysis-ii', exercise_id: 'ex-7', prompt_md: '题干', node_ids: ['k-1'] }],
    open: 9,
    nodeOptions: { items: [{ id: 'k-1', title: '数列极限' }, { id: 'k-9', title: '函数极限' }] }
  });
  const html = App.views.review();
  check('详情可加挂、可解绑', html.includes('data-action="detail-mount"')
    && html.includes('data-action="detail-unmount"'));
  check('已挂知识点显示成可删除的 chip', html.includes('rv-mount-chip') && html.includes('k-1'));
  check('候选知识点进了 datalist', html.includes('rv-node-option-list') && html.includes('函数极限'));
  check('提示挂载变化会撤销人工审核', html.includes('人工审核就撤销'));

  App.api.patchExercise = async (course, exercise, body) => {
    calls.push(['patchExercise', course, exercise, body]);
    return { updated: true, reset: { cleared: 1 } };
  };
  queryValue = 'k-9';
  await App.actions['detail-mount']({ dataset: { exercise: 'ex-7', course: 'analysis-ii' }, disabled: false });
  const addCall = calls.find(call => call[0] === 'patchExercise');
  check('加挂提交整份 node_ids（不是只发新加那个）',
    addCall && addCall[3].node_ids.length === 2 && addCall[3].node_ids.includes('k-9')
    && addCall[3].node_ids.includes('k-1'), JSON.stringify(addCall || null));

  reset({
    tab: 'exercises',
    items: [{ task_id: 9, course_id: 'analysis-ii', exercise_id: 'ex-7', prompt_md: '题干', node_ids: ['k-1', 'k-9'] }],
    open: 9
  });
  await App.actions['detail-unmount']({ dataset: { id: 'k-1' }, disabled: false });
  const dropCall = calls.find(call => call[0] === 'patchExercise');
  check('解绑提交剩下的 node_ids',
    dropCall && dropCall[3].node_ids.length === 1 && dropCall[3].node_ids[0] === 'k-9',
    JSON.stringify(dropCall || null));
}

(async () => {
  await titleTests();
  await mountTests();
  await deleteTests();
  await moveTests();
  await exerciseSideTests();

  console.log('');
  if (failures.length) {
    console.log(`失败 ${failures.length} 项：${failures.join('、')}`);
    process.exit(1);
  }
  console.log('节点管理与挂载测试全部通过。');
})();
