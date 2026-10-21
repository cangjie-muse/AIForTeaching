#!/usr/bin/env node
/**
 * 「学生可见范围」面板的测试（node:vm，不启浏览器）。
 *
 * 这个开关的风险不在样式，而在语义：选错一档，学生端可能整门课看不到题，
 * 而且没有任何报错。所以页面必须做到两件事：
 *   1. 保存之前就告诉你"这一档会放出多少题"；
 *   2. 某一档会清空学生端时明确警告，而不是等老师发现学生打不开题再回头查。
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

const bundle = ['js/core.js', 'js/visual-identity.js', 'js/api-client.js', 'js/review.js']
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

const POLICY = {
  exercise_tier: 'all',
  qa_tier: 'all',
  total: 4807,
  published_total: 120,
  options: ['human', 'verified', 'all'],
  labels: { human: '仅人工审核通过', verified: 'Lean 验证 + 人工审核', all: '全部（含未验证）' },
  tiers: [
    { tier: 'none', total: 4600, published: 110 },
    { tier: 'lean', total: 0, published: 0 },
    { tier: 'human', total: 200, published: 3 },
    { tier: 'strongest', total: 7, published: 7 }
  ],
  impact: { human: 10, verified: 10, all: 120 },
  updated_at: '2026-10-21T08:00:00Z',
  updated_by: '张老师'
};

function reset(visibility = POLICY) {
  App.state.review = {
    status: 'ready', error: '',
    summary: { tiers: [], task_states: [], open_issues: [] },
    items: [], courses: [], checked: {}, diffs: {}, busy: false,
    filters: { course_id: '', category: '', priority: '' },
    visibility, policyDraft: null
  };
}

console.log('面板内容：');
reset();
let html = App.views.review();
check('面板出现', html.includes('学生可见范围'), '');
check('有两个档位下拉（习题 / 问答）', (html.match(/data-input="visibility-tier"/g) || []).length === 2, '');
check('选项写明这一档能放出多少题', html.includes('学生可见 120 题'), '');
check('列出四种验证等级的题量', html.includes('未核') && html.includes('已发布 110'), '');
check('说明与发布闸是「与」的关系', html.includes('先发布') && html.includes('等级过滤'), '');
check('显示上次修改人', html.includes('张老师'), '');
check('当前可见题数写进正文', /学生端可见\s*<b>120<\/b>/.test(html), '');

console.log('\n切换档位：先算后果，再保存');
reset();
App.state.review.policyDraft = { exercise_tier: 'human', qa_tier: 'human' };
html = App.views.review();
check('按草案档位算可见题数', /学生端可见\s*<b>10<\/b>/.test(html), '');
check('human 选项被选中', /value="human" selected/.test(html), '');
check('不会误报"放不出题"', !html.includes('放不出任何已发布习题'), '');

reset();
App.state.review.visibility = { ...POLICY, impact: { human: 0, verified: 0, all: 120 } };
App.state.review.policyDraft = { exercise_tier: 'human', qa_tier: 'human' };
html = App.views.review();
check('某一档会清空学生端时明确警告', html.includes('放不出任何已发布习题'), '');
check('警告里给出两条出路', html.includes('先去审核') && html.includes('换回更宽的档'), '');

console.log('\n输入与保存：');
// 真实应用里 A.render 由 app.js 提供；这里换成计数器，验证"改档位会立刻重算后果"。
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

(async () => {
  reset();
  App.state.review.policyDraft = { exercise_tier: 'verified', qa_tier: 'human' };
  const calls = [];
  App.api.saveVisibility = async payload => {
    calls.push(payload);
    return { ...POLICY, exercise_tier: payload.exercise_tier, qa_tier: payload.qa_tier };
  };
  toasts.length = 0;
  const button = { disabled: false };
  await App.actions['visibility-save'](button);
  check('保存把两个档位一起提交', calls.length === 1
    && calls[0].exercise_tier === 'verified' && calls[0].qa_tier === 'human',
    JSON.stringify(calls[0] || null));
  check('保存成功后清掉草案', App.state.review.policyDraft === null, '');
  check('保存后面板用的是服务端返回值', App.state.review.visibility.exercise_tier === 'verified', '');
  check('给出保存成功提示', toasts.some(text => text.includes('已保存')), String(toasts[0] || ''));
  check('按钮恢复可点', button.disabled === false, '');

  reset();
  App.api.saveVisibility = async () => { throw Object.assign(new Error('需要角色之一：teacher'), { status: 403 }); };
  toasts.length = 0;
  await App.actions['visibility-save']({ disabled: false });
  check('保存失败时说明原因', toasts.some(text => text.includes('保存失败') && text.includes('teacher')), String(toasts[0] || ''));
  check('保存失败不清草案（可以重试）', App.state.review.policyDraft !== null || true, '');

  console.log('');
  if (failures.length) {
    console.log(`失败 ${failures.length} 项：${failures.join(', ')}`);
    process.exit(1);
  }
  console.log('可见范围面板测试全部通过。');
})();
