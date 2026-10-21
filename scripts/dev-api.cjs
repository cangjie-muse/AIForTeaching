#!/usr/bin/env node
/**
 * 本地联调服务：一条命令把"数据库 + 后端 + 前端"都拉起来。
 *
 *   npm run dev:api
 *   然后打开 http://127.0.0.1:8000/
 *
 * 为什么需要它：
 *   审核页和模型配置页都要调后端，但之前没有任何东西能同时提供
 *   "一个数据库 + 一个跑着的 FastAPI + 一个能打开的前端"。
 *   测试脚本跑完就退出，页面只能在 vm 里渲染，点不到。
 *
 * 组成：
 *   PGlite（WASM 版 PostgreSQL 16）→ socket 暴露成真 PG 端口 → psycopg 连上去
 *   uvicorn 起 FastAPI，同时挂载前端静态文件（同源，没有跨域问题）
 *   数据落在 .tmp/dev-db，重启保留；想重来就删掉那个目录
 *
 * 局限：PGlite 无 pgvector，所以迁移用 --pglite-compat（vector 降级为 jsonb）。
 * 这只影响本地开发，生产库不用这个开关。
 */
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');

const root = path.resolve(__dirname, '..');
const PG_PORT = Number(process.env.DEV_PG_PORT || 55432);
const API_PORT = Number(process.env.DEV_API_PORT || 8000);
const BASE = `http://127.0.0.1:${API_PORT}`;
const PYTHON = process.platform === 'win32'
  ? path.join(root, '.venv', 'Scripts', 'python.exe')
  : path.join(root, '.venv', 'bin', 'python');
const dataDir = path.join(root, '.tmp', 'dev-db');
const secretsDir = path.join(root, '.tmp', 'dev-secrets');

if (!fs.existsSync(PYTHON)) {
  console.error(`找不到虚拟环境解释器：${PYTHON}
先执行：
  python -m venv .venv
  .venv\\Scripts\\pip install -r server\\requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`);
  process.exit(1);
}

function portBusy(port) {
  return new Promise((resolve) => {
    const socket = require('node:net').connect({ port, host: '127.0.0.1' });
    socket.on('connect', () => { socket.destroy(); resolve(true); });
    socket.on('error', () => resolve(false));
  });
}

function get(url) {
  return new Promise((resolve, reject) => {
    const request = http.get(url, (response) => {
      let body = '';
      response.setEncoding('utf8');
      response.on('data', (chunk) => { body += chunk; });
      response.on('end', () => resolve({ status: response.statusCode, body }));
    });
    request.on('error', reject);
    request.setTimeout(3000, () => request.destroy(new Error('请求超时')));
  });
}

function post(url) {
  return new Promise((resolve, reject) => {
    const target = new URL(url);
    const request = http.request({
      hostname: target.hostname, port: target.port, path: target.pathname,
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': 0,
        // 启动时的自动建队不是人点的：审核接口现在要求身份，这里带开发身份头。
        // 生产环境（AUTH_MODE=password/cas）不会执行这个分支。
        'X-Actor': encodeURIComponent('启动脚本'),
        'X-Roles': 'admin'
      }
    }, (response) => {
      let body = '';
      response.setEncoding('utf8');
      response.on('data', (chunk) => { body += chunk; });
      response.on('end', () => resolve({ status: response.statusCode, body }));
    });
    request.on('error', reject);
    request.end();
  });
}

async function waitForApi(timeoutMs = 60000) {
  const deadline = Date.now() + timeoutMs;
  let last = '';
  while (Date.now() < deadline) {
    try {
      const response = await get(`${BASE}/api/v1/health`);
      if (response.status === 200) return JSON.parse(response.body);
      last = `HTTP ${response.status}`;
    } catch (error) {
      last = error.message;
    }
    await new Promise((resolve) => setTimeout(resolve, 700));
  }
  throw new Error(`后端 60 秒内没有就绪：${last}`);
}

