#!/usr/bin/env node
/**
 * 整卷审核与发布页：把"服务端事实"翻译成"哪一步能点、不能点是因为什么"。
 *
 * 要钉住的：
 *   1. 每一步能不能点只由服务端返回的事实决定（状态 / 复核人数 / 产物种类数），
 *      前端不许自己猜"应该可以了吧"；
 *   2. 按钮禁用时必须写清楚原因——只把按钮变灰等于让人去猜；
 *   3. 复核签名用服务端给的 manifest_hash + report_hash，前端不自己拼也算不出；
 *   4. 新建作业的三时间点先后关系在前端先挡（可见 < 截止 ≤ 硬关闭）；
 *   5. 撤回必须写原因；已撤回的卷子任何写操作都不能再点。
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
  location: { hash: '#/course/analysis/paper-ops' },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  // App.parseHash() 用 URLSearchParams 解析 ?version=；vm 沙箱里要显式给。
  URLSearchParams
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(['js/core.js', 'js/paper-ops.js'].map(read).join('\n;\n'), sandbox,
  { filename: 'paper-ops.js' });

const App = sandbox.window.App;
App.icon = App.icon || (() => '<svg/>');
App.button = App.button || ((label, action) => `<button data-action="${action}">${label}</button>`);

const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};
const throws = fn => { try { fn(); return null; } catch (error) { return error; } };

// --- 夹具 ---------------------------------------------------------------------
const PAPER = { id: 'pv-1', version_no: 2, paper_kind: 'formal', status: 'draft',
  total_score: '100.00', edit_seq: 3 };

const allRenders = {
  pdf_implemented: false,
  renders: ['student', 'answer', 'rubric'].map(kind => ({
    render_kind: kind, render_id: `r-${kind}`, manifest_hash: 'a'.repeat(64)
  }))
};

const openReview = {
  report_hash: 'b'.repeat(64), manifest_hash: 'c'.repeat(64),
  required_approvals: 2, approval_count: 1, decisions: []
};

const draftAssignment = { id: 'as-1', offering_ref: 'analysis-2026a', status: 'draft',
  visible_at: '2026-05-01T01:00:00Z', due_at: '2026-05-08T01:00:00Z' };
const liveAssignment = { id: 'as-2', offering_ref: 'analysis-2026a', status: 'published',
  visible_at: '2026-04-01T01:00:00Z', due_at: '2026-04-08T01:00:00Z' };

const stateOf = (patch) => Object.assign({
  versionId: 'pv-1', paper: PAPER, review: null, renders: null, assignments: [],
  classes: [], loading: false, error: null, message: null, busy: null,
  withdrawOpen: false, form: {}
}, patch || {});

/** 从渲染出的 HTML 里取出某一步的 <button …>，用于判断是否 disabled。 */
function buttonOf(html, action) {
  const match = html.match(new RegExp(`<button[^>]*data-action="${action}"[^>]*>`));
  return match ? match[0] : '';
}
const disabled = (html, action) => /disabled/.test(buttonOf(html, action));

// --- 纯推导：每步能不能点 -----------------------------------------------------
const draftFlags = App.paperOpsStatus(stateOf());
check('草稿可校验、可导出', draftFlags.canValidate && draftFlags.canRender);
check('草稿且产物未齐不能提交审核', !draftFlags.canSubmit);
check('缺三类产物时逐类列出', draftFlags.missing.length === 3
  && draftFlags.rendersReady === 0, draftFlags.missing.join('、'));
check('草稿不能复核、不能发布、不能撤回',
  !draftFlags.canApprove && !draftFlags.canPublish && !draftFlags.canWithdraw);

const readyFlags = App.paperOpsStatus(stateOf({ renders: allRenders }));
check('产物齐 + 草稿 → 可以提交审核', readyFlags.canSubmit && readyFlags.rendersComplete);
check('产物齐时 missing 为空', readyFlags.missing.length === 0 && readyFlags.rendersReady === 3);

const inReviewFlags = App.paperOpsStatus(stateOf({
  paper: { ...PAPER, status: 'in_review' }, renders: allRenders, review: openReview
}));
check('审核中 + 有报告哈希 → 可以复核', inReviewFlags.canApprove);
check('审核中不能再提交审核', !inReviewFlags.canSubmit);
const noHashFlags = App.paperOpsStatus(stateOf({
  paper: { ...PAPER, status: 'in_review' }, renders: allRenders,
  review: { manifest_hash: 'c'.repeat(64), required_approvals: 2, approval_count: 0 }
}));
check('没有报告哈希就不能复核（签名要绑报告）', !noHashFlags.canApprove);

