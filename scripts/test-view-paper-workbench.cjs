#!/usr/bin/env node
/**
 * 组卷工作台视图：把聚合结果画成一张"每份卷到哪一步"的表。
 *
 * 要钉住的：
 *   1. 每份卷一行，状态与下一步都用中文，不把 in_review 这种内部状态名丢给教师；
 *   2. 空课程给的是"先冻结 Release 再建卷"，不是一张空表；
 *   3. 课程号等外部字符串必须转义（工作台是教师输入课程的入口）；
 *   4. 接口没返回数据时不要抛异常把整页打白。
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
  location: { hash: '#/course/analysis/workbench' },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} }
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(['js/core.js', 'js/paper-workbench.js'].map(read).join('\n;\n'), sandbox,
  { filename: 'paper-workbench.js' });

const App = sandbox.window.App;
const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};

const payload = {
  course_ref: 'analysis',
  counts: { draft: 1, in_review: 0, approved: 1 },
  papers: [
    { paper_version_id: 'a', version_no: 1, paper_kind: 'formal', status: 'draft',
      total_score: '100.00', renders_ready: 0, assignments_published: 0,
      next_action: 'validate' },
    { paper_version_id: 'b', version_no: 2, paper_kind: 'formal', status: 'approved',
      total_score: '100.00', renders_ready: 1, assignments_published: 0,
      next_action: 'render' }
  ]
};

const html = App.paperWorkbenchHtml(payload);
check('每份卷一行', (html.match(/<tr data-status=/g) || []).length === 2);
check('状态显示中文', html.includes('草稿') && html.includes('已批准'));
check('下一步是中文句子', html.includes('跑整卷校验'));
check('渲染未齐时明确要求先导出三类卷', html.includes('先导出学生卷/答案卷/量规卷'));
check('渲染进度显示 n/3', html.includes('1/3'));
check('汇总行有各状态计数', html.includes('草稿 1') && html.includes('已批准 1'));
check('不把内部状态名直接当文案', !html.includes('>in_review<'));
check('不含答案侧字段', !/answer|solution|rubric|statement/i.test(html));

const empty = App.paperWorkbenchHtml({ course_ref: 'analysis', counts: {}, papers: [] });
check('空课程给出下一步指引', empty.includes('先冻结一个 Release'));

const missing = App.paperWorkbenchHtml(null);
check('接口没数据时不抛异常', typeof missing === 'string' && missing.includes('还没有读取到'));

const hostile = App.paperWorkbenchHtml({
  course_ref: '<img src=x onerror=alert(1)>', counts: {}, papers: []
});
check('课程号被转义', !hostile.includes('<img') && hostile.includes('&lt;img'));

console.log();
if (failures.length) {
  console.log(`✗ ${failures.length} 项未通过：${failures.join('、')}`);
  process.exit(1);
}
console.log('✓ 组卷工作台视图全部通过');
