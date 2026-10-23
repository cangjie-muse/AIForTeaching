#!/usr/bin/env node
/**
 * 建设进度汇总页的渲染测试（node:vm，不启浏览器）。
 *
 * 盯的是"平台设置"页删掉之后，它那几块内容有没有真的落到位：
 *   1. 运行方式与服务接入要在建设进度汇总里，且不再出现在学生能看的任何页面；
 *   2. 诊断开关只给管理员；
 *   3. 学习记录导出留在「我的学习」，不跟着平台设置一起消失。
 *
 * 这些是"删页面"最容易顺手删掉的东西——页面没了，功能也没了，但没人发现，
 * 直到下一次要导出记录。
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

const store = new Map();
const sandbox = {
  console: { log: console.log, warn() {}, info() {}, error() {} },
  setTimeout, clearTimeout,
  setInterval: () => 1, clearInterval: () => {},
  URLSearchParams,
  fetch: () => Promise.reject(new Error('建设页测试不发真实请求')),
  prompt: () => '', confirm: () => true,
  location: { hash: '#/construction', href: '#/construction' },
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

const course = (id, title) => ({
  id, title, status: 'ready', subject: '专业课程', categoryLabel: '基础课程',
  direction: '分析', description: '简介', audience: '本科',
  stats: { sections: 10, knowledge: 90, exercises: 349, associations: 300 },
  nodes: [{ id, title, level: 1, path: [title], children: [] }],
  exercises: [], relationships: [], files: [], warnings: [], attachments: []
});
sandbox.window.COURSE_DATA = [course('analysis-ii', '数学分析II')];
vm.createContext(sandbox);

const files = ['js/course-catalog.js', 'js/course-cover-art.js', 'js/core.js',
  'js/visual-identity.js', 'js/storage.js', 'js/api-client.js', 'js/auth.js',
  'js/construction-model.js', 'js/construction-view.js', 'js/pages.js', 'js/workspace.js', 'js/app.js'];
vm.runInContext(files.map(read).join('\n;\n'), sandbox, { filename: 'construction.js' });

const App = sandbox.window.App;
App.dataSource = 'offline';
App.state.auth = { status: 'authenticated', actor: { id: 'admin', role: 'admin' } };

const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};

console.log('运行方式与服务接入：');
const html = App.views.construction();
check('页面标题是建设进度汇总', html.includes('建设进度汇总'));
check('有运行方式与服务接入一块', html.includes('运行方式与服务接入'));
check('写明当前是离线演示数据（后端没连上时）', html.includes('离线演示数据'));
check('模型问答标为已接入', /AI 生成式问答[\s\S]{0,120}已接入/.test(html), '');
check('作业与考试标为待接入', /作业与考试[\s\S]{0,80}待接入/.test(html));
check('讨论与通知标为待接入', /讨论与通知[\s\S]{0,80}待接入/.test(html));
check('保留「查看接口说明」', html.includes('api-info'));
check('保留「代码与资料来源」', html.includes('credits'));
check('管理员能看到诊断开关', html.includes('data-input="debug-setting"'));

apeStudent();
function apeStudent() {
  App.state.auth = { status: 'authenticated', actor: { id: 'stu', role: 'student' } };
  const studentHtml = App.views.construction();
  check('学生看不到诊断开关', !studentHtml.includes('data-input="debug-setting"'));
}

console.log('\n平台设置页确实没了：');
check('没有 settings 视图', !App.views.settings);
check('导航里没有平台设置', !App.sidebar || !App.sidebar().includes('平台设置'));
check('「我的学习」仍能导出记录', App.views.study().includes('export-records'));

console.log('');
if (failures.length) {
  console.log(`失败 ${failures.length} 项：${failures.join('、')}`);
  process.exit(1);
}
console.log('建设进度页与设置迁移测试全部通过。');
