#!/usr/bin/env node
/**
 * QTI 导入导出页：差异分组、错误的可读翻译、导出被拒原因。
 *
 * 要钉住的：
 *   1. error 排在前面（教师先看必须处理的）；
 *   2. 差异码翻成人话，未知码原样显示（不吞掉信息）；
 *   3. draft_ready 明确说"是草稿"，student 不会误以为已审核；
 *   4. 导出 blocked 时列 reasons；
 *   5. 页面注册与输入/动作齐全。
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
  location: { hash: '#/course/analysis/qti-transfer' },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  URLSearchParams
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(['js/core.js', 'js/qti.js'].map(read).join('\n;\n'), sandbox,
  { filename: 'qti.js' });

const App = sandbox.window.App;
App.icon = App.icon || (() => '<svg/>');
App.button = App.button || ((label, action) => `<button data-action="${action}">${label}</button>`);

const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};

// --- 差异分组 --------------------------------------------------------------
const groups = App.qtiGroupDifferences([
  { code: 'missing_prompt', severity: 'error', message: 'q-1 没有题干' },
  { code: 'unsupported_interaction', severity: 'warning', message: '已降级' },
  { code: 'invalid_score', severity: 'error', message: '分值不是正数' },
  { code: 'weird', severity: 'unknown', message: '未知等级' }
]);
check('error 组有两条', groups.error.length === 2, String(groups.error.length));
check('未知等级归入 info', groups.info.length === 1);
check('组内按 code 排序',
  groups.error[0].code === 'invalid_score' && groups.error[1].code === 'missing_prompt');

// --- 差异码翻译 ------------------------------------------------------------
check('已知码翻成人话', App.qtiCodeText('missing_prompt') === '缺少题干');
check('slot 前缀码带槽位号',
  App.qtiCodeText('slot_A1_missing_release').includes('缺来源'));
check('未知码原样返回', App.qtiCodeText('something_new') === 'something_new');
check('空码不抛异常', App.qtiCodeText(null) === '未识别的差异');

// --- 状态文案 --------------------------------------------------------------
check('draft_ready 明说是草稿',
  App.qtiStatusText({ status: 'draft_ready', item_count: 3 }).includes('草稿'));
check('rejected 给出错误数',
  App.qtiStatusText({ status: 'rejected', error_count: 2 }).includes('2'));
check('导出成功给哈希前缀',
  App.qtiExportText({ status: 'exported', item_count: 2,
    payload_sha256: 'abcdef0123456789' }).includes('abcdef012345'));
check('导出被拒给原因数',
  App.qtiExportText({ status: 'blocked', reasons: ['a', 'b'] }).includes('2'));

// --- 渲染 ------------------------------------------------------------------
const empty = App.views['qti-transfer']();
check('页面有课程号输入', empty.includes('data-input="qti-course"'));
check('页面有文件内容输入', empty.includes('data-input="qti-document"'));
check('页面有导入与导出按钮',
  empty.includes('qti-import') && empty.includes('qti-export'));
check('页面说明导入不继承审核',
  empty.includes('不会继承'));

const withRun = App.qtiHtml({
  courseRef: 'analysis', document: '{}',
  run: { status: 'rejected', error_count: 1,
    differences: [{ code: 'missing_prompt', severity: 'error', message: 'q-1 没有题干' }] },
  export: { status: 'blocked', reasons: ['paper_status_draft'] }
});
check('拒绝时展示差异', withRun.includes('data-severity="error"'));
check('拒绝时说明需处理项', withRun.includes('必须处理'));
check('导出拒绝列出原因', withRun.includes('卷子还没批准') || withRun.includes('paper_status_draft'));

check('注册了视图', typeof App.views['qti-transfer'] === 'function');
check('注册了动作',
  ['qti-import', 'qti-export'].every(name => typeof App.actions[name] === 'function'));
check('注册了输入',
  ['qti-course', 'qti-document'].every(name => typeof App.inputs[name] === 'function'));
check('注册了进入钩子', typeof App.after['qti-transfer'] === 'function');

console.log();
if (failures.length) {
  console.log(`✗ ${failures.length} 项未通过：${failures.join('、')}`);
  process.exit(1);
}
console.log('✓ QTI 导入导出页视图全部通过');
