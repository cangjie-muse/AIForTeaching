#!/usr/bin/env node
/**
 * 课件工坊（按章/节生成 PPT、按意见改版）。
 *
 * 测视图与交互；真正的模型调用、pptx 渲染、资料上下文在
 * server/tests/test_courseware.py 与 scripts/test-courseware-render.py 里。
 *
 * 重点钉住：
 *   1. 生成请求带对 course_id/chapter_id/页数/侧重点——这几个字段错了，出来的
 *      就是别的章的内容，且肉眼看不出； 
 *   2. 页面要把 change_log（代码 diff 出来的）显示出来，而不是让教师相信模型自述；
 *   3. 待确认/已确认状态要分开，确认是显式动作；
 *   4. 学生看不到工坊。
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
      id, innerHTML: '', textContent: '', value: '', files: [],
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
  fetch: () => Promise.reject(new Error('课件工坊测试不发真实请求')),
  prompt: () => '', confirm: () => true, open: () => {},
  location: { hash: '#/courseware/analysis-ii', href: '#/courseware/analysis-ii' },
  localStorage: {
    getItem: key => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => store.set(key, String(value)),
    removeItem: key => store.delete(key)
  },
  document: {
    getElementById: element, querySelectorAll: () => [], querySelector: () => null,
    addEventListener() {}, createElement: () => element('tmp'), title: '',
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
sandbox.window.COURSEWARE_DATA = [];
vm.createContext(sandbox);

const files = ['js/course-catalog.js', 'js/course-cover-art.js', 'js/core.js',
  'js/visual-identity.js', 'js/storage.js', 'js/api-client.js', 'js/auth.js',
  'js/pages.js', 'js/queue-health.js', 'js/courseware-studio.js', 'js/courseware-view.js', 'js/app.js'];
vm.runInContext(files.map(read).join('\n;\n'), sandbox, { filename: 'courseware-studio.js' });

const App = sandbox.window.App;
const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

const chaptersFixture = () => ({
  chapters: [
    { id: 'analysis-ii-g013', title: '定积分', deck: { deck_id: 'cw-a', versions: 1, latest: 1 },
      sections: [
        { id: 'analysis-ii-g014', title: '定积分概念', body_length: 900, deck: null },
        { id: 'analysis-ii-g016', title: '微积分学基本公式', body_length: 400, deck: null }
      ] },
    { id: 'analysis-ii-g024', title: '定积分的应用', deck: null, sections: [] }
  ]
});

const versionFixture = () => ({
  id: 'cw-a-v1', number: 1, based_on: null, status: 'needs-review',
  title: '定积分 课件', instruction: '把概念讲细一点', slide_count: 3,
  model: 'deepseek-flash', error: null, created_at: '2026-10-26T09:40:00',
  change_log: [
    { kind: 'create', page: 1, title: '封面', summary: '初版第 1 页' },
    { kind: 'edit', page: 2, title: '定义', summary: '要点 +1' }
  ],
  source_changed: false,
  plan: { slides: [
    { title: '定积分', kind: 'cover', bookPages: 'p.112' },
    { title: '定积分的定义', paragraphs: ['曲边梯形面积'],
      bullets: ['分割', '求和', '取极限'],
      equations: [{ text: '∫_a^b f(x)dx', latex: '\\int_a^b f(x)\\,dx' }],
      speakerNote: '先问学生怎么算面积', sourceNodeIds: ['analysis-ii-g014'] },
    { title: '课堂练习', bullets: ['用定义计算 ∫_0^1 x² dx'], sourceNodeIds: [] }
  ] }
});

const calls = [];
App.api.coursewareChapters = async courseId => { calls.push(['chapters', courseId]); return chaptersFixture(); };
App.api.coursewareDeck = async (deckId, version) => {
  calls.push(['deck', deckId, version]);
  const selected = versionFixture();
  if (version) selected.id = version;
  return {
    deck: { id: deckId, course_id: 'analysis-ii', chapter_id: 'analysis-ii-g013',
      chapter_title: '定积分', audience: '本科二年级', source_changed: false },
    versions: [selected], selected, can_edit: true
  };
};
App.api.coursewareCreate = async body => { calls.push(['create', body]); return { deck_id: 'cw-a', version_id: 'cw-a-v2' }; };
App.api.coursewareRevise = async (deckId, body) => { calls.push(['revise', deckId, body]); return { deck_id: deckId, version_id: 'cw-a-v3' }; };
App.api.coursewareStatus = async (versionId, status) => { calls.push(['status', versionId, status]); return { version_id: versionId, status }; };
App.api.coursewarePptxUrl = versionId => `/api/v1/courseware/versions/${versionId}/pptx`;
App.toast = message => { toasts.push(message); };

(async () => {
  await wait(0);
  App.state.auth = { status: 'ready', actor: { id: 'acc-t', role: 'teacher', display_name: '张老师' } };
  App.course = sandbox.window.CATALOG_COURSES?.[0] || { id: 'analysis-ii', title: '数学分析II', nodes: [
    { id: 'analysis-ii-g013', title: '定积分', level: 2 },
    { id: 'analysis-ii-g014', title: '定积分概念', level: 3 }
  ] };
  App.route = { page: 'courseware', courseId: 'analysis-ii', params: new Map() };

  console.log('空目录：');
  App.api.coursewareChapters = async () => ({ chapters: [] });
  App.state.studio = null;
  await App.loadStudio();
  let html = App.coursewareStudio();
  check('没有章节时提示先导入资源包', html.includes('还没有章节'), html.slice(0, 80));

  console.log('\n生成初版：');
  App.api.coursewareChapters = async courseId => { calls.push(['chapters', courseId]); return chaptersFixture(); };
  App.state.studio = null;
  await App.loadStudio();
  html = App.coursewareStudio();
  check('列出章与节', html.includes('定积分概念') && html.includes('微积分学基本公式'), '');
  check('行业务上的生成按钮', html.includes('data-action="studio-generate"'), '');
  check('显示已有版本列表', html.includes('data-action="studio-open"'), '');

  calls.length = 0; toasts.length = 0;
  App.inputs['studio-pages']({ value: '999' });
  await App.actions['studio-generate']({ disabled: false });
  check('页数越界时不发请求', !calls.some(call => call[0] === 'create'), JSON.stringify(calls));
  check('提示页数范围', toasts.some(text => text.includes('6 到 40')), String(toasts[0]));

  App.inputs['studio-pages']({ value: '14' });
  App.inputs['studio-focus']({ value: '多给一个反例' });
  calls.length = 0;
  await App.actions['studio-generate']({ disabled: false });
  const created = calls.find(call => call[0] === 'create');
  check('生成用的是当前章', created && created[1].chapter_id === 'analysis-ii-g013',
    JSON.stringify(created && created[1]));
  check('下单带课程', created && created[1].course_id === 'analysis-ii', '');
  check('下单带页数与侧重点', created && created[1].pages === 14
    && created[1].focus === '多给一个反例', JSON.stringify(created && created[1]));

  console.log('\n结果展示：');
  await App.actions['studio-open']({ dataset: { id: 'cw-a-v1' } });
  html = App.coursewareStudio();
  check('显示版本号与页数', html.includes('V1') && html.includes('3 页'), '');
  check('待确认状态有标注', html.includes('待教师确认'), '');
  check('显示讲稿', html.includes('先问学生怎么算面积'), '');
  check('显示公式', html.includes('∫_a^b f(x)dx'), '');
  check('显示要点', html.includes('取极限'), '');
  check('显示来源节点', html.includes('analysis-ii-g014'), '');
  check('显示变更清单', html.includes('这一版改了什么（2 处）'), '');
  check('变更里写明页码与动作', html.includes('第2页') && html.includes('要点 +1'), '');
  check('上一版意见有回显', html.includes('把概念讲细一点'), '');
  check('提供 PPTX 下载', html.includes('/api/v1/courseware/versions/cw-a-v1/pptx'), '');

  console.log('\n按意见改一版：');
  calls.length = 0; toasts.length = 0;
  App.inputs['studio-instruction']({ value: '短' });
  await App.actions['studio-revise']({ disabled: false });
  check('意见太短不发请求', !calls.some(call => call[0] === 'revise'), JSON.stringify(calls));
  check('提示写具体意见', toasts.some(text => text.includes('具体的修改意见')), '');

  calls.length = 0;
  App.inputs['studio-instruction']({ value: '第 2 页的公式写错了，应该是 ω = dθ' });
  await App.actions['studio-revise']({ disabled: false });
  const revised = calls.find(call => call[0] === 'revise');
  check('修订带基础版本', revised && revised[2].base_version === 'cw-a-v1',
    JSON.stringify(revised && revised[2]));
  check('修订带意见原文', revised && revised[2].instruction.includes('ω = dθ'), '');

  console.log('\n确认与过期：');
  calls.length = 0;
  await App.actions['studio-confirm']({ dataset: { id: 'cw-a-v1' }, disabled: false });
  check('确认走确认接口', calls.some(call => call[0] === 'status' && call[2] === 'approved'),
    JSON.stringify(calls));

  const changed = versionFixture();
  changed.source_changed = true;
  App.api.coursewareDeck = async () => ({
    deck: { id: 'cw-a', chapter_title: '定积分' },
    versions: [changed], selected: changed, can_edit: true
  });
  await App.actions['studio-open']({ dataset: { id: 'cw-a-v1' } });
  check('资料变了提示可能过期', App.coursewareStudio().includes('可能已过期'), '');

  const failed = versionFixture();
  failed.status = 'failed';
  failed.error = 'RuntimeError: 模型 401';
  App.api.coursewareDeck = async () => ({
    deck: { id: 'cw-a', chapter_title: '定积分' },
    versions: [failed], selected: failed, can_edit: true
  });
  await App.actions['studio-open']({ dataset: { id: 'cw-a-v1' } });
  html = App.coursewareStudio();
  check('失败版本显示原因', html.includes('生成失败') && html.includes('模型 401'), '');
  check('失败版本不给修订框', !html.includes('data-action="studio-revise"'), '');

  console.log('\n权限与挂载：');
  App.state.auth = { status: 'ready', actor: { id: 'acc-s', role: 'student', display_name: '李同学' } };
  check('学生看不到工坊', App.coursewareStudio() === '', '');
  const view = read('js/courseware-view.js');
  check('课件页挂上了工坊', view.includes('A.coursewareStudio()'), '');
  const shell = read('index.html');
  check('页面加载了工坊脚本', shell.includes('js/courseware-studio.js'), '');

  console.log('');
  if (failures.length) {
    console.log(`失败 ${failures.length} 项：${failures.join(', ')}`);
    process.exit(1);
  }
  console.log('课件工坊测试全部通过。');
})();
