#!/usr/bin/env node
/**
 * 考试工作台：新设计的七步流程入口。
 *
 * 要钉住的：
 *   1. 七步顺序固定（蓝图→拼卷→编辑→审核→作答→评分→导入导出）；
 *   2. 有当前课程时直连该课程页面；没有时去选课页，**不替老师默认选课**；
 *   3. 每条都有 href 与说明，不留死链；
 *   4. 页面把三条规定写出来（候选来自冻结 Release / 判不出来不判错 / 成绩只能追加）。
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const files = ['js/core.js', 'js/exam.js'];

const sandbox = {
  console: { log: console.log, warn() {}, error() {} },
  setTimeout, clearTimeout,
  document: { getElementById: () => null, addEventListener() {}, querySelector: () => null,
    querySelectorAll: () => [], body: { classList: { add() {}, remove() {} } } },
  location: { hash: '' },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  URLSearchParams
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(files.map(file => fs.readFileSync(path.join(root, file), 'utf8')).join('\n;\n'),
  sandbox, { filename: 'exam.js' });

const App = sandbox.window.App;
App.icon = () => '<svg/>';
App.button = (label, action) => `<button data-action="${action}">${label}</button>`;
App.url = (page, courseId) => (courseId ? `#/course/${courseId}/${page}` : `#/${page}`);
App.course = null;
App.state = {};

const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};

const steps = App.examSteps();
check('七步流程', steps.length === 7, String(steps.length));
check('第一步是蓝图', steps[0].page === 'paper-build');
check('最后一步是导入导出', steps[steps.length - 1].page === 'qti-transfer');
check('每步都有说明', steps.every(step => step.what && step.what.length > 10));
check('没有重复的页面', new Set(steps.map(step => step.page)).size === steps.length);

check('无课程时去选课页',
  App.examStepHref(steps[0], null) === '#/pick/paper-build');
check('有课程时直连该课程页',
  App.examStepHref(steps[1], 'analysis') === '#/course/analysis/paper-assembly');
check('非法步骤会抛错', (() => {
  try { App.examStepHref(null, null); return false; } catch (error) { return true; }
})());

const html = App.views.exam();
check('页面标题是「命题与考试工作台」', html.includes('命题与考试工作台'));
check('七个入口都渲染出来',
  steps.every(step => html.includes(`data-step="${step.key}"`)));
check('提示先选课', html.includes('选择课程'));
check('写出「候选来自冻结 Release」', html.includes('冻结 Release'));
check('写出「判不出来 ≠ 判错了」', html.includes('判不出来'));
check('写出「成绩只能追加」', html.includes('成绩只能追加'));

check('注册了视图', typeof App.views.exam === 'function');
check('注册了刷新动作', typeof App.actions['exam-refresh'] === 'function');
check('注册了进入钩子', typeof App.after.exam === 'function');

console.log();
if (failures.length) {
  console.log(`✗ ${failures.length} 项未通过：${failures.join('、')}`);
  process.exit(1);
}
console.log('✓ 考试工作台视图全部通过');
