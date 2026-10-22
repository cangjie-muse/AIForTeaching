#!/usr/bin/env node
/**
 * Excel 导入/导出在页面上的行为：表头映射、预览、确认、结果、导出列。
 *
 * 读文件那一步换成假 AppXlsx（真 xlsx 读写另有 test-xlsx-sheet.cjs 用真库测），
 * 这里只验「表 → 请求 → 界面」这条链路别接错。
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');

const downloads = [];
const grid = { sheetName: '账号', rows: [] };

const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  URLSearchParams,
  location: { hash: '#/accounts' },
  fetch: () => Promise.reject(new Error('测试不请求后端')),
  localStorage: {
    store: new Map(),
    getItem(key) { return this.store.has(key) ? this.store.get(key) : null; },
    setItem(key, value) { this.store.set(key, String(value)); }
  },
  document: { getElementById: () => null, addEventListener() {}, body: { classList: { add() {}, remove() {} } } }
};
sandbox.window = sandbox;
sandbox.Blob = class {};
sandbox.URL = { createObjectURL: () => 'blob:test', revokeObjectURL: () => {} };
sandbox.window.confirm = () => true;
sandbox.AppXlsx = {
  readGrid: async () => grid,
  downloadSheet: async (filename, options) => { downloads.push({ filename, options }); },
  download: () => {}
};
vm.createContext(sandbox);

const bundle = ['js/core.js', 'js/visual-identity.js', 'js/api-client.js',
  'js/roster-io.js', 'js/accounts.js', 'js/classes.js']
  .map(file => fs.readFileSync(path.join(root, file), 'utf8')).join('\n;\n');
vm.runInContext(bundle, sandbox, { filename: 'bundle.js' });

const App = sandbox.window.App;
App.render = () => {};
App.markdown = source => source;
const toasts = [];
App.toast = message => toasts.push(message);
const calls = [];
App.api.get = async () => ({ items: [], counts: [] });
App.api.accounts = async () => ({ items: [], counts: {} });
App.api.classes = async () => ({ items: [], total: 0, students: 0 });
App.api.classStudents = async id => {
  calls.push(['classStudents', id]);
  return { items: [{ username: '2023301001', display_name: '张三', enrolled_at: '2026-10-01T08:00:00Z', added_by: '老师' }] };
};

const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};

function resetAccounts(overrides = {}) {
  App.state.accounts = {
    status: 'ready', error: '', items: [
      { username: '2023301001', display_name: '张三', role: 'student', role_label: '学生',
        disabled: false, has_password: true, last_login_at: '2026-10-01T08:00:00Z',
        created_at: '2026-09-01T08:00:00Z' }
    ],
    counts: { admin: 1, teacher: 2, student: 30 }, filter: '', query: '', saving: false,
    draft: { username: '', display_name: '', role: 'student', password: '' },
    ...overrides
  };
}

function resetClasses(overrides = {}) {
  App.state.classes = {
    status: 'ready', error: '', items: [
      { id: 'class-1', course_id: 'analysis-ii', course_title: '数学分析 II',
        name: '数学分析II-01班', academic_year: '2026-2027', term_kind: '秋',
        term: '2026年秋季学期', teacher_name: '张老师', student_count: 3, note: '' }
    ],
    total: 1, seats: 3, courseId: '', openClassId: 'class-1',
    roster: { status: 'ready', error: '', items: [
      { student_id: 'acc-1', username: '2023301001', display_name: '张三',
        enrolled_at: '2026-10-01T08:00:00Z', added_by: '张老师' }
    ] },
    picker: { open: false, query: '', status: 'idle', error: '', items: [], selected: [] },
    form: { courseId: '', name: '', academicYear: '', termKind: '' },
    saving: false,
    ...overrides
  };
}

function fakeFile(name) {
  return { name, arrayBuffer: async () => new ArrayBuffer(8) };
}

function pageTest() {
  console.log('账号页：');
  resetAccounts();
  const html = App.views.accounts();
  check('有批量导入区与选择文件入口',
    html.includes('roster-file-accounts') && html.includes('批量导入账号'));
  check('有模板下载与导出当前列表',
    html.includes('data-action="roster-template" data-kind="accounts"')
    && html.includes('data-action="roster-export" data-kind="accounts"'));
}

function mappingTest() {
  console.log('\n表头映射：');
  grid.rows = [
    ['姓名', '账号', '角色', '初始密码'],
    ['张三', '2023301001', '学生', ''],
    ['', '', '', ''],
    ['李四', 'teacher02', '教师', 'InitPass2026']
  ];
  const parsed = App.rosterIo.parseTable(grid, 'accounts');
  check('列顺序打乱也能对上', parsed.rows && parsed.rows.length === 2,
    JSON.stringify(parsed.rows && parsed.rows.map(row => row.cells.username)));
  check('按表头取到正确的值',
    parsed.rows && parsed.rows[0].cells.username === '2023301001'
    && parsed.rows[0].cells.display_name === '张三');
  check('行号用 Excel 的真实行号（数据从第 2 行起）',
    parsed.rows && parsed.rows[0].line === 2 && parsed.rows[1].line === 4,
    JSON.stringify(parsed.rows && parsed.rows.map(row => row.line)));
  check('全空行被跳过', parsed.rows && parsed.rows.length === 2);

  grid.rows = [['账号', '姓名'], ['2023301001', '张三']];
  const missing = App.rosterIo.parseTable(grid, 'classes');
  check('缺必填列时说明该有哪些列',
    missing.error && missing.error.includes('课程') && missing.error.includes('第一行应该是'),
    missing.error || '');

  grid.rows = [['账号', '姓名']];
  const emptyRows = App.rosterIo.parseTable(grid, 'accounts');
  check('只有表头时提示没有数据行', Boolean(emptyRows.error), emptyRows.error || '');
}

async function previewTest() {
  console.log('\n预览：');
  grid.rows = [['账号', '姓名', '角色'], ['imp-001', '新同学', '学生'],
    ['e2e-teacher', '老账号', '教师'], ['bad name', '错的', '学生']];
  App.state.roster = {};
  App.api.importAccounts = async payload => {
    calls.push(['importAccounts', payload]);
    return {
      kind: 'accounts', dry_run: payload.dry_run,
      results: [
        { line: 2, status: 'ok', action: 'create', values: { username: 'imp-001', display_name: '新同学', role: '学生' }, reason: '将新建账号', will_create_password: true },
        { line: 3, status: 'skip', action: 'create', values: { username: 'e2e-teacher', display_name: '老账号', role: '教师' }, reason: '账号已存在（端到端教师）' },
        { line: 4, status: 'error', action: 'create', values: { username: 'bad name', display_name: '错的', role: '学生' }, reason: '账号只能用字母、数字、点、下划线、短横线' }
      ],
      summary: { rows: 3, ok: 1, skip: 1, error: 1, created: 0 },
      passwords: []
    };
  };
  calls.length = 0;
  await App.inputs['roster-file-accounts']({ files: [fakeFile('账号.xlsx')] });
  const html = App.views.accounts();
  check('选完文件先出预览、不落库',
    calls[0] && calls[0][1].dry_run === true && App.state.roster.accounts.step === 'preview',
    JSON.stringify(calls[0] && calls[0][1].dry_run));
  check('预览逐行写状态与原因',
    html.includes('将新建') && html.includes('跳过') && html.includes('有问题'));
  check('预览给出统计', html.includes('3</b> 行') && html.includes('可导入 <b>1</b>'));
  check('预览阶段只允许导入可导入的行（1 行）', html.includes('确认导入 1 行'));
  check('预览阶段还没有密码表', !html.includes('初始密码（只显示这一次）'));
}

async function confirmTest() {
  console.log('\n确认导入：');
  App.api.importAccounts = async payload => {
    calls.push(['importAccounts', payload]);
    return {
      kind: 'accounts', dry_run: payload.dry_run,
      results: [
        { line: 2, status: 'ok', action: 'create', values: { username: 'imp-001', display_name: '新同学' }, reason: '已新建', created: true, password: 'Kp7mQ2xRta' }
      ],
      summary: { rows: 1, ok: 1, skip: 0, error: 0, created: 1 },
      passwords: [{ line: 2, username: 'imp-001', display_name: '新同学', password: 'Kp7mQ2xRta', generated: true }]
    };
  };
  calls.length = 0;
  await App.actions['roster-import']({ dataset: { kind: 'accounts', class: '' }, disabled: false });
  check('确认后带 dry_run=false 再请求', calls[0][1].dry_run === false,
    JSON.stringify(calls[0] && calls[0][1].dry_run));
  const html = App.views.accounts();
  check('结果里给出初始密码表',
    html.includes('初始密码（只显示这一次）') && html.includes('Kp7mQ2xRta'));
  check('结果里说明新增数量', html.includes('新增 <b>1</b>'));
}

async function downloadTest() {
  console.log('\n下载：');
  downloads.length = 0;
  await App.actions['roster-passwords']({ dataset: { kind: 'accounts' } });
  check('能下载初始密码表',
    downloads[0] && downloads[0].filename === '初始密码表.xlsx'
    && downloads[0].options.rows[0][2] === 'Kp7mQ2xRta',
    JSON.stringify(downloads[0] && downloads[0].filename));

  downloads.length = 0;
  await App.actions['roster-export']({ dataset: { kind: 'accounts' } });
  const sheet = downloads[0] && downloads[0].options;
  check('账号导出不含密码列',
    sheet && !sheet.header.some(title => title.includes('密码')),
    JSON.stringify(sheet && sheet.header));
  check('账号导出带角色与登录方式',
    sheet && sheet.header.includes('角色') && sheet.header.includes('登录方式'));

  downloads.length = 0;
  await App.actions['roster-template']({ dataset: { kind: 'classes' } });
  check('班级模板列与约定一致',
    downloads[0] && downloads[0].options.header.join(',') === '课程,班级名称,学年,学期,任课教师,备注',
    JSON.stringify(downloads[0] && downloads[0].options.header));
  check('模板带填写说明', downloads[0] && downloads[0].options.notes.length >= 3,
    String(downloads[0] && downloads[0].options.notes.length));
}

async function classPageTest() {
  console.log('\n班级页：');
  resetClasses();
  App.state.roster = {};
  const html = App.views.classes();
  check('建班面板里有批量建班', html.includes('批量建班') && html.includes('roster-file-classes'));
  check('名单面板里有导入与导出',
    html.includes('roster-file-students')
    && html.includes('data-action="roster-export" data-kind="students"'));
  check('名单导出按钮写着人数', html.includes('导出名单（1 人）'));

  downloads.length = 0;
  calls.length = 0;
  await App.actions['roster-export']({ dataset: { kind: 'students', class: 'class-1' } });
  check('名单导出取的是这一班的名单',
    calls.some(call => call[0] === 'classStudents' && call[1] === 'class-1'),
    JSON.stringify(calls));
  check('名单导出列是学号/姓名/时间/加入人',
    downloads[0] && downloads[0].options.header.join(',') === '学号,姓名,加入时间,加入人',
    JSON.stringify(downloads[0] && downloads[0].options.header));
}

async function rosterImportTest() {
  console.log('\n名单导入：');
  grid.rows = [['学号', '姓名'], ['2023999001', '新同学']];
  App.state.roster = {};
  App.views.classes();
  App.api.importStudents = async (classId, payload) => {
    calls.push(['importStudents', classId, payload]);
    return {
      kind: 'students', dry_run: payload.dry_run,
      results: [{ line: 2, status: 'ok', action: 'add', values: { username: '2023999001', display_name: '新同学' }, reason: '将新建学生账号并加入（首次登录要改密码）', will_create_account: true }],
      summary: { rows: 1, ok: 1, skip: 0, error: 0, created: 0 },
      passwords: [], class_id: classId
    };
  };
  calls.length = 0;
  App.views.classes();
  await App.inputs['roster-file-students']({ files: [fakeFile('名单.xlsx')] });
  check('名单导入打到这个班', calls[0] && calls[0][1] === 'class-1',
    JSON.stringify(calls[0] && calls[0][1]));
  check('名单预览提示会建账号', App.views.classes().includes('将自动建账号'));
}

async function errorTest() {
  console.log('\n错误与清空：');
  grid.rows = [['学号'], ['1']];
  App.state.roster = {};
  App.views.classes();
  App.api.importStudents = async () => { throw new Error('网络断了'); };
  await App.inputs['roster-file-students']({ files: [fakeFile('名单.xlsx')] });
  check('请求失败时把原因写在页面上', App.views.classes().includes('网络断了'));
  await App.actions['roster-reset']({ dataset: { kind: 'students' } });
  check('取消后回到初始状态',
    App.state.roster.students.step === 'idle'
    && !App.views.classes().includes('网络断了'));
}

(async () => {
  pageTest();
  mappingTest();
  await previewTest();
  await confirmTest();
  await downloadTest();
  await classPageTest();
  await rosterImportTest();
  await errorTest();

  console.log();
  if (failures.length) {
    console.log(`失败 ${failures.length} 项：${failures.join('、')}`);
    process.exit(1);
  }
  console.log('Excel 导入导出界面测试全部通过。');
})();
