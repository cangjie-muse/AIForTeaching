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
// 离线包里残留的规划课（库里没有）不该算进目录：统计写 43、实际只有 33 门能打开
const staticOnly = (sandbox.COURSE_DATA || []).map(c => c.id)
  .filter(id => !['analysis-ii', 'real-analysis'].includes(id));
check('库里没有的离线包课程不进目录统计',
  staticOnly.every(id => !App.allCourses().some(c => c.id === id)),
  JSON.stringify(staticOnly.slice(0, 3)));

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

console.log('\n改课程名：');
// 弹窗里要能改名字：课程名此前只有接口没有界面，导入打错字就改不了
App.courseRows = [{ id: 'analysis-ii', created_by: 'acc-t' }];
App.state.auth = { status: 'ready', actor: { id: 'acc-t', role: 'teacher', display_name: '王老师' } };
App.actions['catalog-tag']({ dataset: { id: 'analysis-ii' } });
check('弹窗里有课程名输入框', element('dialog').innerHTML.includes('id="course-title-input"'), '');
check('输入框预填现有课程名',
  /id="course-title-input" value="数学分析II（库里版本）"/.test(element('dialog').innerHTML), '');
check('卡片上是「编辑」不是「归类」', /data-action="catalog-tag"[^>]*>编辑/.test(App.views.catalog()), '');

const renamed = [];
let titleReloads = 0;
const toasts = [];
App.toast = message => toasts.push(message);
const toastsBefore = toasts.length;
App.api.updateCourse = async (id, patch) => { renamed.push([id, patch]); return { changed: ['title'] }; };
App.loadCatalog = async () => { titleReloads += 1; };
element('course-title-input').value = '  数学分析（二）  ';
await App.actions['catalog-title-save']({ dataset: { id: 'analysis-ii' }, disabled: false });
check('保存时去掉首尾空格', renamed.length === 1 && renamed[0][1].title === '数学分析（二）',
  JSON.stringify(renamed));
check('只传 title，不动归类',
  renamed.length === 1 && !('direction' in renamed[0][1]), JSON.stringify(renamed[0] && renamed[0][1]));
check('改完重拉目录', titleReloads === 1, String(titleReloads));
check('给出改名提示', toasts.slice(toastsBefore).some(text => text.includes('已改名为「数学分析（二）」')),
  String(toasts.slice(toastsBefore)));

renamed.length = 0;
element('course-title-input').value = '   ';
await App.actions['catalog-title-save']({ dataset: { id: 'analysis-ii' }, disabled: false });
check('空课程名不发请求', renamed.length === 0, JSON.stringify(renamed));
check('空课程名给一句人话', toasts.slice(-1)[0].includes('不能为空'), String(toasts.slice(-1)));

console.log('\n新建学科分类：');
const groupsBefore = CourseCatalog.groups.map(group => ({ ...group }));
App.state.auth = { status: 'ready', actor: { id: 'acc-a', role: 'admin', display_name: '管理员' } };
html = App.views.catalog();
check('管理员能看到「新建分类」', html.includes('catalog-add-direction'), '');
App.state.auth = { status: 'ready', actor: { id: 'acc-t', role: 'teacher', display_name: '王老师' } };
check('老师看不到「新建分类」', !App.views.catalog().includes('catalog-add-direction'), '');

// 分类要能删，但 ✕ 不能常驻：一排删除按钮摆在筛选条上，误点就把分类删了。
// 管理员点「编辑分类」进编辑态，✕ 才出现；再点「完成」收起。学生与老师都看不到。
App.state.auth = { status: 'ready', actor: { id: 'acc-a', role: 'admin', display_name: '管理员' } };
App.state.catalogEditing = false;
let adminHtml = App.views.catalog();
check('默认不显示删除按钮', !adminHtml.includes('chip-del'), '');
check('管理员看得到「编辑分类」入口', adminHtml.includes('catalog-edit-toggle'), '');

App.actions['catalog-edit-toggle']();
adminHtml = App.views.catalog();
check('进入编辑态才出现删除按钮',
  (adminHtml.match(/class="chip-del"/g) || []).length === CourseCatalog.groups.length,
  `${(adminHtml.match(/class="chip-del"/g) || []).length} vs ${CourseCatalog.groups.length}`);
check('编辑态下按钮变成「完成」', adminHtml.includes('✓ 完成'), '');
check('编辑态下说清删除的后果', adminHtml.includes('回到「待归类」，不会删课'), '');
check('删除按钮挂的是分类 id',
  CourseCatalog.groups.every(group => adminHtml.includes(`data-id="${group.id}"`)), '');
check('「全部课程」「已接入」这类固定档没有删除按钮',
  !/catalog-del-direction[\s\S]{0,60}data-id="all"/.test(adminHtml) &&
  !/catalog-del-direction[\s\S]{0,60}data-id="ready"/.test(adminHtml), '');

App.actions['catalog-edit-toggle']();
check('再点一次收起删除按钮', !App.views.catalog().includes('chip-del'), '');

App.state.auth = { status: 'ready', actor: { id: 'acc-s', role: 'student', display_name: '学生' } };
check('学生连「编辑分类」都看不到',
  !App.views.catalog().includes('catalog-edit-toggle') && !App.views.catalog().includes('chip-del'), '');
App.state.auth = { status: 'ready', actor: { id: 'acc-t', role: 'teacher', display_name: '王老师' } };
check('老师也看不到分类删除', !App.views.catalog().includes('chip-del'), '');

App.state.auth = { status: 'ready', actor: { id: 'acc-a', role: 'admin', display_name: '管理员' } };
App.actions['catalog-add-direction']();
check('弹窗里有分类名称输入框', element('dialog').innerHTML.includes('direction-label'), '');
App.inputs['direction-label']({ value: '数理逻辑' });

