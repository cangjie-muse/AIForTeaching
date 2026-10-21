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

const bundle = ['js/core.js', 'js/visual-identity.js', 'js/api-client.js', 'js/review.js']
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

console.log();
if (failures.length) {
  console.log(`失败 ${failures.length} 项：${failures.join(', ')}`);
  process.exit(1);
}
console.log('审核页渲染测试全部通过。');
