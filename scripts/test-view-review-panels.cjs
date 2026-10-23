#!/usr/bin/env node
/**
 * 审核页里"口径与全量清单"这几块：按题号查审核记录、课程审核总表、图谱章节树。
 *
 * 队列只装待审的题，所以老师最常查的"这道题到底什么状态"要靠这两块补上。
 * 测的是它们别退化：状态字段别丢、没选课程时别乱查、点题号能直接打开记录。
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
  location: { hash: '#/review' },
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

const bundle = ['js/core.js', 'js/visual-identity.js', 'js/api-client.js',
  'js/review-scope.js', 'js/review-node.js', 'js/publish-panel.js', 'js/review.js']
  .map(file => fs.readFileSync(path.join(root, file), 'utf8')).join('\n;\n');
vm.runInContext(bundle, sandbox, { filename: 'bundle.js' });

const App = sandbox.window.App;
App.markdown = source => `<div class="prose">${source}</div>`;

const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};
const toasts = [];
App.toast = message => toasts.push(message);
// 真实页面里 render 由 app.js 提供；这一层只验状态与请求，渲染交给视图函数自己
App.render = () => {};
let html = '';

function reset(overrides = {}) {
  App.state.review = {
    status: 'ready', error: '',
    summary: { tiers: [], task_states: [], open_issues: [] },
    items: [], courses: [], checked: {}, diffs: {}, busy: false,
    tab: 'exercises',
    filters: { course_id: '', category: '', priority: '' },
    graph: null, graphOpen: {}, nodePanel: '', nodeDetail: null, nodeDetailError: '',
    ledger: null, ledgerTier: '', ledgerQuery: '',
    verification: null, lookupId: '', open: null,
    ...overrides
  };
}

function graphFacts() {
  return {
    totals: { nodes: 320, verified: 40, courses: 2 },
    chapters: [
      { course_id: 'analysis-i', chapter_id: 'ch-1', title: '第一章 实数与函数', nodes: 40, verified: 40 },
      { course_id: 'analysis-i', chapter_id: 'ch-2', title: '第二章 数列极限', nodes: 30, verified: 5 }
    ],
    nodes: []
  };
}

const verifiedRecord = {
  course_id: 'analysis-i', exercise_id: 'pub-a', tier: 'human', review_status: 'published',
  human_verified_at: '2026-10-20T09:30:00Z', human_verified_by: '张老师',
  content_version: 3, updated_at: '2026-10-21T10:00:00Z',
  edited_after_review: true, lean_available: false, proofs: [],
  task: { task_id: 12, state: 'published' }
};

async function graphTests() {
  console.log('知识图谱审核（章节树）：');
  reset({ tab: 'graph' });
  html = App.views.review();
  check('没选课程时提示先选课程', html.includes('先在上面选一门课'));

  reset({
    tab: 'graph',
    graph: graphFacts(),
    graphOpen: { 'ch-2': true },
    filters: { course_id: 'analysis-i', category: '', priority: '' },
    courses: [{ id: 'analysis-i', title: '数学分析 I' }]
  });
  html = App.views.review();
  check('整章未审完时给「整章通过」',
    html.includes('data-verb="verify"') && html.includes('整章通过'));
  check('整章已审完时可撤销', html.includes('data-verb="unverify"') && html.includes('撤销本章通过'));
  check('显示已审/总数', html.includes('30 个知识点 · 已审 5'), '章头上要写清这一章审到哪了');
  check('显示课程总量', /共\s*<b>320<\/b>\s*个节点/.test(html));

  reset({
    tab: 'graph',
    graph: graphFacts(),
    filters: { course_id: 'analysis-i', category: '', priority: '' },
    courses: [{ id: 'analysis-i', title: '数学分析 I' }]
  });
  const verified = [];
  App.api.verifyGraph = async payload => { verified.push(payload); return { changed: 30 }; };
  App.state.review.status = 'loading';
  toasts.length = 0;
  await App.actions['graph-verify']({
    disabled: false, dataset: { course: 'analysis-i', chapter: 'ch-2', verb: 'verify' }
  });
  check('整章通过走图谱接口', verified.length === 1
    && verified[0].course_id === 'analysis-i'
    && verified[0].chapter_id === 'ch-2'
    && verified[0].action === 'verify', JSON.stringify(verified[0] || null));
  check('整章通过后报告节点数', toasts.some(text => text.includes('30 个节点')),
    String(toasts[0] || ''));
}

async function verificationTests() {
  console.log('\n单道题的审核记录：');
  reset({ filters: { course_id: 'analysis-i', category: '', priority: '' }, verification: verifiedRecord });
  html = App.views.review();
  check('查询结果显示可信等级', html.includes('人工通过') || html.includes('人工审核通过'));
  check('显示人工审核时间与人', html.includes('2026-10-20 09:30') && html.includes('张老师'));
  check('审核后被改过要显眼提示', html.includes('审核通过后又被改过'));
  check('说明 Lean 尚未接入', html.includes('Lean 验证尚未接入'));
  check('显示内容版本', html.includes('内容版本 v3'));
  check('显示最近一次审核任务状态', html.includes('最近一次审核任务：已发布'));

  reset({
    filters: { course_id: 'analysis-i', category: '', priority: '' },
    verification: { ...verifiedRecord, tier: 'none', human_verified_at: null, human_verified_by: null,
      edited_after_review: false, content_version: 1 }
  });
  html = App.views.review();
  check('未审核的题明说尚未人工审核', html.includes('尚未人工审核'));
  check('未审核时不误报"审核后被改过"', !html.includes('审核通过后又被改过'));

  reset({
    filters: { course_id: 'analysis-i', category: '', priority: '' },
    verification: {
      ...verifiedRecord, lean_available: true,
      proofs: [
        { ord: 1, compiled: true, faithful_status: 'teacher-reviewed', toolchain: 'lean4:v4.12.0', has_statement: true, compiled_at: '2026-10-20T08:00:00Z' },
        { ord: 2, compiled: false, faithful_status: 'draft', toolchain: 'lean4:v4.12.0', has_statement: false }
      ]
    }
  });
  html = App.views.review();
  check('接入后列出每条 Lean 证明', html.includes('#1') && html.includes('#2'));
  check('区分编译通过与未通过', html.includes('编译通过') && html.includes('编译未通过'));
  check('忠实性已确认与未确认都显示', html.includes('忠实性已确认') && html.includes('忠实性：draft'));
  check('缺形式化命题要标出来', html.includes('缺形式化命题'));

  reset({ verification: { error: '题目不存在：analysis-i/pub-z' } });
  html = App.views.review();
  check('查不到时给错误说明而不是白屏', html.includes('读不到审核记录') && html.includes('题目不存在'));

  reset();
  html = App.views.review();
  check('没选课程时查询按钮禁用', /data-action="review-verify-lookup"[^>]*disabled/.test(html));

  reset({ filters: { course_id: 'analysis-i', category: '', priority: '' }, lookupId: 'pub-a' });
  html = App.views.review();
  check('选了课程则说明在哪儿查', html.includes('在「analysis-i」里查'));
  const lookupCalls = [];
  App.api.exerciseVerification = async (courseId, exerciseId) => {
    lookupCalls.push([courseId, exerciseId]);
    return verifiedRecord;
  };
  App.state.review.status = 'loading';
  await App.actions['review-verify-lookup']({ disabled: false });
  check('按题号查询走单题审核记录接口',
    lookupCalls.length === 1 && lookupCalls[0][0] === 'analysis-i'
    && lookupCalls[0][1] === 'pub-a', JSON.stringify(lookupCalls[0] || null));
  check('查询结果写进状态', App.state.review.verification?.tier === 'human');

  App.api.exerciseVerification = async () => {
    throw Object.assign(new Error('题目不存在：x/y'), { status: 404 });
  };
  App.state.review.status = 'loading';
  await App.actions['review-verify-lookup']({ disabled: false });
  check('查询失败落到错误块', App.state.review.verification?.error?.includes('题目不存在'));
}

async function ledgerTests() {
  console.log('\n课程审核总表：');
  const ledgerRows = [
    {
      id: 'ex-1', course_id: 'analysis-i', tier: 'none', review_status: 'draft',
      human_verified_at: null, human_verified_by: null, lean_confirmed: false,
      edited_after_review: false, task_state: null, prompt: '求极限 $\\lim_{x\\to 0}$'
    },
    {
      id: 'ex-2', course_id: 'analysis-i', tier: 'human', review_status: 'published',
      human_verified_at: '2026-10-20T10:00:00Z', human_verified_by: '张老师',
      lean_confirmed: false, edited_after_review: true, task_state: 'published',
      prompt: '证明单调有界定理'
    }
  ];
  const ledgerBody = {
    course_id: 'analysis-i', items: ledgerRows, total: 2, limit: 100, offset: 0,
    counts: [{ tier: 'none', n: 1 }, { tier: 'human', n: 1 }],
    filter: { tier: '', question: '' }
  };

  reset({ filters: { course_id: 'analysis-i', category: '', priority: '' }, ledger: ledgerBody });
  html = App.views.review();
  check('总表列出全部题目', html.includes('ex-1') && html.includes('ex-2'));
  check('总表显示可信等级徽标', html.includes('未核') && html.includes('人工通过'));
  check('总表显示人工审核时间与审核人', html.includes('2026-10-20 10:00') && html.includes('张老师'));
  check('总表标出审核后被改过', html.includes('审核后改过'));
  check('总表显示工作流与审核任务状态', html.includes('已发布'));
  check('总表显示各档数量芯片', html.includes('全部') && html.includes('未核 1'));
  check('总表提示共多少道', html.includes('共 2 道'));

  reset();
  html = App.views.review();
  check('没选课程时总表提示先选课', html.includes('先在上面的课程筛选里选一门课'));
  check('没选课程时不渲染表格', !html.includes('rv-table-wrap'));

  reset({
    filters: { course_id: 'analysis-i', category: '', priority: '' },
    ledger: ledgerBody, ledgerTier: 'human'
  });
  html = App.views.review();
  const chipsHtml = html.replace(/\s+/g, ' ');
  check('选中等级芯片高亮',
    chipsHtml.includes('class="chip active" data-action="ledger-tier" data-tier="human"')
    && !chipsHtml.includes('class="chip active" data-action="ledger-tier" data-tier="none"'),
    '只有选中的那一档该高亮');
  const ledgerCalls = [];
  App.api.reviewLedger = async params => { ledgerCalls.push(params); return ledgerBody; };
  App.state.review.status = 'loading';
  await App.actions['ledger-tier']({ dataset: { tier: 'none' } });
  check('点等级芯片按等级重查', ledgerCalls.length === 1 && App.state.review.ledgerTier === 'none',
    JSON.stringify(ledgerCalls[0] || null));
  App.state.review.status = 'loading';
  await App.actions['ledger-search']();
  check('搜索也重新拉总表', ledgerCalls.length === 2, String(ledgerCalls.length));

  App.state.review.status = 'loading';
  App.state.review.lookupId = '';
  await App.actions['ledger-open']({ dataset: { id: 'ex-2' } });
  check('点题号打开这道题的审核记录', App.state.review.lookupId === 'ex-2');
}

(async () => {
  await graphTests();
  await verificationTests();
  await ledgerTests();

  console.log('');
  if (failures.length) {
    console.log(`失败 ${failures.length} 项：${failures.join('、')}`);
    process.exit(1);
  }
  console.log('审核页面板测试全部通过。');
})();
