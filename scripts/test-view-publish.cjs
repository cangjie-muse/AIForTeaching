#!/usr/bin/env node
/**
 * 发布出口的前端冒烟测试（node:vm，不启浏览器）。
 *
 * 盯两件事：
 *   1. 合并规则：已发布课程覆盖静态包时，不能把静态包里的资源路径（resourceBase）
 *      覆盖成空——插图路径只在静态包里，覆盖掉学生端就全是裂图。
 *   2. 发布面板：没题通过审核时按钮不能点、且要指向审核队列；发布后要能撤回。
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');

const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  setInterval: () => 1,
  clearInterval: () => {},
  URLSearchParams,
  location: { hash: '#/content' },
  confirm: () => true,
  fetch: () => Promise.reject(new Error('测试中不应发起真实请求')),
  localStorage: {
    store: new Map(),
    getItem(key) { return this.store.has(key) ? this.store.get(key) : null; },
    setItem(key, value) { this.store.set(key, String(value)); }
  },
  document: { getElementById: () => null, addEventListener() {}, body: { classList: { add() {}, remove() {} } } }
};
sandbox.window = sandbox;
sandbox.window.COURSE_DATA = [{
  id: 'analysis-ii', title: '数学分析 II', status: 'ready',
  resourceBase: 'course-packs/course-analysis-ii/', nodes: [{ id: 'old', title: '静态节点', level: 1 }],
  exercises: [], files: ['知识节点.xlsx'], attachments: ['资料/插图/a.png']
}];
vm.createContext(sandbox);

const bundle = [
  'js/course-catalog.js', 'js/core.js', 'js/visual-identity.js',
  'js/api-client.js', 'js/generate.js', 'js/content.js'
].map(file => fs.readFileSync(path.join(root, file), 'utf8')).join('\n;\n');
vm.runInContext(bundle, sandbox, { filename: 'bundle.js' });

const App = sandbox.window.App;
App.render = () => {};
assert.ok(App.mergePublishedCourses, '未注册：App.mergePublishedCourses');

const failures = [];
const check = (name, condition, detail = '') => {
  console.log(`  ${condition ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!condition) failures.push(name);
};

console.log('发布内容合并：');

const published = {
  id: 'analysis-ii', title: '数学分析 II', status: 'ready', resourceBase: '',
  nodes: [{ id: 'new', title: '已发布节点', level: 1 }],
  exercises: [{ id: 'ex-1', prompt: '题干', answer: '答', difficulty: 2, source: '', nodeIds: ['new'] }],
  files: ['知识节点.xlsx', '知识点.md', '习题.md'], attachments: [],
  stats: { nodes: 1, exercises: 1 }, schemaVersion: '1.0', relationships: [],
  reviewStatus: '已发布', publishedAt: '2026-10-21T04:00:00Z'
};
check('合并返回数量', App.mergePublishedCourses([published]) === 1);
const merged = App.courses.find(course => course.id === 'analysis-ii');
check('内容换成已发布的', merged.nodes[0].id === 'new', merged.nodes[0].id);
check('静态包的 resourceBase 保住了',
  merged.resourceBase === 'course-packs/course-analysis-ii/', merged.resourceBase);
check('附件路径保住了', (merged.attachments || []).includes('资料/插图/a.png'),
  JSON.stringify(merged.attachments));
check('发布标记带出来了', merged.reviewStatus === '已发布' && Boolean(merged.publishedAt));

const extra = { ...published, id: 'brand-new-course', title: '新发布课程', resourceBase: '' };
check('新课程追加进去', App.mergePublishedCourses([extra]) === 1);
check('追加后课程数增加', App.courses.some(course => course.id === 'brand-new-course'));
check('空列表不改变任何东西', App.mergePublishedCourses([]) === 0);
check('null 元素被跳过', App.mergePublishedCourses([null, undefined]) === 0);

console.log('\n发布面板：');

function reset(publish) {
  App.state.content = {
    tab: 'edit', courses: [{ id: 'analysis-ii', title: '数学分析 II' }], loaded: true,
    courseId: 'analysis-ii', preview: null, importError: '', importing: false,
    importResult: null, mode: 'replace', editKey: '', editDraft: null, saving: false,
    error: '', notice: '', publish,
    content: {
      course: { id: 'analysis-ii', title: '数学分析 II' },
      nodes: [{ id: 'kp-1', title: '确界原理', level: 3, tags: [], content_md: '正文', review_status: 'draft' }],
      exercises: [{ id: 'ex-1', prompt_md: '题干', answer_md: '答', difficulty: 2, source_raw: '', review_status: 'published', node_ids: ['kp-1'] }]
    }
  };
}

reset({ nodes: 3, published: 0, hidden: 5, published_at: null });
let html = App.views.content();
check('未发布时显示未发布徽标', html.includes('未发布'));
check('没有已发布题时发布按钮禁用', /data-action="content-publish"[^>]*disabled/.test(html), '');
check('并提示去审核队列', html.includes('内容审核'));

reset({ nodes: 3, published: 2, hidden: 5, published_at: '2026-10-21T04:00:00Z' });
html = App.views.content();
check('已发布时显示已发布徽标', html.includes('已发布'));
check('已发布时能撤回', html.includes('content-unpublish'));
check('已发布时不再显示重复的发布按钮', !html.includes('重新发布'));
check('给出发布时间', html.includes('2026-10-21 04:00'), '');
check('说明改动即时生效（发布是闸门不是快照）', html.includes('立刻生效'));

(async () => {
  console.log('\n发布动作：');
  const calls = [];
  sandbox.window.fetch = (url, options) => {
    calls.push({ url: String(url), options });
    const body = JSON.stringify({ nodes: 3, published: 2, hidden: 5, published_at: '2026-10-21T04:00:00Z', exercises: 2, drafts_hidden: 5 });
    return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(body), json: () => Promise.resolve(JSON.parse(body)) });
  };
  reset({ nodes: 3, published: 2, hidden: 5, published_at: null });
  await App.actions['content-publish']();
  check('发布打到 /publish',
    calls.some(call => /\/courses\/analysis-ii\/publish$/.test(call.url)),
    calls.map(call => call.url.split('/api/v1')[1] || call.url).join(' '));
  check('发布后提示里说明藏了多少题', String(App.state.content.notice).includes('5'),
    String(App.state.content.notice));

  calls.length = 0;
  reset({ nodes: 3, published: 2, hidden: 5, published_at: '2026-10-21T04:00:00Z' });
  await App.actions['content-unpublish']();
  check('撤回打到 /unpublish', calls.some(call => /\/unpublish$/.test(call.url)),
    calls.map(call => call.url.split('/api/v1')[1] || call.url).join(' '));

  console.log('');
  if (failures.length) {
    console.log(`失败 ${failures.length} 项：${failures.join(', ')}`);
    process.exit(1);
  }
  console.log('发布出口前端测试全部通过。');
})();
