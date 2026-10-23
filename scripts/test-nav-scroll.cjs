#!/usr/bin/env node
/**
 * 侧栏菜单的滚动位置要留住（node:vm + 最小 DOM 桩）。
 *
 * 真实症状：菜单比一屏长，滚到最底下点「账号管理」，页面刷新式地重渲染，
 * 菜单弹回顶部，人得重新滚回去找刚才点的那一项。
 *
 * 原因是外壳每次导航都整块重建（#app.innerHTML = 侧栏 + 主区），新节点
 * 的 scrollTop 天生是 0。这里把"重建会把 scrollTop 归零"这件事在桩里演出来：
 * 一旦写入 #app.innerHTML，就把侧栏 nav 的 scrollTop 清零，再看 render 有没有接住。
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');

// 侧栏导航容器：整块重建后是一个"新节点"，scrollTop 归零
const navScroller = { scrollTop: 0 };

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
// #app 的 innerHTML 一旦被写，就模拟"旧节点被丢掉、新节点 scrollTop=0"
const app = element('app');
let renderedHtml = '';
Object.defineProperty(app, 'innerHTML', {
  get() { return renderedHtml; },
  set(value) {
    renderedHtml = value;
    navScroller.scrollTop = 0;
  }
});

const sandbox = {
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
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  document: {
    getElementById: element,
    querySelector: selector => (selector === '.sidebar nav' ? navScroller : null),
    querySelectorAll: () => [],
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
  categoryLabel: '基础课程', direction: '分析', description: '不定积分与级数',
  audience: '本科高年级', stats: { sections: 10, knowledge: 90, exercises: 349 },
  nodes: [{ id: 'analysis-ii', title: '数学分析II', level: 1, path: ['数学分析II'], children: [] }],
  exercises: [], relationships: [], files: [], warnings: [], attachments: []
}];
vm.createContext(sandbox);

const files = ['js/course-catalog.js', 'js/course-cover-art.js', 'js/core.js',
  'js/visual-identity.js', 'js/storage.js', 'js/api-client.js',
  'js/pages.js', 'js/catalog-view.js', 'js/app.js'];
vm.runInContext(
  files.map(file => fs.readFileSync(path.join(root, file), 'utf8')).join('\n;\n'),
  sandbox,
  { filename: 'shell.js' }
);

const App = sandbox.window.App;
const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};

console.log('侧栏滚动位置：');
check('首屏渲染后菜单在顶部', navScroller.scrollTop === 0);

navScroller.scrollTop = 480;
check('记录下滚到底部', navScroller.scrollTop === 480);

sandbox.location.hash = '#/accounts';
App.render();
check('换页后菜单没有被弹回顶部', navScroller.scrollTop === 480, `scrollTop=${navScroller.scrollTop}`);
check('换页后菜单仍是同一块菜单', renderedHtml.includes('账号管理') && renderedHtml.includes('内容审核'));

navScroller.scrollTop = 120;
sandbox.location.hash = '#/construction';
App.render();
check('半途滚动的位置同样保留', navScroller.scrollTop === 120, `scrollTop=${navScroller.scrollTop}`);

// 回到顶部再看：不该被"记住"成 480
navScroller.scrollTop = 0;
sandbox.location.hash = '#/catalog';
App.render();
check('滚回顶部后再换页仍在顶部', navScroller.scrollTop === 0);

// 平台设置删了：学生工作台不该出现「先选课再谈设置」这种页
check('菜单里不再有「平台设置」', !renderedHtml.includes('平台设置'));
check('菜单里也没有设置那一项', !/nav-item[^>]*href="#\/settings"/.test(renderedHtml));

console.log('');
if (failures.length) {
  console.log(`失败 ${failures.length} 项：${failures.join('、')}`);
  process.exit(1);
}
console.log('侧栏滚动位置测试全部通过。');
