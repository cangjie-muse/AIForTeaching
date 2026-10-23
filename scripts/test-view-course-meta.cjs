#!/usr/bin/env node
/**
 * 课程改名与改归类的前端测试（node:vm，不启浏览器）。
 *
 * 后端测试证明"接口挡住了不该改的"，这里证明"页面确实把改的东西送出去了，
 * 而且改完课程中心的分组会跟着变"。两类错都很隐蔽：
 *   · 面板把方向清空了（null 和 '' 没区分），点开一次保存就把课踢进「待归类」；
 *   · 目录合并只听静态表，库里的方向改了等于没改。
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');

const elements = new Map();
function element(id) {
  if (!elements.has(id)) {
    elements.set(id, { id, innerHTML: '', textContent: '', title: '', value: '',
      classList: { add() {}, remove() {}, toggle() {} },
      close() {}, showModal() {}, focus() {}, addEventListener() {} });
  }
  return elements.get(id);
}

const store = new Map();
let patched = null;
const sandbox = {
  console: { log: console.log, warn() {}, info() {}, error() {} },
  setTimeout, clearTimeout,
  setInterval: () => 1, clearInterval: () => {},
  URLSearchParams,
  fetch: () => Promise.reject(new Error('面板测试不发真实请求')),
  prompt: () => '', confirm: () => true,
  location: { hash: '#/content', href: '#/content' },
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
sandbox.window.COURSE_DATA = [];
vm.createContext(sandbox);

const files = ['js/course-catalog.js', 'js/core.js', 'js/visual-identity.js', 'js/storage.js',
  'js/api-client.js', 'js/course-meta-panel.js'];
vm.runInContext(files.map(read).join('\n;\n'), sandbox, { filename: 'meta.js' });

const App = sandbox.window.App;
const CourseCatalog = sandbox.window.CourseCatalog;
let renders = 0;
App.render = () => { renders += 1; };
App.toast = () => {};
App.loadCatalog = async () => { App.catalogReloaded = (App.catalogReloaded || 0) + 1; };
// 把网络层换掉：只记下 PATCH 发了什么
App.api = {
  updateCourse: async (courseId, patch) => {
    patched = { courseId, patch };
    return { course_id: courseId, changed: Object.keys(patch), course: {} };
  },
  // 保存成功后面板会用 courseList 的返回值替换 content.courses，
  // 桩必须回显当前列表——返回空数组会把"再保存一次"变成提前 return。
  courseList: async () => (App.state.content && App.state.content.courses) || []
};

const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};

console.log('目录合并听库里的方向：');
const merged = CourseCatalog.merge([
  // 库里给「交换代数」换到微分方程组（静态表里它是代数）
  { id: 'commutative-algebra', title: '交换代数', direction: 'pde', nodes: [], exercises: [], stats: {} },
  // 新导入的课，库里有方向：应该落进那一组
  { id: 'pde-new', title: '偏微分方程（新）', direction: 'pde', nodes: [], exercises: [], stats: {} },
  // 新导入的课，库里没方向：落在「待归类」
  { id: 'unknown', title: '没归类的课', direction: '', nodes: [], exercises: [], stats: {} }
]);
const commutative = merged.find(course => course.id === 'commutative-algebra');
check('库里的方向覆盖静态表', commutative.groupId === 'pde', commutative.groupId);
check('方向名跟着变', commutative.direction === '微分方程', commutative.direction);
const imported = merged.find(course => course.id === 'pde-new');
check('新导入的课按库里的方向归组', imported.groupId === 'pde', imported.groupId);
check('归了组就不再是新导入课程', imported.categoryLabel === '微分方程', imported.categoryLabel);
const unknown = merged.find(course => course.id === 'unknown');
check('没方向的课落待归类', unknown.groupId === 'imported' && unknown.direction === '待归类',
  `${unknown.groupId}/${unknown.direction}`);

const bogus = CourseCatalog.merge([{ id: 'bogus', title: '方向写错的课', direction: 'nope', nodes: [], exercises: [], stats: {} }])
  .find(course => course.id === 'bogus');
check('不认识的方向当待归类，课不消失', bogus && bogus.groupId === 'imported', bogus && bogus.groupId);

console.log('\n面板：');
App.state.content = {
  courseId: 'commutative-algebra',
  courses: [{ id: 'commutative-algebra', title: '交换代数', direction: 'algebra' }]
};
let html = App.courseMetaPanel('commutative-algebra');
check('有课程名称输入框', html.includes('data-input="meta-title"'));
check('框里是现在的名字', /data-input="meta-title" value="交换代数"/.test(html));
check('有学科方向下拉', html.includes('data-input="meta-direction"'));
check('六个方向都在', ['analysis', 'algebra', 'geometry', 'pde', 'probability', 'computing']
  .every(id => html.includes(`value="${id}"`)), '');
check('当前方向被选中', /value="algebra" selected/.test(html));
check('有保存按钮', html.includes('data-action="meta-save"'));

const wrongCourse = App.courseMetaPanel('nope');
check('没选课程时不出面板', wrongCourse === '');

console.log('\n保存：');
App.inputs['meta-title']({ value: '交换代数与同调' });
App.inputs['meta-direction']({ value: 'geometry' });
check('输入框的改动进了状态', App.state.meta.title === '交换代数与同调' && App.state.meta.direction === 'geometry',
  `${App.state.meta.title}/${App.state.meta.direction}`);

(async () => {
  await App.actions['meta-save']();
  check('PATCH 发了这门课', patched && patched.courseId === 'commutative-algebra',
    patched && patched.courseId);
  check('PATCH 带上了新名字', patched && patched.patch.title === '交换代数与同调',
    patched && patched.patch.title);
  check('PATCH 带上了新方向', patched && patched.patch.direction === 'geometry',
    patched && patched.patch.direction);
  check('保存后重拉了课程目录', App.catalogReloaded === 1, String(App.catalogReloaded));
  check('保存后提示改了什么', /已保存/.test(App.state.meta.notice), App.state.meta.notice);
  check('保存后草稿清空（下次显示库里的值）',
    App.state.meta.title === null && App.state.meta.direction === null);

  console.log('\n只开不点，不该把方向弄丢：');
  App.state.content.courseId = 'commutative-algebra';
  App.state.content.courses = [{ id: 'commutative-algebra', title: '交换代数', direction: 'algebra' }];
  html = App.courseMetaPanel('commutative-algebra');
  check('打开面板后方向仍是库里的', /value="algebra" selected/.test(html));
  patched = null;
  await App.actions['meta-save']();
  check('原样保存不会误清方向', patched.patch.direction === 'algebra', patched && patched.patch.direction);

  console.log('\n选「待归类」：');
  App.inputs['meta-direction']({ value: '' });
  patched = null;
  await App.actions['meta-save']();
  check('待归类确实发成空字符串', patched.patch.direction === '', JSON.stringify(patched.patch));

  console.log('');
  if (failures.length) {
    console.log(`失败 ${failures.length} 项：${failures.join('、')}`);
    process.exit(1);
  }
  console.log('课程改名与归类的前端测试全部通过。');
})();
