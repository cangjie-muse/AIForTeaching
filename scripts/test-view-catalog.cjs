#!/usr/bin/env node
/**
 * 课程目录"以数据库为准"的测试（node:vm + 最小 DOM 桩）。
 *
 * 这里挡的是这类事故：静态包里的课被当成真课显示给学生——看起来一切正常，
 * 但审核、可见范围、发布全都不起作用。规则只有三条，必须钉住：
 *   1. 静态包里有、库里没有的课：不显示（它是历史残留）；
 *   2. 库里有、已发布：显示，内容用库的；
 *   3. 库里有、没发布：学生看不到，教师端单列"建设中"。
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');

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
  console: { log: console.log, warn() {}, info() {}, error() {} },
  setTimeout, clearTimeout,
  setInterval: () => 1, clearInterval: () => {},
  URLSearchParams,
  fetch: () => Promise.reject(new Error('目录测试不发真实请求')),
  prompt: () => '', confirm: () => true,
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

const course = (id, title, extra = {}) => ({
  id, title, status: 'ready', subject: '专业课程', categoryLabel: '基础课程',
  stage: '一期核心课程', description: '简介', audience: '本科',
  stats: { sections: 10, knowledge: 90, exercises: 349 },
  nodes: [{ id, title, level: 1, path: [title], children: [] }],
  exercises: [], relationships: [], files: [], warnings: [], attachments: [], ...extra
});

// 静态包里有两门课：analysis-ii（库里也有）和 legacy-only（库里没有）。
sandbox.window.COURSE_DATA = [course('analysis-ii', '数学分析II'), course('legacy-only', '只剩静态包的旧课')];
vm.createContext(sandbox);

const files = ['js/course-catalog.js', 'js/course-cover-art.js', 'js/core.js',
  'js/visual-identity.js', 'js/storage.js', 'js/api-client.js',
  'js/pages.js', 'js/auth.js', 'js/catalog-view.js', 'js/app.js'];
vm.runInContext(files.map(file => read(file)).join('\n;\n'), sandbox, { filename: 'catalog.js' });

const App = sandbox.window.App;
const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};

console.log('静态包与数据库的边界：');
const dropped = App.applyDbCatalog(
  [
    { id: 'analysis-ii', review_status: 'published', exercise_count: 349, knowledge_count: 90 },
    { id: 'real-analysis', review_status: 'draft', exercise_count: 337, knowledge_count: 309 }
  ],
  [course('analysis-ii', '数学分析II（库里版本）', {
    nodes: [{ id: 'analysis-ii', title: '库里的知识点', level: 1, path: ['数学分析II'], children: [] }]
  })]
);
check('库里没有的静态课程被移除', dropped === 1, `移除 ${dropped} 门`);
check('库里为空的课不再出现在目录里', !App.courses.some(c => c.id === 'legacy-only'));
check('已发布的课保留', App.courses.some(c => c.id === 'analysis-ii'));
check('保留的课用的是库里的内容',
  (App.courses.find(c => c.id === 'analysis-ii').nodes[0] || {}).title === '库里的知识点');
check('未发布的课不进学生目录', !App.courses.some(c => c.id === 'real-analysis'));
check('未发布的课记进"建设中"', (App.draftCourses.find(c => c.id === 'real-analysis') || {}).exercises === 337);
check('数据来源标为数据库', App.dataSource === 'db', App.dataSource);

App.state.auth = { status: 'ready', actor: { id: 'acc-t', role: 'teacher', display_name: '王老师' } };
console.log('\n教师端目录：');
let html = App.views.catalog();
check('出现"建设中（未发布）"区块', html.includes('建设中（未发布）'), '');
check('区块里列出未发布的课', html.includes('实变函数') || html.includes('real-analysis') || html.includes('337 道习题'));
check('建设中卡片写明学生看不到', html.includes('尚未发布给学生'));
check('建设中卡片指向课程内容页', html.includes('href="#/content"'));

App.state.auth = { status: 'ready', actor: { id: 'acc-s', role: 'student', display_name: '小明' } };
console.log('\n学生端目录：');
html = App.views.catalog();
check('学生看不到"建设中"区块', !html.includes('建设中（未发布）'), '');

console.log('\n后端连不上时：');
App.dataSource = 'offline';
html = App.views.catalog();
check('离线时静态包照常可用', html.includes('数学分析II'));
const shell = read('js/app.js');
check('顶栏有离线数据提示', shell.includes('离线演示数据'));
check('离线提示写清审核此时不生效', shell.includes('审核与可见范围此时不生效'));

console.log('');
if (failures.length) {
  console.log(`失败 ${failures.length} 项：${failures.join(', ')}`);
  process.exit(1);
}
console.log('课程目录数据来源测试全部通过。');