const approvedFlags = App.paperOpsStatus(stateOf({
  paper: { ...PAPER, status: 'approved' }, renders: allRenders,
  assignments: [draftAssignment]
}));
check('已批准 + 产物齐 + 有草稿 → 可以发布', approvedFlags.canPublish);
check('已批准可以撤回', approvedFlags.canWithdraw);
const noDraftFlags = App.paperOpsStatus(stateOf({
  paper: { ...PAPER, status: 'approved' }, renders: allRenders, assignments: [liveAssignment]
}));
check('没有作业草稿就不能发布', !noDraftFlags.canPublish);
check('已发布作业被单独归到 live', noDraftFlags.live.length === 1
  && noDraftFlags.draft === null);

const withdrawnFlags = App.paperOpsStatus(stateOf({
  paper: { ...PAPER, status: 'withdrawn' }, renders: allRenders
}));
check('已撤回：校验/导出/复核/发布/撤回全禁', !withdrawnFlags.canValidate
  && !withdrawnFlags.canRender && !withdrawnFlags.canSubmit
  && !withdrawnFlags.canApprove && !withdrawnFlags.canPublish
  && !withdrawnFlags.canWithdraw);
check('已撤回标记 withdrawn', withdrawnFlags.withdrawn);
check('缺参数不抛异常', App.paperOpsStatus() .canValidate === true);

// --- 渲染 ---------------------------------------------------------------------
const draftHtml = App.paperOpsHtml(stateOf());
check('草稿页给出状态行', draftHtml.includes('ops-status') && draftHtml.includes('草稿'));
check('草稿页禁用提交审核', disabled(draftHtml, 'ops-submit'));
check('禁用时写明"还差哪几类卷"', draftHtml.includes('先导出三类卷')
  && draftHtml.includes('学生卷、答案卷、量规卷'));
check('草稿页禁用复核', disabled(draftHtml, 'ops-approve'));
check('草稿页禁用发布并说明"还没通过双人复核"',
  disabled(draftHtml, 'ops-publish') && draftHtml.includes('还没通过双人复核'));
check('草稿页禁用撤回并说明原因',
  disabled(draftHtml, 'ops-withdraw-open') && draftHtml.includes('只有已批准的卷子可以撤回'));
check('草稿页可点校验与导出', !disabled(draftHtml, 'ops-validate')
  && !disabled(draftHtml, 'ops-render'));
check('草稿页不给建作业表单', !draftHtml.includes('ops-assignment-create')
  && draftHtml.includes('整卷通过复核后才能新建打印作业'));

const approvedHtml = App.paperOpsHtml(stateOf({
  paper: { ...PAPER, status: 'approved' }, renders: allRenders,
  assignments: [draftAssignment], review: { required_approvals: 2, approval_count: 2 }
}));
check('已批准页可点发布', !disabled(approvedHtml, 'ops-publish'));
check('已批准页给出建作业表单', approvedHtml.includes('ops-assignment-create')
  && approvedHtml.includes('ops-offering') && approvedHtml.includes('ops-due'));
check('已批准页可撤回', !disabled(approvedHtml, 'ops-withdraw-open'));
check('作业表格列出草稿', approvedHtml.includes('as-1') && approvedHtml.includes('草稿'));
check('复核完成写"复核完成（2 人）"', approvedHtml.includes('复核完成（2 人）'));

const inReviewHtml = App.paperOpsHtml(stateOf({
  paper: { ...PAPER, status: 'in_review' }, renders: allRenders, review: openReview
}));
check('审核中页可点复核', !disabled(inReviewHtml, 'ops-approve'));
check('审核中页写清还差几名复核教师', inReviewHtml.includes('还需 1 名复核教师'));
check('签名提示带卷面哈希前缀（服务端给的）',
  inReviewHtml.includes('cccccccc'), '签名必须绑服务端 manifest_hash');

const mineReview = { ...openReview, approval_count: 1,
  decisions: [{ decision: 'approved', decision_seq: 1 }] };
const mineHtml = App.paperOpsHtml(stateOf({
  paper: { ...PAPER, status: 'in_review' }, renders: allRenders, review: mineReview
}));
check('已签过的人看到"等另一位教师"', mineHtml.includes('你已签过本轮'));

const withdrawnHtml = App.paperOpsHtml(stateOf({
  paper: { ...PAPER, status: 'withdrawn' }, renders: allRenders
}));
check('已撤回页顶部有只读告警', withdrawnHtml.includes('已撤回，历史版本只读'));
check('已撤回页每个写动作都禁用', ['ops-validate', 'ops-render', 'ops-submit',
  'ops-approve', 'ops-publish', 'ops-withdraw-open'].every(action => disabled(withdrawnHtml, action)));

