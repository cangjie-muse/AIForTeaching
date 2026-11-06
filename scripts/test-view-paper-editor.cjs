#!/usr/bin/env node
/**
 * 卷面编辑器：渲染与 PATCH 载荷。
 *
 * 要钉住的：
 *   1. 分值必须是两位以内小数，前端先挡（服务端仍会再挡）；
 *   2. 只有**真的变了**的字段才进 operations——没变也发一条会让 edit_seq 白涨；
 *   3. 调序只能在分节内，越界要报错而不是被服务端静默修正；
 *   4. base_version 取当前 edit_seq（乐观锁的锚点）；
 *   5. 缺来源的槽位在页面上就标出来，别等发布时才 422。
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
  location: { hash: '#/course/analysis/paper-editor' },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} }
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(['js/core.js', 'js/paper-editor.js'].map(read).join('\n;\n'), sandbox,
  { filename: 'paper-editor.js' });

const App = sandbox.window.App;
App.icon = App.icon || (() => '<svg/>');
App.button = App.button || ((label, action) => `<button data-action="${action}">${label}</button>`);

const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};
const throws = fn => { try { fn(); return null; } catch (error) { return error; } };

const paper = {
  id: 'pv-1', status: 'draft', total_score: '100.00', edit_seq: 4,
  sections: [
    { code: 'A', title: '计算题', section_score: '60.00', slots: [
      { id: 's1', code: 'A1', slot_no: 1, score: '30.00',
        exercise_version_id: 'v1', release_context_id: 'r1' },
      { id: 's2', code: 'A2', slot_no: 2, score: '30.00',
        exercise_version_id: null, release_context_id: null }
    ] },
    { code: 'B', title: '证明题', section_score: '40.00', slots: [
      { id: 's3', code: 'B1', slot_no: 1, score: '40.00',
        exercise_version_id: 'v3', release_context_id: 'r1' }
    ] }
  ]
};

// --- 渲染 ---------------------------------------------------------------------
const html = App.paperEditorHtml(paper);
check('每个槽位一行', (html.match(/data-slot="/g) || []).length === 3);
check('分值以输入框给出', (html.match(/data-field="score"/g) || []).length === 3);
check('缺来源的槽位被标出', html.includes('缺来源'));
check('完整的槽位标为完整', html.includes('完整'));
check('分节标题带分值', html.includes('计算题') && html.includes('60.00'));
check('有保存按钮', html.includes('paper-save'));
check('空卷面不抛异常', App.paperEditorHtml({ sections: [] }).includes('还没有分节'));
check('null 卷面不抛异常', App.paperEditorHtml(null).includes('还没有读取到卷面'));
const hostile = App.paperEditorHtml({ status: '<img src=x>', total_score: '1', edit_seq: 1, sections: [] });
check('状态文本被转义', !hostile.includes('<img') && hostile.includes('&lt;img'));

// --- 载荷 ---------------------------------------------------------------------
const unchanged = App.paperEditPayload(paper, { score: { s1: '30.00' } });
check('没变的分值不进 operations', unchanged.operations.length === 0);
check('base_version 取 edit_seq', unchanged.base_version === 4);

const changed = App.paperEditPayload(paper, { score: { s1: '35.5' } });
check('改分值产生 score 操作', changed.operations.length === 1
  && changed.operations[0].kind === 'score'
  && changed.operations[0].slot_id === 's1'
  && changed.operations[0].score === '35.50');

const moved = App.paperEditPayload(paper, { move: [{ slotId: 's2', to: 1 }] });
check('调序产生 move 操作', moved.operations.length === 1
  && moved.operations[0].kind === 'move'
  && moved.operations[0].target_slot_no === 1);

check('同位置调序被忽略', App.paperEditPayload(paper, { move: [{ slotId: 's1', to: 1 }] })
  .operations.length === 0);

check('三位小数被拒', throws(() => App.paperEditPayload(paper, { score: { s1: '10.001' } })) !== null);
check('非数字被拒', throws(() => App.paperEditPayload(paper, { score: { s1: 'abc' } })) !== null);
check('跨分节越界被拒', throws(() => App.paperEditPayload(paper, { move: [{ slotId: 's3', to: 2 }] })) !== null);
check('0 位置被拒', throws(() => App.paperEditPayload(paper, { move: [{ slotId: 's1', to: 0 }] })) !== null);

// --- 错误文案 ----------------------------------------------------------------
check('409 过期给的是"别人改过了"', App.paperEditError({ code: 'stale_version' }).includes('别人刚刚改过'));
check('非草稿给的是"克隆新卷"', App.paperEditError({ code: 'paper_not_draft' }).includes('克隆'));
check('未知错误回落到 message', App.paperEditError({ code: 'x', message: '原始错误' }) === '原始错误');

console.log();
if (failures.length) {
  console.log(`✗ ${failures.length} 项未通过：${failures.join('、')}`);
  process.exit(1);
}
console.log('✓ 卷面编辑器视图全部通过');
