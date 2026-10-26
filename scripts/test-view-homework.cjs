#!/usr/bin/env node
/**
 * 作业批改页（学生上传 / 教师复核）。
 *
 * 这一层测视图与交互，真实识别与判分在 server/tests/test_homework.py 里测。
 * 重点钉住：
 *   1. 分数必须分成「AI 初评」和「教师确认」两份显示——混成一份，学生会把初评当成绩；
 *   2. 每题都要露出"识别出的题目"和"你的作答"，否则学生无法判断是认错了还是真错了；
 *   3. 学生看不到改判入口，教师能看到，且改判提交的是 teacher_score 而不是覆盖 auto_score；
 *   4. 没选课程/没选照片时不发请求，只给一句人话。
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
      id, innerHTML: '', textContent: '', title: '', value: '', files: [],
      classList: { add() {}, remove() {}, toggle() {} },
      close() {}, showModal() {}, focus() {}, addEventListener() {}
    });
  }
  return elements.get(id);
}

const store = new Map();
const toasts = [];
const sandbox = {
  console: { log: console.log, warn() {}, info() {}, error() {} },
  setTimeout, clearTimeout,
  setInterval: () => 1, clearInterval: () => {},
  URLSearchParams, URL: { createObjectURL: () => 'blob:fake' }, Date,
  fetch: () => Promise.reject(new Error('作业页测试不发真实请求')),
  prompt: () => '', confirm: () => true,
  open: () => {},
  location: { hash: '#/homework', href: '#/homework' },
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
sandbox.window.COURSE_DATA = [{
  id: 'analysis-ii', title: '数学分析II', status: 'ready', subject: '专业课程',
  categoryLabel: '基础课程', direction: '分析', description: '',
  audience: '本科', stats: { sections: 10, knowledge: 90, exercises: 349 },
  nodes: [], exercises: [], relationships: [], files: [], warnings: [], attachments: []
}];
vm.createContext(sandbox);

const files = ['js/course-catalog.js', 'js/course-cover-art.js', 'js/core.js',
  'js/visual-identity.js', 'js/storage.js', 'js/api-client.js', 'js/auth.js',
  'js/pages.js', 'js/homework.js', 'js/app.js'];
vm.runInContext(files.map(read).join('\n;\n'), sandbox, { filename: 'homework.js' });

const App = sandbox.window.App;
const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

const listFixture = {
  items: [{ id: 'sub-1', title: '第三章习题', status: 'ai-graded', page_count: 2,
    student_id: 'acc-s', submitted_at: '2026-10-26T09:10:00' }],
  is_teacher: false
};
const detailFixture = () => ({
  submission: { id: 'sub-1', title: '第三章习题', status: 'ai-graded', page_count: 2,
    student_id: 'acc-s', error: null },
  pages: [{ page_no: 1, bytes: 900, mime: 'image/jpeg', transcribed: true },
    { page_no: 2, bytes: 800, mime: 'image/jpeg', transcribed: true }],
  items: [
    { ord: 1, page_no: 1, question_no: '1', question: '求 $x^2$ 的导数',
      student_answer: '$2x$', exercise_id: 'ex-1', verdict: 'correct', score: 10,
      full_score: 10, comment: '思路与结论均正确', confidence: 0.95, needs_review: false,
      teacher_score: null, teacher_comment: null },
    { ord: 2, page_no: 1, question_no: '2', question: '求 $\\sin x$ 的积分',
      student_answer: '$-\\cos x$', exercise_id: null, verdict: 'partial', score: 6,
      full_score: 10, comment: '漏了积分常数 $+C$', confidence: 0.42, needs_review: true,
      teacher_score: null, teacher_comment: null }
  ],
  score: { auto: 80, teacher: null, confirmed: false, needs_review: 1 }
});

const calls = [];
App.api.homeworkList = async params => { calls.push(['list', params]); return listFixture; };
App.api.homeworkGet = async id => { calls.push(['get', id]); return detailFixture(); };
App.api.homeworkCreate = async (courseId, title) => {
  calls.push(['create', courseId, title]);
  return { id: 'sub-new', status: 'submitted' };
};
App.api.uploadHomeworkPage = async (id, pageNo, blob) => {
  calls.push(['page', id, pageNo, blob && blob.name]);
  return { page_no: pageNo, page_count: pageNo };
};
App.api.homeworkGrade = async id => { calls.push(['grade', id]); return { job: { id: 9 } }; };
App.api.homeworkReview = async (id, ord, patch) => { calls.push(['review', id, ord, patch]); return {}; };
App.api.homeworkConfirm = async id => { calls.push(['confirm', id]); return {}; };
App.toast = message => { toasts.push(message); };

(async () => {
  await wait(0);
  console.log('学生视角：');
  App.state.auth = { status: 'ready', actor: { id: 'acc-s', role: 'student', display_name: '李同学' } };
  App.state.homework = null;
  let html = App.views.homework();
  check('有上传区', html.includes('上传作业照片'), '');
  check('说清一页一张、不会替学生改答案', html.includes('一页一张') && html.includes('不会替你改答案'), '');

  // 空状态下点提交：不发请求，只提示
  calls.length = 0; toasts.length = 0;
  await App.actions['homework-submit']({ disabled: false });
  // 页面的 A.after 会自动拉一次列表，所以这里只断言"没有发出提交类请求"
  const submitCalls = () => calls.filter(call => ['create', 'page', 'grade'].includes(call[0]));
  check('没选课程时不发提交请求', submitCalls().length === 0, JSON.stringify(submitCalls()));
  check('提示先选课程', toasts[0] && toasts[0].includes('课程'), String(toasts[0]));

  App.actions['homework-course']({ value: 'analysis-ii' });
  toasts.length = 0;
  await App.actions['homework-submit']({ disabled: false });
  check('选了课程但没照片也不发提交请求', submitCalls().length === 0, JSON.stringify(submitCalls()));
  check('提示先选照片', toasts[0] && toasts[0].includes('照片'), String(toasts[0]));

  console.log('\n上传流程：');
  App.actions['homework-files']({ files: [{ name: 'p1.jpg' }, { name: 'p2.jpg' }] });
  check('页面显示已选页数', App.views.homework().includes('已选 2 页'), '');
  App.inputs['homework-title']({ value: '第三章习题' });
  calls.length = 0;
  await App.actions['homework-submit']({ disabled: false });
  const order = calls.map(call => call[0]);
  check('先建提交再上传', order[0] === 'create', order.join(','));
  check('创建时带上课程与标题', calls[0][1] === 'analysis-ii' && calls[0][2] === '第三章习题',
    JSON.stringify(calls[0]));
  check('两张图按 1、2 页上传', calls.filter(c => c[0] === 'page').map(c => c[2]).join(',') === '1,2',
    JSON.stringify(calls.filter(c => c[0] === 'page').map(c => c[2])));
  check('上传完才排队批改', order.indexOf('grade') > order.lastIndexOf('page'), order.join(','));
  check('提交后打开详情', calls.some(c => c[0] === 'get' && c[1] === 'sub-new'), '');

  console.log('\n结果展示：');
  html = App.views.homework();
  check('显示 AI 初评分数', html.includes('AI 初评') && html.includes('80'), '');
  check('显示教师确认分位', html.includes('教师确认分'), '');
  check('未确认时写明仅供自测', html.includes('尚未确认，仅供自测'), '');
  check('提示有几题需人工复核', html.includes('需人工复核'), '');
  check('每题露出识别出的题目', html.includes('求 $x^2$ 的导数'), '');
  check('每题露出学生作答', html.includes('$2x$'), '');
  check('显示判定与点评', html.includes('正确') && html.includes('漏了积分常数'), '');
  check('低置信度题标「识别存疑」', html.includes('识别存疑'), '');
  check('对上题库的题写明题号', html.includes('已对上题库题'), '');
  check('没对上的题说明依据不同', html.includes('题库里没有对应题'), '');
  check('有第二页的入口', html.includes('第 2 页'), '');

  console.log('\n学生看不到改判：');
  check('学生没有改判按钮', !html.includes('data-action="homework-review"'), '');
  check('学生没有全部确认按钮', !html.includes('data-action="homework-confirm"'), '');

  console.log('\n教师复核：');
  App.state.auth = { status: 'ready', actor: { id: 'acc-t', role: 'teacher', display_name: '张老师' } };
  App.state.homework.detail = detailFixture();
  App.state.homework.isTeacher = true;
  html = App.views.homework();
  check('教师看到改判按钮', html.includes('data-action="homework-review"'), '');
  check('教师看到全部确认', html.includes('data-action="homework-confirm"'), '');

  calls.length = 0;
  App.actions['homework-review']({ dataset: { id: 'sub-1', ord: '2', score: '6', comment: '' } });
  check('弹窗带出当前分数', element('dialog').innerHTML.includes('value="6"'), '');
  App.inputs['hw-review-score']({ value: '8' });
  App.inputs['hw-review-comment']({ value: '补上 +C' });
  await App.actions['homework-review-save']({ disabled: false });
  const review = calls.find(call => call[0] === 'review');
  check('改判写的是 teacher_score', review && review[3].teacher_score === 8, JSON.stringify(review && review[3]));
  check('改判带上评语', review && review[3].teacher_comment === '补上 +C', '');
  check('8 分对应部分正确', review && review[3].verdict === 'partial', String(review && review[3].verdict));

  toasts.length = 0;
  App.inputs['hw-review-score']({ value: '12' });
  calls.length = 0;
  await App.actions['homework-review-save']({ disabled: false });
  check('超过满分的分数被挡住', calls.length === 0 && toasts[0].includes('0 到 10'), String(toasts[0]));

  calls.length = 0;
  await App.actions['homework-confirm']({ dataset: { id: 'sub-1' }, disabled: false });
  check('确认调用了确认接口', calls.some(c => c[0] === 'confirm'), JSON.stringify(calls));

  console.log('\n失败与状态：');
  App.state.homework.detail = {
    submission: { id: 'sub-x', title: '出错的作业', status: 'failed', page_count: 1,
      error: 'RuntimeError: 模型 401', student_id: 'acc-s' },
    items: [], pages: [], score: { auto: null, teacher: null, confirmed: false, needs_review: 0 }
  };
  html = App.views.homework();
  check('失败状态显示原因', html.includes('批改失败') && html.includes('模型 401'), '');
  check('失败状态不显示 AI 分', !html.includes('AI 初评 0'), '');

  App.state.homework.detail = {
    submission: { id: 'sub-y', title: '批改中的作业', status: 'grading', page_count: 1,
      error: null, student_id: 'acc-s' },
    items: [], pages: [], score: { auto: null, teacher: null, confirmed: false, needs_review: 0 }
  };
  html = App.views.homework();
  check('批改中给出进度说明', html.includes('正在批改'), '');
  check('批改中说明会自动刷新', html.includes('自动刷新'), '');

  console.log('\n入口与权限：');
  const shell = read('js/app.js');
  check('侧边栏有作业批改入口', shell.includes("navItem('homework'") && shell.includes("'作业批改'"), '');
  check('面包屑标题有作业批改', shell.includes("homework: '作业批改'"), '');
  const auth = read('js/auth.js');
  check('学生可以打开作业页', /homework:\s*\['student'/.test(auth), '');
  App.state.auth = { status: 'anonymous', actor: null };
  check('没登录打不开', App.auth.can('homework') === false, String(App.auth.can('homework')));

  console.log('');
  if (failures.length) {
    console.log(`失败 ${failures.length} 项：${failures.join(', ')}`);
    process.exit(1);
  }
  console.log('作业批改页测试全部通过。');
})();