const made = [];
App.api.createDirection = async label => { made.push(label); return { id: 'dir-x', label }; };
let directionsReloaded = 0, catalogReloaded = 0;
App.loadDirections = async () => {
  directionsReloaded += 1;
  CourseCatalog.setGroups([...groupsBefore, { id: 'dir-x', label: '数理逻辑' }]);
};
App.loadCatalog = async () => { catalogReloaded += 1; };
App.render = () => {};
await App.actions['catalog-direction-save']({ disabled: false });
check('保存时提交的是输入的名字', made[0] === '数理逻辑', JSON.stringify(made));
check('建完重拉分类清单', directionsReloaded === 1, String(directionsReloaded));
check('建完重拉课程目录', catalogReloaded === 1, String(catalogReloaded));
check('新分类立刻出现在筛选条', App.views.catalog().includes('数理逻辑'), '');
check('新分类编号排在最后', CourseCatalog.groups[CourseCatalog.groups.length - 1].number === '07',
  CourseCatalog.groups.map(group => group.number).join(','));

// 收尾：恢复成六组，后面的断言按六组算
CourseCatalog.setGroups(groupsBefore);

console.log('\n删除分类：');
App.state.auth = { status: 'ready', actor: { id: 'acc-a', role: 'admin', display_name: '管理员' } };
App.actions['catalog-add-direction']();
check('管理弹窗里列出已有分类',
  /catalog-del-direction[\s\S]{0,80}data-id="analysis"/.test(element('dialog').innerHTML), '');

const deleted = [];
App.api.deleteDirection = async (id, force) => {
  deleted.push([id, force]);
  return { deleted: id, label: '数理逻辑', courses_moved: force ? 2 : 0 };
};
App.loadDirections = async () => { CourseCatalog.setGroups(groupsBefore); };
App.loadCatalog = async () => {};
let confirms = [];
sandbox.confirm = message => { confirms.push(message); return true; };

await App.actions['catalog-del-direction']({ dataset: { id: 'dir-x', label: '数理逻辑' }, disabled: false });
check('删除前问一次，说清课会回到待归类',
  confirms.length === 1 && confirms[0].includes('回到「待归类」'), JSON.stringify(confirms));
check('直接删不传 force', deleted.length === 1 && !deleted[0][1], JSON.stringify(deleted));

// 有人用着：服务端 409 报课数，再问一次才连课一起改
deleted.length = 0; confirms = [];
App.api.deleteDirection = async (id, force) => {
  deleted.push([id, force]);
  if (!force) {
    const error = new Error('还有 2 门课归在「数理逻辑」下');
    error.status = 409;
    error.detail = { detail: '还有 2 门课归在「数理逻辑」下', courses: 2 };
    throw error;
  }
  return { deleted: id, courses_moved: 2 };
};
await App.actions['catalog-del-direction']({ dataset: { id: 'dir-x', label: '数理逻辑' }, disabled: false });
check('409 后带 force 再删一次', deleted.length === 2 && deleted[1][1] === true, JSON.stringify(deleted));
check('第二次提示里写明几门课', confirms.length === 2 && confirms[1].includes('2 门课'),
  JSON.stringify(confirms));

// 用户在第二次提示上点取消：不该强删
deleted.length = 0; confirms = [];
let asked = 0;
sandbox.confirm = message => { confirms.push(message); asked += 1; return asked === 1; };
await App.actions['catalog-del-direction']({ dataset: { id: 'dir-x', label: '数理逻辑' }, disabled: false });
check('取消就不再强删', deleted.length === 1 && !deleted.some(call => call[1]), JSON.stringify(deleted));
sandbox.confirm = () => true;



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

// 统计条的四个数必须和筛选条同源：以前「门课程」取静态 entries（36），
// 筛选条取渲染用的 all（40），同屏两个数打架。
const statNumbers = [...html.matchAll(/<strong>(\d+)<\/strong><span>([^<]+)</g)]
  .map(match => [match[2], Number(match[1])]);
const statOf = label => (statNumbers.find(([name]) => name === label) || [])[1];
check('统计条四个数都来自同一份课程列表', statOf('门课程') === App.allCourses().length,
  `${statOf('门课程')} vs ${App.allCourses().length}`);
check('已接入 + 待接入 = 门课程',
  statOf('门已接入内容') + statOf('门待接入') === statOf('门课程'),
  `${statOf('门已接入内容')}+${statOf('门待接入')} vs ${statOf('门课程')}`);
check('数字不再补零', !/<strong>0\d<\/strong>/.test(html), '');
check('没有两个「待归类」筛选项',
  (html.match(/待归类<span>/g) || []).length <= 1, String((html.match(/待归类<span>/g) || []).length));
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

console.log('\n待归类也有一档可筛：');
const coursesBefore = App.courses;
App.dataSource = 'db';
// 一门没归类的课：内容接口拿回来的课 direction 是空的，merge 会给它 groupId=imported
App.courses = [{
  ...course('new-import', '新导入的课'),
  groupId: 'imported', directionId: '', direction: '待归类', categoryLabel: '新导入课程'
}];
App.draftCourses = [];
html = App.views.catalog();
check('筛选条里有「待归类」', /data-filter="imported"[^>]*>待归类/.test(html), '');
check('「待归类」也带课数', /data-filter="imported"[^>]*>待归类<span>1</.test(html), '');
check('待归类那一组还在页面上', /data-group="imported"/.test(html), '');
App.courses = coursesBefore;

console.log('');
if (failures.length) {
  console.log(`失败 ${failures.length} 项：${failures.join(', ')}`);
  process.exit(1);
}
console.log('课程目录数据来源测试全部通过。');
})();
