#!/usr/bin/env node
/**
 * 建卷页：蓝图版本 → 候选清单 → 按槽位挑题 → 草稿卷。
 *
 * 要钉住的：
 *   1. 总分与槽位分用整数分算平，且分值全程是字符串——三位小数、非数字一律先挡；
 *   2. 正式卷必须选班级并走双人复核，前端先挡（服务端仍会再挡）；
 *   3. 同一别名再次保存需要 base_version_id：提示必须指向"以它为基础改一版"；
 *   4. 候选的排除原因码要翻成人话，且**整条码值也留在页面上**（排查用）；
 *   5. 建卷命令只送 蓝图版本 + 候选快照 + 已挑的槽位，没挑的槽位不出现。
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
  location: { hash: '#/course/analysis/paper-build' },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  // App.parseHash()/App.url() 用 URLSearchParams；vm 沙箱里要显式给。
  URLSearchParams
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(['js/core.js', 'js/paper-build.js'].map(read).join('\n;\n'), sandbox,
  { filename: 'paper-build.js' });

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

const RELEASE = '11111111-2222-3333-4444-555555555555';

// --- 夹具 ---------------------------------------------------------------------
const VERSION = {
  id: 'bpv-1', version_no: 2, purpose: 'formal', total_score: '100.00',
  duration_seconds: 7200, offering_ref: 'analysis-2026a',
  release_context_id: RELEASE,
  content: {
    purpose: 'formal', review_policy: 'formal_dual_review', total_score: '100.00',
    duration_seconds: 7200,
    sections: [
      { code: 'A', title: '计算题', slots: [{ code: 'A1', score: '40.00' }, { code: 'A2', score: '60.00' }] },
      { code: 'B', title: '证明题', slots: [{ code: 'B1', score: '30.00' }] }
    ]
  }
};

const SNAPSHOT = {
  id: 'snap-1', created: true,
  accepted: [
    { evidence: { exercise_id: 'ex-aaa', exercise_version_id: 'ev-1',
      exercise_kind: 'unknown', difficulty_label: null }, review_status: 'approved',
      license: 'self-authored' },
    { evidence: { exercise_id: 'ex-bbb', exercise_version_id: 'ev-2',
      exercise_kind: 'unknown', difficulty_label: null }, review_status: 'approved',
      license: 'self-authored' }
  ],
  rejected: [
    { exercise_id: 'ex-ccc', exercise_version_id: 'ev-3',
      reasons: ['review_not_approved', 'answer_missing'] }
  ]
};

const LABELS = { items: [
  { exercise_id: 'ex-aaa', exercise_ref: 'analysis-01', title: '连续性', review_status: 'approved' },
  { exercise_id: 'ex-bbb', exercise_ref: 'analysis-02', title: '一致连续', review_status: 'approved' },
  { exercise_id: 'ex-ccc', exercise_ref: 'analysis-03', title: '未审题', review_status: 'draft' }
] };

const stateOf = (patch) => Object.assign({
  courseId: 'analysis', blueprints: [VERSION], selectedId: VERSION.id, selected: VERSION,
  snapshot: SNAPSHOT, labels: {}, classes: [], loading: false, loaded: true,
  error: null, message: null, busy: null, built: null, showForm: true,
  selection: {}, form: App.paperBuildDefaults()
}, patch || {});

function formWith(patch) {
  const base = App.paperBuildDefaults();
  base.release = RELEASE;
  base.offering = 'analysis-2026a';
  return Object.assign(base, patch || {});
}

// --- 表单默认值与请求体 -------------------------------------------------------
const defaults = App.paperBuildDefaults();
check('默认是一份 100.00 分的两槽位蓝图',
  defaults.total === '100.00' && defaults.sections[0].slots.length === 2);
check('默认用途是正式卷 + 双人复核',
  defaults.purpose === 'formal' && defaults.review === 'formal_dual_review');
check('默认不带 base_version_id（第一版）', defaults.baseVersionId === null);

const body = App.paperBuildPayload(formWith());
check('载荷带别名与 Release', body.slug === 'manual' && body.release_context_id === RELEASE);
check('载荷带班级', body.offering_ref === 'analysis-2026a');
check('总分保持字符串', body.total_score === '100.00');
check('时长是整数', body.duration_seconds === 7200);
check('槽位拍平进分节里', body.sections.length === 1
  && body.sections[0].slots.map(slot => slot.code).join(',') === 'A1,A2');
check('只送蓝图字段', Object.keys(body).sort().join(',') ===
  'base_version_id,duration_seconds,offering_ref,purpose,release_context_id,review_policy,'
  + 'sections,slug,total_score');

check('总分与槽位和不一致被拒', (throwsMessage(() =>
  App.paperBuildPayload(formWith({ total: '120.00' }))) || '').includes('不一致'));
check('三位小数被拒', (throwsMessage(() => App.paperBuildPayload(formWith({
  sections: [{ code: 'A', title: '基础', slots: [{ code: 'A1', score: '40.001' }] }],
  total: '40.00'
}))) || '').includes('两位以内小数'));
check('槽位分不是数字被拒', throwsMessage(() => App.paperBuildPayload(formWith({
  sections: [{ code: 'A', title: '基础', slots: [{ code: 'A1', score: 'abc' }] }]
}))) !== null);
check('槽位编号重复被拒', (throwsMessage(() => App.paperBuildPayload(formWith({
  sections: [{ code: 'A', title: '基础', slots: [
    { code: 'A1', score: '50.00' }, { code: 'A1', score: '50.00' }] }]
}))) || '').includes('槽位编号重复'));
check('分节码重复被拒', (throwsMessage(() => App.paperBuildPayload(formWith({
  sections: [
    { code: 'A', title: '一', slots: [{ code: 'A1', score: '50.00' }] },
    { code: 'A', title: '二', slots: [{ code: 'A2', score: '50.00' }] }]
}))) || '').includes('分节码重复'));
check('没有分节被拒', throwsMessage(() => App.paperBuildPayload(formWith({ sections: [] }))) !== null);
check('分节缺标题被拒', throwsMessage(() => App.paperBuildPayload(formWith({
  sections: [{ code: 'A', title: '  ', slots: [{ code: 'A1', score: '100.00' }] }]
}))) !== null);
check('正式卷不选班级被拒', (throwsMessage(() =>
  App.paperBuildPayload(formWith({ offering: '' }))) || '').includes('先选班级'));
check('正式卷走单人复核被拒', (throwsMessage(() =>
  App.paperBuildPayload(formWith({ review: 'single_review' }))) || '').includes('双人复核'));
check('练习卷可以不选班级', !App.paperBuildPayload(formWith({
  purpose: 'practice', review: 'single_review', offering: ''
})).offering_ref);
check('别名非法被拒', (throwsMessage(() =>
  App.paperBuildPayload(formWith({ slug: '期中/期末' }))) || '').includes('别名'));
check('Release 不是 UUID 被拒', (throwsMessage(() =>
  App.paperBuildPayload(formWith({ release: 'release-1' }))) || '').includes('UUID'));
check('时长不是正整数被拒', throwsMessage(() =>
  App.paperBuildPayload(formWith({ duration: '0' }))) !== null);
check('带 base_version_id 时原样送出', App.paperBuildPayload(formWith({
  baseVersionId: 'bpv-9'
})).base_version_id === 'bpv-9');

// --- 以旧版为底 ---------------------------------------------------------------
const clone = App.paperBuildFromVersion(VERSION);
check('复制内容与分值', clone.sections.length === 2
  && clone.sections[1].slots[0].code === 'B1' && clone.sections[1].slots[0].score === '30.00');
check('复制总分与时长', clone.total === '100.00' && clone.duration === '7200');
check('基线指向旧版', clone.baseVersionId === 'bpv-1');
check('复制班级与 Release', clone.offering === 'analysis-2026a' && clone.release === RELEASE);
check('空版本回落到默认', App.paperBuildFromVersion(null).sections.length >= 1);

// --- 槽位与候选 ---------------------------------------------------------------
const slots = App.paperBuildSlots(VERSION);
check('槽位拍平带分节信息', slots.length === 3 && slots[2].sectionCode === 'B'
  && slots[2].sectionTitle === '证明题' && slots[2].score === '30.00');
check('没有蓝图时槽位为空', App.paperBuildSlots(null).length === 0);

const candidates = App.paperBuildCandidates(SNAPSHOT, {
  'ex-aaa': { exercise_ref: 'analysis-01', title: '连续性' }
});
check('可进卷的候选带标签', candidates.accepted[0].title === '连续性'
  && candidates.accepted[0].exerciseRef === 'analysis-01');
check('没有标签的候选回落到短 id', candidates.accepted[1].title === '题目 ex-bbb',
  candidates.accepted[1].title);
check('被挡的候选带原因', candidates.rejected[0].reasons.length === 2
  && candidates.rejected[0].reasons[0].text.includes('审核'));
check('原因码本身保留', candidates.rejected[0].reasons[0].code === 'review_not_approved');
check('未知原因码不吞掉', App.paperBuildCandidates({
  accepted: [], rejected: [{ exercise_id: 'x', reasons: ['some_new_code'] }]
}).rejected[0].reasons[0].text === 'some_new_code');
check('空快照不抛异常', App.paperBuildCandidates(null, null).accepted.length === 0);

const paperBody = App.paperBuildPaperBody(stateOf({
  labels: {}, selection: { A1: 'ev-1', B1: 'ev-2' }
}));
check('建卷命令带蓝图版本与快照', paperBody.blueprint_version_id === 'bpv-1'
  && paperBody.candidate_snapshot_id === 'snap-1');
check('只送挑了的槽位', paperBody.selections.length === 2
  && paperBody.selections.map(item => item.slot_code).join(',') === 'A1,B1');
check('一个都没挑时不伪造选题', App.paperBuildPaperBody(stateOf({ selection: {} }))
  .selections.length === 0);
check('没有快照时拒绝建卷', (throwsMessage(() =>
  App.paperBuildPaperBody(stateOf({ snapshot: null }))) || '').includes('候选清单'));
check('选题里带的是题目版本 id',
  paperBody.selections[0].exercise_version_id === 'ev-1');

// --- 错误文案 -----------------------------------------------------------------
check('base_version_required 指向"以它为基础"',
  App.paperBuildError({ code: 'base_version_required' }).includes('以它为基础'));
check('缺候选清单指向"生成候选清单"',
  App.paperBuildError({ code: 'candidate_snapshot_not_found' }).includes('生成候选清单'));
check('未知错误回落到 message',
  App.paperBuildError({ code: 'x', message: '原始错误' }) === '原始错误');
check('服务端 detail.code 也能识别',
  App.paperBuildError({ message: 'HTTP 409', detail: { code: 'stale_base_version' } })
    .includes('刷新'));
check('空错误不抛异常', typeof App.paperBuildError() === 'string');

// --- 渲染 ---------------------------------------------------------------------
const emptyHtml = App.paperBuildHtml(stateOf({ blueprints: [], selected: null, selectedId: null }));
check('没有蓝图时给"建第一份"的指引', emptyHtml.includes('建第一份'));
check('没有蓝图时仍然显示表单', emptyHtml.includes('build-form'));
check('没有蓝图时不给候选区', !emptyHtml.includes('build-candidates'));

const noSnapshotHtml = App.paperBuildHtml(stateOf({ snapshot: null }));
check('没有候选清单时给生成按钮', noSnapshotHtml.includes('build-snapshot')
  && noSnapshotHtml.includes('生成候选清单'));

const html = App.paperBuildHtml(stateOf({
  labels: { 'ex-aaa': { exercise_ref: 'analysis-01', title: '连续性' },
    'ex-bbb': { exercise_ref: 'analysis-02', title: '一致连续' },
    'ex-ccc': { exercise_ref: 'analysis-03', title: '未审题' } }
}));
check('蓝图列表逐行渲染', (html.match(/data-blueprint=/g) || []).length === 1);
check('状态行带版本与总分', html.includes('第 2 版') && html.includes('100.00'));
check('已选中那行不再给"选这份"', !html.includes('build-select')
  && html.includes('已选中'));
check('每个槽位一个选择框', (html.match(/data-input="build-pick"/g) || []).length === 3);
check('选择框带可读题目名', html.includes('analysis-01 · 连续性'));
check('空槽位选项排在最前', html.includes('（这个槽位先空着）'));
check('被挡的题目给出原因码与中文', html.includes('review_not_approved')
  && html.includes('审核还没通过'));
check('候选统计写清楚', html.includes('2 道可进卷') && html.includes('1 道被挡'));
check('有建卷按钮', html.includes('build-create-paper'));

const builtHtml = App.paperBuildHtml(stateOf({
  built: { id: 'pv-9', version_no: 1, total_score: '100.00', sections: [{ code: 'A' }] }
}));
check('建好后给编辑器与审核发布两个入口',
  builtHtml.includes('paper-editor') && builtHtml.includes('paper-ops')
  && builtHtml.includes('version=pv-9'));

const hostile = App.paperBuildHtml(stateOf({
  selected: { ...VERSION, offering_ref: '<img src=x>' }
}));
check('外部字符串被转义', !hostile.includes('<img') && hostile.includes('&lt;img'));
check('空 state 不抛异常', App.paperBuildHtml(null).includes('还没有读取到建卷数据'));
check('错误显示为 alert', App.paperBuildHtml(stateOf({ error: '炸了' })).includes('role="alert"'));
check('成功提示显示为 status',
  App.paperBuildHtml(stateOf({ message: '已保存' })).includes('role="status"'));
check('busy 时建卷按钮禁用',
  App.paperBuildHtml(stateOf({ busy: 'create' })).includes('disabled'));

// --- 页面入口 -----------------------------------------------------------------
check('注册了视图 paper-build', typeof App.views['paper-build'] === 'function');
check('注册了进入后取数钩子', typeof App.after['paper-build'] === 'function');
check('注册了动作', ['build-refresh', 'build-select', 'build-base', 'build-reset',
  'build-add-section', 'build-add-slot', 'build-remove-slot', 'build-remove-section',
  'build-save-blueprint', 'build-snapshot', 'build-create-paper']
  .every(action => typeof App.actions[action] === 'function'));
check('注册了表单输入', ['build-slug', 'build-purpose', 'build-review', 'build-offering',
  'build-release', 'build-total', 'build-duration', 'build-section-code',
  'build-section-title', 'build-slot-code', 'build-slot-score', 'build-pick']
  .every(id => typeof App.inputs[id] === 'function'));

App.route = App.parseHash('#/course/analysis/paper-build');
App.course = { id: 'analysis', title: '数学分析 I' };
App.state.paperBuild = stateOf({ courseId: 'analysis' });
const page = App.views['paper-build']();
check('页面带标题与表单', page.includes('建卷') && page.includes('build-form'));

App.course = null;
check('没选课程时给指引', App.views['paper-build']().includes('先选择一门课程'));

App.course = { id: 'analysis', title: '数学分析 I' };
App.state.paperBuild = stateOf({ courseId: 'analysis', blueprints: [], selected: null, loading: true });
check('加载中给出提示', App.views['paper-build']().includes('正在读取蓝图'));

App.state.paperBuild = stateOf({
  courseId: 'other', blueprints: [VERSION], selected: VERSION, built: { id: 'old' }
});
const switched = App.views['paper-build']();
check('换课程后清掉旧蓝图', App.state.paperBuild.blueprints.length === 0
  && App.state.paperBuild.built === null);
check('换课程后回到空表单', switched.includes('build-form'));

// --- 取数 ---------------------------------------------------------------------
const asked = [];
App.render = () => {};
App.api = {
  courseBlueprints: courseId => { asked.push(['blueprints', courseId]); return Promise.resolve([VERSION]); },
  classes: () => Promise.resolve({ items: [{ id: 'analysis-2026a', name: '数学分析 I 1 班' }] }),
  candidateSnapshot: id => { asked.push(['snapshot', id]);
    return Promise.reject(Object.assign(new Error('HTTP 404'),
      { code: 'candidate_snapshot_not_found' })); },
  candidateLabels: id => { asked.push(['labels', id]); return Promise.resolve(LABELS); }
};
App.state.paperBuild = stateOf({ courseId: null, blueprints: [], selected: null,
  selectedId: null, snapshot: null, loaded: false });

(async () => {
  await App.loadPaperBuild('analysis');
  check('按课程号取蓝图列表', asked.some(([what, id]) =>
    what === 'blueprints' && id === 'analysis'));
  check('自动选第一份蓝图并读它的候选', App.state.paperBuild.selectedId === 'bpv-1'
    && asked.some(([what, id]) => what === 'snapshot' && id === 'bpv-1'));
  check('读过一次后不再重复请求', await (async () => {
    const before = asked.length;
    await App.loadPaperBuild('analysis');
    return asked.length === before;
  })(), '渲染后 after 钩子会再跑一次，必须靠 loaded 标记挡住');
  check('没有候选快照不算错误', App.state.paperBuild.snapshot === null
    && App.state.paperBuild.error === null);
  check('题目标签按 exercise_id 建了映射',
    App.state.paperBuild.labels['ex-aaa'].title === '连续性');
  check('班级列表可用于选班级', App.state.paperBuild.classes[0].id === 'analysis-2026a');

  console.log();
  if (failures.length) {
    console.log(`✗ ${failures.length} 项未通过：${failures.join('、')}`);
    process.exit(1);
  }
  console.log('✓ 建卷页视图全部通过');
})().catch(error => {
  console.error(error);
  process.exit(1);
});
