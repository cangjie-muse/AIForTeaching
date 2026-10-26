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
  direction: '分析', description: '简介', audience: '本科',
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

(async () => {
const App = sandbox.window.App;
const CourseCatalog = sandbox.window.CourseCatalog;
const CourseGroupIdOf = label => (CourseCatalog.groups.find(group => group.label === label) || {}).id;
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
sandbox.location.hash = '';
App.state.content = { courseId: '', tab: 'import' };
App.actions['course-open-content']({ dataset: { id: 'real-analysis' } });
check('点卡片直接打开这门课', App.state.content.courseId === 'real-analysis', App.state.content.courseId);
check('点卡片直接停在「修改与导出」', App.state.content.tab === 'edit', App.state.content.tab);
check('点卡片跳到内容页', sandbox.location.hash === '#/content', sandbox.location.hash);

check('筛选条里有「正在建设」一档', html.includes('data-filter="draft"'), '');
check('「正在建设」写清有几门', /data-filter="draft">正在建设<span>1</.test(html), '');
// 吸顶的偏移必须让开顶栏：.topbar 也是 sticky(top:0)，工具条写 top:0 会吸在它后面
const css = require('node:fs').readFileSync(require('node:path').join(__dirname, '..', 'styles/catalog.css'), 'utf8');
check('吸顶条让开 76px 高的顶栏', /\.catalog-toolbar\{[^}]*position:sticky[^}]*top:76px/.test(css), '');
check('窄屏让开 64px 高的顶栏', /@media\(max-width:720px\)\{[^@]*\.catalog-toolbar\{top:64px/.test(css), '');
check('跳转落点留出吸顶条高度', /\.catalog-group\{scroll-margin-top:138px\}/.test(css), '');

check('搜索与标签在同一个吸顶工具条里',
  /catalog-toolbar[^>]*>[\s\S]{0,300}catalog-filters[\s\S]{0,3000}search-input/.test(html), '');

check('建设中卡片带上这门课的 id', /data-action="course-open-content" data-id="real-analysis"/.test(html),
  '');

console.log('\n卡片上直接归类：');
App.state.auth = { status: 'ready', actor: { id: 'acc-t', role: 'teacher', display_name: '王老师' } };
html = App.views.catalog();
check('老师在自己导入的课上看到「归类」',
  /class="card-tag" data-action="catalog-tag" data-id="analysis-ii"/.test(html), '');
check('归类入口在卡片右上，不另起一行', /card-cat-right[^>]*>[\s\S]{0,120}catalog-tag/.test(html), '');

// 库里记着是谁导入的：不是自己导的课，老师不该看到入口（服务端也会 403）
App.courseRows = [{ id: 'analysis-ii', created_by: 'acc-other' }];
html = App.views.catalog();
check('别人导入的课不给归类入口', !/data-action="catalog-tag"/.test(html), '');
App.courseRows = [{ id: 'analysis-ii', created_by: '' }];
html = App.views.catalog();
check('老课程没有导入人时老师仍可归类', /data-action="catalog-tag"/.test(html), '');
App.courseRows = [{ id: 'analysis-ii', created_by: 'acc-t' }];

App.actions['catalog-tag']({ dataset: { id: 'analysis-ii' } });
const dialogBody = element('dialog').innerHTML;
check('点归类弹出方向列表', dialogBody.includes('选一个学科方向'), '');
check('列表里六个方向都在',
  ['分析', '代数与数论', '几何与拓扑', '微分方程', '概率统计', '计算与应用']
    .every(label => dialogBody.includes(label)), '');
check('列表里留了「待归类」', /data-direction=""[^>]*>待归类/.test(dialogBody), '');

const saved = [];
let reloads = 0;
App.api.updateCourse = async (id, patch) => { saved.push([id, patch]); return { changed: ['direction'] }; };
App.loadCatalog = async () => { reloads += 1; };
App.render = () => {};
await App.actions['catalog-tag-pick']({ dataset: { id: 'analysis-ii', direction: 'geometry', label: '几何与拓扑' }, disabled: false });
check('选中方向后写库', saved.length === 1 && saved[0][1].direction === 'geometry', JSON.stringify(saved));
check('改完重拉目录（分组要跟着变）', reloads === 1, String(reloads));

App.state.auth = { status: 'ready', actor: { id: 'acc-s', role: 'student', display_name: '小明' } };
console.log('\n学生端目录：');
html = App.views.catalog();
check('学生看不到"建设中"区块', !html.includes('建设中（未发布）'), '');
check('学生看不到归类入口', !html.includes('data-action="catalog-tag"'), '');

console.log('\n目录按学科方向组织：');
App.dataSource = 'db';
App.state.auth = { status: 'ready', actor: { id: 'acc-s', role: 'student', display_name: '小明' } };
html = App.views.catalog();
check('不再出现建设批次', !/一期|二期/.test(html), '');
check('统计条写"个学科方向"', html.includes('个学科方向'), '');
check('六个方向分组都在',
  ['分析', '代数与数论', '几何与拓扑', '微分方程', '概率统计', '计算与应用']
    .every(label => html.includes(`data-group="${CourseGroupIdOf(label)}"`)), CourseCatalog.groups.map(g => g.label).join('、'));
check('每门课都归到某个方向',
  CourseCatalog.entries.every(entry => CourseCatalog.groups.some(group => group.id === entry.groupId)), '');
check('卡片显示方向而不是阶段', html.includes('card-category') && !html.includes('待归类段'));
check('36 门课不重不漏',
  new Set(CourseCatalog.entries.map(entry => entry.id)).size === 36 && CourseCatalog.entries.length === 36);

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
})();
