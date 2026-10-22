#!/usr/bin/env node
/**
 * 模型接口配置页的渲染冒烟测试。
 *
 * 为什么单独测这个页面：
 *   它是唯一会发网络请求的前端页面，失败方式和其他页面不同——后端没起来时
 *   不能白屏、不能死循环、必须给出"先连后端"的引导。这些只有把视图函数真的
 *   跑一遍才能确认。浏览器手动点一次不算回归测试。
 *
 * 不启动浏览器，用 node:vm 提供最小 DOM 垫片。
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
  fetch: () => Promise.reject(new Error('测试中不应发起真实请求')),
  location: { hash: '#/api-config' },
  URLSearchParams,
  Blob,
  URL,
  localStorage: {
    store: new Map(),
    getItem(key) { return this.store.has(key) ? this.store.get(key) : null; },
    setItem(key, value) { this.store.set(key, String(value)); }
  },
  document: { getElementById: () => null, addEventListener() {}, body: { classList: { add() {}, remove() {} } } }
};
sandbox.window = sandbox;
vm.createContext(sandbox);

// 三个文件必须拼成一段执行：core.js 用顶层 const 定义 App，
// 分开 runInContext 时后续脚本看不到这个词法绑定。
const bundle = ['js/core.js', 'js/visual-identity.js', 'js/api-client.js', 'js/api-config.js']
  .map(file => fs.readFileSync(path.join(root, file), 'utf8'))
  .join('\n;\n');
vm.runInContext(bundle, sandbox, { filename: 'bundle.js' });

const App = sandbox.window.App;
assert.ok(App.views['api-config'], '视图未注册：App.views["api-config"]');

const failures = [];
const check = (name, condition, detail = '') => {
  console.log(`  ${condition ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!condition) failures.push(name);
};

// 1. 初始状态（后端未知）：应给出连接引导，而不是空白
App.state.apiConfig = { status: 'error', items: [], me: null, error: 'Failed to fetch' };
let html = App.views['api-config']();
check('后端不可达时给出连接引导', html.includes('先连接后端 API'));
check('连接引导里带后端地址输入框', html.includes('data-input="api-base"'));
check('连接引导里带重试按钮', html.includes('data-action="api-config-refresh"'));

// 2. 只读角色：写操作按钮必须禁用
const reviewer = { actor: '审-1', roles: ['reviewer'], can_view: true, can_edit: false };
App.state.apiConfig = {
  status: 'ready',
  me: reviewer,
  error: '',
  items: [{
    name: 'deepseek-chat', kind: 'chat', base_url: 'https://api.deepseek.com', model: 'deepseek-flash',
    data_scope: 'external', dim: null, enabled: true, key_configured: true,
    last_test_at: '2026-10-20T09:00:00Z', last_test_ok: true, last_test_note: '返回：pong'
  }]
};
html = App.views['api-config']();
check('只读角色下「新增端点」被禁用', /data-action="api-config-new"[^>]*disabled/.test(html));
check('只读角色下「编辑」被禁用', /data-action="api-config-edit"[^>]*disabled/.test(html));
check('只读角色下「自检」被禁用', /data-action="api-config-test"[^>]*disabled/.test(html));
check('只读角色下仍能看到模型与地址', html.includes('deepseek-flash') && html.includes('api.deepseek.com'));
check('密钥只显示「已配置」，不显示内容', html.includes('已配置') && !/sk-[A-Za-z0-9]/.test(html));

// 3. 管理员：按钮可点
App.state.apiConfig.me = { actor: '管-1', roles: ['admin'], can_view: true, can_edit: true };
html = App.views['api-config']();
check('管理员下「新增端点」可点', !/data-action="api-config-new"[^>]*disabled/.test(html));
check('管理员下「编辑」可点', !/data-action="api-config-edit"[^>]*disabled/.test(html));

// 4. 出校端点必须显示为"出校"，避免"以为数据没出校"
check('外部端点标注为出校', html.includes('出校'));

// 5. 维度提示：向量端点显示 1024
App.state.apiConfig.items = [{
  name: 'bailian-embedding', kind: 'embedding', base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
  model: 'qwen3.7-text-embedding', data_scope: 'external', dim: 1024, enabled: false, key_configured: false
}];
html = App.views['api-config']();
check('向量端点显示维度', html.includes('1024 维'));
check('未配置密钥时提示未配置', html.includes('未配置'));

// 6. 空列表：给出下一步该做什么，而不是"暂无数据"
App.state.apiConfig.items = [];
html = App.views['api-config']();
check('空列表给出操作引导', html.includes('还没有登记任何端点'));

// 7. 问答语料面板：14 门课一片 0 块时，"为什么拒答"必须看得见
App.state.apiConfig.corpus = {
  courses_ready: 0,
  total_chunks: 0,
  items: [
    { course_id: 'analysis', title: '数学分析 I', nodes: 12, nodes_with_text: 12, chunks: 0 },
    { course_id: 'numerical-linear-algebra', title: '数值线性代数', nodes: 30, nodes_with_text: 28, chunks: 0 }
  ]
};
html = App.views['api-config']();
check('语料面板显示总块数与就绪课程数', html.includes('0 / 2 门课有语料') && html.includes('共 0 块'));
check('每门课列出知识点数与片段数', html.includes('数学分析 I') && html.includes('12 / 12'));
check('一块都没有时标未入库', html.includes('未入库'));
check('说明没有语料时会拒答而不是乱答', html.includes('没有依据'));

// 8. 切完之后：块数与状态跟着变，管理员能点重建
App.state.apiConfig.corpus = {
  courses_ready: 2,
  total_chunks: 531,
  items: [
    { course_id: 'analysis', title: '数学分析 I', nodes: 12, nodes_with_text: 12, chunks: 159 },
    { course_id: 'numerical-linear-algebra', title: '数值线性代数', nodes: 30, nodes_with_text: 28, chunks: 372 }
  ]
};
App.state.apiConfig.me = { actor: '管-1', roles: ['admin'], can_view: true, can_edit: true };
html = App.views['api-config']();
check('切完显示真实块数', html.includes('2 / 2 门课有语料') && html.includes('共 531 块'));
check('有语料的课标已入库', html.includes('已入库'));
check('管理员可点「全部重建」', !/data-action="api-config-corpus-rebuild"[^>]*disabled/.test(html));

// 9. 只读角色不能重建（前端置灰只是体验，后端仍是 403）
App.state.apiConfig.me = { actor: '审-1', roles: ['reviewer'], can_view: true, can_edit: false };
html = App.views['api-config']();
check('只读角色下「全部重建」被禁用', /data-action="api-config-corpus-rebuild"[^>]*disabled/.test(html));

// 10. 语料接口挂了也不能拖垮整页端点配置
App.state.apiConfig.corpus = null;
App.state.apiConfig.items = [{
  name: 'deepseek-chat', kind: 'chat', base_url: 'https://api.deepseek.com', model: 'deepseek-flash',
  data_scope: 'external', enabled: true, key_configured: true
}];
App.state.apiConfig.corpusError = '需要角色之一：reviewer/教师/管理员';
html = App.views['api-config']();
check('语料读失败时端点表照常显示', html.includes('deepseek-flash'));
check('语料读失败时说明原因', html.includes('读取语料状态失败') && html.includes('不影响上面的端点配置'));

console.log();
if (failures.length) {
  console.log(`失败 ${failures.length} 项：${failures.join(', ')}`);
  process.exit(1);
}
console.log('渲染冒烟测试全部通过。');