const renderRow = (html, kind) => {
  const match = html.match(new RegExp(`<tr data-kind="${kind}">([\\s\\S]*?)</tr>`));
  return match ? match[1] : '';
};
check('三类产物各一行', ['student', 'answer', 'rubric'].every(kind =>
  renderRow(approvedHtml, kind).includes(`下载`)));
check('产物下载地址可点', approvedHtml.includes('/api/v1/paper-renders/r-answer/download'));
// 预览与下载是两条路由：复核人要先在浏览器里看到卷面，而不是先存一个文件再打开。
check('每行都有预览入口', ['student', 'answer', 'rubric'].every(kind =>
  renderRow(approvedHtml, kind).includes('/preview')));
check('预览走独立路由而不是下载',
  approvedHtml.includes('/api/v1/paper-renders/r-answer/preview')
  && !renderRow(approvedHtml, 'answer').match(/<a href="[^"]*\/preview"[^>]*download/),
  '预览链接不能带 download 属性');
check('预览在新标签页打开', renderRow(approvedHtml, 'answer').includes('target="_blank"'));
check('产物未生成时写"未生成"', renderRow(draftHtml, 'student').includes('未生成'));
check('产物未生成时不给预览链接', !renderRow(draftHtml, 'student').includes('/preview'));
check('没有 PDF 依赖时先说明产物是可打印 HTML',
  approvedHtml.includes('可打印 HTML'), '别让人下载后才发现');

const withdrawOpenHtml = App.paperOpsHtml(stateOf({
  paper: { ...PAPER, status: 'approved' }, renders: allRenders, withdrawOpen: true
}));
check('点撤回后才出现原因输入框', withdrawOpenHtml.includes('withdraw-reason')
  && withdrawOpenHtml.includes('ops-withdraw-confirm'));
check('撤回提示写清保留什么', withdrawOpenHtml.includes('历史版本与已有提交保留'));

const errorHtml = App.paperOpsHtml(stateOf({ error: '<b>炸了</b>' }));
check('错误显示为 alert 且被转义',
  errorHtml.includes('role="alert"') && errorHtml.includes('&lt;b&gt;'));
const messageHtml = App.paperOpsHtml(stateOf({ message: '已排队 3 类产物' }));
check('成功提示显示为 status', messageHtml.includes('role="status"')
  && messageHtml.includes('已排队 3 类产物'));

check('空 state 不抛异常', App.paperOpsHtml(null).includes('还没有读取到卷子'));
check('busy 时所有动作按钮禁用', ['ops-validate', 'ops-render', 'ops-submit']
  .every(action => disabled(App.paperOpsHtml(stateOf({ renders: allRenders, busy: 'submit' })),
    action)), '连点两下会拿到第二次的 409');
const hostile = App.paperOpsHtml(stateOf({
  paper: { ...PAPER, paper_kind: '<img src=x>', total_score: '1' }
}));
check('外部字符串被转义', !hostile.includes('<img') && hostile.includes('&lt;img'));

// --- 建作业载荷 ---------------------------------------------------------------
const good = App.paperOpsAssignmentPayload({
  offering: 'analysis-2026a', visible: '2026-05-01T09:00', due: '2026-05-08T09:00',
  close: '2026-05-08T09:00'
});
check('载荷带班级与三个 ISO 时间点', good.offering_ref === 'analysis-2026a'
  && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/.test(good.visible_at)
  && !!good.hard_close_at);
check('截止等于硬关闭是允许的', good.due_at === good.hard_close_at);
// 阶段 C 起多一个 distribution_kind（默认 print）：字段集合必须**精确**，
// 多传或少传都会让服务端 extra=forbid / 默认值判断失准。
check('请求体字段精确', Object.keys(good).sort().join(',')
  === 'distribution_kind,due_at,hard_close_at,offering_ref,visible_at');
check('未选分发方式时默认打印卷', good.distribution_kind === 'print');
check('可以选在线作答', App.paperOpsAssignmentPayload({
  offering: 'analysis-2026a', visible: '2026-05-01T09:00',
  due: '2026-05-08T09:00', close: '2026-05-08T09:00',
  distribution: 'online'
}).distribution_kind === 'online');
check('非法分发方式被拒', throws(() => App.paperOpsAssignmentPayload({
  offering: 'a', visible: '2026-05-01T09:00', due: '2026-05-08T09:00',
  close: '2026-05-08T09:00', distribution: 'video'
})) !== null);
check('没选班级被拒', (throws(() => App.paperOpsAssignmentPayload(
  { visible: '2026-05-01T09:00', due: '2026-05-08T09:00', close: '2026-05-08T09:00' }))
  || {}).message === '先选一个班级');
