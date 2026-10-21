#!/usr/bin/env node
/**
 * 「学生可见范围」面板 + 知识图谱审核面板的测试（node:vm，不启浏览器）。
 *
 * 这个开关的风险不在样式，而在语义：选错一档，学生端可能整门课看不到题，
 * 而且没有任何报错。所以页面必须做到三件事：
 *   1. 保存之前就告诉你"这一档会放出多少题/多少节点"；
 *   2. 某一档会清空学生端时明确警告，而不是等老师发现学生打不开题再回头查；
 *   3. 分清"全局口径"与"这门课单独设置"，否则改试点课会顺手改掉整校口径。
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
    'js/review-scope.js', 'js/review.js']
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

const LABELS = { human: '仅人工审核通过', verified: 'Lean 验证 + 人工审核', all: '全部（含未验证）' };
const GRAPH_LABELS = { all: '全部（含未审核节点）', human: '仅人工审核通过' };

function policy(overrides = {}) {
  return {
    scope: 'global',
    course_id: null,
    effective: { exercise_tier: 'all', qa_tier: 'all', graph_tier: 'all' },
    source: 'global',
    global: { exercise_tier: 'all', qa_tier: 'all', graph_tier: 'all' },
    override: null,
    options: ['human', 'verified', 'all'],
    labels: LABELS,
    hints: { human: '', verified: 'Lean 验证尚未接入', all: '' },
    graph_options: ['all', 'human'],
    graph_labels: GRAPH_LABELS,
    tiers: [
      { tier: 'none', total: 4600, published: 110 },
      { tier: 'lean', total: 0, published: 0 },
      { tier: 'human', total: 200, published: 3 },
      { tier: 'strongest', total: 7, published: 7 }
    ],
    impact: { human: 10, verified: 10, all: 120 },
    total: 4807,
    published_total: 120,
    graph: { total: 320, verified: 40, pending: 280, impact: { all: 320, human: 40 } },
    courses: [
      { id: 'analysis-i', title: '数学分析 I', has_override: true, exercise_tier: 'human', qa_tier: 'human', graph_tier: 'human' },
      { id: 'pilot', title: '试点课', has_override: false, exercise_tier: null, qa_tier: null, graph_tier: null }
    ],
    updated_at: '2026-10-21T08:00:00Z',
    updated_by: '张老师',
    ...overrides
  };
}

function graphFacts() {
  return {
    totals: { nodes: 320, verified: 40, courses: 2 },
    chapters: [
      { course_id: 'analysis-i', chapter_id: 'ch-1', title: '第一章 实数与函数', nodes: 40, verified: 40 },
      { course_id: 'analysis-i', chapter_id: 'ch-2', title: '第二章 数列极限', nodes: 30, verified: 5 }
    ]
  };
}

function reset({ visibility = policy(), graph = null, scope = '', draft = null } = {}) {
  App.state.review = {
    status: 'ready', error: '',
    summary: { tiers: [], task_states: [], open_issues: [] },
    items: [], courses: [], checked: {}, diffs: {}, busy: false,
    filters: { course_id: '', category: '', priority: '' },
    visibility, policyScope: scope, graph, policyDraft: draft, open: null
  };
}

console.log('全局口径：');
reset();
let html = App.views.review();
check('面板出现', html.includes('学生可见范围'));
check('有作用范围选择器', html.includes('data-input="visibility-scope"'));
check('三个档位下拉（习题 / 问答 / 图谱）',
  (html.match(/data-input="visibility-tier"/g) || []).length === 3);
check('选项写明这一档能放出多少题', html.includes('学生可见 120 题'));
check('图谱选项写明能放出多少节点', html.includes('学生可见 320 节点'));
check('列出四种验证等级的题量', html.includes('未核') && html.includes('已发布 110'));
check('显示全局口径摘要', html.includes('全局默认：'));
check('说明与发布闸是「与」的关系', html.includes('先发布') && html.includes('等级过滤'));
check('显示上次修改人', html.includes('张老师'));
check('当前可见数写进正文', /学生端可见\s*<b>120<\/b>\s*道已发布习题/.test(html));
check('全局时不显示「取消本课设置」', !html.includes('data-action="visibility-reset"'));

console.log('\n课程覆盖：');
reset({
  scope: 'analysis-i',
  visibility: policy({
    scope: 'course', course_id: 'analysis-i', source: 'course',
    effective: { exercise_tier: 'human', qa_tier: 'human', graph_tier: 'human' },
    override: { exercise_tier: 'human', qa_tier: 'human', graph_tier: 'human', updated_at: '2026-10-21T09:00:00Z', updated_by: '李老师' }
  }),
  graph: graphFacts()
});
html = App.views.review();
check('显示「这门课单独设置」', html.includes('这门课单独设置'));
check('同时给出全局口径作对照', html.includes('全局是 习题'));
check('有取消本课设置的按钮', html.includes('data-action="visibility-reset"'));
check('课程作用域里选中了这门课', /value="analysis-i" selected/.test(html));
check('图谱面板出现章节表', html.includes('第一章 实数与函数') && html.includes('第二章 数列极限'));

console.log('\n课程没有单独设置（跟随全局）：');
reset({ scope: 'pilot', visibility: policy({ scope: 'course', course_id: 'pilot', source: 'global' }) });
html = App.views.review();
check('明说跟随全局', html.includes('这门课跟随全局'));
check('保存前没有「取消本课设置」', !html.includes('data-action="visibility-reset"'));

console.log('\n切换档位：先算后果，再保存');
reset();
App.state.review.policyDraft = { exercise_tier: 'human', qa_tier: 'human', graph_tier: 'all' };
html = App.views.review();
check('按草案档位算可见题数', /学生端可见\s*<b>10<\/b>/.test(html));
check('human 选项被选中', /value="human" selected/.test(html));
check('不会误报"放不出题"', !html.includes('放不出任何已发布习题'));

reset();
App.state.review.visibility = policy({ impact: { human: 0, verified: 0, all: 120 } });
App.state.review.policyDraft = { exercise_tier: 'human', qa_tier: 'human', graph_tier: 'all' };
html = App.views.review();
check('某一档会清空学生端时明确警告', html.includes('放不出任何已发布习题'));
check('警告里给出两条出路', html.includes('先去审核') && html.includes('换回更宽的档'));

reset();
App.state.review.visibility = policy({ graph: { total: 320, verified: 0, pending: 320, impact: { all: 320, human: 0 } } });
App.state.review.policyDraft = { exercise_tier: 'all', qa_tier: 'all', graph_tier: 'human' };
html = App.views.review();
check('图谱切到仅人工但没有节点通过时警告', html.includes('学生端会看不到知识图谱'));

reset();
App.state.review.policyDraft = { exercise_tier: 'verified', qa_tier: 'all', graph_tier: 'all' };
html = App.views.review();
check('选 Lean 档时如实说明尚未接入', html.includes('Lean 验证尚未接入'));

console.log('\n知识图谱审核：');
reset();
html = App.views.review();
check('没选课程时提示先选课程', html.includes('在上面选一门课'));

reset({ scope: 'analysis-i', visibility: policy({ scope: 'course', course_id: 'analysis-i' }), graph: graphFacts() });
html = App.views.review();
check('已全部通过的章可撤销', html.includes('data-verb="unverify"'));
check('未通过的章给「整章人工通过」', html.includes('data-verb="verify"') && html.includes('整章人工通过'));
check('显示已审/总数', html.includes('5/30 已审'));
check('显示课程总量', /320 个节点，已人工审核 <b>40<\/b>/.test(html));

console.log('\n输入与保存：');
// 真实应用里 A.render 由 app.js 提供；这里换个计数器，验证"改档位会立刻重算后果"。
let renders = 0;
App.render = () => { renders += 1; };

reset();
App.inputs['visibility-tier']({ value: 'verified', dataset: { kind: 'exercise' } });
check('改习题档位只改习题', App.state.review.policyDraft.exercise_tier === 'verified'
  && App.state.review.policyDraft.qa_tier === 'all',
  JSON.stringify(App.state.review.policyDraft));
check('改档位会立刻重渲染（先算后果）', renders === 1, String(renders));
App.inputs['visibility-tier']({ value: 'human', dataset: { kind: 'qa' } });
check('改问答档位只改问答', App.state.review.policyDraft.qa_tier === 'human'
  && App.state.review.policyDraft.exercise_tier === 'verified',
  JSON.stringify(App.state.review.policyDraft));
App.inputs['visibility-tier']({ value: 'human', dataset: { kind: 'graph' } });
check('改图谱档位只改图谱', App.state.review.policyDraft.graph_tier === 'human'
  && App.state.review.policyDraft.qa_tier === 'human',
  JSON.stringify(App.state.review.policyDraft));

async function visibilityTests() {
  reset();
  App.state.review.policyDraft = { exercise_tier: 'verified', qa_tier: 'human', graph_tier: 'human' };
  const calls = [];
  App.api.saveVisibility = async (payload, courseId) => {
    calls.push({ payload, courseId });
    return policy();
  };
  toasts.length = 0;
  const button = { disabled: false };
  App.state.review.status = 'loading'; // 防止真正发请求
  await App.actions['visibility-save'](button);
  check('保存把三档一起提交', calls.length === 1
    && calls[0].payload.exercise_tier === 'verified'
    && calls[0].payload.qa_tier === 'human'
    && calls[0].payload.graph_tier === 'human',
    JSON.stringify(calls[0] || null));
  check('全局保存不带 course_id', calls.length === 1 && calls[0].courseId === undefined,
    String(calls[0] && calls[0].courseId));
  check('保存成功后清掉草案', App.state.review.policyDraft === null);
  check('给出保存成功提示', toasts.some(text => text.includes('已保存')), String(toasts[0] || ''));

  reset({
    scope: 'analysis-i',
    visibility: policy({ scope: 'course', course_id: 'analysis-i', source: 'course', override: { exercise_tier: 'human', qa_tier: 'human', graph_tier: 'human' } })
  });
  const scoped = [];
  App.api.saveVisibility = async (payload, courseId) => { scoped.push(courseId); return policy(); };
  App.state.review.status = 'loading';
  await App.actions['visibility-save']({ disabled: false });
  check('课程作用域保存带上 course_id', scoped[0] === 'analysis-i', String(scoped[0]));

  reset({
    scope: 'analysis-i',
    visibility: policy({ scope: 'course', course_id: 'analysis-i', source: 'course', override: { exercise_tier: 'human', qa_tier: 'human', graph_tier: 'human' } })
  });
  let resetId = null;
  App.api.resetVisibility = async courseId => { resetId = courseId; return policy(); };
  App.state.review.status = 'loading';
  toasts.length = 0;
  await App.actions['visibility-reset']({ disabled: false });
  check('取消本课设置带上 course_id', resetId === 'analysis-i', String(resetId));
  check('取消后提示回到跟随全局', toasts.some(text => text.includes('跟随全局')), String(toasts[0] || ''));

  reset({ scope: 'analysis-i', visibility: policy({ scope: 'course', course_id: 'analysis-i' }), graph: graphFacts() });
  const verified = [];
  App.api.verifyGraph = async payload => { verified.push(payload); return { changed: 30 }; };
  App.state.review.status = 'loading';
  toasts.length = 0;
  await App.actions['graph-verify']({ disabled: false, dataset: { chapter: 'ch-2', verb: 'verify' } });
  check('整章通过走图谱接口', verified.length === 1
    && verified[0].course_id === 'analysis-i'
    && verified[0].chapter_id === 'ch-2'
    && verified[0].action === 'verify',
    JSON.stringify(verified[0] || null));
  check('整章通过后报告节点数', toasts.some(text => text.includes('30 个节点')), String(toasts[0] || ''));

  reset();
  App.api.saveVisibility = async () => { throw Object.assign(new Error('需要角色之一：teacher'), { status: 403 }); };
  toasts.length = 0;
  App.state.review.status = 'loading';
  await App.actions['visibility-save']({ disabled: false });
  check('保存失败时说明原因', toasts.some(text => text.includes('保存失败') && text.includes('teacher')), String(toasts[0] || ''));

}

async function verificationTests() {
  console.log('\n单道题的审核记录：');
  const verifiedRecord = {
    course_id: 'analysis-i', exercise_id: 'pub-a', tier: 'human', review_status: 'published',
    human_verified_at: '2026-10-20T09:30:00Z', human_verified_by: '张老师',
    content_version: 3, updated_at: '2026-10-21T10:00:00Z',
    edited_after_review: true, lean_available: false, proofs: [],
    task: { task_id: 12, state: 'published' }
  };
  reset();
  App.state.review.verification = verifiedRecord;
  App.state.review.filters.course_id = 'analysis-i';
  html = App.views.review();
  check('查询结果显示可信等级', html.includes('人工通过') || html.includes('人工审核通过'));
  check('显示人工审核时间与人', html.includes('2026-10-20 09:30') && html.includes('张老师'));
  check('审核后被改过要显眼提示', html.includes('审核通过后又被改过'));
  check('说明 Lean 尚未接入', html.includes('Lean 验证尚未接入'));
  check('显示内容版本', html.includes('内容版本 v3'));
  check('显示最近一次审核任务状态', html.includes('最近一次审核任务：已发布'));

  reset();
  App.state.review.verification = {
    ...verifiedRecord, tier: 'none', human_verified_at: null, human_verified_by: null,
    edited_after_review: false, content_version: 1
  };
  App.state.review.filters.course_id = 'analysis-i';
  html = App.views.review();
  check('未审核的题明说尚未人工审核', html.includes('尚未人工审核'));
  check('未审核时不误报"审核后被改过"', !html.includes('审核通过后又被改过'));

  reset();
  App.state.review.verification = {
    ...verifiedRecord, lean_available: true,
    proofs: [
      { ord: 1, compiled: true, faithful_status: 'teacher-reviewed', toolchain: 'lean4:v4.12.0', has_statement: true, compiled_at: '2026-10-20T08:00:00Z' },
      { ord: 2, compiled: false, faithful_status: 'draft', toolchain: 'lean4:v4.12.0', has_statement: false }
    ]
  };
  App.state.review.filters.course_id = 'analysis-i';
  html = App.views.review();
  check('接入后列出每条 Lean 证明', html.includes('#1') && html.includes('#2'));
  check('区分编译通过与未通过', html.includes('编译通过') && html.includes('编译未通过'));
  check('忠实性已确认与未确认都显示', html.includes('忠实性已确认') && html.includes('忠实性：draft'));
  check('缺形式化命题要标出来', html.includes('缺形式化命题'));

  reset();
  App.state.review.verification = { error: '题目不存在：analysis-i/pub-z' };
  html = App.views.review();
  check('查不到时给错误说明而不是白屏', html.includes('读不到审核记录') && html.includes('题目不存在'));

  reset();
  App.state.review.filters.course_id = '';
  html = App.views.review();
  check('没选课程时查询按钮禁用', /data-action="review-verify-lookup"[^>]*disabled/.test(html));

  reset();
  App.state.review.filters.course_id = 'analysis-i';
  App.state.review.lookupId = 'pub-a';
  html = App.views.review();
  check('没选课程时提示先选课程（选了则显示在哪儿查）', html.includes('在「analysis-i」里查'));
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

  App.api.exerciseVerification = async () => { throw Object.assign(new Error('题目不存在：x/y'), { status: 404 }); };
  App.state.review.status = 'loading';
  await App.actions['review-verify-lookup']({ disabled: false });
  check('查询失败落到错误块', App.state.review.verification?.error?.includes('题目不存在'));

}

(async () => {
  await visibilityTests();
  await verificationTests();

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

  reset();
  App.state.review.filters.course_id = 'analysis-i';
  App.state.review.ledger = ledgerBody;
  html = App.views.review();
  check('总表列出全部题目', html.includes('ex-1') && html.includes('ex-2'));
  check('总表显示可信等级徽标', html.includes('未核') && html.includes('人工通过'));
  check('总表显示人工审核时间与审核人', html.includes('2026-10-20 10:00') && html.includes('张老师'));
  check('总表标出审核后被改过', html.includes('审核后改过'));
  check('总表显示工作流与审核任务状态', html.includes('已发布'));
  check('总表显示各档数量芯片', html.includes('全部') && html.includes('未核 1'));
  check('总表提示共多少道', html.includes('共 2 道'));

  reset();
  App.state.review.filters.course_id = '';
  html = App.views.review();
  check('没选课程时总表提示先选课', html.includes('先在上面的课程筛选里选一门课'));
  check('没选课程时不渲染表格', !html.includes('rv-table-wrap'));

  reset();
  App.state.review.filters.course_id = 'analysis-i';
  App.state.review.ledger = ledgerBody;
  App.state.review.ledgerTier = 'human';
  html = App.views.review();
  const chipsHtml = html.replace(/\s+/g, ' ');
  check('选中等级芯片高亮',
    chipsHtml.includes('class="chip active" data-action="ledger-tier" data-tier="human"')
    && !chipsHtml.includes('class="chip active" data-action="ledger-tier" data-tier="none"'),
    '只有选中的那一档该高亮');
  const ledgerCalls = [];
  App.api.reviewLedger = async params => {
    ledgerCalls.push(params);
    return ledgerBody;
  };
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

  console.log('');
  if (failures.length) {
    console.log(`失败 ${failures.length} 项：${failures.join(', ')}`);
    process.exit(1);
  }
  console.log('可见范围面板测试全部通过。');
})();
