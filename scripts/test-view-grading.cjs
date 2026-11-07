#!/usr/bin/env node
/**
 * 教师复核台与学生成绩页：确认≠发布、撤回留原因、学生只见已发布版本。
 *
 * 要钉住的：
 *   1. confirm 请求体只含 decisions/base/max_scores（服务端 extra=forbid）；
 *   2. 逐项合计可核对，空输入会算出 0 而不是 NaN；
 *   3. 错误码要原样呈现（教师需要知道是幂等键缺失还是分数越界）；
 *   4. 学生页没有成绩时给出"确认≠发布"的解释，而不是空白；
 *   5. 已发布成绩带"申诉这一版"，状态可回显。
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');

const sandbox = {
  console: { log: console.log, warn() {}, error() {} },
  setTimeout, clearTimeout,
  document: { getElementById: () => null, addEventListener() {}, querySelector: () => null,
    querySelectorAll: () => [], body: { classList: { add() {}, remove() {} } } },
  location: { hash: '#/course/analysis/grading-review' },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  URLSearchParams
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(['js/core.js', 'js/grading.js'].map(read).join('\n;\n'), sandbox,
  { filename: 'grading.js' });

const App = sandbox.window.App;
App.icon = App.icon || (() => '<svg/>');
App.button = App.button || ((label, action) => `<button data-action="${action}">${label}</button>`);

const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};

// --- 确认请求体 ---------------------------------------------------------------
const body = App.gradingConfirmBody({
  itemScores: { 'item-a': '5.00', 'item-b': '9.00' },
  itemMaxScores: { 'item-a': '5.00', 'item-b': '10.00' },
  baseRevisionNo: 1
});
check('确认体包含两项决定', body.decisions.length === 2, JSON.stringify(body.decisions));
check('确认体只含三个顶层字段', Object.keys(body).length === 3, Object.keys(body).join(','));
check('确认体带基准版本', body.base_revision_no === 1);
check('确认体带每项满分', body.item_max_scores['item-b'] === '10.00');
check('分数以字符串传输（避免浮点误差）', typeof body.decisions[0].score === 'string');
check('空分项数组', body.decisions[0].criterion_scores.length === 0);

check('逐项合计正确', App.gradingTotal({ a: '5.00', b: '9.00' }) === 14);
check('空输入合计为 0 而不是 NaN', App.gradingTotal({}) === 0);
check('非法输入被忽略', App.gradingTotal({ a: 'abc' }) === 0);

// --- 错误文案 -----------------------------------------------------------------
check('幂等键错误原样呈现',
  App.gradingErrorText({ detail: { code: 'idempotency_key_required' } })
    .includes('idempotency_key_required'));
check('确认缺项错误可读',
  App.gradingErrorText({ detail: { code: 'review_incomplete' } }).includes('不能发布'));
check('未知错误回落到服务端 message',
  App.gradingErrorText({ detail: { code: 'x', message: '原始说明' } }) === '原始说明');
check('无结构的错误也不会抛',
  App.gradingErrorText(null) === '请求失败');

// --- 教师页渲染 ---------------------------------------------------------------
const review = App.views['grading-review']();
check('教师页有提交标识输入', review.includes('data-input="grading-submission"'));
check('教师页有逐项得分输入', review.includes('data-input="grading-items"'));
check('教师页三个动作齐全',
  review.includes('grading-confirm') && review.includes('grading-publish')
  && review.includes('grading-withdraw'));
check('教师页说明确认≠发布', review.includes('confirmed') && review.includes('发布'));

// --- 学生页渲染 ---------------------------------------------------------------
const emptyStudent = App.views['student-feedback']();
check('无成绩时解释"确认≠发布"', emptyStudent.includes('确认不等于发布'));

const state = App.state.studentFeedback = {
  submissionId: null, itemScores: {}, loading: false, loaded: true, error: null,
  notice: null, busy: null, result: null, appeals: {}, baseRevisionNo: 0,
  grades: [{ submission_id: 's-1', revision_no: 2, total_score: '14.00',
    published_at: '2026-11-07T00:00:00Z',
    items: [{ submission_item_id: 'i-1', score: '5.00' }] }]
};
sandbox.window.App.render = () => {};
const withGrades = App.views['student-feedback']();
check('学生页显示版本号与总分',
  withGrades.includes('第 2 版') && withGrades.includes('14.00'));
check('学生页提供申诉入口', withGrades.includes('feedback-appeal'));
check('未申诉时显示可申诉', App.gradingAppealState(state, 2) === '可申诉');
check('已申诉回显状态',
  App.gradingAppealState({ appeals: { 2: { status: 'open' } } }, 2)
    .includes('等待教师受理'));

check('注册了教师复核台视图', typeof App.views['grading-review'] === 'function');
check('注册了我的成绩视图', typeof App.views['student-feedback'] === 'function');
check('注册了三个教师动作',
  ['grading-confirm', 'grading-publish', 'grading-withdraw']
    .every(name => typeof App.actions[name] === 'function'));
check('注册了学生动作',
  ['feedback-refresh', 'feedback-appeal'].every(name => typeof App.actions[name] === 'function'));
check('注册了两个输入', ['grading-submission', 'grading-items']
  .every(name => typeof App.inputs[name] === 'function'));

console.log();
if (failures.length) {
  console.log(`✗ ${failures.length} 项未通过：${failures.join('、')}`);
  process.exit(1);
}
console.log('✓ 教师复核台与学生成绩页视图全部通过');
