#!/usr/bin/env node
/**
 * 班级与选课页（教师 / 管理员）。
 *
 * 重点不是"表格画出来了"，而是这几条规则不能破：
 *   1. 学生没有建班、加人的权限（菜单不显示、直接开页面也是"没有权限"）；
 *   2. 建班前不许把学生加进去——先有班才有人；
 *   3. 已经在班上的学生不再出现在"可添加"列表里，避免重复点；
 *   4. 删班要提示会连带移除多少人的选课记录；
 *   5. 学期只能从"学年 + 秋/春/夏"里选，手打不进去。
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

const calls = [];
const classesFixture = {
  items: [
    {
      id: 'class-a', course_id: 'analysis-ii', course_title: '数学分析II',
      name: '数学分析II-01班',
      academic_year: '2026-2027', term_kind: '秋', term: '2026年秋季学期',
      teacher_id: 'acc-t', teacher_name: '张老师', student_count: 2, created_at: '2026-10-01'
    }
  ],
  total: 1,
  students: 2
};
const rosterFixture = {
  items: [
    { student_id: 'acc-s1', username: '2024001', display_name: '李同学',
      enrolled_at: '2026-10-02T10:00:00', added_by: '张老师', disabled: false }
  ],
  total: 1
};
const studentsFixture = {
  items: [
    { id: 'acc-s1', username: '2024001', display_name: '李同学', disabled: false },
    { id: 'acc-s2', username: '2024002', display_name: '王同学', disabled: false }
  ],
  total: 2
};

const sandbox = {
  console: { log: console.log, warn() {}, info() {}, error() {} },
  setTimeout, clearTimeout, setInterval: () => 1, clearInterval: () => {},
  URLSearchParams, URL, Date,
  fetch: () => Promise.reject(new Error('不发真实请求')),
  prompt: () => '', confirm: () => true,
  location: { hash: '#/classes', href: '#/classes' },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  document: {
    getElementById: element,
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
sandbox.window.COURSE_DATA = [{
  id: 'analysis-ii', title: '数学分析II', status: 'ready', subject: '专业课程',
  categoryLabel: '基础课程', direction: '分析', description: '',
  audience: '本科', stats: { sections: 10, knowledge: 90, exercises: 349 },
  nodes: [], exercises: [], relationships: [], files: [], warnings: [], attachments: []
}];
vm.createContext(sandbox);

const files = ['js/course-catalog.js', 'js/course-cover-art.js', 'js/core.js',
  'js/visual-identity.js', 'js/storage.js', 'js/api-client.js', 'js/auth.js',
  'js/pages.js', 'js/catalog-view.js', 'js/classes.js', 'js/app.js'];
vm.runInContext(files.map(read).join('\n;\n'), sandbox, { filename: 'classes.js' });

const App = sandbox.window.App;
const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};

// 接口全部用桩：这一层测视图与交互，真实权限在 e2e 里验
App.api.classes = async params => { calls.push(['classes', params]); return classesFixture; };
App.api.classStudents = async id => { calls.push(['students-of', id]); return rosterFixture; };
App.api.students = async params => { calls.push(['students', params]); return studentsFixture; };
App.api.createClass = async payload => { calls.push(['create', payload]); return { ...classesFixture.items[0], id: 'class-new' }; };
App.api.addStudents = async (id, ids) => { calls.push(['add', id, ids]); return { added: ids.length, skipped: 0, total: 1 }; };
App.api.removeStudent = async (id, studentId) => { calls.push(['remove', id, studentId]); return { removed: true, total: 0 }; };
App.api.deleteClass = async id => { calls.push(['delete', id]); return { deleted: id, students_removed: 0 }; };
App.toast = message => calls.push(['toast', message]);

const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

(async () => {
  console.log('学生视角：');
  App.state.auth = { actor: { id: 'acc-s', role: 'student', display_name: '李同学' } };
  check('学生打不开班级页', App.auth.can('classes') === false);
  sandbox.location.hash = '#/classes';
  App.render();
  await wait(30);
  let html = elements.get('app').innerHTML;
  check('学生看到的是"没有权限"', html.includes('当前身份没有这个页面的权限'), '');
  check('学生页面上没有"新建班级"', !html.includes('新建班级'));
  check('学生没有触发任何班级接口', !calls.some(call => call[0] === 'classes'));

  console.log('\n教师视角：');
  App.state.auth = { actor: { id: 'acc-t', role: 'teacher', display_name: '张老师' } };
  // 换身份后把页面状态清回初始值（真浏览器里是新开一页，状态本来就是干净的）
  App.state.classes = {
    status: 'idle', error: '', courseId: '', items: [], total: 0, seats: 0, openClassId: '',
    roster: { status: 'idle', error: '', items: [] },
    form: { courseId: '', name: '', academicYear: '', termKind: '', note: '' },
    picker: { open: false, query: '', status: 'idle', error: '', items: [], selected: [] },
    saving: false
  };
  App.render();
  await wait(60);
  html = elements.get('app').innerHTML;
  check('教师能打开班级页', html.includes('班级与选课'));
  check('列出了已有班级', html.includes('数学分析II-01班'));
  check('有新建班级表单', html.includes('新建班级'));
  check('学生没有建班入口（这里有）', html.includes('data-action="classes-create"'));

  console.log('\n菜单分组：');
  const teacherNav = html.slice(0, html.indexOf('<main'));
  const teachingAt = teacherNav.indexOf('教学与建设');
  check('内容审核在教学与建设分组里',
    teacherNav.indexOf('内容审核') > teachingAt, '还在系统管理里');
  check('班级与选课也在教学与建设分组里',
    teacherNav.indexOf('班级与选课') > teachingAt, '没放进教学与建设');
  check('教师看不到账号管理与模型接口配置',
    !teacherNav.includes('账号管理') && !teacherNav.includes('模型接口配置'));
  check('教师能在系统管理里看到学生可见范围',
    teacherNav.includes('系统管理') && teacherNav.includes('学生可见范围'),
    '可见范围是教师的活儿，不该只给管理员');

  App.state.auth = { actor: { id: 'acc-a', role: 'admin', display_name: '管理员' } };
  App.render();
  await wait(30);
  const adminNav = elements.get('app').innerHTML;
  const adminSection = adminNav.slice(adminNav.indexOf('系统管理'), adminNav.indexOf('<main'));
  check('管理员能看到系统管理', adminNav.includes('系统管理'));
  check('系统管理里是可见范围 + 账号管理 + 模型接口配置',
    adminSection.includes('账号管理') && adminSection.includes('学生可见范围')
    && !adminSection.includes('内容审核'), '内容审核没搬走');
  // 后面的用例要用教师身份继续
  App.state.auth = { actor: { id: 'acc-t', role: 'teacher', display_name: '张老师' } };

  console.log('\n展开名单：');
  await App.actions['classes-open']({ dataset: { id: 'class-a' } });
  await wait(40);
  html = elements.get('app').innerHTML;
  check('名单里出现学生学号', html.includes('2024001'));
  check('名单显示是谁加进来的', html.includes('张老师'));
  check('有"添加学生"按钮', html.includes('classes-picker-open'));

  console.log('\n添加学生：');
  await App.actions['classes-picker-open']();
  await wait(40);
  html = elements.get('app').innerHTML;
  check('选择器列出可加的学生', html.includes('2024002'));
  check('已在班上的学生不再出现', !html.includes('data-input="classes-pick" value="acc-s1"'),
    '李同学应被过滤');
  App.inputs['classes-pick']({ value: 'acc-s2', checked: true });
  html = elements.get('app').innerHTML;
  check('勾选后按钮显示人数', html.includes('加入所选（1）'));
  await App.actions['classes-add-selected']({ disabled: false });
  await wait(40);
  check('调用了加人接口', calls.some(call => call[0] === 'add' && call[2].includes('acc-s2')));

  console.log('\n学期口径：');
  check('秋季学期显示成 2026年秋季学期',
    App.classTermLabel('2026-2027', '秋') === '2026年秋季学期', App.classTermLabel('2026-2027', '秋'));
  check('春季学期落在学年第二年',
    App.classTermLabel('2026-2027', '春') === '2027年春季学期', App.classTermLabel('2026-2027', '春'));
  check('夏季学期同样落在第二年',
    App.classTermLabel('2026-2027', '夏') === '2027年夏季学期', App.classTermLabel('2026-2027', '夏'));
  html = elements.get('app').innerHTML;
  check('表单给的是学年下拉，不是手打学期',
    html.includes('data-input="classes-form-year"') && !html.includes('data-input="classes-form-term"'),
    '还在让人手打学期');
  check('表单里能选秋季学期', html.includes('<option value="秋" selected>秋季学期</option>'));
  check('建班前就把将建成的学期显示出来', html.includes('将建成：2026年秋季学期'),
    '没显示学期文案');

  console.log('\n建班与删班：');
  App.state.classes.form = { courseId: 'analysis-ii', name: '  ', academicYear: '2026-2027', termKind: '秋' };
  await App.actions['classes-create']({ disabled: false });
  check('班名为空时不提交', !calls.some(call => call[0] === 'create'));
  check('提示了原因', calls.some(call => call[0] === 'toast' && String(call[1]).includes('班级名称')));
  App.state.classes.form = { courseId: 'analysis-ii', name: '数学分析II-02班', academicYear: '2026-2027', termKind: '秋' };
  await App.actions['classes-create']({ disabled: false });
  const sent = calls.filter(call => call[0] === 'create').pop();
  check('建班提交的是学年 + 学期，不是自由文本',
    sent && sent[1].academic_year === '2026-2027' && sent[1].term_kind === '秋' && !('term' in sent[1]),
    JSON.stringify(sent && sent[1]));

  const asked = [];
  sandbox.confirm = message => { asked.push(message); return false; };
  await App.actions['classes-delete']({ dataset: { id: 'class-a', name: '数学分析II-01班', count: '2' } });
  check('删班提示里写明会移除几个人', asked.length === 1 && asked[0].includes('2 名学生'));

  console.log('');
  if (failures.length) {
    console.log(`失败 ${failures.length} 项：${failures.join('、')}`);
    process.exit(1);
  }
  console.log('班级与选课页测试全部通过。');
})();
