#!/usr/bin/env node
/**
 * 习题页的分层逻辑测试（node:vm + 最小 DOM 桩）。
 *
 * 教学口径是「先引导，作答后解锁完整解答」。这一条不是文案，是行为：
 * 没作答时，完整解答必须真的看不到；作答（或明确点"我卡住了"）之后才出现。
 * 之前的实现把参考简解直接放进折叠面板，学生一进来就能点开，等于没有分层；
 * 而保存作答的提示又写着"完整解答已解锁"，说的和做的不一致。
 *
 * 同时盯住提交成功后的提示文案：后端返回的是 unlocks_full / attempt_no，
 * 不是 status。按 status 判断会走错分支，弹出"undefined；作答只保存在本机"。
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');

const SECRET = 'ANSWER-SECRET-不可提前看到';

const elements = new Map();
const element = id => {
  if (!elements.has(id)) {
    elements.set(id, {
      id, innerHTML: '', textContent: '', value: '', open: true,
      classList: { add() {}, remove() {}, toggle() { return false; } },
      close() {}, showModal() {}, focus() {}, addEventListener() {}
    });
  }
  return elements.get(id);
};

const store = new Map();
const toasts = [];
const confirms = [];
const sandbox = {
  console: { log: console.log, warn() {}, info() {}, error() {} },
  setTimeout, clearTimeout,
  setInterval: () => 1, clearInterval: () => {},
  URLSearchParams,
  fetch: () => Promise.reject(Object.assign(new Error('测试不请求后端'), { status: 0 })),
  confirm: message => { confirms.push(message); return true; },
  location: { hash: '#/course/demo/exercises', href: '#/course/demo/exercises' },
  localStorage: {
    getItem: key => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => store.set(key, String(value)),
    removeItem: key => store.delete(key)
  },
  document: {
    getElementById: element,
    querySelectorAll: () => [],
    querySelector: () => null,
    addEventListener() {},
    createElement: () => element('tmp'),
    title: '',
    body: { classList: { add() {}, remove() {}, toggle() {} } }
  }
};
sandbox.window = sandbox;
sandbox.window.addEventListener = () => {};
sandbox.window.COURSE_DATA = [];
vm.createContext(sandbox);

const files = ['js/course-catalog.js', 'js/core.js', 'js/visual-identity.js', 'js/learning.js'];
vm.runInContext(files.map(file => fs.readFileSync(path.join(root, file), 'utf8')).join('\n;\n'),
  sandbox, { filename: 'learning.js' });

const App = sandbox.window.App;
// 用文本标记代替真实 Markdown 渲染：这里验的是"能不能看到"，不是排版。
App.markdown = source => `MD(${source})`;
App.toast = message => toasts.push(message);

const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};

const course = {
  id: 'demo', title: '演示课', status: 'ready',
  stats: { exercises: 2, associations: 2 },
  nodes: [
    { id: 'ch-1', title: '第一章', level: 2, path: ['演示课', '第一章'], chapterId: 'ch-1', children: ['kp-1'] },
    { id: 'kp-1', title: '确界原理', level: 3, path: ['演示课', '第一章', '确界原理'], chapterId: 'ch-1', children: [], exerciseIds: ['ex-1', 'ex-2'] }
  ],
  exercises: [
    { id: 'ex-1', prompt: '叙述确界原理', answer: SECRET, difficulty: 2, source: '教材《数学分析》上册，书页12', nodeIds: ['kp-1'] },
    { id: 'ex-2', prompt: '举例说明', answer: '举例答案', difficulty: 1, source: '自拟', nodeIds: ['kp-1'] }
  ]
};
App.course = course;
App.route = { page: 'exercises', params: new URLSearchParams() };
App.state = { exercisePage: 1 };

function render() {
  element('exercise-count').textContent = '';
  element('exercise-results').innerHTML = '';
  element('exercise-pagination').innerHTML = '';
  App.after.exercises();
  return element('exercise-results').innerHTML;
}

console.log('未作答：完整解答必须看不到');
let html = render();
check('渲染出题目', html.includes('叙述确界原理'), '');
check('有引导层', html.includes('exercise-guide') && html.includes('先想清楚再动笔'), '');
check('引导里给出关联概念', html.includes('确界原理'), '');
check('引导里给出处', html.includes('书页12'), '');
check('完整解答被封住', html.includes('完整解答已锁定') && html.includes('solution-gate'), '');
check('答案正文没有泄露', !html.includes(SECRET), SECRET);
check('来源中的答案也不在页面上', !html.includes('ANSWER-SECRET'), '');
check('提供"我卡住了"的出口', html.includes('reveal-solution'), '');

console.log('\n保存作答后：自动解锁');
App.local[App.record('answer', 'ex-1')] = '我的思路：先证上界存在';
html = render();
check('作答已保存标记', html.includes('已保存'), '');
check('完整解答出现', html.includes(SECRET), '');
const lockCount = (html.match(/完整解答已锁定/g) || []).length;
check('已作答的那道不再显示锁定条（只剩未作答的那道）', lockCount === 1, `锁定条 ${lockCount} 处`);
check('未作答查看的徽标不出现', !html.includes('未作答查看'), '');
check('另一道未作答的题仍然锁着', html.includes('完整解答已锁定'), '');

console.log('\n点"我卡住了"：明确解锁并记账');
App.local = { debug: false };
App.save = () => true;
html = render();
check('清空本地记录后又锁上', html.includes('完整解答已锁定'), '');
App.actions['reveal-solution']({ dataset: { id: 'ex-1' } });
check('先问一句再解锁', confirms.length === 1 && confirms[0].includes('跳过思考'), String(confirms[0] || ''));
check('记下解锁时间', Boolean(App.local[App.record('revealed', 'ex-1')]), '');
html = render();
check('解锁后能看到答案', html.includes(SECRET), '');
check('标注为未作答查看', html.includes('未作答查看'), '');
check('提示说明记为未作答查看', toasts.some(text => text.includes('未作答查看')), '');

console.log('\n保存空作答：不解锁');
App.local = { debug: false };
App.actions['save-answer'] = App.actions['save-answer'];
element('answer-ex-2').value = '   ';
toasts.length = 0;
App.actions['save-answer']({ dataset: { id: 'ex-2' } });
check('空作答给出明确提示', toasts.some(text => text.includes('仍然锁定')), String(toasts[0] || ''));
check('空作答没有解锁', !App.local[App.record('revealed', 'ex-2')], '');

console.log('\n提交成功的提示文案：');
(async () => {
  App.local = { debug: false };
  element('answer-ex-1').value = '证明：上界存在';
  App.api = { submitAttempt: async () => ({ id: 7, attempt_no: 3, unlocks_full: true }) };
  toasts.length = 0;
  await App.actions['save-answer']({ dataset: { id: 'ex-1' } });
  const toast = toasts[0] || '';
  check('提示里有提交次数', toast.includes('第 3 次'), toast);
  check('提示里没有 undefined', !toast.includes('undefined'), toast);
  check('提示确认已解锁', toast.includes('已解锁'), toast);

  toasts.length = 0;
  App.api.submitAttempt = async () => { throw Object.assign(new Error('连不上'), { status: 0 }); };
  await App.actions['save-answer']({ dataset: { id: 'ex-1' } });
  const offline = toasts[0] || '';
  check('断网时仍解锁并说明只在本机', offline.includes('已在本机解锁') && !offline.includes('undefined'), offline);

  toasts.length = 0;
  App.api.submitAttempt = async () => { throw Object.assign(new Error('题目不存在'), { status: 404 }); };
  await App.actions['save-answer']({ dataset: { id: 'ex-1' } });
  const missing = toasts[0] || '';
  check('题目未入库时提示准确', missing.includes('还没导入后端题目库'), missing);

  console.log('\n后端已有作答：换设备也解锁');
  App.local = { debug: false };
  App.api = { get: async () => ({ items: [{ exercise_id: 'ex-2' }] }) };
  render();
  await new Promise(resolve => setTimeout(resolve, 0));
  check('记下服务端作答记录', Boolean(App.local[App.record('answered-server', 'ex-2')]), '');
  html = render();
  check('服务端记录也能解锁完整解答', html.includes('举例答案'), '');
  check('解锁不靠本地作答文本', !App.local[App.record('answer', 'ex-2')], '');

  console.log('');
  if (failures.length) {
    console.log(`失败 ${failures.length} 项：${failures.join(', ')}`);
    process.exit(1);
  }
  console.log('习题页分层逻辑测试全部通过。');
})();
