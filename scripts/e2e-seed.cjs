#!/usr/bin/env node
/**
 * 把 db/seed.sql 真正导进一个数据库再检查。
 *
 * 为什么需要它：`npm run verify:db` 只做静态检查——解析 12 MB 的 SQL 文本、数行数。
 * 那份脚本有 14935 条 INSERT，此前从未被执行过一次。静态检查看不出语法错误、
 * 约束冲突、枚举值写错、数组字面量写错，而这些恰好会在服务器上第一次导入时炸出来。
 *
 * 这里起一个真的 PostgreSQL（PGlite）→ 应用迁移 → 导入 seed.sql → 断言行数与内容。
 *
 * 用法：npm run test:seed
 */
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const PORT = Number(process.env.SEED_PG_PORT || 55434);
const PYTHON = process.platform === 'win32'
  ? path.join(root, '.venv', 'Scripts', 'python.exe')
  : path.join(root, '.venv', 'bin', 'python');

const env = {
  ...process.env,
  PYTHONIOENCODING: 'utf-8',
  DATABASE_URL: `postgresql://postgres@127.0.0.1:${PORT}/postgres?sslmode=disable`,
  DB_POOL_MAX: '1',
  AUTH_MODE: 'dev',
  APP_ENV: 'test'
};

/** 异步 spawn：PGlite 的 socket 服务端在本进程里，spawnSync 会把事件循环卡死。 */
function run(label, args) {
  return new Promise((resolve, reject) => {
    console.log(`\n── ${label} ──`);
    const child = spawn(PYTHON, args, { cwd: root, env, stdio: 'inherit' });
    child.on('error', reject);
    child.on('exit', code => (code === 0 ? resolve() : reject(new Error(`${label} 失败（退出码 ${code}）`))));
  });
}

async function main() {
  if (!fs.existsSync(PYTHON)) {
    console.error(`找不到虚拟环境解释器：${PYTHON}`);
    process.exit(1);
  }
  const busy = await new Promise(resolve => {
    const socket = require('node:net').connect({ port: PORT, host: '127.0.0.1' });
    socket.on('connect', () => { socket.destroy(); resolve(true); });
    socket.on('error', () => resolve(false));
  });
  if (busy) {
    console.error(`端口 ${PORT} 被占用；设置 SEED_PG_PORT 换一个端口。`);
    process.exit(1);
  }

  const { PGlite } = await import('@electric-sql/pglite');
  const { PGLiteSocketServer } = await import('@electric-sql/pglite-socket');
  const { pgcrypto } = await import('@electric-sql/pglite/contrib/pgcrypto');
  const db = await PGlite.create({ extensions: { pgcrypto } });
  const server = new PGLiteSocketServer({ db, port: PORT, host: '127.0.0.1' });
  await server.start();
  console.log(`PGlite 已监听 127.0.0.1:${PORT}`);

  let code = 0;
  try {
    await run('应用迁移', ['db/migrate.py', '--apply', '--pglite-compat']);
    await run('导入并检查 seed.sql', ['server/tests/test_seed_import.py']);
  } catch (error) {
    console.error(`\n${error.message}`);
    code = 1;
  } finally {
    await server.stop();
    await db.close();
  }
  process.exit(code);
}

main().catch(error => {
  console.error(error);
  process.exit(1);
});
