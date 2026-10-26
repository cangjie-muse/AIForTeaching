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

console.log('未发布的题：说清"在库里但没发布"');
const withDrafts = { ...course, exercises: undefined };
App.course = withDrafts;
App.courseRows = [{ id: course.id, exercise_count: 9090 }];
const draftHtml = App.views.exercises();
check('报出库里的题数', draftHtml.includes('9090 道题'), '');
check('说清是卡在发布闸', draftHtml.includes('一道都还没发布'), '');
check('给出去审核的入口', draftHtml.includes('去内容审核'), '');
App.courseRows = [];
App.course = course;
App.course = { ...withDrafts, id: 'course-without-exercises' };
const emptyHtml = App.views.exercises();
check('库里也一道没有时走原来的空状态', emptyHtml.includes('题库等待接入'),
  emptyHtml.slice(0, 120));
App.course = course;
App.courseRows = [];

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

  console.log('\n换课不丢筛选与页码：');
  const wide = {
    id: 'wide', title: '宽课', status: 'ready',
    stats: { exercises: 20, associations: 20 },
    nodes: [{ id: 'w-ch', title: '第一章', level: 2, chapterId: 'w-ch', path: ['宽课', '第一章'], children: [], exerciseIds: [] }],
    exercises: Array.from({ length: 20 }, (_, index) => ({
      id: `w-${index}`, prompt: `第 ${index} 题`, answer: `答案 ${index}`, difficulty: 1,
      source: '自拟', nodeIds: ['w-ch']
    }))
  };
  App.course = wide;
  App.route = { page: 'exercises', params: new URLSearchParams() };
  App.state = {};
  App.views.exercises();
  element('exercise-search').value = '第 1';
  element('exercise-chapter').value = '';
  element('exercise-difficulty').value = '1';
  App.actions['exercise-page'] = App.actions['exercise-page'];
  App.inputs['exercise-filter']();
  check('筛选写进这门课的记忆', App.courseMemory('wide').exerciseDifficulty === '1', String(App.courseMemory('wide').exerciseDifficulty));
  App.state.exercisePage = 2;
  App.courseMemory().exercisePage = 2;
  const wideHtml = App.views.exercises();
  check('换回来时页码仍在', App.state.exercisePage === 2, String(App.state.exercisePage));
  check('换回来时搜索词回填', wideHtml.includes('value="第 1"'), '');
  check('换回来时难度回填', wideHtml.includes('value="1" selected'), '');
  check('另一门课的记忆不受影响', App.courseMemory('demo').exerciseDifficulty === undefined, '');

  console.log('\n三维标签：难度档 / 来源 / 可信度');
  // 老数据没有 tags 时不显示徽标、不出现筛选——不能因为升级把老课程弄成空白页。
  const legacy = {
    id: 'legacy', title: '老课', status: 'ready', stats: { exercises: 1, associations: 1 },
    nodes: [{ id: 'l-ch', title: '第一章', level: 2, chapterId: 'l-ch', path: ['老课'], children: [], exerciseIds: [] }],
    exercises: [{ id: 'l-1', prompt: '老题', answer: '老答案', difficulty: 3, source: '自拟', nodeIds: ['l-ch'] }]
  };
  App.course = legacy;
  App.route = { page: 'exercises', params: new URLSearchParams() };
  App.state = {};
  let legacyHtml = App.views.exercises();
  check('没有标签数据时不渲染徽标', !legacyHtml.includes('exercise-tags'), '');
  check('没有标签数据时不出现来源筛选', !legacyHtml.includes('exercise-origin'), '');
  check('没有标签数据时不出现可信度筛选', !legacyHtml.includes('exercise-trust'), '');

  const tagged = {
    id: 'tagged', title: '标签课', status: 'ready', stats: { exercises: 3, associations: 3 },
    nodes: [{ id: 't-ch', title: '第一章', level: 2, chapterId: 't-ch', path: ['标签课'], children: [], exerciseIds: [] }],
    exercises: [
      { id: 't-1', prompt: '教材题', answer: '答案一', difficulty: 2, source: '教材', nodeIds: ['t-ch'],
        tags: { labels: ['基础', '教材原题', '人工已核'], codes: { difficulty: 'basic', origin: 'textbook', trust: 'human' }, text: '基础 · 教材原题 · 人工已核' } },
      { id: 't-2', prompt: '自拟题', answer: '答案二', difficulty: 5, source: '自拟', nodeIds: ['t-ch'],
        tags: { labels: ['挑战', '平台自拟', '未核'], codes: { difficulty: 'challenge', origin: 'self-authored', trust: 'none' }, text: '挑战 · 平台自拟 · 未核' } },
      { id: 't-3', prompt: '导出题', answer: '答案三', difficulty: 3, source: '导出', nodeIds: ['t-ch'],
        tags: { labels: ['中等', '同事导出', '未核'], codes: { difficulty: 'medium', origin: 'colleague-export', trust: 'none' }, text: '中等 · 同事导出 · 未核' } }
    ]
  };
  App.course = tagged;
  App.state = {};
  // 下拉框在视图函数返回的 string 里，卡片在 render() 写进的结果区里：两处都要看。
  const taggedView = App.views.exercises();
  element('exercise-search').value = '';
  element('exercise-difficulty').value = '';
  element('exercise-origin').value = '';
  element('exercise-trust').value = '';
  let taggedHtml = render();
  check('卡上显示三枚组合标签', taggedHtml.includes('基础') && taggedHtml.includes('教材原题') && taggedHtml.includes('人工已核'), '');
  check('三枚标签用不同色系区分', taggedHtml.includes('ex-tag-band') && taggedHtml.includes('ex-tag-origin') && taggedHtml.includes('ex-tag-trust'), '');
  check('来源下拉按出现次数列出', /教材原题（1）/.test(taggedView) && /平台自拟（1）/.test(taggedView), '');
  check('可信度下拉带计数', /未核（2）/.test(taggedView), '');

  element('exercise-origin').value = 'textbook';
  let filtered = render();
  check('按来源筛选只剩教材原题', filtered.includes('教材题') && !filtered.includes('自拟题'), '');
  check('筛选结果数正确', element('exercise-count').textContent.includes('1 道'), element('exercise-count').textContent);

  element('exercise-origin').value = '';
  element('exercise-trust').value = 'none';
  filtered = render();
  check('按可信度筛选排除已核的题', !filtered.includes('教材题') && filtered.includes('自拟题'), '');
  check('未核题数量为 2', element('exercise-count').textContent.includes('2 道'), element('exercise-count').textContent);

  element('exercise-trust').value = '';
  element('exercise-search').value = '人工已核';
  filtered = render();
  check('标签文字能当搜索词', filtered.includes('教材题') && !filtered.includes('自拟题'), '');

  console.log('');
  if (failures.length) {
    console.log(`失败 ${failures.length} 项：${failures.join(', ')}`);
    process.exit(1);
  }
  console.log('习题页分层逻辑测试全部通过。');
})();
