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
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  // App.url() 用 URLSearchParams 拼查询串；jsdom 里自带，vm 沙箱里要显式给。
  URLSearchParams
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

// --- 页面入口 -----------------------------------------------------------------
check('注册了视图 workbench', typeof App.views.workbench === 'function');
check('注册了进入后取数钩子', typeof App.after.workbench === 'function');
check('注册了刷新动作', typeof App.actions['workbench-refresh'] === 'function'
  && typeof App.actions['workbench-retry'] === 'function');

App.course = { id: 'analysis', title: '数学分析 I' };
// 图标与按钮由 visual-identity.js 提供；本文件只测工作台的渲染分支，给占位即可。
App.icon = App.icon || (() => '<svg/>');
App.button = App.button || ((label, action) => `<button data-action="${action}">${label}</button>`);
App.state.workbench = { courseId: 'analysis', data: payload, loading: false, error: null };
const page = App.views.workbench();
check('页面包含标题与表格', page.includes('组卷工作台') && page.includes('workbench-table'));
check('页面嵌入了下一步文案', page.includes('跑整卷校验'));
check('每行有进编辑器的链接', page.includes('paper-editor')
  && page.includes('version='), '编辑器必须能从工作台进入');
check('每行有进审核发布页的链接', page.includes('paper-ops')
  && page.includes('审核与发布'), '校验/复核/发布不在编辑器里，必须另给入口');

App.state.workbench = { courseId: 'analysis', data: null, loading: true, error: null };
check('加载中给出提示而不是空白', App.views.workbench().includes('正在读取工作台'));

App.state.workbench = { courseId: 'analysis', data: null, loading: false, error: '<b>boom</b>' };
const failed = App.views.workbench();
check('读失败时显示错误并转义', failed.includes('读不到工作台') && failed.includes('&lt;b&gt;'));

console.log();
if (failures.length) {
  console.log(`✗ ${failures.length} 项未通过：${failures.join('、')}`);
  process.exit(1);
}
console.log('✓ 组卷工作台视图全部通过');
