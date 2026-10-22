#!/usr/bin/env node
/**
 * 知识图谱页的编辑模式：开关、就地改名、拖动画布换归属、批量提交。
 *
 * 画布的真实手感（拖动跟手、虚线预览、缩放时的定位）只能在浏览器里看，
 * 这里验的是会出错的那一半：状态怎么攒、提交什么顺序、失败停在哪、本地模型有没有跟着走。
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');

// ---- 极简假 DOM：这些用例要的是"节点上的输入框能不能收到回车"，不是排版 ----
function makeEl(id = '') {
  const listeners = {};
  const el = {
    id, innerHTML: '', textContent: '', value: '', hidden: false, disabled: false,
    dataset: {}, style: {}, className: '', children: [], tagName: 'DIV',
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener(type, handler) { (listeners[type] = listeners[type] || []).push(handler); },
    removeEventListener() {},
    fire(type, event) { (listeners[type] || []).forEach(handler => handler(event)); },
    listeners,
    querySelector(selector) {
      if (!el.lookups.has(selector)) el.lookups.set(selector, makeEl(selector));
      return el.lookups.get(selector);
    },
    lookups: new Map(),
    querySelectorAll() { return []; },
    appendChild(child) { el.children.push(child); return child; },
    remove() {},
    setAttribute() {}, getAttribute() { return null; }, focus() {}, select() {},
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 400, height: 300, right: 400, bottom: 300 })
  };
  return el;
}

function makeDom() {
  const byId = new Map();
  return {
    getElementById(id) {
      if (!byId.has(id)) byId.set(id, makeEl(id));
      return byId.get(id);
    },
    createElement: () => makeEl(),
    querySelector: () => makeEl(),
    querySelectorAll: () => [],
    addEventListener() {},
    body: { classList: { add() {}, remove() {} } },
    byId
  };
}

const sandbox = {
  console, setTimeout, clearTimeout, URLSearchParams,
  location: { hash: '#/graph/analysis-ii' },
  fetch: () => Promise.reject(new Error('测试不请求后端')),
  localStorage: {
    store: new Map(),
    getItem(key) { return this.store.has(key) ? this.store.get(key) : null; },
    setItem(key, value) { this.store.set(key, String(value)); }
  },
  document: makeDom()
};
sandbox.window = sandbox;
vm.createContext(sandbox);

const bundle = ['js/core.js', 'js/visual-identity.js', 'js/api-client.js',
  'js/graph-model.js', 'js/graph-atlas.js', 'js/graph-view.js']
  .map(file => fs.readFileSync(path.join(root, file), 'utf8')).join('\n;\n');
vm.runInContext(bundle, sandbox, { filename: 'bundle.js' });

const App = sandbox.window.App;
App.markdown = source => `<div class="prose">${source}</div>`;
App.render = () => {};
const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};
const calls = [];
const toasts = [];
App.toast = message => toasts.push(message);

function makeCourse() {
  const rootNode = {
    id: 'root', title: '数学分析 II', level: 1, parentId: null, kind: 'container',
    chapterId: null, path: ['数学分析 II'], children: [{ id: 'ch-1' }, { id: 'ch-2' }],
    exerciseIds: [], isRoot: true
  };
  const ch1 = {
    id: 'ch-1', title: '第一章 极限', level: 2, parentId: 'root', kind: 'container',
    chapterId: 'ch-1', path: ['数学分析 II', '第一章 极限'], children: [{ id: 'k-2' }],
    exerciseIds: []
  };
  const ch2 = {
    id: 'ch-2', title: '第二章 微分', level: 2, parentId: 'root', kind: 'container',
    chapterId: 'ch-2', path: ['数学分析 II', '第二章 微分'], children: [],
    exerciseIds: []
  };
  const leaf = {
    id: 'k-2', title: '函数极限', level: 3, parentId: 'ch-1', kind: 'knowledge',
    chapterId: 'ch-1', path: ['数学分析 II', '第一章 极限', '函数极限'], children: [],
    exerciseIds: ['ex-1'], source: '教材 p.12', content: '函数极限的定义。'
  };
  return {
    id: 'analysis-ii', title: '数学分析 II', status: 'ready',
    nodes: [rootNode, ch1, ch2, leaf],
    stats: { knowledge: 1, exercises: 1, sections: 2 },
    exercises: [{ id: 'ex-1' }],
    relationships: [
      { type: 'contains', source: 'root', target: 'ch-1' },
      { type: 'contains', source: 'root', target: 'ch-2' },
      { type: 'contains', source: 'ch-1', target: 'k-2' }
    ]
  };
}

function reset() {
  sandbox.document = makeDom();
  App.course = makeCourse();
  App.courseMemory = () => ({});
  App.state = { networkEdit: { on: false, drafts: [], busy: false, error: '' } };
  App.state.review = { nodePanel: '', nodeDetail: null, nodeDetailError: '', nodeCourse: '', filters: {} };
  App.route = { page: 'graph', params: new Map(), course: 'analysis-ii', query: {} };
  App.network.current = null; App.network.selected = null;
  App.network.camera = { x: 0, y: 0, scale: 1 };
  App.actions['read-node'] = () => {};
  App.actions['node-close'] = () => { App.state.review.nodePanel = ''; };
  App.auth = { can: () => true };
  calls.length = 0;
  toasts.length = 0;
}

// 假布局：坐标固定，方便断言拖动命中了哪个容器。
function fakeCanvas() {
  App.network.current = {
    scope: 'all', focusId: 'root', parentId: null, sharedCount: 0, width: 800, height: 600,
    edges: App.course.relationships.map(edge => ({ ...edge, exerciseIds: [] })),
    nodes: [
      { id: 'root', title: '数学分析 II', kind: 'container', isRoot: true, level: 1, x: 400, y: 300 },
      { id: 'ch-1', title: '第一章 极限', kind: 'container', isRoot: false, level: 2, x: 200, y: 200 },
      { id: 'ch-2', title: '第二章 微分', kind: 'container', isRoot: false, level: 2, x: 600, y: 200 },
      { id: 'k-2', title: '函数极限', kind: 'knowledge', isRoot: false, level: 3, x: 200, y: 420 }
    ]
  };
  App.state.networkFocus = 'root';
  App.state.networkLastFocus = 'root';
}

async function shellTests() {
  console.log('骨架与开关：');
  reset();
  let html = App.views.graph();
  check('教师看到编辑开关', html.includes('data-action="network-edit-toggle"'));
  check('没开编辑模式时没有浮条', !html.includes('id="net-edit-bar"'));
  check('下方那块编辑面板已经删掉（改在画布与右栏）', !html.includes('id="network-edit"'));

  App.auth = { can: () => false };
  html = App.views.graph();
  check('学生没有编辑开关', !html.includes('data-action="network-edit-toggle"'));
  check('学生看不到未审统计', !html.includes('id="network-review-summary"'));

  reset();
  App.views.graph();
  App.actions['network-edit-toggle']();
  html = App.views.graph();
  check('点开关进入编辑模式', App.state.networkEdit.on === true);
  check('编辑模式下出现浮条与提交按钮',
    html.includes('id="net-edit-bar"') && html.includes('data-action="network-commit"'));
  check('浮条说清怎么改（双击改名 / 拖动换归属）',
    html.includes('双击章/节/知识点就地改名') && html.includes('换归属'));
}

async function renameTests() {
  console.log('\n就地改名：');
  reset();
  fakeCanvas();
  App.views.graph();
  App.state.networkEdit.on = true;
  App.actions['network-rename']({ dataset: { id: 'k-2' } });
  const host = sandbox.document.getElementById('network-canvas').children[0];
  check('双击后在画布上挂了一个输入框', Boolean(host && host.className === 'net-rename'));
  const input = host.querySelector('input');
  input.value = '函数极限（改）';
  input.fire('keydown', { key: 'Enter', preventDefault() {} });
  const draft = App.state.networkEdit.drafts[0];
  check('回车记成待提交的改名（不落库）',
    Boolean(draft) && draft.kind === 'rename' && draft.id === 'k-2' && draft.title === '函数极限（改）',
    JSON.stringify(draft || null));
  check('这时还没发请求', calls.length === 0);

  App.actions['network-rename']({ dataset: { id: 'k-2' } });
  const again = sandbox.document.getElementById('network-canvas').children[1];
  const blank = again.querySelector('input');
  blank.value = '   ';
  blank.fire('keydown', { key: 'Enter', preventDefault() {} });
  check('空标题不记（也不会把节点改成空）', App.state.networkEdit.drafts.length === 1);

  App.actions['network-rename']({ dataset: { id: 'k-2' } });
  const esc = sandbox.document.getElementById('network-canvas').children[2];
  const escInput = esc.querySelector('input');
  escInput.value = '不要了';
  escInput.fire('keydown', { key: 'Escape', preventDefault() {} });
  check('Esc 取消不掉草稿也不会写入', App.state.networkEdit.drafts.length === 1);
}

async function containerTests() {
  console.log('\n章 / 节的改名与单击：');
  reset();
  fakeCanvas();
  App.state.networkEdit.on = true;
  const opened = [];
  App.network.open = id => opened.push(id);
  App.network.select('ch-1', false);
  check('编辑模式单击章：不立刻跳进下一层（等第二击）', opened.length === 0);
  await new Promise(done => setTimeout(done, 320));
  check('没有第二击就照旧进入', opened.length === 1 && opened[0] === 'ch-1');

  opened.length = 0;
  App.network.select('ch-1', false);
  App.network.cancelEnter();
  await new Promise(done => setTimeout(done, 320));
  check('双击时取消进入（不会再跳一下）', opened.length === 0);

  App.network.select('root', false);
  check('根节点不走延后（课程根不能就地改名）', opened.length === 1 && opened[0] === 'root');

  opened.length = 0;
  App.state.networkEdit.on = false;
  App.network.select('ch-1', false);
  check('非编辑模式单击章：立刻进入', opened.length === 1 && opened[0] === 'ch-1');

  // 章也能改名：同样先进草稿，提交时才落库
  reset();
  fakeCanvas();
  App.views.graph();
  App.state.networkEdit.on = true;
  App.actions['network-rename']({ dataset: { id: 'ch-1' } });
  const host = sandbox.document.getElementById('network-canvas').children[0];
  const input = host.querySelector('input');
  input.value = '第一章 极限与连续';
  input.fire('keydown', { key: 'Enter', preventDefault() {} });
  const draft = App.state.networkEdit.drafts[0];
  check('章/节也能双击改名（记成草稿）',
    Boolean(draft) && draft.kind === 'rename' && draft.id === 'ch-1' && draft.title === '第一章 极限与连续',
    JSON.stringify(draft || null));
}

async function dragTests() {
  console.log('\n拖动换归属：');
  reset();
  fakeCanvas();
  App.state.networkEdit.on = true;
  App.network.draftMove('k-2', 'ch-2');
  check('拖到另一章记成待提交的移动',
    App.state.networkEdit.drafts.some(item => item.kind === 'move' && item.id === 'k-2' && item.parentId === 'ch-2'));
  App.network.draftMove('k-2', 'ch-1');
  check('拖回原来的章等于没改（不会重复记）',
    App.state.networkEdit.drafts.filter(item => item.kind === 'move').length === 1);

  const hit = App.network.containerAt({ x: 205, y: 210 });
  check('松手位置能命中容器节点', Boolean(hit) && hit.id === 'ch-1', hit && hit.id);
  check('落在空白处不算命中', App.network.containerAt({ x: 20, y: 20 }) === null);
  check('知识点不算容器（不能挂到知识点底下）',
    App.network.containerAt({ x: 200, y: 420 }) === null);
}

async function commitTests() {
  console.log('\n提交：');
  reset();
  fakeCanvas();
  App.state.networkEdit.on = true;
  App.state.networkEdit.drafts = [
    { kind: 'move', id: 'k-2', parentId: 'ch-2' },
    { kind: 'rename', id: 'k-2', title: '函数极限（改）' }
  ];
  App.api.patchNode = async (course, node, body) => {
    calls.push(['patchNode', course, node, body]);
    return { move: body.parent_id ? { nodes: 1 } : null };
  };
  await App.actions['network-commit']();
  check('改名先提交、移动后提交（path 要用新标题算）',
    calls.length === 2 && calls[0][3].title === '函数极限（改）' && calls[1][3].parent_id === 'ch-2',
    JSON.stringify(calls.map(call => call[3])));
  check('提交带上课程 id', calls.every(call => call[1] === 'analysis-ii'));
  check('提交完草稿清空', App.state.networkEdit.drafts.length === 0);
  check('本地模型跟着改：标题', App.course.nodes.find(n => n.id === 'k-2').title === '函数极限（改）');
  check('本地模型跟着改：父节点与层级',
    App.course.nodes.find(n => n.id === 'k-2').parentId === 'ch-2'
    && App.course.nodes.find(n => n.id === 'k-2').level === 3);
  check('本地模型的 path 也重算了',
    App.course.nodes.find(n => n.id === 'k-2').path.join('/') === '数学分析 II/第二章 微分/函数极限（改）',
    App.course.nodes.find(n => n.id === 'k-2').path.join('/'));
  check('连线跟着改挂（画布不会还连着旧章）',
    App.course.relationships.some(edge => edge.type === 'contains' && edge.target === 'k-2' && edge.source === 'ch-2'));
  check('提完要求重排画布（不然新位置可能跑到框外）', App.state.networkRefit === true);
  check('给出"提交了几处"的反馈', toasts.some(text => text.includes('2 处')), String(toasts[0] || ''));
}

async function failureTests() {
  console.log('\n提交失败：');
  reset();
  fakeCanvas();
  App.state.networkEdit.on = true;
  App.state.networkEdit.drafts = [
    { kind: 'rename', id: 'k-2', title: '函数极限（改）' },
    { kind: 'move', id: 'k-2', parentId: 'ch-2' }
  ];
  App.api.patchNode = async (course, node, body) => {
    calls.push(['patchNode', course, node, body]);
    if (body.parent_id) throw new Error('不能把节点移到它自己的子树里');
    return {};
  };
  await App.actions['network-commit']();
  check('失败的停在移动这一步', App.state.networkEdit.drafts.length === 1
    && App.state.networkEdit.drafts[0].kind === 'move');
  check('错误里写清是哪个节点、为什么', String(App.state.networkEdit.error).includes('函数极限')
    && String(App.state.networkEdit.error).includes('子树'), String(App.state.networkEdit.error));
  check('已成功的那一处仍然落到本地模型',
    App.course.nodes.find(n => n.id === 'k-2').title === '函数极限（改）');
  check('失败的移动没有骗人（父节点没变）',
    App.course.nodes.find(n => n.id === 'k-2').parentId === 'ch-1');

  reset();
  fakeCanvas();
  App.state.networkEdit.on = true;
  App.state.networkEdit.drafts = [{ kind: 'rename', id: 'k-2', title: 'x' }];
  await App.actions['network-discard']();
  check('放弃会清空草稿', App.state.networkEdit.drafts.length === 0);
  check('放弃也说了句话', toasts.some(text => text.includes('放弃')));
}

async function selectTests() {
  console.log('\n编辑模式下点节点：');
  reset();
  fakeCanvas();
  App.state.networkEdit.on = true;
  let loaded = null;
  App.reviewNode = {
    loadDetail: (id, course) => { loaded = [id, course]; App.state.review.nodePanel = id; },
    panelFor: id => (App.state.review.nodePanel === id ? '<div class="rv-node-panel">面板</div>' : '')
  };
  await App.network.select('k-2', false);
  check('打开右侧表单（不是阅读页）',
    Boolean(loaded) && loaded[0] === 'k-2' && loaded[1] === 'analysis-ii', JSON.stringify(loaded));
  check('右侧渲染的是节点面板',
    sandbox.document.getElementById('network-detail').innerHTML.includes('rv-node-panel'));

  reset();
  fakeCanvas();
  loaded = null;
  App.reviewNode = { loadDetail: id => { loaded = [id]; }, panelFor: () => '' };
  App.auth = { can: () => false };
  await App.network.select('k-2', false);
  check('学生点节点还是看详情（没有编辑表单）', loaded === null);
}

async function localChangeTests() {
  console.log('\n本地模型同步（右栏写动作之后）：');
  reset();
  App.networkLocalChange('rename', { id: 'k-2', title: '新名字' });
  const leaf = App.course.nodes.find(n => n.id === 'k-2');
  check('改名写回节点', leaf.title === '新名字');
  check('path 末段跟着换', leaf.path.slice(-1)[0] === '新名字');
  App.networkLocalChange('move', { id: 'k-2', parentId: 'ch-2' });
  check('移动后旧章不再列它',
    !App.course.nodes.find(n => n.id === 'ch-1').children.some(child => child.id === 'k-2'));
  check('移动后新章列上它',
    App.course.nodes.find(n => n.id === 'ch-2').children.some(child => child.id === 'k-2'));
  App.networkLocalChange('delete', { id: 'ch-2' });
  check('删除把节点从模型里拿掉', !App.course.nodes.some(n => n.id === 'ch-2'));
  check('删除连带子树', !App.course.nodes.some(n => n.id === 'k-2'));
  check('删除也清掉相关连线',
    !App.course.relationships.some(edge => edge.source === 'ch-2' || edge.target === 'ch-2'));
}

async function atlasTests() {
  console.log('\n表格视图：');
  reset();
  const normal = sandbox.window.GraphAtlas.render(App.course, s => s, {});
  const editing = sandbox.window.GraphAtlas.render(App.course, s => s, { edit: true });
  check('平时点知识点 = 去阅读页', normal.includes('data-action="read-node"'));
  check('编辑模式下点知识点 = 打开右侧表单', editing.includes('data-action="network-edit-point"'));
  check('编辑模式下点位上标"可编辑"', editing.includes('可编辑'));
}

(async () => {
  await shellTests();
  await renameTests();
  await containerTests();
  await dragTests();
  await commitTests();
  await failureTests();
  await selectTests();
  await localChangeTests();
  await atlasTests();
  console.log();
  if (failures.length) {
    console.log(`失败 ${failures.length} 项：${failures.join('、')}`);
    process.exit(1);
  }
  console.log('图谱页编辑模式测试全部通过。');
})();
