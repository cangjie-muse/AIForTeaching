#!/usr/bin/env node
/**
 * 应用外壳的渲染冒烟测试（node:vm + 最小 DOM 桩，不启浏览器）。
 *
 * 为什么需要它：侧边栏、面包屑、当前课程条都是 PWA 外壳的一部分，藏在 app.js 里，
 * 视图测试碰不到。而外壳恰恰是最容易悄悄坏的地方——菜单少一项、链接丢了课程 id、
 * 课程条不出现，页面本身都"渲染成功"，测试全绿，用户却觉得导航莫名其妙。
 *
 * 这里真的跑一遍 A.render()，检查最终拼出来的 HTML：
 *   1. 菜单形状固定：没选课程时「课程概览/大纲」也在，不能前后跳变；
 *   2. 没选课程时课程入口指向 #/pick/<page>；
 *   3. 打开课程后出现当前课程条，且「换课程」回选课页。
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');

const elements = new Map();
function element(id) {
  if (!elements.has(id)) {
    elements.set(id, {
      id, innerHTML: '', textContent: '', title: '',
      classList: { add() {}, remove() {}, toggle() {} },
      close() {}, showModal() {}, focus() {}, addEventListener() {}
    });
  }
  return elements.get(id);
}

const store = new Map();
const sandbox = {
  // 外壳在无后端、无 IndexedDB 的环境里会打警告，这是测试环境的正常噪音，静音。
  console: { log: console.log, warn() {}, info() {}, error() {} },
  setTimeout,
  clearTimeout,
  setInterval: () => 1,
  clearInterval: () => {},
  URLSearchParams,
  fetch: () => Promise.reject(new Error('外壳测试不发真实请求')),
  prompt: () => '',
  confirm: () => true,
  location: { hash: '#/catalog', href: '#/catalog' },
  localStorage: {
    getItem: key => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => store.set(key, String(value)),
    removeItem: key => store.delete(key)
  },
  document: {
    getElementById: element,
    querySelectorAll: () => [],
    querySelector: () => null,
    addEventListener() {},
    createElement: () => element('tmp'),
    title: '',
    body: { classList: { add() {}, remove() {}, toggle() {} } }
  }
};
sandbox.window = sandbox;
sandbox.window.addEventListener = () => {};
sandbox.window.COURSE_DATA = [{
  id: 'analysis-ii', title: '数学分析II', status: 'ready', subject: '专业课程',
  categoryLabel: '基础课程', stage: '一期核心课程', description: '不定积分与级数',
  audience: '本科高年级', stats: { sections: 10, knowledge: 90, exercises: 349 },
  nodes: [{ id: 'analysis-ii', title: '数学分析II', level: 1, path: ['数学分析II'], children: [] }],
  exercises: [], relationships: [], files: [], warnings: [], attachments: []
}];
vm.createContext(sandbox);

const files = ['js/course-catalog.js', 'js/course-cover-art.js', 'js/core.js',
  'js/visual-identity.js', 'js/storage.js', 'js/api-client.js',
  'js/pages.js', 'js/catalog-view.js', 'js/app.js'];
const bundle = files.map(file => fs.readFileSync(path.join(root, file), 'utf8')).join('\n;\n');
vm.runInContext(bundle, sandbox, { filename: 'shell.js' });

const App = sandbox.window.App;
const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};

console.log('课程中心（未选课程）：');
let html = elements.get('app').innerHTML;
check('外壳已渲染', html.includes('course-bar') || html.includes('sidebar'), '');
check('菜单里一直有课程概览', html.includes('课程概览'), '');
check('菜单里一直有课程大纲', html.includes('课程大纲'), '');
check('未选课程时概览指向选课页', html.includes('href="#/pick/overview"'), '');
check('未选课程时知识图谱指向选课页', html.includes('href="#/pick/graph"'), '');
check('未选课程时习题指向选课页', html.includes('href="#/pick/exercises"'), '');
check('未选课程时没有当前课程条', !html.includes('class="course-bar"'), '');
check('课程中心自己的链接正常', html.includes('href="#/catalog"'));

console.log('\n进入课程：');
sandbox.location.hash = '#/course/analysis-ii/overview';
App.render();
html = elements.get('app').innerHTML;
check('渲染成功（没有落到错误页）', !html.includes('没有找到这个页面'), '');
check('出现当前课程条', html.includes('class="course-bar"'));
check('课程条写明课程名', html.includes('数学分析II'));
check('课程条带规模（10 章 / 90 知识点 / 349 题）',
  html.includes('10 章') && html.includes('90 个知识点') && html.includes('349 道习题'));
check('课程条有换课程入口', html.includes('换课程') && html.includes('href="#/pick/overview"'), '');
check('课程条高亮当前页', /class="course-bar-link active"[^>]*href="#\/course\/analysis-ii\/overview"/.test(html), '');
check('课程条里的本课链接带课程 id', html.includes('href="#/course/analysis-ii/overview"'));
check('面包屑显示课程名', html.includes('数学分析II</a>'));
check('侧边栏此时也带课程 id', html.includes('href="#/course/analysis-ii/exercises"'));

console.log('\n待接入课程：');
sandbox.location.hash = '#/course/homology/overview';
App.render();
html = elements.get('app').innerHTML;
check('待接入课程也能打开概览', !html.includes('没有找到这个页面'), '');
check('课程条标注内容待接入', html.includes('内容待接入'), '');

console.log('');
if (failures.length) {
  console.log(`失败 ${failures.length} 项：${failures.join(', ')}`);
  process.exit(1);
}
console.log('应用外壳渲染测试全部通过。');
process.exit(0);
