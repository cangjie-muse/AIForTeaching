'use strict';

// 0035 的真实行为断言：pipeline_job 认 paper-assembly，且 worker 的领取白名单
// （generation.claim_next）真的包含它。
//
// 为什么要在数据库层再钉一次：`kind` 的 CHECK 与 worker 白名单是**两处独立的名单**。
// 只改一处的结果是"任务能入队但永远没人领"——比直接报错更难发现，因为队列看起来
// 正常（有 queued 行），只是进度永远停在 0。
const assert = require('node:assert/strict');
const crypto = require('node:crypto');

const sha = (seed) => crypto.createHash('sha256').update(String(seed)).digest('hex');

module.exports = async function checkAssemblyJobKind(db) {
  // 1) paper-assembly 必须是合法 kind（0035 的 CHECK 真的扩了）。
  const inserted = await db.query(
    `INSERT INTO pipeline_job (kind, payload, requested_by)
     VALUES ('paper-assembly', '{"assembly_run_id":"x"}'::jsonb, 'verify')
     RETURNING id, kind, status`,
  );
  const row = inserted.rows[0];
  assert.equal(row.kind, 'paper-assembly');
  assert.equal(row.status, 'queued', '新任务必须是 queued，worker 才领得到');

  // 2) 未知 kind 仍然被 CHECK 拒绝（没有把约束放宽成"任意字符串"）。
  const bogus = await db.query(
    "INSERT INTO pipeline_job (kind, payload) VALUES ('paper-assembly-v2', '{}'::jsonb)",
  ).then(() => null).catch((error) => error);
  assert.ok(bogus, '未知 kind 必须被 CHECK 拒绝');
  assert.equal(bogus.code, '23514', `未知 kind 应触发 23514，实际 ${bogus.code}`);

  // 3) worker 白名单必须同时包含 paper-render 与 paper-assembly：静态读源码，
  //    因为这一段是 SQL 字符串拼出来的，数据库无法替我们验证。
  const source = require('node:fs').readFileSync(
    require('node:path').join(__dirname, '..', 'server', 'app', 'generation.py'),
    'utf8');
  const match = source.match(/kind IN \(([\s\S]*?)\)/);
  assert.ok(match, '没能从 generation.py 里找到 claim_next 的 kind 白名单');
  const whitelist = match[1].split(',').map((item) => item.trim().replace(/'/g, ''));
  for (const kind of ['paper-render', 'paper-assembly']) {
    assert.ok(whitelist.includes(kind), `worker 白名单缺少 ${kind}`);
  }

  // 4) 迁移文件本身必须是幂等的写法（先 DROP IF EXISTS 再加），否则第二遍会炸。
  const migration = require('node:fs').readFileSync(
    require('node:path').join(__dirname, '..', 'db', 'migrations', '0035_assembly_job_kind.sql'),
    'utf8');
  assert.ok(/DROP CONSTRAINT IF EXISTS pipeline_job_kind_check/.test(migration),
    '0035 必须用 DROP CONSTRAINT IF EXISTS 保证可重复执行');

  await db.query('DELETE FROM pipeline_job WHERE id = $1', [row.id]);
  return `paper-assembly 可入队（status=queued）、未知 kind 被 23514 拒绝、`
    + `worker 白名单含 ${whitelist.length} 项（含 paper-render 与 paper-assembly）、`
    + `迁移写法幂等 ${sha('ok').slice(0, 8)}`;
};
