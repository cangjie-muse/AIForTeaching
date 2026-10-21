#!/usr/bin/env node
/**
 * 端到端测试：把 PGlite 变成真的 PostgreSQL 端口，让 FastAPI + psycopg 连上去。
 *
 * 为什么绕这一圈：
 *   本机没有 PostgreSQL，但项目里 review.py / admin.py / main.py 写了大量手写 SQL，
 *   从来没有任何一次经过真正的 PG 协议执行。PGlite 本身是 WASM 版 PostgreSQL，
 *   pglite-socket 把它的 wire protocol 暴露在 TCP 上，于是 psycopg 可以当它是
 *   一台普通数据库服务器——SQL 语法、约束、触发器、视图全部真的被编译执行。
 *
 * 局限（必须说在前面）：
 *   PGlite 是单连接的嵌入式 PG，行为接近但不等于服务器版 PG（插件、并行查询、
 *   JSONB 索引细节可能不同）。它证明"SQL 能跑通、约束生效"，不证明"生产性能"。
 *
 * 用法：npm run test:api
 */
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
// 默认换到 55442，不再和设备开发库（dev:api 的 55432）抢端口。
// 以前两者同端口，跑一次 test:api 就必须先把 dev:api 杀掉——开发服务器每被
// 杀一次，正在看页面的人就撞上一次"网页打不开"。测试不该有这种副作用。
const PORT = Number(process.env.E2E_PG_PORT || 55442);
const PYTHON = process.platform === 'win32'
  ? path.join(root, '.venv', 'Scripts', 'python.exe')
  : path.join(root, '.venv', 'bin', 'python');

if (!fs.existsSync(PYTHON)) {
  console.error(`找不到虚拟环境解释器：${PYTHON}\n先运行：python -m venv .venv && .venv/Scripts/pip install -r server/requirements.txt`);
  process.exit(1);
}

async function main() {
  // 端口被占时给一句人话，而不是让 PGlite 抛 EADDRINUSE 堆栈。
  // 最常见的原因是 npm run dev:api 还开着。
  const busy = await new Promise((resolve) => {
    const socket = require('node:net').connect({ port: PORT, host: '127.0.0.1' });
    socket.on('connect', () => { socket.destroy(); resolve(true); });
    socket.on('error', () => resolve(false));
  });
  if (busy) {
    console.error(`端口 ${PORT} 被占用，端到端测试无法启动数据库。\n`
      + '多半是 npm run dev:api 还开着：先停掉它，或设置 E2E_PG_PORT 换一个端口。');
    process.exit(1);
  }

  const { PGlite } = await import('@electric-sql/pglite');
  const { PGLiteSocketServer } = await import('@electric-sql/pglite-socket');

  // pgcrypto 在 PGlite 里是 contrib 扩展，必须显式加载注册后 CREATE EXTENSION 才有效。
  // pgvector 没有，所以 schema 用 --pglite-compat 降级（见 db/migrate.py）。
  const { pgcrypto } = await import('@electric-sql/pglite/contrib/pgcrypto');
  const db = await PGlite.create({ extensions: { pgcrypto } });  const server = new PGLiteSocketServer({ db, port: PORT, host: '127.0.0.1' });
  await server.start();
  console.log(`PGlite 已监听 127.0.0.1:${PORT}`);

  // 放在仓库内的 .tmp/ 下：系统临时目录在 Windows 上会因 ACL/受控文件夹访问
  // 导致子进程写文件被拒（EACCES），换到工作区就稳定了。
  // 只清自己的子目录，绝不清整个 .tmp —— 开发库 .tmp/dev-db 也在那里，
  // 之前"顺手删 .tmp"直接把开发数据删了。
  const workDir = path.join(root, '.tmp', 'e2e-run');
  const secretsDir = path.join(workDir, 'secrets');
  fs.rmSync(workDir, { recursive: true, force: true });
  fs.mkdirSync(secretsDir, { recursive: true });
  const env = {
    ...process.env,
    // sslmode=disable：PGlite 的 socket 不实现 TLS 握手，
    // psycopg 默认 prefer 会先发 SSLRequest，等不到响应就超时。
    DATABASE_URL: `postgresql://postgres@127.0.0.1:${PORT}/postgres?sslmode=disable`,
    DB_POOL_MAX: '1',              // PGlite 单连接，池开大会撞车
    AUTH_MODE: 'dev',
    AUTH_DEV_ROLES: '',  // 默认不兜底角色：本地也必须登录，否则权限无从验证
    APP_ENV: 'test',
    MODEL_EGRESS: 'allowlist',
    SECRETS_DIR: secretsDir
  };

  // 必须用异步 spawn：spawnSync 会阻塞事件循环，PGlite 的 socket 服务端
  // 就没法 accept 连接，psycopg 会一直等到连接超时。
  const run = (label, args) => new Promise((resolve, reject) => {
    console.log(`\n── ${label} ──`);
    const child = spawn(PYTHON, args, { cwd: root, env, stdio: 'inherit' });
    child.on('error', reject);
    child.on('exit', (status) => {
      if (status === 0) resolve();
      else reject(new Error(`${label} 失败（退出码 ${status}）`));
    });
  });

  let code = 0;
  try {
    await run('应用迁移', ['db/migrate.py', '--apply', '--pglite-compat']);
    await run('端到端接口测试', ['server/tests/e2e_api.py']);
  } catch (error) {
    console.error(`\n${error.message}`);
    code = 1;
  } finally {
    await server.stop();
    await db.close();
    // 只删本轮产物。Windows 上偶尔删不掉（文件仍被占用），那是清理问题，不该让测试判失败。
    try {
      fs.rmSync(workDir, { recursive: true, force: true });
    } catch (error) {
      console.warn(`[清理] ${workDir} 未删干净（不影响测试结果）：${error.code || error.message}`);
    }
  }
  process.exit(code);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
