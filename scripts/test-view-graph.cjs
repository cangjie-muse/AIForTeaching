#!/usr/bin/env node
/**
 * 知识图谱页的"按课程记忆"测试（node:vm，不启动画布）。
 *
 * 换课再换回来，应该接着上次的层级看，而不是掉回全课程视图。
 * 之前的实现用 A.state.networkCourse === c.id 判断，只记得"当前这门课"：
 * 一看别的课就把记忆覆盖了，换回来等于重开。现在记忆按课程 id 分开存。
 *
 * 这里只跑 A.views.graph()（纯字符串拼装），不碰画布 DOM：
 * 记的是"恢复出来的范围/显示方式"，不测 SVG。
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');

const elements = new Map();
const element = id => {
  if (!elements.has(id)) {
    elements.set(id, {
      id, innerHTML: '', textContent: '', value: '', open: true, hidden: false,
      classList: { add() {}, remove() {}, toggle() {} },
      close() {}, showModal() {}, focus() {}, addEventListener() {}
    });
  }
  return elements.get(id);
};

const store = new Map();
const sandbox = {
  console: { log: console.log, warn() {}, info() {}, error() {} },
  setTimeout, clearTimeout, setInterval: () => 1, clearInterval: () => {},
  URLSearchParams,
  fetch: () => Promise.reject(new Error('不请求后端')),
  location: { hash: '#/course/a/graph', href: '#/course/a/graph' },
  localStorage: {
    getItem: key => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => store.set(key, String(value)),
    removeItem: key => store.delete(key)
  },
  document: {
    getElementById: element, querySelectorAll: () => [], querySelector: () => element('tmp'),
    addEventListener() {}, createElement: () => element('tmp'), title: '',
    body: { classList: { add() {}, remove() {}, toggle() {} } }
  }
};
sandbox.window = sandbox;
sandbox.window.addEventListener = () => {};
sandbox.window.COURSE_DATA = [];
vm.createContext(sandbox);

const files = ['js/course-catalog.js', 'js/core.js', 'js/visual-identity.js', 'js/graph-view.js'];
vm.runInContext(files.map(file => fs.readFileSync(path.join(root, file), 'utf8')).join('\n;\n'),
  sandbox, { filename: 'graph-view.js' });

const App = sandbox.window.App;
App.state = {};
App.markdown = source => String(source);

const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};

function makeCourse(id, title, chapters) {
  return {
    id, title, status: 'ready',
    stats: { sections: chapters.length, knowledge: chapters.length, exercises: 0, associations: 0 },
    nodes: [{ id: `${id}-root`, title, level: 1, path: [title], children: chapters.map(chapter => chapter.id) },
      ...chapters],
    exercises: []
  };
}
const chapter = (courseId, id, title) => ({ id, title, level: 2, chapterId: id, path: ['课', title], children: [`${id}-kp`], exerciseIds: [] });

const courseA = makeCourse('a', '课程甲', [chapter('a', 'a-ch-1', '第一章'), chapter('a', 'a-ch-2', '第二章')]);
const courseB = makeCourse('b', '课程乙', [chapter('b', 'b-ch-1', '第一章')]);

App.course = courseA;
App.route = { page: 'graph', params: new URLSearchParams() };

console.log('第一次进入：全课程视图');
let html = App.views.graph();
check('渲染出图谱页', html.includes('浏览范围'), '');
check('默认范围是全课程', App.state.networkFocus === '__all__', String(App.state.networkFocus));
check('共用习题开关默认打开', html.includes('network-shared') && html.includes('checked'), '');

console.log('\n在课程甲里展开第二章，然后换课再换回来');
App.courseMemory('a').graphFocus = 'a-ch-2';
App.course = courseA;
App.views.graph();
check('恢复上次的层级（第二章）', App.state.networkFocus === 'a-ch-2', String(App.state.networkFocus));
App.course = courseB;
App.views.graph();
check('换到课程乙时用乙自己的记忆', App.state.networkFocus === '__all__', String(App.state.networkFocus));
App.course = courseA;
App.views.graph();
check('换回课程甲后仍是第二章（旧实现会丢掉）', App.state.networkFocus === 'a-ch-2', String(App.state.networkFocus));

console.log('\n显示方式与开关也按课程记住');
App.courseMemory('a').graphDisplay = 'table';
App.courseMemory('a').graphShowShared = false;
App.course = courseA;
html = App.views.graph();
check('表格模式被记住', App.network.display === 'table', String(App.network.display));
check('共用习题开关关闭状态被记住', !html.includes('network-shared" checked'), '');
App.course = courseB;
html = App.views.graph();
check('换到没记忆的课程回到图谱模式', App.network.display === 'graph', String(App.network.display));
check('换到没记忆的课程开关恢复默认', html.includes('network-shared') && html.includes('checked'), '');

console.log('\n无效记忆要回落，而不是白屏');
App.courseMemory('a').graphFocus = 'not-a-node';
App.course = courseA;
App.views.graph();
// 内容改版后旧章节 id 会失效；退到"全课程"比退到某一章更不容易误导。
check('失效的章节退到全课程视图', App.state.networkFocus === '__all__', String(App.state.networkFocus));
check('失效记忆被就地纠正', App.courseMemory('a').graphFocus === '__all__', String(App.courseMemory('a').graphFocus));

console.log('');
if (failures.length) {
  console.log(`失败 ${failures.length} 项：${failures.join(', ')}`);
  process.exit(1);
}
console.log('知识图谱记忆测试全部通过。');
