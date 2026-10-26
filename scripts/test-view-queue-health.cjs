#!/usr/bin/env node
/**
 * 队列提示条：把"点了没反应"翻译成一句人话。
 *
 * 要钉住的：
 *   1. 没有积压时不打扰（返回空字符串）；
 *   2. worker 明显没在跑时，说清"任务不会自己开始"，并给出重启命令；
 *   3. worker 在跑但一件卡很久时，给的是看日志的命令，而不是重启建议；
 *   4. 健康检查按 30 秒缓存，不要在每次渲染里打接口。
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');

const sandbox = {
  console: { log: console.log, warn() {}, error() {} },
  setTimeout, clearTimeout,
  document: { getElementById: () => null, addEventListener() {}, querySelector: () => null,
    querySelectorAll: () => [], body: { classList: { add() {}, remove() {} } } },
  location: { hash: '#/course/analysis-ii/courseware' },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} }
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(['js/core.js', 'js/queue-health.js'].map(read).join('\n;\n'), sandbox,
  { filename: 'queue-health.js' });

const App = sandbox.window.App;
// 图标由 visual-identity.js 提供，这里只测提示逻辑，给个占位即可
App.icon = App.icon || (() => '<svg/>');
const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};
const tick = () => new Promise(resolve => setTimeout(resolve, 0));

let calls = [];
let renders = 0;
App.render = () => { renders += 1; };
const reply = payload => { App.api = { health: async () => { calls.push('health'); return payload; } }; };

(async () => {
  console.log('没有积压：');
  reply({ queue: { queued: 0, running: 0, worker_alive: null } });
  App.state.queueHealth = null;
  check('首次调用先返回空（还在取数据）', App.queueNotice() === '');
  await tick(); await tick();
  check('取到数据后也不打扰', App.queueNotice() === '');

  console.log('\nworker 没在跑：');
  calls = [];
  reply({ queue: { queued: 3, running: 0, worker_alive: false,
    since_last_claim_seconds: 5400, oldest_queued_seconds: 900 } });
  App.state.queueHealth = null;
  App.queueNotice();
  await tick(); await tick();
  let html = App.queueNotice();
  check('说明后台没在处理', html.includes('后台没有在处理'), html.slice(0, 60));
  check('给出重启命令', html.includes('systemctl restart whu-worker'));
  check('写清等了多久', html.includes('1 小时前') || html.includes('90 分'), html.slice(0, 140));
  check('说任务不会自己开始', html.includes('不会自己开始'));

  console.log('\n有人干活但卡住：');
  reply({ queue: { queued: 1, running: 1, worker_alive: true,
    since_last_claim_seconds: 20, oldest_queued_seconds: 300 } });
  App.state.queueHealth = null;
  App.queueNotice();
  await tick(); await tick();
  html = App.queueNotice();
  check('提示已排队', html.includes('已排队'), html.slice(0, 60));
  check('建议看日志而不是重启', html.includes('journalctl') && !html.includes('restart'));
  check('标成 warn 而不是 bad', html.includes('warn'));

  console.log('\n缓存：');
  calls = [];
  const before = App.state.queueHealth.at;
  App.queueNotice(); App.queueNotice(); App.queueNotice();
  await tick();
  check('30 秒内只请求一次', calls.length === 0, `又打了 ${calls.length} 次`);
  check('时间戳没被刷新', App.state.queueHealth.at === before);

  console.log('\n渲染：');
  reply({ queue: { queued: 2, worker_alive: false, since_last_claim_seconds: 999,
    oldest_queued_seconds: 999 } });
  App.state.queueHealth = null;
  const startRenders = renders;
  App.queueNotice();
  await tick(); await tick();
  check('状态变化后触发一次重绘', renders > startRenders, `${renders - startRenders} 次`);

  console.log('');
  if (failures.length) {
    console.log(`队列提示测试失败 ${failures.length} 项：${failures.join('、')}`);
    process.exit(1);
  }
  console.log('队列提示测试全部通过。');
})();
