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
const bundle = ['js/core.js', 'js/visual-identity.js', 'js/api-config.js']
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

console.log();
if (failures.length) {
  console.log(`失败 ${failures.length} 项：${failures.join(', ')}`);
  process.exit(1);
}
console.log('渲染冒烟测试全部通过。');
