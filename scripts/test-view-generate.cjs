#!/usr/bin/env node
/**
 * 教材生成页的渲染冒烟测试（node:vm，不启浏览器）。
 *
 * 重点不是版式，而是几条会让老师卡住的行为：
 *   - 没选文件、没填教材名、起止页填反了，要给出具体原因，而不是"提交失败"
 *   - 任务失败时把后端原因原样显示（例如缺 PDF 解析引擎）
 *   - 生成成功后必须说清"进了待审"，不能让老师以为已经上线
 *   - 生成的题要能一键跳到审核队列
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');

const timers = [];
const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  setInterval: (fn, ms) => { timers.push(fn); return timers.length; },
  clearInterval: () => {},
  URLSearchParams,
  location: { hash: '#/generate' },
  fetch: () => Promise.reject(new Error('测试中不应发起真实请求')),
  localStorage: {
    store: new Map(),
    getItem(key) { return this.store.has(key) ? this.store.get(key) : null; },
    setItem(key, value) { this.store.set(key, String(value)); }
  },
  document: { getElementById: () => null, addEventListener() {}, body: { classList: { add() {}, remove() {} } } }
};
sandbox.window = sandbox;
vm.createContext(sandbox);

const bundle = ['js/core.js', 'js/visual-identity.js', 'js/api-client.js', 'js/generate.js']
  .map(file => fs.readFileSync(path.join(root, file), 'utf8'))
  .join('\n;\n');
vm.runInContext(bundle, sandbox, { filename: 'bundle.js' });

const App = sandbox.window.App;
assert.ok(App.views.generate, '视图未注册：App.views.generate');
assert.ok(App.actions['generate-start'], '未注册动作：generate-start');

// 动作内部会调用 A.render() 重绘；vm 里没有 DOM，桩掉即可
App.render = () => {};

const failures = [];
const check = (name, condition, detail = '') => {
  console.log(`  ${condition ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!condition) failures.push(name);
};

function reset(overrides) {
  App.state.generate = {
    courses: [{ id: 'analysis-ii', title: '数学分析 II' }],
    loaded: true,
    courseId: 'analysis-ii',
    title: '数学分析（第三版）下册',
    pageFrom: 1,
    pageTo: 20,
    upload: null,
    job: null,
    error: '',
    busy: false,
    timer: null,
    ...overrides
  };
}

console.log('教材生成页：');

// 1. 初始表单
reset();
let html = App.views.generate();
check('页面说明生成结果先进审核', html.includes('审核通过才进学生端') || html.includes('审核通过'));
check('课程下拉来自后端课程列表', html.includes('analysis-ii'));
check('起止页有默认值', html.includes('id="gen-from"') && html.includes('id="gen-to"'));
check('有文件选择框', html.includes('type="file"') && html.includes('accept="application/pdf"'));

// 2. 上传后展示探测结果
reset({
  upload: { page_count: 320, has_text_layer: false, engines_available: ['pypdf'], textbook_id: 'x' }
});
html = App.views.generate();
check('显示页数', html.includes('320'));
check('扫描件会明说可能需要 OCR', html.includes('无文字层'));
check('显示可用的解析引擎', html.includes('pypdf'));

// 3. 任务进行中
reset({ job: { id: 7, status: 'running', progress: 45, attempts: 1 } });
html = App.views.generate();
check('进行中显示进度', html.includes('45') && html.includes('生成中'));
check('进行中不显示"已完成"', !html.includes('已完成'));

// 4. 失败：原因原样显示，并给出装依赖的那条命令
reset({
  job: {
    id: 8, status: 'failed', progress: 0,
    error: 'PdfEngineMissing: 服务器上没有 PDF 解析引擎'
  }
});
html = App.views.generate();
check('失败原因原样显示', html.includes('PdfEngineMissing'));
check('给出安装 pypdf 的指引', html.includes('pip install pypdf'));

// 5. 成功：必须点明待审，并能跳审核
reset({
  job: {
    id: 9, status: 'succeeded', progress: 100,
    result: { nodes: 6, exercises: 12, warnings: ['第 3 题的知识点「极限」不在本章知识点里'],
              pack_dir: 'course-packs/analysis-ii/generated/job-9' }
  }
});
html = App.views.generate();
check('成功时显示知识点与题数', html.includes('6') && html.includes('12'));
check('成功时点明进入待审', html.includes('draft') || html.includes('待审'));
check('成功时能跳到审核队列', html.includes('data-action') && html.includes('review'));
check('警告可展开查看', html.includes('<details') && html.includes('极限'));

// 6. 表单校验：没选文件时给具体原因
(async () => {
  reset();
  let message = '';
  App.toast = text => { message = text; };
  const button = { disabled: false };
  await App.actions['generate-start'](button).catch(error => { message = error.message; });
  check('没选文件时给出明确提示', message.includes('先选一个 PDF'), message);

  // 7. 页码填反
  reset({ pageFrom: 30, pageTo: 10 });
  sandbox.document.getElementById = id => (id === 'gen-file'
    ? { files: [{ name: 'book.pdf', type: 'application/pdf' }] }
    : null);
  message = '';
  await App.actions['generate-start']({ disabled: false }).catch(error => { message = error.message; });
  check('结束页小于起始页时拒绝提交', message.includes('结束页'), message);

  console.log('');
  if (failures.length) {
    console.log(`失败 ${failures.length} 项：${failures.join(', ')}`);
    process.exit(1);
  }
  console.log('教材生成页测试全部通过。');
})();
