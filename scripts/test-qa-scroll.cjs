#!/usr/bin/env node
/**
 * 问答页的消息区滚动位置要留住（node:vm + 最小 DOM 桩）。
 *
 * 真实症状：每问一个问题、每收到一个回答，消息区就跳回顶部，得重新往下滑。
 * 原因是整页重渲染（#app.innerHTML 换掉），#qa-messages 是全新节点，scrollTop=0。
 *
 * 桩里把这件事演出来：一写 #app.innerHTML，就把消息区的 scrollTop 清零。
 * 然后看两件事：贴着底部时新回答进来要自动到底；上翻看历史时不许被拽走。
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');

// 消息区：scrollHeight/clientHeight 由测试直接摆布，scrollTop 会被重建清零
const messages = { scrollTop: 0, scrollHeight: 0, clientHeight: 400 };
let windowScrollY = 0;

const elements = new Map();
function element(id) {
  if (!elements.has(id)) {
    elements.set(id, {
      id, innerHTML: '', textContent: '', title: '', value: '',
      classList: { add() {}, remove() {}, toggle() {} },
      close() {}, showModal() {}, focus() {}, addEventListener() {},
      querySelector: () => null, querySelectorAll: () => []
    });
  }
  return elements.get(id);
}

const app = element('app');
let renderedHtml = '';
Object.defineProperty(app, 'innerHTML', {
  get() { return renderedHtml; },
  set(value) {
    renderedHtml = value;
    // 重建 = 新节点：滚动位置归零（这正是用户看到的现象）
    messages.scrollTop = 0;
    windowScrollY = 0;
  }
});

const sandbox = {
  console: { log: console.log, warn() {}, info() {}, error() {} },
  setTimeout, clearTimeout,
  setInterval: () => 1, clearInterval: () => {},
  URLSearchParams,
  fetch: () => Promise.reject(new Error('滚动测试不发真实请求')),
  prompt: () => '', confirm: () => true,
  // 问答页挂在课程下（#/qa 会被 normalizeRoute 带到选课页）
  location: { hash: '#/course/analysis/qa', href: '#/course/analysis/qa' },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  document: {
    // 只有当前 DOM 里真的有消息区，才返回它——首屏进来时上一个页面没有这个消息区，
    // 这一点必须和浏览器一致，否则首屏会被误判成"用户已经上翻"。
    getElementById: id => (id === 'qa-messages'
      ? (renderedHtml.includes('id="qa-messages"') ? messages : null)
      : element(id)),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {},
    createElement: () => element('tmp'),
    title: '',
    body: { classList: { add() {}, remove() {}, toggle() {} } }
  }
};
sandbox.window = sandbox;
sandbox.window.addEventListener = () => {};
sandbox.window.scrollY = 0;
sandbox.window.scrollTo = (x, y) => { windowScrollY = y; };
Object.defineProperty(sandbox, 'scrollY', { get: () => windowScrollY });
sandbox.COURSE_DATA = [{
  id: 'analysis', title: '数学分析', status: 'ready', subject: '专业课程',
  categoryLabel: '基础课程', direction: '分析', description: '',
  audience: '本科', stats: { sections: 8, knowledge: 159, exercises: 416 },
  nodes: [{ id: 'analysis', title: '数学分析', level: 1, path: ['数学分析'], children: [] }],
  exercises: [], relationships: [], files: [], warnings: [], attachments: []
}];
vm.createContext(sandbox);

vm.runInContext(
  ['js/course-catalog.js', 'js/course-cover-art.js', 'js/core.js',
   'js/visual-identity.js', 'js/storage.js', 'js/api-client.js',
   'js/pages.js', 'js/app.js', 'js/qa-chat.js']
    .map(file => fs.readFileSync(path.join(root, file), 'utf8')).join('\n;\n'),
  sandbox,
  { filename: 'qa-scroll.js' }
);

const App = sandbox.window.App;
const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};

const state = App.state.qa;

console.log('首次进入问答页：');
messages.scrollHeight = 1200;
messages.clientHeight = 400;
App.render();
check('默认贴在底部（最新的对话在眼前）', messages.scrollTop === 1200, `scrollTop=${messages.scrollTop}`);

console.log('\n用户上翻看历史，此时重渲染不应把他拽走：');
messages.scrollTop = 300;
App.render();
check('位置被保留', messages.scrollTop === 300, `scrollTop=${messages.scrollTop}`);
check('没有被当成"贴底"', state.stickBottom === false);

console.log('\n上翻状态下又收到一个回答（内容变长）：');
messages.scrollHeight = 1600;
App.render();
check('仍在原位置，不跳到底也不跳回顶', messages.scrollTop === 300, `scrollTop=${messages.scrollTop}`);

console.log('\n贴着底部时新回答进来：自动滚到最新');
messages.scrollHeight = 1600;
messages.scrollTop = 1200;          // 贴底（gap = 0）
App.render();
check('自动到底', messages.scrollTop === 1600, `scrollTop=${messages.scrollTop}`);

console.log('\n刚发出问题：无论之前在哪，都跳到自己的问题那里');
messages.scrollTop = 100;
state.pinBottom = true;
App.render();
state.pinBottom = false;
check('发出后贴底', messages.scrollTop === 1600, `scrollTop=${messages.scrollTop}`);

console.log('\n容差：差一两行也算贴底（避免像素误差把人钉在中间）');
messages.scrollHeight = 1600;
messages.scrollTop = 1190;          // gap = 10
App.render();
check('gap<32 视为贴底', messages.scrollTop === 1600, `scrollTop=${messages.scrollTop}`);

console.log('\n没有消息区（首屏还没渲染）时不能报错：');
elements.delete('qa-messages');
const originalGet = sandbox.document.getElementById;
sandbox.document.getElementById = id => (id === 'qa-messages' ? null : originalGet(id));
let crashed = '';
try { App.render(); } catch (error) { crashed = String(error && error.message || error); }
check('不抛异常', !crashed, crashed);
sandbox.document.getElementById = originalGet;

console.log('');
if (failures.length) {
  console.log(`失败 ${failures.length} 项：${failures.join('、')}`);
  process.exit(1);
}
console.log('问答页滚动位置测试全部通过。');
