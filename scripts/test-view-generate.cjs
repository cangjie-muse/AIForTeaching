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
    mode: 'book',
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

// 0. 默认是整本模式：老师只需要上传一个 PDF
reset();
var html = App.views.generate();
check('默认是整本模式', html.includes('整本（自动切章）') && html.includes('上传并识别章节'));
check('整本模式不要求填起止页', !html.includes('id="gen-from"'));
check('有「单章」模式可切', html.includes('data-mode="chapter"'));

// 0b. 切章预览：先给老师看一眼，再决定跑不跑
reset({ plan: { source: 'heading', warning: '', items: [
  { key: 'ch-1', title: '第1章 实数', page_from: 1, page_to: 12, chars: 8000 },
  { key: 'ch-2', title: '第2章 极限', page_from: 13, page_to: 28, chars: 9000 }
] } });
html = App.views.generate();
check('预览列出每一章', html.includes('第1章 实数') && html.includes('第2章 极限'));
check('预览说明按标题还是按页窗切', html.includes('按章标题'));
check('预览后要确认才开跑', html.includes('generate-confirm-book'));

// 0c. 页窗兜底要提示出来
reset({ plan: { source: 'window', warning: '没有识别到两个以上的章标题，已按每 15 页自动切分',
  items: [{ key: 'part-1', title: '第 1–15 页（自动切分）', page_from: 1, page_to: 15, chars: 500 }] } });
html = App.views.generate();
check('页窗切分会显示警告', html.includes('没有识别到两个以上的章标题'));

// 1. 初始表单
reset();
html = App.views.generate();
check('页面说明生成结果先进审核', html.includes('审核通过才进学生端') || html.includes('审核通过'));
check('课程下拉来自后端课程列表', html.includes('analysis-ii'));
reset({ mode: 'chapter' });
html = App.views.generate();
check('单章模式有起止页默认值', html.includes('id="gen-from"') && html.includes('id="gen-to"'));
reset();
html = App.views.generate();
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

// 5b. 整本任务：显示每章进度，失败的章能单独重试
reset({
  job: { id: 12, kind: 'generate-book', status: 'failed', progress: 10, attempts: 1,
         result: { chapters: 2, succeeded: 1, failed: [{ key: 'ch-2', title: '第2章 极限', error: '这一章没有抽到文字' }] } },
  children: [
    { id: 13, chapter_key: 'ch-1', chapter_title: '第1章 实数', status: 'succeeded', result: {} },
    { id: 14, chapter_key: 'ch-2', chapter_title: '第2章 极限', status: 'failed', error: '这一章没有抽到文字', result: {} }
  ]
});
html = App.views.generate();
check('整本任务标题标出「整本」', html.includes('整本'));
check('列出每章状态', html.includes('第1章 实数') && html.includes('第2章 极限'));
check('失败章给出原因', html.includes('这一章没有抽到文字'));
check('失败章能单独重试', html.includes('generate-retry') && html.includes('data-job="14"'));
check('整本也能重试失败的章', html.includes('重试失败的章'));
check('显示完成章数', html.includes('1/2 章完成'));

// 6. 表单校验：没选文件时给具体原因
(async () => {
  reset();
  let message = '';
  App.toast = text => { message = text; };
  const button = { disabled: false };
  await App.actions['generate-start'](button).catch(error => { message = error.message; });
  check('没选文件时给出明确提示', message.includes('先选一个 PDF'), message);

  // 7. 页码填反
  reset({ mode: 'chapter', pageFrom: 30, pageTo: 10 });
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
