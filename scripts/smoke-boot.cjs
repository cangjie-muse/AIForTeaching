#!/usr/bin/env node
/**
 * 真实联调：用浏览器里那套 js 跑一遍启动流程，对着正在运行的后端。
 *
 * 为什么单独做：目录合并那段代码曾经因为"把 {items:[...]} 当数组用"而永远不执行，
 * 所有 vm 测试都绿，页面上却看不到库里的改动。那种错误只有真发一次请求才会暴露。
 *
 * 用法：先起 npm run dev:api，再 node scripts/smoke-boot.cjs
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const BASE = process.env.API_BASE || 'http://127.0.0.1:8000';

const elements = new Map();
const element = id => {
  if (!elements.has(id)) {
    elements.set(id, {
      id, innerHTML: '', textContent: '', title: '', hidden: false,
      classList: { add() {}, remove() {}, toggle() {} },
      close() {}, showModal() {}, focus() {}, addEventListener() {}
    });
  }
  return elements.get(id);
};

const sandbox = {
  console: { log: console.log, warn() {}, info() {}, error() {} },
  setTimeout, clearTimeout, setInterval: () => 1, clearInterval: () => {},
  URLSearchParams, URL,
  // 唯一和单元测试不同的地方：真的把请求打到本机后端
  fetch: (url, options) => fetch(new URL(url, BASE).toString(), options),
  prompt: () => '', confirm: () => true,
  location: { hash: '#/catalog', href: '#/catalog' },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  document: {
    getElementById: element, querySelectorAll: () => [], querySelector: () => null,
    addEventListener() {}, createElement: () => element('tmp'), title: '',
    body: { classList: { add() {}, remove() {}, toggle() {} } }
  }
};
sandbox.window = sandbox;
sandbox.window.addEventListener = () => {};
sandbox.window.COURSE_DATA = [{
  id: 'legacy-static-only', title: '只在静态包里的旧课', status: 'ready',
  subject: '专业课程', categoryLabel: '基础课程', stage: '一期核心课程',
  description: '旧', audience: '本科', stats: { sections: 1, knowledge: 1, exercises: 1 },
  nodes: [{ id: 'legacy-static-only', title: '旧节点', level: 1, path: ['旧'], children: [] }],
  exercises: [], relationships: [], files: [], warnings: [], attachments: []
}];
vm.createContext(sandbox);

console.log(`对着 ${BASE} 跑一遍启动流程…`);
const files = ['js/course-catalog.js', 'js/course-cover-art.js', 'js/core.js',
  'js/visual-identity.js', 'js/storage.js', 'js/api-client.js',
  'js/pages.js', 'js/catalog-view.js', 'js/app.js'];
vm.runInContext(files.map(file => read(file)).join('\n;\n'), sandbox, { filename: 'boot.js' });

const App = sandbox.window.App;
const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

(async () => {
  for (let i = 0; i < 60 && App.dataSource === 'static'; i += 1) await sleep(500);
  console.log('');
  check('启动后数据来源是数据库', App.dataSource === 'db', App.dataSource);
  check('课程数量与库里一致（14 门）', App.courses.length === 14, `${App.courses.length} 门`);
  check('只在静态包里的课没被显示', !App.courses.some(c => c.id === 'legacy-static-only'));
  check('没有"建设中"（14 门都已发布）', App.draftCourses.length === 0, `${App.draftCourses.length} 门`);

  const course = App.courses.find(c => c.id === 'analysis-ii');
  check('数学分析II 来自库（有 349 道题）', (course?.exercises || []).length === 349,
    `${(course?.exercises || []).length} 道`);
  check('题干是库里的正文',
    String((course?.exercises || [])[0]?.prompt || '').length > 0);

  const html = element('app').innerHTML;
  check('目录页渲染出课程卡片', (html.match(/class="course-card"/g) || []).length >= 14,
    `${(html.match(/class="course-card"/g) || []).length} 张`);
  check('顶栏没有误报"离线演示数据"', !html.includes('离线演示数据'));

  console.log('');
  if (failures.length) {
    console.log(`失败 ${failures.length} 项：${failures.join('、')}`);
    process.exit(1);
  }
  console.log('启动流程联调通过。');
  process.exit(0);
})();
