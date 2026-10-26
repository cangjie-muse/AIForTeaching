#!/usr/bin/env node
/**
 * 脚本加载顺序测试。
 *
 * 为什么要有这个测试：`js/core.js` 里才定义 `App`，而各功能模块在**顶层**就写
 * `const A = App`。一旦某个模块的 <script> 排在 core.js 之前，它在浏览器里会抛
 * `App is not defined`，整个文件作废——但页面照样能打开，只是那个功能静默消失，
 * 表现为「A.xxx is not a function」。
 *
 * 这正是线上出现过的故障：作业批改与课件工坊两个模块的标签被插到了 core.js 前面，
 * 单测各自 vm 里按正确顺序加载，所以全绿，浏览器里却是坏的。
 *
 * 本测试按 index.html 的**真实顺序**把脚本逐个跑一遍，并静态检查顺序约束。
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const sources = [...html.matchAll(/<script defer src="([^"]+)"><\/script>/g)]
  .map(match => match[1]);

const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};

const element = () => ({
  innerHTML: '', textContent: '', value: '', files: [], style: {}, dataset: {},
  classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
  appendChild() {}, removeChild() {}, remove() {}, setAttribute() {}, getAttribute: () => null,
  addEventListener() {}, removeEventListener() {}, querySelector: () => null,
  querySelectorAll: () => [], focus() {}, blur() {}, close() {}, showModal() {}, click() {}
});

// 页面元素一律给个对象：真实浏览器里 getElementById 不会返回 null，
// 桩要是返回 null，就会把"桩太薄"误报成"脚本坏了"。
const nodes = new Map();
const node = id => {
  if (!nodes.has(id)) nodes.set(id, element());
  return nodes.get(id);
};

const sandbox = {
  console: { log() {}, warn() {}, error() {}, info() {} },
  setTimeout, clearTimeout, setInterval: () => 1, clearInterval() {},
  requestAnimationFrame: callback => setTimeout(callback, 0),
  document: {
    addEventListener() {}, getElementById: node, createElement: element,
    querySelector: () => null, querySelectorAll: () => [], title: '',
    body: element(), documentElement: element(), head: element()
  },
  location: { hash: '', href: 'http://localhost/', pathname: '/', search: '' },
  navigator: { userAgent: 'node', clipboard: { writeText: async () => {} } },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {}, clear() {} },
  sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  fetch: () => Promise.reject(new Error('脚本顺序测试不发请求')),
  matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
  URL: { createObjectURL: () => 'blob:x', revokeObjectURL() {} },
  URLSearchParams, FormData: class {}, Blob: class {}, FileReader: class {},
  prompt: () => '', confirm: () => false, alert() {}, open() {}
};
sandbox.window = sandbox;
sandbox.window.addEventListener = () => {};
sandbox.window.removeEventListener = () => {};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

const local = sources.filter(src => src.startsWith('js/'));
const broken = [];
for (const src of local) {
  const file = path.join(root, src.split('?')[0]);
  if (!fs.existsSync(file)) { broken.push([src, '文件不存在']); continue; }
  try {
    vm.runInContext(fs.readFileSync(file, 'utf8'), sandbox, { filename: src });
  } catch (error) {
    broken.push([src, error.message.split('\n')[0]]);
  }
}

console.log('按 index.html 顺序加载：');
check('所有脚本都能执行', broken.length === 0, broken.map(item => item.join(' → ')).join('；'));

const A = sandbox.window.App || {};
const views = A.views || {};
const registered = Object.keys(views);

console.log('\n功能确实装上了：');
for (const [label, ok] of [
  ['课程中心', typeof views.catalog === 'function'],
  ['课件生成与修改', typeof views.courseware === 'function'],
  ['课件工坊', typeof A.coursewareStudio === 'function'],
  ['队列提示', typeof A.queueNotice === 'function'],
  ['作业批改', typeof views.homework === 'function'],
  ['内容审核', typeof views.review === 'function']
]) check(label, ok);
check('工坊的动作已注册', typeof (A.actions || {})['studio-generate'] === 'function');
check('作业动作已注册', Object.keys(A.actions || {}).some(key => key.startsWith('homework-')));
check('教师页面至少 10 个', registered.length >= 10, `${registered.length} 个：${registered.join(', ')}`);

console.log('\n顺序约束（静态）：');
const indexOf = src => sources.findIndex(item => item.startsWith(src));
const coreIndex = indexOf('js/core.js');
check('core.js 存在', coreIndex >= 0);
const offenders = local.filter(src => {
  const body = fs.readFileSync(path.join(root, src.split('?')[0]), 'utf8');
  // 顶层就取 App 的模块，必须排在 core.js 之后
  return /^\s*(?:'use strict';\s*)?(?:\/\*[\s\S]*?\*\/\s*)?\(function\s*\(\)\s*\{\s*const A = App/m.test(body)
    && indexOf(src) < coreIndex;
});
check('没有模块排在 core.js 之前取 App', offenders.length === 0, offenders.join(', '));
check('每个 js 文件都已挂到 index.html',
  fs.readdirSync(path.join(root, 'js')).filter(name => name.endsWith('.js'))
    .every(name => sources.some(src => src.startsWith(`js/${name}`))),
  fs.readdirSync(path.join(root, 'js')).filter(name => name.endsWith('.js'))
    .filter(name => !sources.some(src => src.startsWith(`js/${name}`))).join(', '));

console.log('');
if (failures.length) {
  console.log(`脚本顺序测试失败 ${failures.length} 项：${failures.join('、')}`);
  process.exit(1);
}
console.log('脚本顺序测试全部通过。');
