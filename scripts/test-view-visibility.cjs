#!/usr/bin/env node
/**
 * 「学生可见范围」页（从内容审核搬到系统管理之后）。
 *
 * 这个开关的风险不在样式，而在语义：选错一档，学生端可能整门课看不到题，
 * 而且没有任何报错。所以测的是几条不能破的规则：
 *   1. 学生打不开，教师与管理员能打开；
 *   2. 保存之前就告诉你"这一档会放出多少题/多少节点"；
 *   3. 某一档会清空学生端时明确警告，而不是等老师发现学生打不开题再回头查；
 *   4. 分清"全局口径"与"这门课单独设置"，换作用范围要丢掉未保存的改动。
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');

const elements = new Map();
function element(id) {
  if (!elements.has(id)) {
    elements.set(id, {
      id, innerHTML: '', textContent: '', title: '',
      classList: { add() {}, remove() {}, toggle() {} },
      close() {}, showModal() {}, focus() {}, addEventListener() {}
    });
  }
  return elements.get(id);
}

const calls = [];
const sandbox = {
  console: { log: console.log, warn() {}, info() {}, error() {} },
  setTimeout, clearTimeout, setInterval: () => 1, clearInterval: () => {},
  URLSearchParams, URL, Date,
  fetch: () => Promise.reject(new Error('测试中不发真实请求')),
  prompt: () => '', confirm: () => true,
  location: { hash: '#/visibility', href: '#/visibility' },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  document: {
    getElementById: element,
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {},
    createElement: () => element('tmp'),
    title: '',
    body: { classList: { add() {}, remove() {}, toggle() {} } }
  }
};
sandbox.window = sandbox;
sandbox.window.addEventListener = () => {};
sandbox.window.COURSE_DATA = [{
  id: 'analysis-ii', title: '数学分析II', status: 'ready', subject: '专业课程',
  categoryLabel: '基础课程', direction: '分析', description: '',
  audience: '本科', stats: { sections: 10, knowledge: 90, exercises: 349 },
  nodes: [], exercises: [], relationships: [], files: [], warnings: [], attachments: []
}];
vm.createContext(sandbox);

const files = ['js/course-catalog.js', 'js/course-cover-art.js', 'js/core.js',
  'js/visual-identity.js', 'js/storage.js', 'js/api-client.js', 'js/auth.js',
  'js/pages.js', 'js/catalog-view.js', 'js/visibility.js', 'js/app.js'];
vm.runInContext(files.map(read).join('\n;\n'), sandbox, { filename: 'visibility.js' });

const App = sandbox.window.App;
const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

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
      { id: 'analysis-ii', title: '数学分析II', has_override: true, exercise_tier: 'human' },
      { id: 'pilot', title: '试点课', has_override: false, exercise_tier: null }
    ],
    updated_at: '2026-10-21T08:00:00Z',
    updated_by: '张老师',
    ...overrides
  };
}

function reset({ scope = '', draft = null, status = 'ready', error = '', data = policy() } = {}) {
  App.state.visibility = {
    status, error, policy: status === 'ready' ? data : null,
    scope, draft, saving: false
  };
}

App.api.visibility = async scope => {
  calls.push(['visibility', scope]);
  if (scope === 'analysis-ii') {
    return policy({ scope: 'course', course_id: 'analysis-ii', source: 'global' });
  }
  return policy();
};
App.api.saveVisibility = async (body, scope) => {
  calls.push(['save', scope, body]);
  return { ok: true };
};
App.api.resetVisibility = async scope => { calls.push(['reset', scope]); return { ok: true }; };
App.toast = message => calls.push(['toast', message]);

(async () => {
  console.log('权限与菜单：');
  App.state.auth = { actor: { id: 'acc-s', role: 'student', display_name: '李同学' } };
  check('学生打不开可见范围页', App.auth.can('visibility') === false);
  sandbox.location.hash = '#/visibility';
  App.render();
  await wait(30);
  let html = elements.get('app').innerHTML;
  check('学生直接开页面只看到"没有权限"', html.includes('当前身份没有这个页面的权限'));
  check('学生页面上没有可见范围开关', !html.includes('data-input="visibility-tier"'));

  App.state.auth = { actor: { id: 'acc-t', role: 'teacher', display_name: '张老师' } };
  check('教师能打开可见范围页', App.auth.can('visibility') === true);
  App.state.auth = { actor: { id: 'acc-a', role: 'admin', display_name: '管理员' } };
  check('管理员能打开可见范围页', App.auth.can('visibility') === true);

  App.state.auth = { actor: { id: 'acc-t', role: 'teacher', display_name: '张老师' } };
  App.state.visibility = { status: 'idle', error: '', policy: null, scope: '', draft: null, saving: false };
  App.render();
  await wait(60);
  html = elements.get('app').innerHTML;
  const nav = html.slice(0, html.indexOf('<main'));
  check('菜单里有「学生可见范围」', nav.includes('学生可见范围'));
  check('它挂在系统管理这一组下',
    nav.indexOf('学生可见范围') > nav.indexOf('系统管理'), '应该和账号管理同组');
  sandbox.location.hash = '#/review';
  App.render();
  await wait(30);
  html = elements.get('app').innerHTML;
  check('内容审核页里不再有可见范围开关', !html.includes('data-input="visibility-tier"'),
    '开关应该只在系统设置里');

  console.log('\n渲染：');
  reset();
  html = App.views.visibility();
  check('有作用范围选择器', html.includes('data-input="visibility-scope"'));
  check('三个档位下拉（习题 / 问答 / 图谱）',
    (html.match(/data-input="visibility-tier"/g) || []).length === 3);
  check('选项写明这一档能放出多少题', html.includes('学生可见 120 题'));
  check('图谱选项写明能放出多少节点', html.includes('学生可见 320 节点'));
  check('列出各验证等级的题量', html.includes('已发布 110'));
  check('显示全局口径摘要', html.includes('全局默认：'));
  check('说明与发布闸是「与」的关系', html.includes('先发布') && html.includes('等级过滤'));
  check('显示上次修改人', html.includes('张老师'));
  check('当前可见数写进正文', /学生端可见\s*<b>120<\/b>\s*道已发布习题/.test(html));
  check('全局时不显示「取消本课设置」', !html.includes('data-action="visibility-reset"'));

  console.log('\n课程覆盖：');
  reset({
    scope: 'analysis-ii',
    data: policy({
      scope: 'course', course_id: 'analysis-ii', source: 'course',
      effective: { exercise_tier: 'human', qa_tier: 'human', graph_tier: 'human' },
      override: { exercise_tier: 'human', qa_tier: 'human', graph_tier: 'human', updated_by: '李老师' }
    })
  });
  html = App.views.visibility();
  check('显示「这门课单独设置」', html.includes('这门课单独设置'));
  check('同时给出全局口径作对照', html.includes('全局是 习题'));
  check('有取消本课设置的按钮', html.includes('data-action="visibility-reset"'));
  check('作用范围里选中了这门课', /value="analysis-ii" selected/.test(html));

  reset({ scope: 'pilot', data: policy({ scope: 'course', course_id: 'pilot', source: 'global' }) });
  html = App.views.visibility();
  check('明说跟随全局', html.includes('这门课跟随全局'));
  check('跟随全局时没有「取消本课设置」', !html.includes('data-action="visibility-reset"'));

  console.log('\n切换档位：先算后果，再保存');
  reset();
  App.state.visibility.draft = { exercise_tier: 'human', qa_tier: 'human', graph_tier: 'all' };
  html = App.views.visibility();
  check('按草案档位算可见题数', /学生端可见\s*<b>10<\/b>/.test(html));
  check('human 选项被选中', /value="human" selected/.test(html));
  check('不会误报"放不出题"', !html.includes('放不出任何已发布习题'));

  reset({ data: policy({ impact: { human: 0, verified: 0, all: 120 } }) });
  App.state.visibility.draft = { exercise_tier: 'human', qa_tier: 'human', graph_tier: 'all' };
  html = App.views.visibility();
  check('某一档会清空学生端时明确警告', html.includes('放不出任何已发布习题'));
  check('警告里给出两条出路', html.includes('先去审核') && html.includes('换回更宽的档'));

  reset({ data: policy({ graph: { total: 320, verified: 0, pending: 320, impact: { all: 320, human: 0 } } }) });
  App.state.visibility.draft = { exercise_tier: 'all', qa_tier: 'all', graph_tier: 'human' };
  html = App.views.visibility();
  check('图谱切到仅人工但没有节点通过时警告', html.includes('学生端会看不到知识图谱'));

  reset();
  App.state.visibility.draft = { exercise_tier: 'verified', qa_tier: 'all', graph_tier: 'all' };
  html = App.views.visibility();
  check('选 Lean 档时如实说明尚未接入', html.includes('Lean 验证尚未接入'));
  check('提示改动还没保存', html.includes('改动还没保存'));

  console.log('\n输入与保存：');
  let renders = 0;
  App.render = () => { renders += 1; };
  reset();
  App.inputs['visibility-tier']({ value: 'verified', dataset: { kind: 'exercise' } });
  check('改习题档位只改习题', App.state.visibility.draft.exercise_tier === 'verified'
    && App.state.visibility.draft.qa_tier === 'all',
    JSON.stringify(App.state.visibility.draft));
  check('改档位立刻重渲染（先算后果）', renders === 1, String(renders));
  App.inputs['visibility-tier']({ value: 'human', dataset: { kind: 'qa' } });
  check('改问答档位只改问答', App.state.visibility.draft.qa_tier === 'human'
    && App.state.visibility.draft.exercise_tier === 'verified',
    JSON.stringify(App.state.visibility.draft));
  App.inputs['visibility-tier']({ value: 'human', dataset: { kind: 'graph' } });
  check('改图谱档位只改图谱', App.state.visibility.draft.graph_tier === 'human'
    && App.state.visibility.draft.qa_tier === 'human',
    JSON.stringify(App.state.visibility.draft));

  reset();
  App.state.visibility.draft = { exercise_tier: 'verified', qa_tier: 'human', graph_tier: 'human' };
  calls.length = 0;
  await App.actions['visibility-save']({ disabled: false });
  const saved = calls.filter(call => call[0] === 'save').pop();
  check('保存把三档一起提交', saved && saved[2].exercise_tier === 'verified'
    && saved[2].qa_tier === 'human' && saved[2].graph_tier === 'human',
    JSON.stringify(saved && saved[2]));
  check('全局保存不带 course_id', saved && saved[1] === undefined, String(saved && saved[1]));
  check('保存成功后清掉草案', App.state.visibility.draft === null);
  check('提示说的是全局口径', calls.some(call => call[0] === 'toast'
    && String(call[1]).includes('全局口径')), String((calls.find(c => c[0] === 'toast') || [])[1] || ''));

  reset({ scope: 'analysis-ii' });
  App.state.visibility.draft = { exercise_tier: 'human', qa_tier: 'human', graph_tier: 'human' };
  calls.length = 0;
  await App.actions['visibility-save']({ disabled: false });
  const scoped = calls.filter(call => call[0] === 'save').pop();
  check('课程覆盖保存带上 course_id', scoped && scoped[1] === 'analysis-ii', String(scoped && scoped[1]));

  reset({
    scope: 'analysis-ii',
    data: policy({ scope: 'course', course_id: 'analysis-ii', source: 'course', override: { exercise_tier: 'human', qa_tier: 'human', graph_tier: 'human' } })
  });
  calls.length = 0;
  await App.actions['visibility-reset']({ disabled: false });
  check('取消本课设置带上 course_id',
    calls.some(call => call[0] === 'reset' && call[1] === 'analysis-ii'));
  check('取消后提示回到跟随全局',
    calls.some(call => call[0] === 'toast' && String(call[1]).includes('跟随全局')));

  console.log('\n换作用范围：');
  reset();
  App.state.visibility.draft = { exercise_tier: 'human', qa_tier: 'all', graph_tier: 'all' };
  calls.length = 0;
  App.inputs['visibility-scope']({ value: 'analysis-ii' });
  check('换作用范围丢掉未保存的改动', App.state.visibility.draft === null);
  check('换作用范围重新读这门课的口径',
    calls.some(call => call[0] === 'visibility' && call[1] === 'analysis-ii'),
    JSON.stringify(calls));
  await wait(40);
  check('换过去后页面说的是这门课', App.views.visibility().includes('这门课跟随全局'));
  check('切过去后重新读了设置', App.state.visibility.policy?.course_id === 'analysis-ii',
    JSON.stringify(App.state.visibility.policy?.course_id));

  console.log('\n失败与权限：');
  reset();
  App.api.saveVisibility = async () => {
    throw Object.assign(new Error('需要角色之一：teacher'), { status: 403 });
  };
  calls.length = 0;
  await App.actions['visibility-save']({ disabled: false });
  check('保存失败时说明原因',
    calls.some(call => call[0] === 'toast' && String(call[1]).includes('保存失败')
      && String(call[1]).includes('teacher')),
    String((calls.find(c => c[0] === 'toast') || [])[1] || ''));

  reset({ status: 'denied' });
  html = App.views.visibility();
  check('后端拒绝时说明没有权限', html.includes('没有这个权限'));

  reset({ status: 'error', error: '无法连接后端' });
  html = App.views.visibility();
  check('读不到设置时给错误说明与重试', html.includes('读不到当前设置') && html.includes('无法连接后端'));

  console.log('');
  if (failures.length) {
    console.log(`失败 ${failures.length} 项：${failures.join('、')}`);
    process.exit(1);
  }
  console.log('学生可见范围页测试全部通过。');
})();