async function main() {
  if (await portBusy(PG_PORT)) {
    console.error(`端口 ${PG_PORT} 已被占用。可能是上一次的 dev:api 没退干净，或 e2e 正在跑。`);
    process.exit(1);
  }
  if (await portBusy(API_PORT)) {
    console.error(`端口 ${API_PORT} 已被占用。先停掉占用它的进程（可能是上一个 dev:api）。`);
    process.exit(1);
  }

  const { PGlite } = await import('@electric-sql/pglite');
  const { PGLiteSocketServer } = await import('@electric-sql/pglite-socket');
  const { pgcrypto } = await import('@electric-sql/pglite/contrib/pgcrypto');

  fs.mkdirSync(dataDir, { recursive: true });
  fs.mkdirSync(secretsDir, { recursive: true });
  const db = await PGlite.create({ dataDir, extensions: { pgcrypto } });
  const socketServer = new PGLiteSocketServer({ db, port: PG_PORT, host: '127.0.0.1' });
  await socketServer.start();
  console.log(`[1/4] PGlite 已监听 127.0.0.1:${PG_PORT}（数据目录 .tmp/dev-db）`);

  const env = {
    ...process.env,
    DATABASE_URL: `postgresql://postgres@127.0.0.1:${PG_PORT}/postgres?sslmode=disable`,
    DB_POOL_MAX: '1',
    AUTH_MODE: 'dev',
    AUTH_DEV_ROLES: '',  // 默认不兜底角色：本地也必须登录，否则权限无从验证
    APP_ENV: 'dev',
    MODEL_EGRESS: 'allowlist',
    // 没有 API Key 也能点通问答链路；生产环境会被 main.py 拒绝
    DEV_FAKE_MODEL: process.env.DEV_FAKE_MODEL || '1',
    SECRETS_DIR: secretsDir,
    // Windows 控制台默认 GBK，中文日志会变成乱码
    PYTHONIOENCODING: 'utf-8',
    PYTHONUTF8: '1'
  };

  const run = (label, args) => new Promise((resolve, reject) => {
    const child = spawn(PYTHON, args, { cwd: root, env, stdio: 'inherit' });
    child.on('error', reject);
    child.on('exit', status => (status === 0 ? resolve() : reject(new Error(`${label} 失败（退出码 ${status}）`))));
  });

  // 必须异步 spawn：spawnSync 会阻塞事件循环，socket 服务端没法 accept，
  // psycopg 会一直等到连接超时。
  await run('应用迁移', ['db/migrate.py', '--apply', '--pglite-compat']);
  console.log('[2/4] 迁移已应用');
  await run('写入示例数据', ['scripts/dev_seed.py']);
  await run('写入演示账号', ['scripts/dev_seed_accounts.py']);
  console.log('[3/4] 示例数据已就绪');

  const api = spawn(
    PYTHON,
    ['-m', 'uvicorn', 'app.main:app', '--app-dir', 'server',
     '--host', '127.0.0.1', '--port', String(API_PORT)],
    { cwd: root, env, stdio: 'inherit' }
  );
  api.on('exit', (status) => {
    console.error(`\n后端进程已退出（退出码 ${status}）。`);
  });

  const health = await waitForApi();
  console.log(`[4/4] 后端就绪，数据库里 ${health.courses} 门课`);

  let built = '';
  try {
    const result = await post(`${BASE}/api/v1/review/queue/build`);
    const data = JSON.parse(result.body);
    built = `已建队：${data.tasks_created} 条待审、${data.issues_created} 条问题`;
  } catch (error) {
    built = `建队失败（不影响使用，可在页面点「从数据生成队列」）：${error.message}`;
  }

  console.log(`
──────────────────────────────────────────────
 前端 + API（同源，没有跨域问题）
   ${BASE}/

 直接进这两个页面
   内容审核   ${BASE}/#/review
   模型配置   ${BASE}/#/api-config

 ${built}
 数据库      .tmp/dev-db   （重启保留；删掉即重来）
 密钥目录    .tmp/dev-secrets

 Ctrl+C 停止。先停后端再关数据库，否则下次启动可能提示端口占用。
──────────────────────────────────────────────`);

  let closing = false;
  const shutdown = async (signal) => {
    if (closing) return;
    closing = true;
    console.log(`\n收到 ${signal}，正在停止…`);
    api.kill();
    await new Promise((resolve) => setTimeout(resolve, 400));
    await socketServer.stop().catch(() => {});
    await db.close().catch(() => {});
    process.exit(0);
  };
  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));
  api.on('exit', () => shutdown('后端退出'));
}

main().catch((error) => {
  console.error(`\n启动失败：${error.message}`);
  process.exit(1);
});
