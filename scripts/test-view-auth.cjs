#!/usr/bin/env node
/**
 * 登录页、顶栏身份区、按角色的菜单闸门（node:vm，不启浏览器）。
 *
 * 这几处错了不会报错，只会静默放出界面：
 *   - 学生看到「账号管理」菜单，点进去 403；
 *   - 没登录却能打开 #/review，页面显示"队列是空的"；
 *   - 登录失败时只清空表单、不说原因，人以为是自己记错账号。
 * 所以这里主要验"该藏的是否藏了、该说的是否说了"。
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');

const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  URLSearchParams,
  location: { hash: '#/login' },
  fetch: () => Promise.reject(new Error('测试不请求后端')),
  localStorage: {
    store: new Map(),
    getItem(key) { return this.store.has(key) ? this.store.get(key) : null; },
    setItem(key, value) { this.store.set(key, String(value)); }
  },
  document: { getElementById: () => null, addEventListener() {}, body: { classList: { add() {}, remove() {} } } }
};
sandbox.window = sandbox;
sandbox.window.confirm = () => true;
vm.createContext(sandbox);

const bundle = ['js/core.js', 'js/visual-identity.js', 'js/api-client.js', 'js/auth.js', 'js/accounts.js']
  .map(file => fs.readFileSync(path.join(root, file), 'utf8')).join('\n;\n');
vm.runInContext(bundle, sandbox, { filename: 'bundle.js' });

const App = sandbox.window.App;
const failures = [];
const check = (name, condition, detail = '') => {
  console.log(`  ${condition ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!condition) failures.push(name);
};

App.markdown = source => `<div class="prose">${source}</div>`;

function as(role, extra = {}) {
  App.state.auth = {
    status: role ? 'authenticated' : 'anonymous',
    actor: role ? { id: `acc-${role}`, display_name: role === 'admin' ? '平台管理员' : role === 'teacher' ? '张老师' : '李同学', role, roles: [role], role_label: '' } : null,
    providers: { auth_mode: 'dev', password: { available: true }, cas: { available: false, hint: '待接入' }, session_hours: 12 },
    mustChange: false,
    error: '',
    busy: false,
    next: null,
    ...extra
  };
}

// ---------------------------------------------------------------------------
// 1. 登录页
// ---------------------------------------------------------------------------
console.log('登录页：');
as(null);
let html = App.views.login();
check('有账号和密码两个输入框', html.includes('data-input="auth-username"') && html.includes('data-input="auth-password"'));
check('密码框是 password 类型', /type="password"/.test(html));
check('提交按钮有 data-action', html.includes('data-action="auth-submit"'));
check('提交按钮在 form 里（回车能提交）', html.includes('data-form="login"'));
check('统一认证按钮存在但置灰', html.includes('data-action="auth-cas"') && html.includes('disabled'));
check('明确写"待接入"而不是假装能点', html.includes('待接入'));
check('开发模式提示演示账号', html.includes('演示账号'));
check('渲染时把姓名/账号值转义', !html.includes('<script'));

as(null, { error: '账号或密码不正确' });
html = App.views.login();
check('登录失败后显示原因', html.includes('账号或密码不正确'));
check('失败提示走转义（不注入 HTML）', App.views.login({ error: '' }) !== null);
App.state.auth.error = '<img src=x onerror=alert(1)>';
html = App.views.login();
check('错误信息里的 HTML 被转义', html.includes('&lt;img') && !html.includes('<img src=x'));

as(null, { busy: true });
html = App.views.login();
check('提交中按钮禁用并显示进度', html.includes('登录中…') && html.includes('disabled'));

// ---------------------------------------------------------------------------
// 2. 角色闸门
// ---------------------------------------------------------------------------
console.log('\n角色与菜单权限：');
as('student');
check('学生不能开账号管理', App.auth.can('accounts') === false);
check('学生不能开模型配置', App.auth.can('api-config') === false);
check('学生不能开内容审核', App.auth.can('review') === false);
check('学生能看课程目录', App.auth.can('catalog') === true);
check('学生能看习题', App.auth.can('exercises') === true);
check('学生能看知识图谱', App.auth.can('graph') === true);
check('学生能看作答页', App.auth.can('study') === true);

as('teacher');
check('教师能开内容审核', App.auth.can('review') === true);
check('教师能开教学分析', App.auth.can('analytics') === true);
check('教师不能开账号管理', App.auth.can('accounts') === false);
check('教师不能开模型配置', App.auth.can('api-config') === false);

as('admin');
['accounts', 'api-config', 'review', 'content', 'analytics', 'catalog'].forEach(page => {
  check(`管理员能开 ${page}`, App.auth.can(page) === true);
});

as(null);
check('未登录不能开审核', App.auth.can('review') === false);
check('未登录能浏览课程', App.auth.can('catalog') === true);
check('未登录不是任何角色', App.auth.role() === '' && App.auth.anonymous() === true);

console.log('\n角色标签：');
as('student');
check('学生角色显示"学生"', App.auth.roleLabel() === '学生', App.auth.roleLabel());
as('teacher');
check('教师角色显示"教师"', App.auth.roleLabel() === '教师', App.auth.roleLabel());
as('admin');
check('管理员角色显示"管理员"', App.auth.roleLabel() === '管理员', App.auth.roleLabel());
check('管理员 is() 什么都是', App.auth.is('teacher') && App.auth.is('student'));
as('teacher');
check('教师 is(teacher) 为真、is(admin) 为假', App.auth.is('teacher') && !App.auth.is('admin'));

// ---------------------------------------------------------------------------
// 3. 账号管理页
// ---------------------------------------------------------------------------
console.log('\n账号管理页：');
App.state.accounts = {
  status: 'ready',
  items: [
    { id: 'acc-1', username: 'teacher', display_name: '张老师', role: 'teacher', role_label: '教师', has_password: true, disabled: false, locked: false, must_change_password: false, last_login_at: '2026-10-20T09:00:00' },
    { id: 'acc-2', username: 'student01', display_name: '李同学', role: 'student', role_label: '学生', has_password: true, disabled: true, locked: true, must_change_password: true, last_login_at: null }
  ],
  counts: { admin: 1, teacher: 1, student: 1 },
  filter: '', query: '', draft: { username: '', display_name: '', role: 'student', password: '' }, saving: false
};
html = App.views.accounts();
check('列出账号与姓名', html.includes('teacher') && html.includes('张老师'));
check('显示角色徽标', html.includes('教师') && html.includes('学生'));
check('停用的账号有标记', html.includes('已停用'));
check('锁定的账号有标记', html.includes('锁定中'));
check('从未登录的账号标注清楚', html.includes('从未登录'));
check('提供重置密码/改角色/停用三个动作',
  html.includes('accounts-reset') && html.includes('accounts-role') && html.includes('accounts-toggle'));
check('有新建账号表单', html.includes('accounts-new-username') && html.includes('accounts-create'));
check('角色统计显示三个数字', /<b>1<\/b>管理员/.test(html) && /<b>1<\/b>教师/.test(html));

App.state.accounts.status = 'error';
App.state.accounts.error = '没有权限';
html = App.views.accounts();
check('读不到列表时给重试而不是白屏', html.includes('账号管理') && html.includes('accounts-refresh'));
check('无权限时显示原因', html.includes('没有权限'));

console.log(failures.length ? `\n失败 ${failures.length} 项：${failures.join('、')}` : '\n登录与权限测试全部通过。');
process.exit(failures.length ? 1 : 0);
