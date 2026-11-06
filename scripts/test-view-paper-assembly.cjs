#!/usr/bin/env node
/**
 * 约束组卷页：候选供给 → 预检 → 跑一次求解 → 比较方案 → 选方案落草稿卷。
 *
 * 要钉住的都是"界面会不会说谎"：
 *   1. `feasible` 必须显示成「可行（未证明最优）」，不能跟 `optimal` 一样；
 *   2. `infeasible` 不是错误：要把诊断逐条列出来，且**不渲染**建卷按钮；
 *   3. 求解器没装（failed + ortools 提示）时给可执行的安装命令，也不给建卷按钮；
 *   4. 求解参数先在前端挡（方案数 1—3、时限 ≥1 秒、种子是整数），不发注定 422 的请求；
 *   5. 建卷成功后必须提示"还要过整卷校验与双人复核"，不能让人以为已经能发。
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');

const sandbox = {
  console: { log: console.log, warn() {}, error() {} },
  setTimeout, clearTimeout,
  document: {
    getElementById: () => null, addEventListener() {}, querySelector: () => null,
    querySelectorAll: () => [], body: { classList: { add() {}, remove() {} } }
  },
  location: { hash: '#/course/analysis/paper-assembly' },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  URLSearchParams
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(['js/core.js', 'js/paper-assembly.js'].map(read).join('\n;\n'), sandbox,
  { filename: 'paper-assembly.js' });

const App = sandbox.window.App;
App.icon = App.icon || (() => '<svg/>');
App.button = App.button || ((label, action) => `<button data-action="${action}">${label}</button>`);

const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};
const throwsMessage = fn => {
  try { fn(); return null; } catch (error) { return error.message; }
};

// --- 夹具 ---------------------------------------------------------------------

const VERSION = {
  id: 'bpv-1', version_no: 2, purpose: 'formal', total_score: '100.00',
  assembly_mode: 'constraint_assisted'
};

const SUPPLY = {
  blueprint_version_id: 'bpv-1',
  candidate_manifest_hash: 'a'.repeat(64),
  candidates: 8,
  exclusions: [{ exercise_version_id: 'ev-x', reason: 'exposure_conflict' }],
  supply: { A1: 5, A2: 0, B1: 3 },
  issues: [{ code: 'slot_has_no_candidate', message: '槽位 A2 没有任何可选候选',
    slot_code: 'A2' }],
  solvable_precheck: false
};

const OPTIMAL_RUN = {
  assembly_run_id: 'run-1', blueprint_version_id: 'bpv-1', status: 'optimal',
  solved: true, optimal_proven: true,
  candidate_manifest_hash: 'b'.repeat(64), candidate_snapshot_id: 'snap-1',
  solver_name: 'cp-sat', solver_version: '9.11.4210',
  solver_config: { random_seed: 7, time_limit_seconds: 2, max_solutions: 1 },
  best_bound: 0, optimality_gap_milli: 0,
  diagnostics: { precheck_codes: [], issues: [] },
  objective_breakdown: {}, created_at: '2026-11-06T06:00:00+00:00',
  started_at: '2026-11-06T06:00:00+00:00', completed_at: '2026-11-06T06:00:01+00:00',
  solutions: [
    { variant_label: 'single', manifest_hash: 'c'.repeat(64), optimal_proven: true,
      solver_status: 'optimal', objective_terms: [],
      selections: [{ slot_code: 'A1' }, { slot_code: 'A2' }] },
    { variant_label: 'single-2', manifest_hash: 'd'.repeat(64), optimal_proven: false,
      solver_status: 'feasible', objective_terms: [], selections: [{ slot_code: 'A1' }] }
  ]
};

const FEASIBLE_RUN = { ...OPTIMAL_RUN, status: 'feasible', solved: true,
  optimal_proven: false,
  solutions: [{ variant_label: 'single', manifest_hash: 'e'.repeat(64),
    optimal_proven: false, solver_status: 'feasible', objective_terms: [],
    selections: [{ slot_code: 'A1' }] }] };

const INFEASIBLE_RUN = {
  ...OPTIMAL_RUN, status: 'infeasible', solved: false, optimal_proven: false,
  solutions: [], best_bound: null, optimality_gap_milli: null,
  diagnostics: {
    reason: 'precheck_proved',
    issues: [
      { code: 'slot_has_no_candidate', message: '槽位 A2 没有任何可选候选', slot_code: 'A2' },
      { code: 'duration_impossible', message: '总时长下界 3600 秒超过上限 3000 秒',
        slot_code: null }
    ],
    summary: { slots: 3, candidates: 8, exclusions: 1 }
  }
};

const FAILED_NO_SOLVER = {
  ...OPTIMAL_RUN, status: 'failed', solved: false, optimal_proven: false,
  solutions: [], solver_name: 'unavailable', solver_version: 'unavailable',
  diagnostics: {
    reason: 'solver_error',
    detail: 'SolverUnavailableError: CP-SAT 求解器不可用：本机未安装 ortools。'
  }
};

// --- 状态机与标签 ---------------------------------------------------------------

const stateOf = patch => ({ courseId: 'analysis', blueprints: [VERSION],
  selectedId: 'bpv-1', selected: VERSION, supply: SUPPLY, runs: [OPTIMAL_RUN],
  run: OPTIMAL_RUN, created: null, config: { random_seed: '7', time_limit_seconds: '2',
    max_solutions: '1' }, ...patch });

check('最优运行标「最优（已证明）」',
  App.paperAssemblyStatus(OPTIMAL_RUN).includes('已证明'));
check('可行运行标「可行（未证明最优）」',
  App.paperAssemblyStatus(FEASIBLE_RUN) === '可行（未证明最优）',
  App.paperAssemblyStatus(FEASIBLE_RUN));
check('可行与最优的标签不同',
  App.paperAssemblyStatus(FEASIBLE_RUN) !== App.paperAssemblyStatus(OPTIMAL_RUN));
check('无解标签写清是约束冲突', App.paperAssemblyStatus(INFEASIBLE_RUN).includes('无解'));
check('最优是绿色、可行是蓝色、无解是红色',
  App.paperAssemblyTone(OPTIMAL_RUN) === 'green'
  && App.paperAssemblyTone(FEASIBLE_RUN) === 'blue'
  && App.paperAssemblyTone(INFEASIBLE_RUN) === 'red');
check('未知状态原样显示，不假装成功',
  App.paperAssemblyStatus({ status: 'cancelled' }) === '已取消');

// 一句话结论：四种状态必须说清"下一步是什么、有没有建卷、有没有放松约束"。
check('最优结论说可以选方案',
  App.paperAssemblyVerdict(OPTIMAL_RUN).includes('已证明最优'));
check('可行结论明确写「未证明最优」',
  App.paperAssemblyVerdict(FEASIBLE_RUN).includes('可行，未证明最优'));
check('无解结论写明没有自动放松、没有建卷',
  App.paperAssemblyVerdict(INFEASIBLE_RUN).includes('未自动放松')
  && App.paperAssemblyVerdict(INFEASIBLE_RUN).includes('未创建试卷'));
check('超时无解结论写明未创建试卷',
  App.paperAssemblyVerdict({ status: 'timeout', solutions: [] })
    .includes('超时，无可行解；未创建试卷'));
check('超时但有解时保留方案可用性',
  App.paperAssemblyVerdict({ status: 'timeout', solutions: [{}] })
    .includes('找到的方案可用'));
check('失败结论写明未创建试卷',
  App.paperAssemblyVerdict(FAILED_NO_SOLVER).includes('未创建试卷'));
check('没有运行时给中性结论',
  App.paperAssemblyVerdict(null).includes('还没有运行记录'));

check('装了 ortools 时不算降级', App.paperAssemblyDegraded(OPTIMAL_RUN) === false);
check('缺求解器的失败被识别为降级',
  App.paperAssemblyDegraded(FAILED_NO_SOLVER) === true);
check('其它失败不算降级',
  App.paperAssemblyDegraded({ status: 'failed',
    diagnostics: { reason: 'solver_error', detail: 'ValueError: 模型非法' } }) === false);

// --- 方案摘要 -------------------------------------------------------------------

const summaries = App.paperAssemblySolutions(OPTIMAL_RUN);
check('方案逐个列出', summaries.length === 2);
check('方案带序号与题数', summaries[0].index === 0 && summaries[0].selections === 2);
check('未证明最优的方案在摘要里就是 false', summaries[1].optimal === false);
check('方案哈希截断显示', summaries[0].hash.length === 12);
check('空运行返回空方案列表', App.paperAssemblySolutions(null).length === 0);

// --- 参数校验 -------------------------------------------------------------------

check('合法参数返回整数',
  App.paperAssemblyConfig({ random_seed: '11', time_limit_seconds: '5',
    max_solutions: '2' }).max_solutions === 2);
check('方案数 4 被前端拒绝',
  throwsMessage(() => App.paperAssemblyConfig({ random_seed: '1',
    time_limit_seconds: '5', max_solutions: '4' })).includes('1—3'));
check('方案数 0 被拒绝',
  throwsMessage(() => App.paperAssemblyConfig({ random_seed: '1',
    time_limit_seconds: '5', max_solutions: '0' })).includes('1—3'));
check('时限 0 被拒绝',
  throwsMessage(() => App.paperAssemblyConfig({ random_seed: '1',
    time_limit_seconds: '0', max_solutions: '1' })).includes('1 秒'));
check('非整数种子被拒绝',
  throwsMessage(() => App.paperAssemblyConfig({ random_seed: 'abc',
    time_limit_seconds: '5', max_solutions: '1' })).includes('整数'));

// --- 页面渲染 -------------------------------------------------------------------

const optimalHtml = App.paperAssemblyHtml(stateOf({}));
check('页面画出蓝图选择器', optimalHtml.includes('assembly-blueprint'));
check('页面画出候选供给表', optimalHtml.includes('data-slot="A1"'));
check('没有候选的槽位被标出来', optimalHtml.includes('这一槽没有可选题目'));
check('预检问题逐条列出并带码值',
  optimalHtml.includes('data-code="slot_has_no_candidate"')
  && optimalHtml.includes('没有可选题目'));
check('预检通过/不通过都要说明"不保证可解"',
  optimalHtml.includes('不保证可解') || optimalHtml.includes('组合上的冲突'));
check('最优方案可建卷', optimalHtml.includes('data-action="assembly-select"'));
check('方案表列出未证明最优的那一个',
  optimalHtml.includes('未证明最优'));

const feasibleHtml = App.paperAssemblyHtml(stateOf({ run: FEASIBLE_RUN, runs: [FEASIBLE_RUN] }));
check('可行运行页面写「可行（未证明最优）」',
  feasibleHtml.includes('可行（未证明最优）'));

const infeasibleHtml = App.paperAssemblyHtml(stateOf({
  run: INFEASIBLE_RUN, runs: [INFEASIBLE_RUN], created: null }));
check('无解页列出诊断原因', infeasibleHtml.includes('无解（约束互相冲突）'));
check('无解页逐条给出问题码',
  infeasibleHtml.includes('data-code="duration_impossible"'));
check('无解页不给建卷按钮', !infeasibleHtml.includes('assembly-select'));
check('无解页说明"没有方案就不会建卷"',
  infeasibleHtml.includes('没有方案就不会建卷'));

const degradedHtml = App.paperAssemblyHtml(stateOf({
  run: FAILED_NO_SOLVER, runs: [FAILED_NO_SOLVER] }));
check('缺求解器页给出安装命令',
  degradedHtml.includes('requirements-assembly.txt'));
check('缺求解器页不给建卷按钮', !degradedHtml.includes('assembly-select'));
check('缺求解器页说明不会给假方案',
  degradedHtml.includes('不会给出任何'));

const createdHtml = App.paperAssemblyHtml(stateOf({
  created: { paper_version_id: 'pv-9', manifest_hash: 'f'.repeat(64),
    status: 'draft', next_step: '草稿卷仍需整卷校验与双人复核，才能提交审核通过' }
}));
check('建卷成功后给出草稿提示', createdHtml.includes('草稿卷已建好'));
check('建卷成功后提示还要复核', createdHtml.includes('双人复核'));
check('建卷成功后给编辑与审核入口',
  createdHtml.includes('paper-editor') && createdHtml.includes('paper-ops'));

const emptyHtml = App.paperAssemblyHtml(stateOf({ courseId: null }));
check('没有课程时提示先选课程', emptyHtml.includes('先选择一门课程'));

const noBlueprintHtml = App.paperAssemblyHtml(stateOf({
  blueprints: [], selected: null, supply: null, run: null, runs: [] }));
check('没有蓝图时指向建卷页', noBlueprintHtml.includes('建卷'));

const hostileHtml = App.paperAssemblyHtml(stateOf({
  run: { ...OPTIMAL_RUN, solver_name: '<img src=x>', status: 'optimal' } }));
check('外部字符串被转义', !hostileHtml.includes('<img')
  && hostileHtml.includes('&lt;img'));

// --- 排队路径 --------------------------------------------------------------------

const queuedHtml = App.paperAssemblyHtml(stateOf({
  run: null, queued: { assembly_run_id: 'run-q', status: 'queued', job_id: '42' }
}));
check('排队后页面说明还没有结论', queuedHtml.includes('还没有运行记录'));
check('排队不显示建卷按钮', !queuedHtml.includes('assembly-select'));
check('排队状态的说法不冒充完成',
  App.paperAssemblyVerdict({ status: 'queued', solutions: [] }).includes('没有结论'));
check('运行中状态也不冒充完成',
  App.paperAssemblyVerdict({ status: 'running', solutions: [] }).includes('没有结论'));

// --- 手工蓝图不能跑求解 ------------------------------------------------------------

const manualHtml = App.paperAssemblyHtml(stateOf({
  selected: { ...VERSION, assembly_mode: 'manual' },
  blueprints: [{ ...VERSION, assembly_mode: 'manual' }]
}));
check('手工蓝图页说明不能跑求解', manualHtml.includes('手工组卷'));
check('手工蓝图页不给开始求解按钮', !manualHtml.includes('assembly-start'));
check('手工蓝图页给出可执行下一步',
  manualHtml.includes('constraint_assisted') && manualHtml.includes('paper-build'));

// --- 方案对照 --------------------------------------------------------------------

const twoSolutions = { ...OPTIMAL_RUN, solutions: [
  { ...OPTIMAL_RUN.solutions[0] },
  { ...OPTIMAL_RUN.solutions[1] }
] };
const compareButtonHtml = App.paperAssemblyHtml(stateOf({ run: twoSolutions,
  runs: [twoSolutions] }));
check('两个方案时出现对照按钮', compareButtonHtml.includes('assembly-compare'));
const singleSolution = { ...OPTIMAL_RUN, solutions: [OPTIMAL_RUN.solutions[0]] };
check('只有一个方案时没有对照按钮',
  !App.paperAssemblyHtml(stateOf({ run: singleSolution, runs: [singleSolution] }))
    .includes('assembly-compare'));

const comparedHtml = App.paperAssemblyHtml(stateOf({ run: twoSolutions,
  runs: [twoSolutions],
  comparison: {
    note: '结构匹配的 A/B 卷；不代表统计等值',
    structurally_matched: false,
    statistical_equivalence: false,
    issues: [{ code: 'shared_version_not_anchored', message: '槽位 A1 两卷用了同一道题' }],
    shared_version_ids: ['ev-1'], shared_family_ids: [],
    summary: { slots: 2 }
  } }));
check('对照块显示固定措辞', comparedHtml.includes('不代表统计等值'));
check('对照块列出重叠问题', comparedHtml.includes('data-code="shared_version_not_anchored"'));
check('对照块明确不是难度等值结论',
  comparedHtml.includes('不是') && comparedHtml.includes('等值'));

// --- 视图注册 -------------------------------------------------------------------

check('注册了视图 paper-assembly', typeof App.views['paper-assembly'] === 'function');
check('注册了进入后取数钩子', typeof App.after['paper-assembly'] === 'function');
check('注册了三个动作', ['assembly-refresh', 'assembly-start', 'assembly-select']
  .every(action => typeof App.actions[action] === 'function'));
check('注册了四个输入', ['assembly-blueprint', 'assembly-seed', 'assembly-seconds',
  'assembly-solutions'].every(name => typeof App.inputs[name] === 'function'));

// --- 页面外壳 -------------------------------------------------------------------

App.state = {};
App.course = null;
const noCourse = App.views['paper-assembly']();
check('没有课程时页面给出指引', noCourse.includes('先选择一门课程'));

App.course = { id: 'analysis' };
App.state.paperAssembly = stateOf({ loading: true, blueprints: [] });
const loading = App.views['paper-assembly']();
check('加载中给出提示而不是空白', loading.includes('正在读取'));

App.state.paperAssembly = stateOf({ error: '炸了', blueprints: [], loading: false });
const failed = App.views['paper-assembly']();
check('读失败时显示错误', failed.includes('读不到组卷数据') && failed.includes('炸了'));

App.state.paperAssembly = stateOf({});
const rendered = App.views['paper-assembly']();
check('页面包含标题与求解表单',
  rendered.includes('约束组卷') && rendered.includes('assembly-start'));

// --- 取数 -----------------------------------------------------------------------

const asked = [];
App.render = () => {};
App.api = {
  courseBlueprints: courseId => {
    asked.push(['blueprints', courseId]);
    return Promise.resolve([VERSION]);
  },
  assemblyCandidateSupply: id => {
    asked.push(['supply', id]);
    return Promise.resolve(SUPPLY);
  },
  assemblyRuns: (id, limit) => {
    asked.push(['runs', id, limit]);
    return Promise.resolve([OPTIMAL_RUN]);
  }
};
App.state.paperAssembly = stateOf({ courseId: null, blueprints: [], selected: null,
  selectedId: null, supply: null, runs: [], run: null, loaded: false });

(async () => {
  await App.loadPaperAssembly('analysis');
  check('按课程号取蓝图列表',
    asked.some(([what, id]) => what === 'blueprints' && id === 'analysis'));
  check('自动选第一份蓝图', App.state.paperAssembly.selectedId === 'bpv-1');
  check('同时取候选供给与运行历史',
    asked.some(([what, id]) => what === 'supply' && id === 'bpv-1')
    && asked.some(([what, id]) => what === 'runs' && id === 'bpv-1'));
  check('默认展示最近一次运行',
    App.state.paperAssembly.run.assembly_run_id === 'run-1');
  check('读过一次后不再重复请求', await (async () => {
    const before = asked.length;
    await App.loadPaperAssembly('analysis');
    return asked.length === before;
  })(), '渲染后 after 钩子会再跑一次，必须靠 loaded 标记挡住');

  console.log();
  if (failures.length) {
    console.log(`✗ ${failures.length} 项未通过：${failures.join('、')}`);
    process.exit(1);
  }
  console.log('✓ 约束组卷页视图全部通过');
})().catch(error => {
  console.error(error);
  process.exit(1);
});
