#!/usr/bin/env node
/**
 * 学生作答台：本地时钟只用于显示、冲突不静默覆盖、交卷后只读。
 *
 * 要钉住的：
 *   1. 倒计时负数按 0（不会显示 -00:05 让人以为还能写）；
 *   2. 已交卷 / 已撤回 / 截止后一律不可编辑；
 *   3. 保存载荷只含三个字段（服务端 extra=forbid，多传就 422）；
 *   4. 冲突必须给出"服务端第几版"，否则学生不知道覆盖了什么；
 *   5. 回执里要出现哈希（申诉凭证）。
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
  location: { hash: '#/course/analysis/online-exam' },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  URLSearchParams
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(['js/core.js', 'js/online-exam.js'].map(read).join('\n;\n'), sandbox,
  { filename: 'online-exam.js' });

const App = sandbox.window.App;
App.icon = App.icon || (() => '<svg/>');
App.button = App.button || ((label, action) => `<button data-action="${action}">${label}</button>`);

const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};

const NOW = Date.parse('2026-11-07T01:00:00Z');
const DETAIL = {
  attempt_id: 'a-1', assignment_id: 'as-1', paper_version_id: 'p-1',
  status: 'in_progress', assignment_status: 'published', attempt_no: 1,
  started_at: '2026-11-07T00:30:00Z',
  server_now: '2026-11-07T01:00:00Z',
  server_deadline_at: '2026-11-07T02:00:00Z',
  worker_due_at: '2026-11-07T02:10:00Z',
  save_debounce_ms: 800, max_save_interval_ms: 5000, late_policy: 'read_only_grace',
  slots: [
    { slot_id: 's-1', slot_no: 1, stem: '求极限', input_kind: 'text',
      raw_response: { text: '我的答案' }, parse_status: 'parsed', revision_no: 2 },
    { slot_id: 's-2', slot_no: 2, stem: '证明连续', input_kind: 'text',
      raw_response: null, parse_status: 'empty', revision_no: 0 }
  ]
};

const stateOf = patch => ({ assignmentId: 'as-1', attemptId: 'a-1', detail: DETAIL,
  loading: false, loaded: true, error: null, message: null, busy: null,
  receipts: {}, conflicts: {}, clientIds: {}, receipt: null, ...patch });

// --- 倒计时 ---------------------------------------------------------------------
check('倒计时按服务端截止算', App.onlineExamCountdown(DETAIL, NOW) === '60:00',
  App.onlineExamCountdown(DETAIL, NOW));
check('过了截止显示 00:00 而不是负数',
  App.onlineExamCountdown(DETAIL, NOW + 3 * 3600 * 1000) === '00:00');
check('没有截止时间就不显示', App.onlineExamCountdown({}, NOW) === '');
check('时间戳非法也不抛异常',
  App.onlineExamCountdown({ server_deadline_at: 'nope' }, NOW) === '');

// --- 可编辑判定 -----------------------------------------------------------------
check('进行中可编辑', App.onlineExamEditable({ ...DETAIL, server_deadline_at: '2099-01-01T00:00:00Z' }));
check('已交卷不可编辑',
  App.onlineExamEditable({ ...DETAIL, status: 'submitted',
    server_deadline_at: '2099-01-01T00:00:00Z' }) === false);
check('已撤回不可编辑',
  App.onlineExamEditable({ ...DETAIL, assignment_status: 'withdrawn',
    server_deadline_at: '2099-01-01T00:00:00Z' }) === false);
check('截止后不可编辑',
  App.onlineExamEditable({ ...DETAIL, server_deadline_at: '2020-01-01T00:00:00Z' }) === false);

// --- 保存载荷 -------------------------------------------------------------------
const body = App.onlineExamSaveBody({ slot_id: 's-1', revision_no: 2,
  draftText: '改过的答案', clientRevisionId: 'rev-1' });
check('载荷只有三个字段', Object.keys(body).length === 3, JSON.stringify(body));
check('载荷带基准版本', body.base_revision_no === 2);
check('载荷带客户端修订标识', body.client_revision_id === 'rev-1');
check('载荷把输入放进 raw_response',
  body.raw_response.text === '改过的答案');

// --- 冲突文案 -------------------------------------------------------------------
check('冲突文案写出服务端版本',
  App.onlineExamConflictText({ server_result: { revision_no: 2 } }).includes('第 2 版'));
check('没有服务端版本时说明无法回退',
  App.onlineExamConflictText({ server_result: { revision_no: 0 } }).includes('还没有'));
check('空冲突返回空串', App.onlineExamConflictText(null) === '');

// --- 页面渲染 -------------------------------------------------------------------
// 渲染断言不能用"写死的 2026-11-07 02:00"：随着真实时间流逝，考试会被判成已截止，
// 于是"有交卷按钮"就随日期变化而失败（这条测试自己就抓到过）。用远期截止。
const FUTURE = { ...DETAIL, server_deadline_at: '2099-01-01T00:00:00Z' };
const runningHtml = App.onlineExamHtml(stateOf({ detail: FUTURE }));
check('作答中显示状态徽章', runningHtml.includes('作答中'));
check('作答中给出倒计时', runningHtml.includes('data-exam-clock'));
check('作答中有交卷按钮', runningHtml.includes('exam-finalize'));
check('每道题都有输入框', runningHtml.includes('data-input="exam-answer"'));
check('已保存的答案回填到输入框', runningHtml.includes('我的答案'));

const submittedHtml = App.onlineExamHtml(stateOf({
  detail: { ...DETAIL, status: 'submitted' },
  receipt: { item_count: 2, unanswered_count: 1, invalid_count: 0,
    receipt_hash: 'f'.repeat(64) }
}));
check('已交卷显示回执', submittedHtml.includes('已交卷'));
check('已交卷显示回执哈希', submittedHtml.includes('ffffffffffffffff'));
check('已交卷没有交卷按钮', !submittedHtml.includes('exam-finalize'));
check('已交卷输入框只读', submittedHtml.includes('readonly'));

const conflictHtml = App.onlineExamHtml(stateOf({
  conflicts: { 's-1': { server_result: { revision_no: 2, raw_response: { text: '服务端版本' } } } }
}));
check('冲突用 alert 展示', conflictHtml.includes('role="alert"'));
check('冲突给出两个选择',
  conflictHtml.includes('exam-use-server') && conflictHtml.includes('exam-keep-mine'));

const emptyHtml = App.onlineExamHtml(stateOf({ detail: null, attemptId: null }));
check('未开始显示开始按钮', emptyHtml.includes('exam-start'));
check('未开始说明计时由服务端判定', emptyHtml.includes('服务端'));

check('错误按码翻译', App.onlineExamError({ detail: { code: 'attempt_finalized' } })
  .includes('结束'));
check('未知错误回落到 message',
  App.onlineExamError({ detail: { code: 'x', message: '原始说明' } }) === '原始说明');

check('注册了视图 online-exam', typeof App.views['online-exam'] === 'function');
check('注册了进入后取数钩子', typeof App.after['online-exam'] === 'function');
check('注册了五个动作', ['exam-refresh', 'exam-start', 'exam-finalize',
  'exam-use-server', 'exam-keep-mine'].every(name => typeof App.actions[name] === 'function'));
check('注册了作答输入', typeof App.inputs['exam-answer'] === 'function');

console.log();
if (failures.length) {
  console.log(`✗ ${failures.length} 项未通过：${failures.join('、')}`);
  process.exit(1);
}
console.log('✓ 在线作答台视图全部通过');
