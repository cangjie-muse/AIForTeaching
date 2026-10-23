#!/usr/bin/env node
/**
 * 「课程内容」合并页的渲染冒烟测试（node:vm，不启浏览器）。
 *
 * 这一页是把「课程导入」和「教材生成」合成一个入口后的界面。要盯住的是合并带来的
 * 三个风险，而不是版式：
 *   1. 生成那块界面必须还在（合并不能把原来的功能弄丢）
 *   2. 导入必须落到数据库（提交到 /content/import），不能又只存在浏览器里
 *   3. 导出必须能拼回导入器认的三件套（6 级节点列 + 两份 md 引用）
 *   4. 在线修改必须真的调接口，且停用走 retired 而不是删除
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');

const calls = [];
const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  setInterval: () => 1,
  clearInterval: () => {},
  URLSearchParams,
  location: { hash: '#/content' },
  confirm: () => true,
  alert: () => {},
  fetch: (url, options) => {
    calls.push({ url: String(url), options });
    return Promise.reject(new Error('测试中不应发起真实请求'));
  },
  localStorage: {
    store: new Map(),
    getItem(key) { return this.store.has(key) ? this.store.get(key) : null; },
    setItem(key, value) { this.store.set(key, String(value)); }
  },
  document: { getElementById: () => null, addEventListener() {}, body: { classList: { add() {}, remove() {} } } }
};
sandbox.window = sandbox;
vm.createContext(sandbox);

const bundle = ['js/core.js', 'js/visual-identity.js', 'js/api-client.js', 'js/generate.js', 'js/content.js']
  .map(file => fs.readFileSync(path.join(root, file), 'utf8'))
  .join('\n;\n');
vm.runInContext(bundle, sandbox, { filename: 'bundle.js' });

const App = sandbox.window.App;
assert.ok(App.views.content, '视图未注册：App.views.content');
assert.ok(App.panels.generate, '生成的正文面板未暴露：App.panels.generate');
App.render = () => {};

const failures = [];
const check = (name, condition, detail = '') => {
  console.log(`  ${condition ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!condition) failures.push(name);
};

function reset(overrides) {
  App.state.content = {
    tab: 'generate',
    courses: [{ id: 'analysis-ii', title: '数学分析 II' }, { id: 'symplectic', title: '辛几何' }],
    loaded: true,
    courseId: 'analysis-ii',
    preview: null,
    importError: '',
    importing: false,
    importResult: null,
    mode: 'replace',
    content: null,
    editKey: '',
    editDraft: null,
    saving: false,
    error: '',
    notice: '',
    ...overrides
  };
  App.state.generate = {
    courses: [{ id: 'analysis-ii', title: '数学分析 II' }],
    loaded: true, mode: 'book', courseId: 'analysis-ii', title: '数学分析',
    pageFrom: 1, pageTo: 20, upload: null, job: null, error: '', busy: false, timer: null
  };
}

console.log('课程内容页（合并入口）：');

// 1. 导航只剩一个入口
reset();
let html = App.views.content();
check('页面标题是「课程内容」', html.includes('课程内容'), '');
check('三个标签都在', ['AI 生成', '导入资源包', '修改与导出'].every(t => html.includes(t)));
check('默认停在生成标签', html.includes('整本（自动切章）') || html.includes('生成'), '');
check('生成面板被复用（不是重写一份）', App.views.content().includes(App.panels.generate().slice(0, 60)));

// 2. 导入标签：没选文件时给说明
reset({ tab: 'import' });
html = App.views.content();
check('导入标签有文件选择', html.includes('content-folder') && html.includes('content-files'));
check('导入标签说明会写库', html.includes('数据库'));
check('文件选择挂 input 事件（页面没有 change 监听）',
  /data-input="content-folder"/.test(html) && /data-input="content-files"/.test(html));

// 3. 解析成功后的预览：要能选目标课程、能选更新方式
const pkg = {
  id: 'course-analysis-ii', title: '数学分析 II', schemaVersion: '1.0',
  description: '简介', audience: '本科', subject: '数学',
  nodes: [
    { id: 'course-analysis-ii', title: '数学分析 II', path: ['数学分析 II'], level: 1, parentId: null, chapterId: null, content: '', exerciseAnchor: 'course-analysis-ii', tags: [], source: '' },
    { id: 'ch-1', title: '第一章 实数', path: ['数学分析 II', '第一章 实数'], level: 2, parentId: 'course-analysis-ii', chapterId: 'ch-1', content: '', exerciseAnchor: 'ch-1', tags: [], source: '' },
    { id: 'kp-1', title: '确界原理', path: ['数学分析 II', '第一章 实数', '确界原理'], level: 3, parentId: 'ch-1', chapterId: 'ch-1', content: '正文', exerciseAnchor: 'kp-1', tags: ['定理'], source: '书页12' }
  ],
  exercises: [{ id: 'gen-ch-1-abc-001', prompt: '题干', answer: '答', difficulty: 3, source: '教材《数学分析》上册，书页12', nodeIds: ['kp-1'] }],
  warnings: ['1 个知识点没有题目（例如「确界原理」）。']
};
reset({ tab: 'import', preview: pkg, courseId: 'analysis-ii' });
html = App.views.content();
check('预览显示知识点与题目数', html.includes('3 个知识点') && html.includes('1 道题'), '');
check('有课程名称输入框', html.includes('id="content-name"'), '');
check('名称默认取资源包里的课程名', html.includes('value="数学分析 II"'), '');

// 课程名称是唯一入口：名字对上哪门课就是哪门课，对不上就新建。
// 提示必须当场说清"和包对不对得上、库里有没有"，不能等点下确认才报错。
reset({ tab: 'import', preview: pkg, courseName: '数学分析 II',
  courses: [{ id: 'analysis-ii', title: '数学分析 II' }] });
html = App.views.content();
check('名字与包一致时说明一致', html.includes('与资源包里的课程名一致'), '');
check('已有同名课程时提示会更新它', html.includes('已经有《数学分析 II》'), '');
check('提示里带课程 id', html.includes('id：analysis-ii'), '');

reset({ tab: 'import', preview: pkg, courseName: '数学分析 二',
  courses: [{ id: 'analysis-ii', title: '数学分析 II' }] });
html = App.views.content();
check('名字与包不一致时明确指出两边各是什么',
  html.includes('资源包里的课程名是《数学分析 II》') && html.includes('你填的是《数学分析 二》'), '');
check('不一致且没有同名课程时说要新建',
  html.includes('还没有这门课，导入后会新建'), '');

reset({ tab: 'import', preview: pkg, courseName: '拓扑学',
  courses: [{ id: 'topology', title: '拓扑学' }] });
html = App.views.content();
check('改成另一门已存在的课也如实说会更新它',
  html.includes('已经有《拓扑学》') && html.includes('id：topology'), '');
check('不再有"目标课程"下拉（避免两套口径打架）', !html.includes('content-target'), '');
check('两种更新方式都在', html.includes('整体更新') && html.includes('只增改'));
check('提示项可展开', html.includes('1 条提示'));

// 3b. 单文件增量导入：要写明写入哪门课，别让人对着 id 猜
reset({ tab: 'import', docPreview: {
  course_id: 'analysis-ii', knowledge: { changed: 2, unchanged: 5 },
  exercises: { added: 1, updated: 1, unchanged: 3, unlinked: 0 }, warnings: []
} });
html = App.views.content();
check('单文件导入写明写入哪门课', html.includes('写入《数学分析 II》'), '');
check('单文件导入有改动计数', html.includes('知识点正文更新') && html.includes('题目新增'), '');

// 4. 导入结果：要显示停用了多少
reset({ tab: 'import', importResult: {
  course_id: 'analysis-ii', batch: 'abc123', mode: 'replace',
  nodes_upserted: 3, nodes_retired: 1, exercises_upserted: 2, exercises_retired: 4
} });
html = App.views.content();
check('导入结果给批次号', html.includes('abc123'));
check('导入结果说清停用了多少题', html.includes('4') && html.includes('习题停用'));
check('导入结果能跳到图谱与审核', html.includes('#/course/analysis-ii/graph') || html.includes('/graph'));

// 5. 修改与导出：知识点、习题都能改，停用走 retired
reset({ tab: 'edit', content: {
  course: { id: 'analysis-ii', title: '数学分析 II' },
  nodes: [
    { id: 'kp-1', title: '确界原理', level: 3, tags: ['定理'], content_md: '正文', review_status: 'draft', chapter_id: 'ch-1' },
    { id: 'kp-2', title: '阿基米德性', level: 3, tags: [], content_md: '', review_status: 'teacher-reviewed', chapter_id: 'ch-1' }
  ],
  exercises: [
    { id: 'gen-1-abc-001', prompt_md: '题干', answer_md: '答', difficulty: 3, source_raw: '书页12',
      review_status: 'draft', node_ids: ['kp-1'] }
  ]
} });
html = App.views.content();
check('列出知识点', html.includes('确界原理') && html.includes('阿基米德性'));
check('显示审核状态', html.includes('草稿') && html.includes('教师已审'));
check('每项有编辑与停用', html.includes('content-node-edit') && html.includes('content-node-retire'));
check('列出习题', html.includes('gen-1-abc-001') && html.includes('content-ex-edit'));
check('有导出按钮', html.includes('content-export'));

// 6. 打开编辑器后用草稿覆盖原值
reset({ tab: 'edit', editKey: 'node:kp-1', editDraft: { title: '改名后', tags: ['引理'], content_md: '新正文' },
  content: { course: { id: 'analysis-ii', title: '数学分析 II' },
    nodes: [{ id: 'kp-1', title: '确界原理', level: 3, tags: ['定理'], content_md: '旧正文', review_status: 'draft' }],
    exercises: [] } });
html = App.views.content();
check('编辑器显示草稿值', html.includes('改名后') && html.includes('新正文') && html.includes('引理'));
check('编辑器有保存与取消', html.includes('content-node-save') && html.includes('content-cancel'));

// 7. 导出要拼出导入器认的列：6 级节点 + 两份 md 引用
const bundleRows = (nodes) => nodes.map(node => {
  const cells = ['', '', '', '', '', ''];
  node.path.forEach((part, index) => { if (index < 6) cells[index] = part; });
  return [...cells, `知识点.md#${node.id}；习题.md#${node.exercise_anchor || node.id}`, (node.tags || []).join('、')];
});
const rows = bundleRows(pkg.nodes);
check('导出行数等于知识点数', rows.length === pkg.nodes.length, String(rows.length));
check('每行 8 列（6 级节点 + 说明 + 标签）', rows.every(row => row.length === 8));
check('说明列同时引用两份 md',
  rows.every(row => row[6].includes('知识点.md#') && row[6].includes('习题.md#')));
check('一级节点也在里面（导入器要求有且只有一个根）', rows[0][0] === '数学分析 II', rows[0][0]);

// 8. 动作确实调后端接口，而不是只改内存
(async () => {
  console.log('\n接口调用：');

  const moduleCalls = [];
  // 课程列表由"服务器"给：课程存在与否只认这一份，不认页面里那份旧缓存
  sandbox.window.fetch = (url, options) => {
    const target = String(url);
    moduleCalls.push({ url: target, options });
    const isList = /\/api\/v1\/courses$/.test(target);
    const body = isList
      ? { items: [{ id: 'analysis-ii', title: '数学分析 II' }] }
      : { ok: true, course_id: 'analysis-ii', course_title: '数学分析 II', course_created: false,
        batch: 'abc', nodes_upserted: 3, exercises_upserted: 1, nodes_retired: 0, exercises_retired: 0 };
    return Promise.resolve({
      ok: true, status: 200,
      text: () => Promise.resolve(JSON.stringify(body)),
      json: () => Promise.resolve(body)
    });
  };
  let catalogReloads = 0;
  App.loadCatalog = async () => { catalogReloads += 1; };
  reset({ tab: 'import', preview: pkg, courseName: '数学分析 II' });
  await App.actions['content-import']();
  check('导入打到 /content/import', moduleCalls.some(c => c.url.includes('/content/import')),
    moduleCalls.map(c => c.url.replace(/^https?:\/\/[^/]+/, '')).join(' '));
  check('同名课程已存在 → 导入到那门课的 id 上',
    moduleCalls.some(c => c.url.includes('/courses/analysis-ii/content/import')),
    moduleCalls.map(c => c.url.split('/api/v1')[1] || c.url).join(' '));
  check('导入请求体带三件套解析结果',
    moduleCalls.some(c => String(c.options?.body || '').includes('"nodes"')),
    '');
  check('导入后记住了批次结果', App.state.content.importResult?.batch === 'abc', '');
  check('导入后重拉课程目录（课程总览才会跟着变）', catalogReloads >= 1, `${catalogReloads} 次`);
  check('已存在时不带 title_override（不改名）',
    moduleCalls.some(c => c.url.includes('/content/import')
      && JSON.parse(c.options.body).title_override === null), '');

  // 改了名字、库里又没有这门课：按新名字建，并把名字写进 course.title
  moduleCalls.length = 0;
  reset({ tab: 'import', preview: pkg, courseName: '数学分析 II（2026）' });
  await App.actions['content-import']();
  const renameCall = moduleCalls.find(c => c.url.includes('/content/import'));
  check('名字与包不一致 → 带上 title_override',
    renameCall && JSON.parse(renameCall.options.body).title_override === '数学分析 II（2026）',
    renameCall ? JSON.parse(renameCall.options.body).title_override : '没有导入请求');
  check('库里没有这门课 → 用资源包里的 id 新建',
    renameCall && renameCall.url.includes('/courses/course-analysis-ii/content/import'), '');

  moduleCalls.length = 0;
  reset({ tab: 'edit', content: { course: { id: 'analysis-ii', title: 'x' }, nodes: [], exercises: [] } });
  await App.actions['content-node-retire']({ dataset: { id: 'kp-1' } });
  check('停用知识点走 retire 接口', moduleCalls.some(c => c.url.includes('/nodes/kp-1/retire')),
    moduleCalls.map(c => c.url.split('/api/v1')[1] || c.url).join(' '));

  moduleCalls.length = 0;
  reset({ tab: 'edit', content: { course: { id: 'analysis-ii', title: 'x' }, nodes: [], exercises: [] } });
  await App.actions['content-ex-retire']({ dataset: { id: 'ex-1' } });
  check('停用习题走 retire 接口', moduleCalls.some(c => c.url.includes('/exercises/ex-1/retire')), '');

  console.log('');
  if (failures.length) {
    console.log(`失败 ${failures.length} 项：${failures.join(', ')}`);
    process.exit(1);
  }
  console.log('课程内容页渲染测试全部通过。');
})();
