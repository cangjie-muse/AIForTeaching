#!/usr/bin/env node
/**
 * 审核工作台的渲染冒烟测试（node:vm，不启浏览器）。
 *
 * 重点不是"页面长什么样"，而是几条会出错的行为：
 *   - 后端不可达时给引导，不白屏
 *   - 无审核权限时明确说"没有权限"，而不是"队列是空的"
 *   - 空队列时告诉人下一步点哪里
 *   - 题目里的 HTML 被转义（题干来自教材，不能当 HTML 信任）
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');

const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  URLSearchParams,
  location: { hash: '#/review' },
  fetch: () => Promise.reject(new Error('测试中不应发起真实请求')),
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

const bundle = ['js/core.js', 'js/visual-identity.js', 'js/api-client.js', 'js/diff.js',
    'js/review-scope.js', 'js/review-node.js', 'js/review.js']
  .map(file => fs.readFileSync(path.join(root, file), 'utf8'))
  .join('\n;\n');
vm.runInContext(bundle, sandbox, { filename: 'bundle.js' });

const App = sandbox.window.App;
assert.ok(App.views.review, '视图未注册：App.views.review');

// App.markdown 要走 katex + marked + DOMPurify + 真 DOM，那是 core.js 的职责，
// 在 vm 里复刻不了也不该复刻。这里换成探针，验证"题干确实经过安全渲染入口"，
// 而不是被页面自己拼进 innerHTML。
const markdownCalls = [];
App.markdown = (source) => { markdownCalls.push(source); return '<div class="prose">[md]</div>'; };

const failures = [];
const check = (name, condition, detail = '') => {
  console.log(`  ${condition ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!condition) failures.push(name);
};

function reset(overrides) {
  App.state.review = {
    status: 'ready',
    error: '',
    summary: { tiers: [], task_states: [], open_issues: [] },
    items: [],
    diffs: {},
    filters: { course_id: '', category: '', priority: '' },
    checked: {},
    busy: false,
    ...overrides
  };
}

// 1. 后端不可达
reset({ status: 'error', error: '无法连接后端（同源）' });
let html = App.views.review();
check('后端不可达时给出地址输入与重试', html.includes('data-input="api-base"') && html.includes('data-action="review-refresh"'));

// 2. 无权限
reset({ status: 'denied' });
html = App.views.review();
check('无权限时明说没有权限', html.includes('没有审核权限'));
check('无权限时不显示「队列是空的」', !html.includes('队列是空的'));

// 3. 空队列
reset();
html = App.views.review();
check('空队列时指引用「从数据生成队列」', html.includes('从数据生成队列') && html.includes('队列是空的'));

// 4. 正常列表
reset({
  courses: [{ id: 'analysis', title: '数学分析 I', exercise_count: 416 },
            { id: 'analysis-ii', title: '数学分析 II', exercise_count: 349 }],
  summary: {
    tiers: [{ course_id: 'math', tier: 'none', n: 3 }],
    task_states: [{ state: 'pending', n: 3 }, { state: 'approved', n: 1 }],
    open_issues: [{ category: 'page-imprecise', n: 2 }]
  },
  items: [{
    task_id: 7, course_id: 'math', exercise_id: 'ex-1', state: 'pending', priority: 2,
    prompt_md: '求极限 <img src=x onerror=alert(1)>', difficulty: 3,
    provenance: 'textbook', source_page: 12, tier: 'none', node_ids: ['n-1'],
    issues: [{ category: 'page-imprecise', severity: 'major',
               detail: '页码与 PDF 不对应 <script>alert(1)</script>' }]
  }]
});
html = App.views.review();
check('列表显示题干摘要', html.includes('求极限'));
check('题干转义后出现在列表', html.includes('&lt;img') && !html.includes('<img src=x'));
check('问题内容里的 HTML 被转义', html.includes('&lt;script&gt;') && !html.includes('<script>alert(1)'));
check('显示大盘统计', html.includes('待审') && html.includes('已通过'));
check('问题分类可点击筛选', html.includes('data-action="review-filter"') && html.includes('页码不精确'));
check('优先级高亮', html.includes('rv-priority high'));
check('可信度显示为「未核」', html.includes('未核'));
check('有课程筛选且列出题量', html.includes('data-input="review-course"') && html.includes('数学分析 II（349 题）'));

// 5. 打开详情
App.state.review.open = 7;
html = App.views.review();
check('详情显示题目编号与来源', html.includes('ex-1') && html.includes('书页 12'));
check('详情显示挂载知识点', html.includes('n-1'));
check('详情里的题干经过 App.markdown', markdownCalls.some(text => String(text).includes('求极限')));
check('详情提供领取/通过/退回/修改', ['claim', 'approve', 'request-changes'].every(v => html.includes(`data-verb="${v}"`)) && html.includes('data-action="review-edit"'));

// 6. 勾选后出现批量入口
App.state.review.checked = { 7: true };
html = App.views.review();
check('勾选后出现批量通过', html.includes('data-action="review-batch-approve"'));
check('勾选行有高亮样式', html.includes('rv-selected'));

// 7. 未挂载知识点要显眼警告，不能静默
reset({
  items: [{
    task_id: 9, course_id: 'math', exercise_id: 'ex-9', state: 'pending', priority: 1,
    prompt_md: '题', tier: 'none', node_ids: [], issues: []
  }]
});
App.state.review.open = 9;
html = App.views.review();
check('未挂知识点显示警告', html.includes('未挂任何知识点'));

// 8. 修订差异
const diffItem = {
  task_id: 9, course_id: 'math', exercise_id: 'ex-9', state: 'pending', priority: 1,
  prompt_md: '改后的题干', tier: 'none', node_ids: ['n-2'], issues: []
};
reset({
  items: [diffItem],
  diffs: {
    'math/ex-9': {
      status: 'ready',
      current: { prompt_md: '求 $x^2$ 的极限', answer_md: '解：略', difficulty: 3, source_page: 20, node_ids: ['n-2'] },
      revisions: [{
        version: 1, prompt_md: '求 $x$ 的极限', answer_md: '解：详', difficulty: 2,
        source_page: 18, node_ids: ['n-1'], changed_by: '张老师',
        changed_at: '2026-10-20T09:30:00+08:00', change_note: '核对页码'
      }]
    }
  }
});
App.state.review.open = 9;
html = App.views.review();
check('显示修订差异区块', html.includes('rv-diff') && html.includes('第 1 次修改前'));
check('差异定位到字符（有增有删）', html.includes('df-add') && html.includes('df-del'));
check('显示了改动人与说明', html.includes('张老师') && html.includes('核对页码'));
check('标量变化并列显示', html.includes('df-scalar') && html.includes('2026-10-20 09:30'));
check('挂载变化显示增减', html.includes('- n-1'));

reset({
  items: [diffItem],
  diffs: { 'math/ex-9': { status: 'ready', current: { prompt_md: '题' }, revisions: [] } }
});
App.state.review.open = 9;
html = App.views.review();
check('没有修订时明说导入时就是这样', html.includes('还没有修订记录'));

reset({
  items: [diffItem],
  diffs: { 'math/ex-9': { status: 'error', error: '后端未启动' } }
});
App.state.review.open = 9;
html = App.views.review();
check('读不到修订历史时不白屏', html.includes('读不到修订历史') && html.includes('后端未启动'));

// 5. 分栏：习题 / 图谱
reset();
html = App.views.review();
check('审核页分成两块：习题审核 / 知识图谱审核',
  html.includes('rv-tabs') && html.includes('习题审核') && html.includes('知识图谱审核'));
check('默认停在习题审核', html.includes('data-action="review-tab" data-tab="exercises" role="tab" aria-selected="true"'));
check('习题审核里不再有学生可见范围', !html.includes('学生可见范围'),
  '可见范围应该搬去系统设置');

reset({
  tab: 'graph',
  courses: [{ id: 'analysis-ii', title: '数学分析 II' }],
  filters: { course_id: '', category: '', priority: '' }
});
html = App.views.review();
check('图谱审核没选课时先要人选课', html.includes('先在上面选一门课'));
check('图谱审核自带课程选择器', html.includes('data-input="review-course"'));

reset({
  tab: 'graph',
  courses: [{ id: 'analysis-ii', title: '数学分析 II' }],
  filters: { course_id: 'analysis-ii', category: '', priority: '' },
  graphOpen: { 'ch-1': true },
  graph: {
    totals: { nodes: 3, verified: 1 },
    root: { id: 'analysis-ii', title: '数学分析 II', verified: false },
    chapters: [{ chapter_id: 'ch-1', title: '第一章 极限', nodes: 2, verified: 1 }],
    nodes: [
      { id: 'ch-1', chapter_id: 'ch-1', parent_id: null, title: '第一章 极限',
        kind: 'container', level: 2, verified: false },
      { id: 'k-1', chapter_id: 'ch-1', parent_id: 'ch-1', title: '数列极限',
        kind: 'knowledge', level: 3, verified: true, exercise_count: 3 },
      { id: 'k-2', chapter_id: 'ch-1', parent_id: 'ch-1', title: '函数极限',
        kind: 'knowledge', level: 3, verified: false, exercise_count: 0 }
    ]
  }
});
html = App.views.review();
check('图谱按章成卡片', html.includes('rv-chapter') && html.includes('第一章 极限'));
check('章节显示知识点数与已审数', html.includes('2 个知识点 · 已审 1'));
check('展开后列出这一章的子节点',
  html.includes('数列极限') && html.includes('函数极限'), '子节点没渲染出来');
check('已审节点显示"已通过"', html.includes('已通过'));
check('未审节点显示"未审"', html.includes('未审'));
check('每个节点都有通过/撤销',
  html.includes('data-action="graph-node-verify"'));
check('行上给「去图谱里改」的深链（带 node 参数）',
  html.includes('去图谱里改') && html.includes('node=k-2'));
check('行上直接显示挂题数', html.includes('3 题'));
check('没挂题的节点明说未挂题', html.includes('未挂题'), '空着会让人以为是加载失败');
check('章一级仍可整章通过',
  html.includes('data-action="graph-verify" data-chapter="ch-1"'));
check('没有重复渲染章节容器节点',
  (html.match(/data-node="ch-1"/g) || []).length === 0,
  '章节容器不该出现在子节点列表里');
check('课程根节点单列，给「整门课通过」',
  html.includes('data-action="graph-root-verify"') && html.includes('整门课通过'),
  '根节点没通过时学生端的树长不出来');

reset({
  tab: 'graph',
  courses: [{ id: 'analysis-ii', title: '数学分析 II' }],
  filters: { course_id: 'analysis-ii', category: '', priority: '' },
  graph: {
    totals: { nodes: 1, verified: 1 },
    root: { id: 'analysis-ii', title: '数学分析 II', verified: true },
    chapters: [],
    nodes: []
  }
});
html = App.views.review();
check('根节点已通过时显示徽标并可撤销',
  html.includes('课程根节点已通过') && html.includes('data-verb="unverify"')
  && html.includes('撤销整门课'));

reset({
  tab: 'graph',
  courses: [{ id: 'analysis-ii', title: '数学分析 II' }],
  filters: { course_id: 'analysis-ii', category: '', priority: '' },
  graphOpen: { 'ch-1': true },
  nodePanel: 'k-2',
  nodeDetail: {
    node: { id: 'k-2', title: '函数极限', kind: 'knowledge', level: 3, verified: false },
    parent: { id: 'k-1', title: '数列极限' },
    chapter: { id: 'ch-1', title: '第一章 极限' },
    children: [],
    exercises: [{ id: 'ex-9', prompt_md: '求函数极限', tier: 'none', review_status: 'draft', node_ids: ['k-2'] }],
    counts: { children: 0, descendants: 0, exercises: 1 }
  },
  graph: {
    totals: { nodes: 3, verified: 0 },
    chapters: [{ chapter_id: 'ch-1', title: '第一章 极限', nodes: 2, verified: 0 }],
    nodes: [
      { id: 'k-1', chapter_id: 'ch-1', parent_id: 'ch-1', title: '数列极限',
        kind: 'knowledge', level: 3, verified: false, exercise_count: 0 },
      { id: 'k-2', chapter_id: 'ch-1', parent_id: 'ch-1', title: '函数极限',
        kind: 'knowledge', level: 3, verified: false, exercise_count: 1 }
    ]
  }
});
html = App.views.review();
// 2026-10-22 分工调整：状态里 nodePanel 还开着，但审核页不该再渲染编辑面板——
// 改标题/挂题/移动/删除都在图谱页。这四条断言就是这条分工的看门人。
check('审核页不再渲染编辑面板（即使状态里开着）',
  !html.includes('data-input="node-title"') && !html.includes('保存标题'));
check('审核页没有挂题/解绑入口',
  !html.includes('data-action="node-unmount"') && !html.includes('data-action="node-mount-search"'));
check('审核页没有删除入口', !html.includes('data-action="node-delete"'));
check('审核页没有「管理」按钮', !html.includes('data-action="node-manage"'));
check('审核页说明「只审不改」并指向图谱页',
  html.includes('只审不改') || html.includes('在「知识图谱」页做'));

(async () => {
  // 逐节点通过：只传这一个节点的 id，不要顺手把整章标掉
  reset({
    tab: 'graph',
    status: 'ready',
    filters: { course_id: 'analysis-ii', category: '', priority: '' }
  });
  const graphCalls = [];
  App.api.verifyGraph = async payload => { graphCalls.push(payload); return { changed: 1 }; };
  App.render = () => {};
  App.toast = message => graphCalls.push({ toast: message });
  await App.actions['graph-node-verify']({
    dataset: { course: 'analysis-ii', node: 'k-2', verb: 'verify' }, disabled: false
  });
  await App.actions['graph-root-verify']({
    dataset: { course: 'analysis-ii', node: 'analysis-ii', verb: 'verify' }, disabled: false
  });
  const rootCall = graphCalls.find(call => call.node_ids && call.node_ids[0] === 'analysis-ii');
  check('整门课通过提交的是课程根节点',
    rootCall && rootCall.node_ids.length === 1 && !rootCall.chapter_id,
    JSON.stringify(rootCall || null));

  const nodeCall = graphCalls.find(call => call.node_ids);
  check('单节点通过只提交这个节点',
    nodeCall && nodeCall.node_ids.length === 1 && nodeCall.node_ids[0] === 'k-2'
    && !nodeCall.chapter_id, JSON.stringify(nodeCall));
  check('单节点通过后提示了结果',
    graphCalls.some(call => String(call.toast || '').includes('已通过这个节点')));

  // ---------------------------------------------------------------------------
  // 决定之后的反馈：通过到底改了什么，得让人一眼看见
  // ---------------------------------------------------------------------------
  console.log('\n决定之后的反馈：');
  reset({
    items: [{
      task_id: 7, course_id: 'c1', exercise_id: 'ex-7', prompt_md: '题干', issues: [], node_ids: []
    }],
    open: 7
  });
  App.state.content = { content: { exercises: [{ id: 'ex-7', tier: 'none' }] } };
  App.state.review.verification = {
    exercise_id: 'ex-7', tier: 'none', proofs: [], lean_available: false
  };

  const renders = [];
  const toasts = [];
  App.render = () => { renders.push(1); };
  App.toast = message => toasts.push(message);
  App.api.get = async () => ({ items: [], counts: [], task_states: [] });
  App.api.visibility = async () => ({
    effective: { exercise_tier: 'all', qa_tier: 'all', graph_tier: 'all' },
    labels: {}, graph_labels: {}, scope: 'global'
  });
  App.api.post = async () => ({
    task_id: 7,
    state: 'approved',
    exercise: {
      course_id: 'c1', exercise_id: 'ex-7', review_status: 'teacher-reviewed',
      human_verified: true, tier: 'human', visible_to_students: false
    }
  });

  await App.actions['review-decide']({ dataset: { verb: 'approve', task: '7' } });
  const approveToast = toasts[toasts.length - 1] || '';
  check('通过后提示写明已标为人工已审', approveToast.includes('人工已审'), approveToast);
  check('通过后提示写明学生仍看不到', approveToast.includes('学生仍看不到'), approveToast);
  check('通过后会重渲染（面板跟着更新）', renders.length > 0, String(renders.length));
  check('通过后作废课程内容页缓存（否则切过去还是旧等级）',
    App.state.content.content === null);
  check('通过后查看中的审核记录也被清掉', App.state.review.verification === null);

  App.state.content = { content: { exercises: [] } };
  App.api.post = async () => ({
    task_id: 7,
    state: 'changes-requested',
    exercise: {
      course_id: 'c1', exercise_id: 'ex-7', review_status: 'draft',
      human_verified: false, tier: 'none', visible_to_students: false
    }
  });
  await App.actions['review-decide']({ dataset: { verb: 'request-changes', task: '7' } });
  const rejectToast = toasts[toasts.length - 1] || '';
  check('退回后提示说明撤销了人工审核', rejectToast.includes('撤销'), rejectToast);

  App.api.post = async () => ({
    task_id: 7,
    state: 'published',
    exercise: {
      course_id: 'c1', exercise_id: 'ex-7', review_status: 'published',
      human_verified: true, tier: 'human', visible_to_students: true
    }
  });
  await App.actions['review-decide']({ dataset: { verb: 'publish', task: '7' } });
  const publishToast = toasts[toasts.length - 1] || '';
  check('发布后提示说明学生可见', publishToast.includes('可见'), publishToast);

  console.log();
  if (failures.length) {
    console.log(`失败 ${failures.length} 项：${failures.join(', ')}`);
    process.exit(1);
  }
  console.log('审核页渲染测试全部通过。');
})();
