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
  'js/api-client.js', 'js/generate.js', 'js/publish-panel.js', 'js/content.js'
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

function resetPublish(status) {
  App.state.publish = {
    courseId: 'analysis-ii', status, loading: false, saving: false, error: '', notice: ''
  };
  App.state.review = {
    courses: [{ id: 'analysis-ii', title: '数学分析 II' }],
    filters: { course_id: 'analysis-ii' }
  };
  App.state.content = { courses: [{ id: 'analysis-ii', title: '数学分析 II' }], courseId: 'analysis-ii' };
}

resetPublish(null);
let html = App.publishPanel('');
check('没选课先说去选课', html.includes('先选一门课'));

resetPublish({ nodes: 3, published: 0, hidden: 5, published_at: null });
html = App.publishPanel('analysis-ii');
check('未发布时显示未发布徽标', html.includes('未发布'));
check('未发布时给两个动作',
  html.includes('publish-to-students') && html.includes('publish-all'));
check('说明发布只管已审的题', html.includes('「人工已审」的题一起放出'));
check('指路「批量通过」', html.includes('批量通过'));

resetPublish({ nodes: 3, published: 2, hidden: 5, published_at: '2026-10-21T04:00:00Z' });
html = App.publishPanel('analysis-ii');
check('已发布时显示已发布徽标', html.includes('已发布'));
check('已发布时能撤回', html.includes('publish-retract'));
check('已发布时不出现发布按钮', !html.includes('publish-to-students'));
check('给出发布时间', html.includes('2026-10-21 04:00'), '');
check('说明改动即时生效（发布是闸门不是快照）', html.includes('立刻生效'));

// 课程内容页不再管发布，只留一句指路
App.state.content = {
  tab: 'edit', courses: [{ id: 'analysis-ii', title: '数学分析 II' }], loaded: true,
  courseId: 'analysis-ii', preview: null, importError: '', importing: false,
  importResult: null, mode: 'replace', editKey: '', editDraft: null, saving: false,
  error: '', notice: '', publish: null,
  content: {
    course: { id: 'analysis-ii', title: '数学分析 II' },
    nodes: [{ id: 'kp-1', title: '确界原理', level: 3, tags: [], content_md: '正文', review_status: 'draft' }],
    exercises: []
  }
};
html = App.views.content();
check('内容页不再有发布按钮', !html.includes('data-action="content-publish"'));
check('内容页指向审核页发布', html.includes('去审核页发布'));

(async () => {
  console.log('\n发布动作：');
  const calls = [];
  sandbox.window.fetch = (url, options) => {
    calls.push({ url: String(url), options });
    const body = JSON.stringify({
      nodes: 3, published: 2, hidden: 5, published_at: null,
      exercises: 2, drafts_hidden: 5, course_id: 'analysis-ii'
    });
    return Promise.resolve({
      ok: true, status: 200,
      text: () => Promise.resolve(body), json: () => Promise.resolve(JSON.parse(body))
    });
  };

  resetPublish({ nodes: 3, published: 2, hidden: 5, published_at: null });
  await App.actions['publish-to-students']();
  check('发布打到 /publish',
    calls.some(call => /\/courses\/analysis-ii\/publish$/.test(call.url)),
    calls.map(call => call.url.split('/api/v1')[1] || call.url).join(' '));
  check('发布后提示里说明藏了多少题', String(App.state.publish.notice).includes('5'),
    String(App.state.publish.notice));

  calls.length = 0;
  resetPublish({ nodes: 3, published: 0, hidden: 269, published_at: null });
  await App.actions['publish-all']();
  check('整课放出先打 publish-all',
    calls.some(call => /\/exercises\/publish-all$/.test(call.url)),
    calls.map(call => call.url.split('/api/v1')[1] || call.url).join(' '));
  check('整课放出顺带把课程发布',
    calls.some(call => /\/courses\/analysis-ii\/publish$/.test(call.url)));
  check('整课放出提示里说明跳过审核',
    String(App.state.publish.notice).includes('没有人工校验标记'), String(App.state.publish.notice));

  calls.length = 0;
  resetPublish({ nodes: 3, published: 2, hidden: 5, published_at: '2026-10-21T04:00:00Z' });
  await App.actions['publish-retract']();
  check('撤回打到 /unpublish', calls.some(call => /\/unpublish$/.test(call.url)),
    calls.map(call => call.url.split('/api/v1')[1] || call.url).join(' '));

  console.log('');
  if (failures.length) {
    console.log(`失败 ${failures.length} 项：${failures.join(', ')}`);
    process.exit(1);
  }
  console.log('发布出口前端测试全部通过。');
})();
