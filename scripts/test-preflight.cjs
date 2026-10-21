#!/usr/bin/env node
/**
 * 上线自检脚本的测试。
 *
 * 自检脚本本身也可能骗人：如果它对危险配置只报警告、或者连不上库时默默跳过，
 * 那它会给人一种"检查过了"的错觉——比不检查更糟。所以这里正面测几种必须失败的情形。
 *
 * 前三项不需要数据库；第四项起一个真的 PostgreSQL（PGlite）来验证库检查这一路。
 */
const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const PORT = Number(process.env.PREFLIGHT_PG_PORT || 55433);
const PYTHON = process.platform === 'win32'
  ? path.join(root, '.venv', 'Scripts', 'python.exe')
  : path.join(root, '.venv', 'bin', 'python');

const failures = [];
const check = (name, condition, detail = '') => {
  console.log(`  ${condition ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!condition) failures.push(name);
};

/* 连库那一步必须用异步 spawn：spawnSync 会阻塞事件循环，而 PGlite 的 socket 服务端
 * 就跑在本进程里，被阻塞时无法 accept，psycopg 只会等到连接超时——一个假故障。 */
function runPreflightAsync(args, env = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(PYTHON, ['scripts/preflight.py', ...args], {
      cwd: root,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', ...env }
    });
    let out = '';
    child.stdout.on('data', chunk => { out += chunk; });
    child.stderr.on('data', chunk => { out += chunk; });
    child.on('error', reject);
    child.on('exit', code => resolve({ code, out }));
  });
}

function runPreflight(args, env = {}) {
  const result = spawnSync(PYTHON, ['scripts/preflight.py', ...args], {
    cwd: root,
    // 子进程的中文输出要显式声明 UTF-8，否则 Windows 控制台代码页会把断言用的字串弄乱
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', ...env },
    encoding: 'utf8'
  });
  return { code: result.status, out: `${result.stdout || ''}${result.stderr || ''}` };
}

async function main() {
  if (!fs.existsSync(PYTHON)) {
    console.error(`找不到虚拟环境解释器：${PYTHON}`);
    process.exit(1);
  }

  // 1. prod + dev 认证必须阻断，而不是警告
  const risky = runPreflight(['--offline'], { APP_ENV: 'prod', AUTH_MODE: 'dev', DEV_FAKE_MODEL: '0' });
  check('生产环境配 dev 认证被判阻断', risky.code === 1, `退出码 ${risky.code}`);
  check('并说明后果（可冒充管理员）', risky.out.includes('冒充管理员'));

  // 2. 生产环境开演示模式也必须阻断
  const fake = runPreflight(['--offline'], {
    APP_ENV: 'prod', AUTH_MODE: 'cas', DEV_FAKE_MODEL: '1'
  });
  check('生产环境开演示模式被判阻断', fake.code === 1, `退出码 ${fake.code}`);
  check('指出演示模式的影响', fake.out.includes('DEV_FAKE_MODEL=1'));

  // 3. 非法出校策略要报错，而不是当成 any
  const egress = runPreflight(['--offline'], {
    APP_ENV: 'dev', AUTH_MODE: 'dev', MODEL_EGRESS: 'whatever'
  });
  check('非法出校策略被判阻断', egress.code === 1, `退出码 ${egress.code}`);
  check('列出合法取值', egress.out.includes('internal-only'));

  // 4. 开发环境离线自检不应阻断
  const dev = runPreflight(['--offline'], {
    APP_ENV: 'dev', AUTH_MODE: 'dev', MODEL_EGRESS: 'allowlist', DEV_FAKE_MODEL: '0'
  });
  check('开发环境离线自检无阻断项', dev.code === 0, `退出码 ${dev.code}`);
  check('明确告知数据库未检查', dev.out.includes('--offline'));

  // 5. GBK 控制台不能因为打印不出勾就崩掉（Windows 默认就是 GBK）
  const gbk = runPreflight(['--offline'], {
    PYTHONIOENCODING: 'gbk', APP_ENV: 'dev', AUTH_MODE: 'dev', MODEL_EGRESS: 'allowlist'
  });
  check('GBK 控制台下不崩', !gbk.out.includes('Traceback'), gbk.out.slice(-120));
  check('GBK 控制台退回 ASCII 记号', gbk.out.includes('[OK]'));
  check('GBK 控制台退出码仍正确', gbk.code === 0, `退出码 ${gbk.code}`);

  // 6. 真的连一个库：迁移应通过，pgvector 缺失应被指出
  const { PGlite } = await import('@electric-sql/pglite');
  const { PGLiteSocketServer } = await import('@electric-sql/pglite-socket');
  const { pgcrypto } = await import('@electric-sql/pglite/contrib/pgcrypto');
  const db = await PGlite.create({ extensions: { pgcrypto } });
  const server = new PGLiteSocketServer({ db, port: PORT, host: '127.0.0.1' });
  await server.start();

  const url = `postgresql://postgres@127.0.0.1:${PORT}/postgres?sslmode=disable`;
  const applyMigrations = () => new Promise((resolve, reject) => {
    const child = spawn(PYTHON, ['db/migrate.py', '--apply', '--pglite-compat'], {
      cwd: root, env: { ...process.env, PYTHONIOENCODING: 'utf-8', DATABASE_URL: url }, stdio: 'ignore'
    });
    child.on('error', reject);
    child.on('exit', status => (status === 0 ? resolve() : reject(new Error(`迁移失败：${status}`))));
  });

  let withDb;
  try {
    await applyMigrations();
    withDb = await runPreflightAsync(['--url', url], { APP_ENV: 'dev', AUTH_MODE: 'dev' });
  } finally {
    await server.stop();
    await db.close();
  }

  if (process.env.DEBUG_PREFLIGHT) console.log(withDb.out);
  check('连上真库时报出数据库版本', withDb.out.includes('PostgreSQL'), '');
  check('迁移已应用', withDb.out.includes('数据库迁移') && withDb.out.includes('全部应用'));
  check('缺 pgvector 被判阻断而不是忽略', withDb.code === 1 && withDb.out.includes('pgvector'), `退出码 ${withDb.code}`);
  check('说清缺 pgvector 的后果', withDb.out.includes('检索不可用'));
  check('有阻断项时给出处理清单', withDb.out.includes('必须处理才能上线'));
  check('查不了的项目标为无法检查而不是通过', withDb.out.includes('备份可恢复性'));

  console.log();
  if (failures.length) {
    console.log(`失败 ${failures.length} 项：${failures.join(', ')}`);
    process.exit(1);
  }
  console.log('上线自检测试全部通过。');
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