check('时间没填被拒', throws(() => App.paperOpsAssignmentPayload(
  { offering: 'a' })) !== null);
check('可见晚于截止被拒', (throws(() => App.paperOpsAssignmentPayload({
  offering: 'a', visible: '2026-05-09T09:00', due: '2026-05-08T09:00',
  close: '2026-05-10T09:00' })) || {}).message === '可见时间必须早于截止时间');
check('截止晚于硬关闭被拒', (throws(() => App.paperOpsAssignmentPayload({
  offering: 'a', visible: '2026-05-01T09:00', due: '2026-05-10T09:00',
  close: '2026-05-09T09:00' })) || {}).message === '截止时间不能晚于硬关闭时间');

// --- 错误文案 -----------------------------------------------------------------
check('产物未齐给的是"先导出"',
  App.paperOpsError({ code: 'renders_not_ready' }).includes('先导出'));
check('重复复核人给的是"需要另一位教师"',
  App.paperOpsError({ code: 'duplicate_reviewer' }).includes('另一位教师'));
check('命题人不能自审被说清',
  App.paperOpsError({ code: 'contributor_cannot_review' }).includes('命题人'));
check('卷面变过时签名作废被说清',
  App.paperOpsError({ code: 'review_manifest_changed' }).includes('不算数'));
check('未知错误回落到 message',
  App.paperOpsError({ code: 'x', message: '原始错误' }) === '原始错误');
check('服务端 detail.code 也能识别',
  App.paperOpsError({ message: 'HTTP 409', detail: { code: 'renders_not_ready' } })
    .includes('先导出'));
check('空错误不抛异常', typeof App.paperOpsError() === 'string');

// --- 页面入口 -----------------------------------------------------------------
check('注册了视图 paper-ops', typeof App.views['paper-ops'] === 'function');
check('注册了进入后取数钩子', typeof App.after['paper-ops'] === 'function');
check('注册了六个流程动作', ['ops-refresh', 'ops-validate', 'ops-render', 'ops-submit',
  'ops-approve', 'ops-assignment-create', 'ops-publish', 'ops-withdraw-open',
  'ops-withdraw-confirm'].every(action => typeof App.actions[action] === 'function'));
check('注册了表单输入', ['ops-offering', 'ops-visible', 'ops-due', 'ops-close',
  'withdraw-reason'].every(id => typeof App.inputs[id] === 'function'));

App.course = { id: 'analysis', title: '数学分析 I' };

App.route = App.parseHash('#/course/analysis/paper-ops');
check('缺 version 参数时给指引', App.views['paper-ops']().includes('缺少卷版本号'));

App.route = App.parseHash('#/course/analysis/paper-ops?version=pv-1');
App.state.paperOps = stateOf({ loading: true, paper: null });
check('加载中给出提示而不是空白', App.views['paper-ops']().includes('正在读取卷子'));

App.state.paperOps = stateOf({ paper: null, error: '读不到卷子' });
check('读失败时显示错误', App.views['paper-ops']().includes('读不到这份卷'));

App.state.paperOps = stateOf({ renders: allRenders, review: openReview,
  paper: { ...PAPER, status: 'in_review' } });
const page = App.views['paper-ops']();
check('页面包含标题与步骤表', page.includes('整卷审核与发布') && page.includes('ops-steps'));
check('页面把状态行画出来', page.includes('ops-status') && page.includes('审核中'));

// 进入页面时按 URL 里的 version 取数（不发真请求：只看它要了哪份卷）。
const asked = [];
App.render = () => {};
App.api = {
  paperVersion: id => { asked.push(id); return Promise.resolve(PAPER); },
  paperReview: () => Promise.resolve(openReview),
  paperRenders: () => Promise.resolve(allRenders),
  paperAssignments: () => Promise.resolve([]),
  classes: () => Promise.resolve({ items: [] })
};
App.state.paperOps = stateOf({ versionId: null, paper: null });

(async () => {
  App.after['paper-ops']();
  await new Promise(resolve => setTimeout(resolve, 0));
  check('进入页面按 URL 的 version 取数', asked.length === 1 && asked[0] === 'pv-1');
  check('取数后填进 state', App.state.paperOps.paper === PAPER);

  console.log();
  if (failures.length) {
    console.log(`✗ ${failures.length} 项未通过：${failures.join('、')}`);
    process.exit(1);
  }
  console.log('✓ 整卷审核与发布视图全部通过');
})().catch(error => {
  console.error(error);
  process.exit(1);
});
