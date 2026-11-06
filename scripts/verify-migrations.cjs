#!/usr/bin/env node
'use strict';

/**
 * 迁移真引擎验证：用 PGlite（WASM 编译的真实 PostgreSQL）把 db/migrations/*.sql 真的跑一遍。
 *
 * 为什么需要它：
 *   `npm run check` 里的 Python 套件只做静态断言——30 个手写迁移从来没有被真正的
 *   PostgreSQL 解析、编译、执行过。FORCE RLS、CHECK、UNIQUE(section_id, slot_no)、
 *   触发器这些"数据库层"的保证，只有真引擎能证明。PGlite 是 PostgreSQL 的 WASM 版，
 *   @electric-sql/pglite 起一个空库即可；本脚本会打印它实际报告的版本（不是注释里
 *   写的 PG16，实测见输出）。
 *
 * 三个阶段：
 *   一、空库按文件名顺序执行全部迁移，每个迁移一行（序号 + 文件名 + 耗时）。
 *   二、在真库上做行为断言：写夹具数据、造真实 UPDATE/INSERT，看引擎接不接受，
 *       而不是再读一遍 SQL 文件。
 *   三、幂等：按 db/migrate.py 的口径（schema_migration + checksum）重跑第二遍，
 *       应全部跳过且不报错；另把 0027 起"语句级幂等"的新迁移真实重放一次。
 *
 * 已知局限（不藏）：
 *   * PGlite 默认不带 pgvector：0001 的 `CREATE EXTENSION vector` 会 0A000。本脚本打印
 *     扩展名、行号、SQLSTATE，把该迁移标记为「PGlite 无法验证」，再按 db/migrate.py
 *     --pglite-compat 的同一套规则降级继续（vector(N)→jsonb、跳过 hnsw 索引），让后面
 *     的迁移仍被真引擎验证。降级掉的事实（embedding 真实类型、hnsw 索引）在摘要里单独列出，绝不写成"通过"。
 *   * PGlite 是单连接嵌入式 PG，不验证并发与性能。
 *   * 旧迁移（0001–0026）的 SQL 本身不可重复执行（CREATE TYPE/TABLE/ADD COLUMN 都没有
 *     IF NOT EXISTS），第二遍靠 migrate.py 的 schema_migration 跳过而不是靠 SQL 幂等；
 *     脚本会实测一条并如实标为"说明项"。
 *
 * 用法：npm run test:migrations；退出码 0 = 迁移/断言/幂等均符合预期，1 = 有失败。
 * 不需要 DATABASE_URL，不写盘，不修改任何迁移文件。
 */

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const checkReviewEvidence = require('./verify-paper-review-behaviour.cjs'), checkAssignmentPublishFields = require('./verify-assignment-publish-behaviour.cjs'), checkAssemblyRunBehaviour = require('./verify-assembly-run-behaviour.cjs'), checkSlotAndItemRules = require('./verify-slot-and-item-behaviour.cjs'), checkPaperProvenance = require('./verify-paper-provenance-behaviour.cjs'), checkAssemblyJobKind = require('./verify-assembly-job-kind.cjs');  // 迁移行为断言拆成独立文件，本文件已到 max-lines 上限

const root = path.resolve(__dirname, '..');
const MIGRATIONS_DIR = path.join(root, 'db', 'migrations');
const NAME_WIDTH = 30;

// ───────────────────────────── 通用小工具 ─────────────────────────────

const sha256Hex = (text) => crypto.createHash('sha256').update(text, 'utf8').digest('hex');
const hash64 = (seed) => sha256Hex(`verify-migrations:${seed}`);
const elapsedMs = (startedAt) => Number(process.hrtime.bigint() - startedAt) / 1e6;
const formatMs = (value) => `${value.toFixed(1)} ms`;
const countNewlines = (text) => (String(text).match(/\n/g) || []).length;
const pad = (text, width) => String(text).padEnd(width, ' ');

function firstLine(text, limit = 110) {
  const line = String(text || '')
    .split('\n')
    .map((item) => item.trim())
    .find((item) => item.length > 0) || '';
  return line.length > limit ? `${line.slice(0, limit)}…` : line;
}

function describeSqlError(error) {
  const lines = [`SQLSTATE：${error.code || '（引擎未给）'}`, `原始报错：${error.message}`];
  if (error.detail) lines.push(`detail：${error.detail}`);
  if (error.hint) lines.push(`hint：${error.hint}`);
  if (error.constraint) lines.push(`约束：${error.constraint}`);
  return lines;
}

// ─────────────────────────── SQL 语句切分 ───────────────────────────
// 迁移文件里有 DO $$ … $$ 与 plpgsql 函数体，不能按 ';' 硬切。
// 这里只做词法级扫描：行注释、可嵌套块注释、单/双引号、$tag$ 引号。
// 这样每条语句都能带原始起始行号，出错时能指到"第几行"。

function isCommentOnly(sql) {
  const stripped = sql.replace(/--[^\n]*/g, '').replace(/\/\*[\s\S]*?\*\//g, '');
  return stripped.trim() === '';
}

function scanQuoted(source, start, quote) {
  let index = start + 1;
  while (index < source.length) {
    if (source[index] === quote) {
      if (source[index + 1] === quote) {
        index += 2;
        continue;
      }
      return index + 1;
    }
    index += 1;
  }
  return source.length;
}

function scanBlockComment(source, start) {
  let depth = 0;
  let index = start;
  while (index < source.length) {
    if (source.startsWith('/*', index)) {
      depth += 1;
      index += 2;
    } else if (source.startsWith('*/', index)) {
      depth -= 1;
      index += 2;
      if (depth === 0) return index;
    } else {
      index += 1;
    }
  }
  return source.length;
}

function scanDollarQuoted(source, start) {
  const head = /^\$([A-Za-z_][A-Za-z0-9_]*)?\$/.exec(source.slice(start));
  if (!head) return -1;
  const end = source.indexOf(head[0], start + head[0].length);
  return end === -1 ? source.length : end + head[0].length;
}

// 语句前面常有一整块注释（0002 的第一条语句前面就有 3 行）。报错行号要落在 SQL 上，
// 而不是注释上；offset 是第一条非注释字符在语句里的下标，供 errorLine 精确定位。
function leadingTrivia(sql) {
  let index = 0;
  let lines = 0;
  while (index < sql.length) {
    if (/\s/.test(sql[index])) {
      lines += sql[index] === '\n' ? 1 : 0;
      index += 1;
    } else if (sql.startsWith('--', index)) {
      const end = sql.indexOf('\n', index);
      if (end === -1) return { lines, offset: index };
      lines += 1;
      index = end + 1;
    } else if (sql.startsWith('/*', index)) {
      const end = sql.indexOf('*/', index);
      if (end === -1) return { lines, offset: index };
      lines += countNewlines(sql.slice(index, end + 2));
      index = end + 2;
    } else {
      return { lines, offset: index };
    }
  }
  return { lines, offset: index };
}

function splitStatements(source) {
  // 统一换行为 \n：仓库文件是 CRLF，只认 \n 会把 \r 当成语句首字符，行号整体错一位。
  source = source.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  const statements = [];
  let buffer = '';
  let line = 1;
  let startLine = 1;
  let started = false;
  let index = 0;

  const start = () => {
    if (!started) {
      startLine = line;
      started = true;
    }
  };
  const flush = () => {
    const sql = buffer.trim();
    if (sql && !isCommentOnly(sql)) {
      const trivia = leadingTrivia(sql);
      statements.push({ sql, line: startLine + trivia.lines, offset: trivia.offset });
    }
    buffer = '';
    started = false;
  };

  while (index < source.length) {
    const ch = source[index];
    let end = -1;
    if (ch === '\n') {
      buffer += ch;
      line += 1;
      index += 1;
      continue;
    }
    if (ch === '-' && source[index + 1] === '-') {
      start();
      const lineEnd = source.indexOf('\n', index);
      end = lineEnd === -1 ? source.length : lineEnd;
    } else if (ch === '/' && source[index + 1] === '*') {
      start();
      end = scanBlockComment(source, index);
    } else if (ch === "'" || ch === '"') {
      start();
      end = scanQuoted(source, index, ch);
    } else if (ch === '$') {
      end = scanDollarQuoted(source, index);
      if (end > 0) start();
    }
    if (end > 0) {
      const chunk = source.slice(index, end);
      buffer += chunk;
      line += countNewlines(chunk);
      index = end;
      continue;
    }
    if (ch === ';') {
      buffer += ch;
      index += 1;
      flush();
      continue;
    }
    start();
    buffer += ch;
    index += 1;
  }
  flush();
  return statements;
}

function isTransactionBoundary(statement) {
  const normalized = statement.sql.replace(/;\s*$/, '').trim().toUpperCase();
  return normalized === 'BEGIN' || normalized === 'COMMIT' || normalized === 'START TRANSACTION';
}

// ─────────────────────── 迁移文件读取 / 校验和 ───────────────────────

function listMigrationFiles() {
  return fs.readdirSync(MIGRATIONS_DIR)
    .filter((name) => /^\d{4}_.+\.sql$/.test(name))
    .sort();
}

function readMigration(file) {
  return fs.readFileSync(path.join(MIGRATIONS_DIR, file), 'utf8');
}

// 与 db/migrate.py 的 strip_txn 一致：去掉文件自带的 BEGIN;/COMMIT;，事务交给调用方
function stripTxn(text) {
  return text.split(/\r?\n/)
    .filter((line) => {
      const normalized = line.trim().toUpperCase();
      return normalized !== 'BEGIN;' && normalized !== 'COMMIT;';
    })
    .join('\n')
    .trim() + '\n';
}

// 与 db/migrate.py 的 checksum() 同口径：sha256(strip_txn(text)) 的前 16 位
function checksumOf(text) {
  return sha256Hex(stripTxn(text)).slice(0, 16);
}

// ─────────────────────────── 数据库操作 ───────────────────────────

async function rows(db, sql, params) {
  const result = await db.query(sql, params);
  return result.rows;
}

async function first(db, sql, params) {
  const result = await rows(db, sql, params);
  return result[0];
}

async function rollbackQuietly(db) {
  try {
    await db.exec('ROLLBACK');
  } catch (error) {
    // 事务可能已经结束；回滚失败不该盖住真正的报错
  }
}

async function expectSqlError(db, sql, params) {
  try {
    await db.query(sql, params);
  } catch (error) {
    return error;
  }
  throw new Error('预期这条 SQL 会被引擎拒绝，但它执行成功了');
}

function errorLine(run) {
  const statement = run.statement;
  if (!statement) return null;
  const position = Number(run.error.position);
  if (!Number.isFinite(position) || position <= 0) return statement.line;
  const body = statement.sql.slice(statement.offset || 0);
  return statement.line + countNewlines(body.slice(0, position - 1));
}

async function runStatements(db, statements) {
  const executable = statements.filter((statement) => !isTransactionBoundary(statement));
  await db.exec('BEGIN');
  for (let index = 0; index < executable.length; index += 1) {
    try {
      await db.exec(executable[index].sql);
    } catch (error) {
      await rollbackQuietly(db);
      return { ok: false, error, statement: executable[index], executed: index, total: executable.length };
    }
  }
  await db.exec('COMMIT');
  return { ok: true, executed: executable.length, total: executable.length };
}

// ─────────────────── PGlite 缺扩展：照实报 + 按 migrate.py 降级 ───────────────────

function missingExtension(error) {
  const message = String((error && error.message) || '');
  const notAvailable = /extension "([^"]+)" is not available/i.exec(message);
  if (notAvailable) return notAvailable[1];
  const controlFile = /could not open extension control file[^:]*[/\\]([a-z0-9_]+)\.control/i.exec(message);
  return controlFile ? controlFile[1] : null;
}

function degradeStatements(statements, extension) {
  // 只对 vector 有降级规则，且规则与 db/migrate.py 的 pglite_compat() 完全一致。
  // 其他扩展没有通用降级法：宁可停下，也不猜。
  if (extension !== 'vector') return null;
  const lost = new Set();
  const degraded = statements.map((statement) => {
    if (/CREATE\s+EXTENSION/i.test(statement.sql) && /\bvector\b/i.test(statement.sql)) {
      lost.add('扩展 vector 未安装（CREATE EXTENSION vector 被跳过）');
      return { ...statement, sql: '-- [verify] PGlite 未提供 pgvector：跳过 CREATE EXTENSION vector' };
    }
    if (/USING\s+hnsw/i.test(statement.sql)) {
      lost.add('hnsw 向量索引被跳过');
      return { ...statement, sql: '-- [verify] PGlite 未提供 pgvector：跳过 hnsw 索引' };
    }
    if (/\bvector\s*\(\s*\d+\s*\)/i.test(statement.sql)) {
      lost.add('vector(N) 列类型被降级为 jsonb');
      return { ...statement, sql: statement.sql.replace(/vector\s*\(\s*\d+\s*\)/gi, 'jsonb') };
    }
    return statement;
  });
  return { statements: degraded, lost: [...lost] };
}

async function applyMigration(db, file) {
  const statements = splitStatements(readMigration(file));
  const startedAt = process.hrtime.bigint();
  const raw = await runStatements(db, statements);
  if (raw.ok) {
    return { file, status: 'ok', elapsed: elapsedMs(startedAt), executed: raw.executed, total: raw.total };
  }
  const extension = missingExtension(raw.error);
  if (!extension) {
    return { file, status: 'failed', elapsed: elapsedMs(startedAt), run: raw };
  }
  const degraded = degradeStatements(statements, extension);
  const detail = { extension, line: errorLine(raw), error: raw.error };
  if (!degraded) {
    return { file, status: 'blocked', elapsed: elapsedMs(startedAt), run: raw, detail };
  }
  const retry = await runStatements(db, degraded.statements);
  if (!retry.ok) {
    return { file, status: 'failed', elapsed: elapsedMs(startedAt), run: retry, detail: { ...detail, retry: retry.error } };
  }
  return {
    file,
    status: 'degraded',
    elapsed: elapsedMs(startedAt),
    executed: retry.executed,
    total: retry.total,
    detail,
    lost: degraded.lost
  };
}

async function recordApplied(db, file) {
  const digest = checksumOf(readMigration(file));
  await db.query('INSERT INTO schema_migration (version, checksum) VALUES ($1, $2)', [file, digest]);
}

// ─────────────────────────── 输出 ───────────────────────────

function printOutcome(outcome, index, total) {
  const head = `  [${String(index + 1).padStart(2, '0')}/${total}] ${pad(outcome.file, NAME_WIDTH)} ${formatMs(outcome.elapsed).padStart(10)}`;
  if (outcome.status === 'ok') {
    console.log(`${head}  通过（${outcome.executed} 条语句）`);
    return;
  }
  if (outcome.status === 'degraded') {
    const { detail } = outcome;
    console.log(`${head}  通过（PGlite 降级：缺扩展 ${detail.extension}，第 ${detail.line} 行；${outcome.executed} 条语句）`);
    console.log(`          原始报错：${detail.error.message}（SQLSTATE ${detail.error.code}）\n          已按 db/migrate.py --pglite-compat 降级：${outcome.lost.join('、')}\n          标记：PGlite 无法验证（本次只执行了其余语句，不伪造该迁移通过）`);
    return;
  }
  if (outcome.status === 'blocked') {
    const { detail } = outcome;
    console.log(`${head}  阻塞：PGlite 无法验证（缺扩展 ${detail.extension}，第 ${detail.line} 行）`);
    console.log(`          原始报错：${detail.error.message}（SQLSTATE ${detail.error.code}）`);
    console.log(`          本脚本没有 ${detail.extension} 的降级规则；后续迁移依赖不明，已中止。`);
    return;
  }
  const { run } = outcome;
  const line = errorLine(run);
  console.log(`${head}  失败`);
  console.log(`          迁移：${outcome.file}${line ? ` 第 ${line} 行` : ''}（语句 ${run.executed + 1}/${run.total}）`);
  console.log(`          SQL：${firstLine(run.statement && run.statement.sql)}`);
  describeSqlError(run.error).forEach((item) => console.log(`          ${item}`));
}

async function bootstrapMigrationTable(db) {
  await db.exec(`
CREATE TABLE IF NOT EXISTS schema_migration (
  version    text PRIMARY KEY,
  checksum   text NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now()
);`);
}

async function applyPass(db, files, label) {
  console.log(`\n── ${label} ──`);
  const outcomes = [];
  const startedAt = process.hrtime.bigint();
  let stopped = false;
  for (let index = 0; index < files.length; index += 1) {
    const outcome = await applyMigration(db, files[index]);
    outcomes.push(outcome);
    if (outcome.status === 'ok' || outcome.status === 'degraded') {
      await recordApplied(db, files[index]);
    }
    printOutcome(outcome, index, files.length);
    if (outcome.status === 'failed' || outcome.status === 'blocked') {
      stopped = true;
      break;
    }
  }
  const elapsed = elapsedMs(startedAt);
  const passed = outcomes.filter((outcome) => outcome.status === 'ok' || outcome.status === 'degraded');
  const statements = passed.reduce((sum, outcome) => sum + outcome.executed, 0);
  if (stopped) {
    console.log(`  已中止：后续 ${files.length - outcomes.length} 个迁移未执行（它们依赖失败的迁移）。`);
  } else {
    const degraded = passed.filter((outcome) => outcome.status === 'degraded');
    const suffix = degraded.length ? `，其中 ${degraded.length} 个降级执行` : '';
    console.log(`  合计：${passed.length}/${files.length} 个迁移通过${suffix}，共 ${statements} 条语句，耗时 ${formatMs(elapsed)}`);
  }
  return {
    ok: !stopped,
    outcomes,
    passed: passed.length,
    statements,
    elapsed,
    degraded: passed.filter((outcome) => outcome.status === 'degraded'),
    failed: stopped ? outcomes[outcomes.length - 1] : null
  };
}

// ─────────────────────────── 夹具与行为断言 ───────────────────────────

// 夹具用固定 uuid，失败时可以直接照着 id 去查；每行多个只为少占篇幅。
const fixedUuid = (n) => `${String(n).padStart(8, '0')}-0000-4000-8000-000000000000`;

function fixtureStatements() {
  const ids = {
    course: fixedUuid(1), offering: fixedUuid(2), releaseContext: fixedUuid(3), blueprint: fixedUuid(4),
    blueprintVersion: fixedUuid(5), candidateSnapshot: fixedUuid(6), paperApproved: fixedUuid(7),
    paperApprovedGroup: fixedUuid(8), paperDraft: fixedUuid(9), paperDraftGroup: fixedUuid(10),
    section: fixedUuid(11), slot1: fixedUuid(12), slot2: fixedUuid(13), policy: fixedUuid(14),
    assignment: fixedUuid(15), assignmentItem: fixedUuid(16), changeSet: fixedUuid(17),
    actor: fixedUuid(18), sourceRelease: fixedUuid(19), exerciseVersion: fixedUuid(20)
  };
  const hash = (seed) => `'${hash64(seed)}'`;
  const statements = [
    `INSERT INTO assessment.paper_blueprint_version
       (id, blueprint_id, version_no, course_id, release_context_id, purpose, assembly_mode, total_score, duration_seconds, blueprint_ast, content_hash, change_set_id, created_by)
     VALUES ('${ids.blueprintVersion}', '${ids.blueprint}', 1, '${ids.course}', '${ids.releaseContext}', 'formal', 'manual', 100.00, 5400, '{"kind":"verify-migrations"}'::jsonb, ${hash('blueprint')}, '${ids.changeSet}', '${ids.actor}')`,
    `INSERT INTO assessment.candidate_snapshot (id, blueprint_version_id, candidate_manifest, candidate_manifest_hash, created_by)
     VALUES ('${ids.candidateSnapshot}', '${ids.blueprintVersion}', '{"items":[]}'::jsonb, ${hash('snapshot')}, '${ids.actor}')`,
    `INSERT INTO assessment.paper_version
       (id, paper_id, version_no, blueprint_version_id, candidate_snapshot_id, course_id, offering_id, release_context_id, paper_kind, total_score, duration_seconds, status, manifest_hash, submitted_manifest_hash, review_manifest_hash, created_by, approved_at)
     VALUES ('${ids.paperApproved}', '${ids.paperApprovedGroup}', 1, '${ids.blueprintVersion}', '${ids.candidateSnapshot}', '${ids.course}', '${ids.offering}', '${ids.releaseContext}', 'formal', 100.00, 5400, 'approved', ${hash('approved')}, ${hash('approved')}, ${hash('approved')}, '${ids.actor}', now())`,
    `INSERT INTO assessment.paper_version
       (id, paper_id, version_no, blueprint_version_id, candidate_snapshot_id, course_id, offering_id, release_context_id, paper_kind, total_score, duration_seconds, status, manifest_hash, created_by)
     VALUES ('${ids.paperDraft}', '${ids.paperDraftGroup}', 1, '${ids.blueprintVersion}', '${ids.candidateSnapshot}', '${ids.course}', '${ids.offering}', '${ids.releaseContext}', 'formal', 50.00, 5400, 'draft', ${hash('draft')}, '${ids.actor}')`,
    `INSERT INTO assessment.paper_section (id, paper_version_id, code, title, instruction, sort_order, section_score, content_hash)
     VALUES ('${ids.section}', '${ids.paperDraft}', 'A', 'A 卷', '验证用分节', 1, 50.00, ${hash('section')})`,
    `INSERT INTO assessment.paper_slot (id, section_id, code, slot_no, score, content_hash, release_context_id)
     VALUES ('${ids.slot1}', '${ids.section}', 'A1', 1, 10.00, ${hash('slot1')}, '${ids.releaseContext}'), ('${ids.slot2}', '${ids.section}', 'A2', 2, 10.00, ${hash('slot2')}, '${ids.releaseContext}')`,
    `INSERT INTO assessment.attempt_policy_version
       (id, policy_id, version_no, policy_kind, content_hash, max_attempts, behavior, late_policy, show_syntax_errors, show_math_interpretation, show_input_preview, correctness_release, score_release, hint_release, solution_release, answer_release, allow_item_check, allow_retry, allow_flag, allow_review, allow_upload)
     VALUES ('${ids.policy}', '${ids.policy}', 1, 'practice', ${hash('policy')}, NULL, 'deferred', 'reject', true, true, true, 'immediate', 'immediate', 'none', 'none', 'none', true, true, true, true, true)`,
    `INSERT INTO assessment.assignment
       (id, course_id, offering_id, paper_version_id, attempt_policy_version_id, release_context_id, visible_at, due_at, hard_close_at, distribution_kind, status, created_by)
     VALUES ('${ids.assignment}', '${ids.course}', '${ids.offering}', '${ids.paperDraft}', '${ids.policy}', '${ids.releaseContext}', '2026-01-05T00:00:00Z'::timestamptz, '2026-01-06T00:00:00Z'::timestamptz, '2026-01-07T00:00:00Z'::timestamptz, 'print', 'scheduled', '${ids.actor}')`,
    `INSERT INTO assessment.assignment_item
       (id, assignment_id, paper_slot_id, ord, release_context_id, source_release_id, exercise_version_id, score, content_hash)
     VALUES ('${ids.assignmentItem}', '${ids.assignment}', '${ids.slot1}', 1, '${ids.releaseContext}', '${ids.sourceRelease}', '${ids.exerciseVersion}', 10.00, ${hash('assignment-item')})`
  ];
  return { ids, statements };
}

async function writeFixtures(db) {
  const { ids, statements } = fixtureStatements();
  await db.exec('BEGIN');
  for (const statement of statements) {
    try {
      await db.exec(statement);
    } catch (error) {
      await rollbackQuietly(db);
      throw new Error(`夹具写入失败：${firstLine(statement, 70)} → SQLSTATE ${error.code} / ${error.message}`);
    }
  }
  await db.exec('COMMIT');
  return { ...ids, count: statements.length };
}

async function checkApprovedImmutable(db, ids) {
  const sql = 'UPDATE assessment.paper_version SET total_score = total_score + 1 WHERE id = $1';
  const error = await expectSqlError(db, sql, [ids.paperApproved]);
  assert.equal(error.code, 'P0001', `approved 卷的 UPDATE 应被触发器以 P0001 拒绝，实际 SQLSTATE ${error.code}`);
  assert.ok(/approved paper is immutable/i.test(error.message), `报错应含 approved paper is immutable，实际：${error.message}`);
  const row = await first(db, 'SELECT total_score::text AS text FROM assessment.paper_version WHERE id = $1', [ids.paperApproved]);
  assert.equal(row.text, '100.00', `被拒绝的 UPDATE 不应改到分数，实际 ${row.text}`);
  return `UPDATE 被拒：SQLSTATE ${error.code} / ${error.message}；分数仍是 ${row.text}`;
}

async function checkNegativeSlotNo(db, ids) {
  await db.query('UPDATE assessment.paper_slot SET slot_no = $2 WHERE id = $1', [ids.slot1, -1]);
  await db.query('UPDATE assessment.paper_slot SET slot_no = $2 WHERE id = $1', [ids.slot2, -2]);
  const slotRows = await rows(db, 'SELECT id, slot_no FROM assessment.paper_slot WHERE id IN ($1, $2) ORDER BY slot_no', [ids.slot1, ids.slot2]);
  assert.equal(slotRows.length, 2, '两个槽位都应存在');
  assert.deepEqual(slotRows.map((row) => row.slot_no), [-2, -1], '两个槽位都应持有负数 slot_no');
  return 'slot_no 1→-1、2→-2 均被引擎接受（没有 CHECK；两阶段调序的前提成立）';
}

async function checkNumericScale(db, ids) {
  const expected = [['paper_blueprint_version', 'total_score'], ['paper_version', 'total_score'],
    ['paper_section', 'section_score'], ['paper_slot', 'score'], ['assignment_item', 'score'],
    ['paper_edit_event', 'previous_score'], ['paper_edit_event', 'new_score']];
  const numericColumns = await rows(db, `
    SELECT c.relname AS tbl, a.attname AS col, format_type(a.atttypid, a.atttypmod) AS type
      FROM pg_attribute a
      JOIN pg_class c ON c.oid = a.attrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'assessment' AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
       AND a.atttypid = 'numeric'::regtype
     ORDER BY 1, 2`);
  for (const [tbl, col] of expected) {
    const found = numericColumns.filter((row) => row.tbl === tbl && row.col === col);
    assert.equal(found.length, 1, `assessment.${tbl}.${col} 不存在或不是 numeric 列`);
    assert.equal(found[0].type, 'numeric(8,2)', `assessment.${tbl}.${col} 实际声明为 ${found[0].type}`);
  }
  const unexpected = numericColumns
    .filter((row) => !expected.some(([tbl, col]) => tbl === row.tbl && col === row.col))
    .map((row) => `${row.tbl}.${row.col} ${row.type}`);
  assert.deepEqual(unexpected, ['attempt_policy_version.practice_penalty numeric(5,2)'],
    `assessment 里出现了未预期的 numeric 列：${unexpected.join('、') || '（空）'}`);

  const overflow = await expectSqlError(db, 'UPDATE assessment.paper_slot SET score = 1000000.00 WHERE id = $1', [ids.slot1]);
  assert.equal(overflow.code, '22003', `1000000.00 应数值溢出（22003），实际 SQLSTATE ${overflow.code}`);
  await db.query('UPDATE assessment.paper_slot SET score = 999999.99 WHERE id = $1', [ids.slot1]);
  const max = await first(db, 'SELECT score::text AS text FROM assessment.paper_slot WHERE id = $1', [ids.slot1]);
  assert.equal(max.text, '999999.99', `999999.99 应原样存下（上限 6 位整数），实际 ${max.text}`);
  await db.query('UPDATE assessment.paper_slot SET score = 1.005 WHERE id = $1', [ids.slot1]);
  const rounded = await first(db, 'SELECT score::text AS text FROM assessment.paper_slot WHERE id = $1', [ids.slot1]);
  assert.equal(rounded.text, '1.01', `1.005 应按 scale=2 四舍五入为 1.01，实际 ${rounded.text}`);
  await db.query('UPDATE assessment.paper_slot SET score = 10.00 WHERE id = $1', [ids.slot1]);
  return `7 个 numeric(8,2) 列确认；1000000.00 → 22003、999999.99 接受、1.005 → ${rounded.text}`;
}

async function setupRlsRole(db) {
  await db.exec('CREATE ROLE verify_rls NOLOGIN');
  await db.exec('GRANT USAGE ON SCHEMA assessment TO verify_rls');
  await db.exec('GRANT SELECT ON assessment.paper_version TO verify_rls');
}

async function checkRowLevelSecurity(db, ids) {
  const tables = ['paper_version', 'paper_section', 'paper_slot', 'paper_render'];
  const flags = await rows(db, `
    SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'assessment' AND c.relname = ANY ($1)`, [tables]);
  assert.equal(flags.length, 4, `应查到 4 张表，实际 ${flags.length}`);
  for (const row of flags) {
    assert.equal(row.relrowsecurity, true, `assessment.${row.relname} 的 relrowsecurity 不是 true`);
    assert.equal(row.relforcerowsecurity, true, `assessment.${row.relname} 的 relforcerowsecurity 不是 true`);
  }
  const policies = await rows(db, `
    SELECT tablename, count(*)::int AS total FROM pg_policies
     WHERE schemaname = 'assessment' AND tablename = ANY ($1) GROUP BY tablename`, [tables]);
  assert.equal(policies.length, 4, `4 张表都应有策略，实际只有 ${policies.map((row) => row.tablename).join('、')}`);

  await db.exec('BEGIN');
  let hidden;
  let visible;
  try {
    await setupRlsRole(db);
    await db.exec('SET LOCAL ROLE verify_rls');
    hidden = await first(db, 'SELECT count(*)::int AS total FROM assessment.paper_version');
    await db.exec("SET LOCAL app.assessment_service = 'on'");
    visible = await first(db, 'SELECT count(*)::int AS total FROM assessment.paper_version');
  } finally {
    await rollbackQuietly(db);
  }
  assert.equal(hidden.total, 0, `非超级用户未设作用域时应读到 0 行，实际 ${hidden.total}`);
  assert.ok(visible.total >= 1, `设 app.assessment_service=on 后应能读到夹具行，实际 ${visible.total}`);
  return `4 表 relrowsecurity/relforcerowsecurity 均 true；非超级用户 role 未设作用域读到 ${hidden.total} 行、设 on 后读到 ${visible.total} 行`;
}

async function checkAssignmentTimeline(db, ids) {
  const insertSql = `
    INSERT INTO assessment.assignment
      (id, course_id, offering_id, paper_version_id, attempt_policy_version_id, release_context_id,
       visible_at, due_at, hard_close_at, distribution_kind, status, created_by)
    VALUES ($1, $2, $3, $4, $5, $6, $7::timestamptz, $8::timestamptz, $9::timestamptz, 'print', 'scheduled', $10)`;
  const base = [ids.course, ids.offering, ids.paperDraft, ids.policy, ids.releaseContext];
  await db.query(insertSql, ['1b1b1b1b-1b1b-4b1b-8b1b-1b1b1b1b1b1b', ...base,
    '2026-02-01T00:00:00Z', '2026-02-02T00:00:00Z', '2026-02-03T00:00:00Z', ids.actor]);
  const visibleAfterDue = await expectSqlError(db, insertSql, ['1c1c1c1c-1c1c-4c1c-8c1c-1c1c1c1c1c1c', ...base,
    '2026-02-05T00:00:00Z', '2026-02-02T00:00:00Z', '2026-02-03T00:00:00Z', ids.actor]);
  assert.equal(visibleAfterDue.code, '23514', `visible_at > due_at 应触发 23514，实际 SQLSTATE ${visibleAfterDue.code}`);
  assert.ok(/check constraint/i.test(visibleAfterDue.message), `报错应提到 CHECK 约束，实际：${visibleAfterDue.message}`);
  assert.ok(/assignment/.test(String(visibleAfterDue.constraint || '')), `约束名应属于 assessment.assignment，实际 ${visibleAfterDue.constraint}`);
  const dueAfterClose = await expectSqlError(db, insertSql, ['1d1d1d1d-1d1d-4d1d-8d1d-1d1d1d1d1d1d', ...base,
    '2026-02-01T00:00:00Z', '2026-02-06T00:00:00Z', '2026-02-03T00:00:00Z', ids.actor]);
  assert.equal(dueAfterClose.code, '23514', `due_at > hard_close_at 应触发 23514，实际 SQLSTATE ${dueAfterClose.code}`);
  return `合法插入接受；倒挂两种（visible>due、due>hard_close）都被 23514 拒绝（约束 ${visibleAfterDue.constraint}）`;
}

async function checkDraftEditable(db, ids) {
  await db.query('UPDATE assessment.paper_version SET total_score = 101.00 WHERE id = $1', [ids.paperDraft]);
  const row = await first(db, 'SELECT total_score::text AS text FROM assessment.paper_version WHERE id = $1', [ids.paperDraft]);
  assert.equal(row.text, '101.00', `draft 卷应可改分，实际 ${row.text}`);
  return 'draft 卷改分成功（101.00）：触发器只挡 approved/withdrawn，没有误伤草稿';
}

function buildChecks(ids) {
  return [
    { title: 'approved 卷的 UPDATE 被触发器拒绝（approved paper is immutable）', run: (db) => checkApprovedImmutable(db, ids) },
    { title: 'paper_slot.slot_no 允许更新成负数（两阶段调序前提）', run: (db) => checkNegativeSlotNo(db, ids) },
    { title: 'assessment 各表 numeric(8,2) 真是 8 位精度 2 位小数', run: (db) => checkNumericScale(db, ids) },
    { title: 'paper_version/section/slot/render 四表 relrowsecurity + relforcerowsecurity 均真', run: (db) => checkRowLevelSecurity(db, ids) },
    { title: 'assessment.assignment 的 visible_at <= due_at <= hard_close_at 拒绝倒挂', run: (db) => checkAssignmentTimeline(db, ids) },
    { title: 'UNIQUE(section_id, slot_no) 与 assignment_item 只追加都由引擎把关', run: (db) => checkSlotAndItemRules(db, ids, expectSqlError, hash64) },
    { title: 'draft 卷仍可改（反向对照，证明触发器没挡过宽）', run: (db) => checkDraftEditable(db, ids) },
    { title: '0032 复核证据：逐人唯一 + 贡献者不能自批 + append-only', run: (db) => checkReviewEvidence(db, ids, expectSqlError) },
    { title: '0033 发布痕迹：已发布必须有 published_at，撤回必须有理由', run: (db) => checkAssignmentPublishFields(db, ids, expectSqlError) },
    { title: '0034 求解运行：无解不许带方案 + 最多 3 个 + 组卷来源成对', run: (db) => checkAssemblyRunBehaviour(db, ids, expectSqlError) },
    { title: '0034 落卷来源：组卷成对、手工两列仍空、单边补写被拒', run: (db) => checkPaperProvenance(db, ids) },
    { title: '0035 求解可以入队：paper-assembly 是合法 kind 且 worker 会领', run: (db) => checkAssemblyJobKind(db) }
  ];
}

async function runBehaviourChecks(db, report) {
  console.log('\n── 二、真实行为断言（在真库里写数据、造 UPDATE/INSERT，不是读文件） ──');
  let ids;
  try {
    ids = await writeFixtures(db);
  } catch (error) {
    console.log(`  FAIL  夹具写入失败，后续断言无法进行`);
    console.log(`        ${error.message}`);
    report.checks.push({ ok: false, title: '夹具写入', note: error.message });
    return;
  }
  console.log(`  夹具：向 assessment.* 写入 ${ids.count} 条真实数据（蓝图/快照/两个卷版本/分节/槽位/策略/作业/作业项）`);
  for (const check of buildChecks(ids)) {
    let result;
    try {
      result = { ok: true, title: check.title, note: await check.run(db) };
    } catch (error) {
      result = { ok: false, title: check.title, note: error.message };
    }
    report.checks.push(result);
    const index = String(report.checks.length).padStart(2, '0');
    console.log(`  ${index}. ${result.ok ? 'PASS' : 'FAIL'}  ${result.title}`);
    if (result.ok) {
      console.log(`        ${result.note}`);
    } else {
      console.log(`        失败原因：${result.note}`);
    }
  }
}

// ─────────────────────────── 幂等 ───────────────────────────

async function secondPass(db, files) {
  const startedAt = process.hrtime.bigint();
  const recorded = await rows(db, 'SELECT version, checksum FROM schema_migration ORDER BY version');
  const applied = new Map(recorded.map((row) => [row.version, row.checksum]));
  const mismatched = [];
  for (let index = 0; index < files.length; index += 1) {
    const started = process.hrtime.bigint();
    const skip = applied.get(files[index]) === checksumOf(readMigration(files[index]));
    if (!skip) mismatched.push(files[index]);
    const head = `  [${String(index + 1).padStart(2, '0')}/${files.length}] ${pad(files[index], NAME_WIDTH)} ${formatMs(elapsedMs(started)).padStart(10)}`;
    console.log(`${head}  ${skip ? '跳过（已应用，checksum 一致）' : '未记录或 checksum 不一致'}`);
  }
  const total = await first(db, 'SELECT count(*)::int AS total FROM schema_migration');
  return {
    ok: mismatched.length === 0 && total.total === files.length,
    elapsed: elapsedMs(startedAt),
    skipped: files.length - mismatched.length,
    mismatched,
    recorded: total.total,
    expected: files.length
  };
}

// 0027 起的迁移在注释里声明"全部语句幂等"，这里真的把 SQL 重放一次（不是读文件）
function replayableFiles(files) {
  return files.filter((file) => file.slice(0, 4) >= '0027');
}

async function replayNewerMigrations(db, files) {
  const targets = replayableFiles(files);
  const results = [];
  for (const file of targets) {
    const startedAt = process.hrtime.bigint();
    const run = await runStatements(db, splitStatements(readMigration(file)));
    const ok = run.ok;
    const head = `  ${pad(file, NAME_WIDTH)} ${formatMs(elapsedMs(startedAt)).padStart(10)}`;
    const suffix = ok
      ? `重复执行通过（${run.executed} 条语句，无报错）`
      : `重复执行失败：第 ${errorLine(run)} 行 SQLSTATE ${run.error.code} / ${run.error.message}`;
    console.log(`${head}  ${suffix}`);
    results.push({ file, ok, run });
  }
  return results;
}

// 0001–0026 的旧迁移没有 IF NOT EXISTS：第二遍注定报"已存在"。实测一条并说明，
// 这不是失败项，因为 migrate.py 用 schema_migration + checksum 拦截重复执行。
async function diagnoseLegacyReplay(db) {
  const file = '0002_learning_jobs.sql';
  const run = await runStatements(db, splitStatements(readMigration(file)));
  if (run.ok) {
    console.log(`  诊断（说明项）：${file} 第二遍竟然也通过了（旧迁移比预期更幂等）`);
    return { file, ok: true };
  }
  console.log(`  诊断（说明项，不计入通过/失败）：${file} 第二遍 → 第 ${errorLine(run)} 行 SQLSTATE ${run.error.code} / ${run.error.message}\n`
    + '        0001–0026 是"只跑一次"的写法（CREATE TYPE/TABLE/ADD COLUMN 都没有 IF NOT EXISTS）；\n'
    + '        重复执行由 db/migrate.py 的 schema_migration + checksum 拦截，而不是靠 SQL 自身幂等。');
  return { file, ok: false, code: run.error.code, message: run.error.message, line: errorLine(run) };
}

async function runIdempotencyChecks(db, files, report) {
  console.log('\n── 三、幂等：第二遍执行同一批迁移 ──');
  console.log('  （口径 = db/migrate.py：schema_migration 记录 version + checksum，已应用则跳过）');
  report.second = await secondPass(db, files);
  if (!report.second.ok) {
    console.log(`  第二遍异常：${report.second.recorded}/${report.second.expected} 条记录；未跳过：${report.second.mismatched.join('、') || '无'}`);
  }
  console.log('  SQL 级重放（0027 起的新迁移自称语句幂等，这里真的再执行一遍）：');
  report.replays = await replayNewerMigrations(db, files);
  report.diagnostic = await diagnoseLegacyReplay(db);
}

// ─────────────────────────── 摘要 ───────────────────────────

function printSummary(report, ok) {
  const { pass1, checks, second, replays, diagnostic } = report;
  const checksPassed = checks.filter((check) => check.ok).length;
  const replaysPassed = replays.filter((item) => item.ok).length;
  const unverified = pass1 ? pass1.degraded : [];
  console.log('\n════════════════════════════ 摘要 ════════════════════════════');
  const pass1Text = pass1
    ? `${pass1.passed}/${report.files.length} 个（失败 ${pass1.failed ? 1 : 0}，PGlite 无法验证 ${unverified.length}）`
    : '未执行';
  console.log(
    `引擎        ${report.engine}\n`
    + `迁移        ${pass1Text}\n`
    + `语句        ${pass1 ? pass1.statements : 0} 条（第一遍实际执行）\n`
    + `断言        ${checks.length} 项，通过 ${checksPassed}，失败 ${checks.length - checksPassed}`
  );
  if (second) {
    console.log(
      `幂等        第二遍 ${second.skipped}/${report.files.length} 跳过，schema_migration ${second.recorded} 行（${second.ok ? '无报错' : '有异常'}）\n`
      + `SQL 级重放   ${replaysPassed}/${replays.length} 个新迁移（0027 起）重复执行通过\n`
      + `旧迁移诊断   ${diagnostic.file} 第二遍 ${diagnostic.ok ? '通过' : `被 ${diagnostic.code} 拦下（预期，靠 schema_migration 跳过）`}`
    );
  }
  console.log(`耗时        ${formatMs(report.totalElapsed)}（第一遍 ${pass1 ? formatMs(pass1.elapsed) : '—'}，断言 ${formatMs(report.checksElapsed)}，幂等 ${formatMs(report.idempotencyElapsed)}）`);
  console.log(`退出码      ${ok ? 0 : 1}`);

  if (unverified.length) {
    console.log('\n── PGlite 无法验证的迁移（不伪造通过） ──');
    for (const outcome of unverified) {
      console.log(`  ${outcome.file}：缺扩展 ${outcome.detail.extension}（第 ${outcome.detail.line} 行，SQLSTATE ${outcome.detail.error.code}）\n`
        + `    ${outcome.detail.error.message}\n`
        + `    已按 migrate.py --pglite-compat 降级继续：${outcome.lost.join('、')}\n`
        + '    因此以下事实仍未经真引擎确认：embedding 列的真实类型是 vector(1024)、hnsw 索引可用、pgvector 扩展可用。');
    }
  }
  if (!ok) {
    console.log('\n失败项：');
    if (pass1 && pass1.failed) {
      const { failed } = pass1;
      const line = failed.run ? errorLine(failed.run) : failed.detail && failed.detail.line;
      console.log(`  - 迁移 ${failed.file}${line ? ` 第 ${line} 行` : ''}（${failed.status}）`);
    }
    checks.filter((check) => !check.ok).forEach((check) => console.log(`  - 断言「${check.title}」：${check.note}`));
    if (second && !second.ok) console.log(`  - 幂等第二遍：未跳过 ${second.mismatched.join('、') || '记录数不符'}`);
    replays.filter((item) => !item.ok).forEach((item) => console.log(`  - SQL 级重放 ${item.file}：SQLSTATE ${item.run.error.code} / ${item.run.error.message}`));
  }
  console.log('══════════════════════════════════════════════════════════════');
}

// ─────────────────────────── 主流程 ───────────────────────────

async function main() {
  const files = listMigrationFiles();
  if (files.length === 0) {
    console.error('db/migrations 下没有 NNNN_*.sql 迁移文件');
    process.exit(1);
  }
  const { PGlite } = await import('@electric-sql/pglite');
  const { pgcrypto } = await import('@electric-sql/pglite/contrib/pgcrypto');
  const db = await PGlite.create({ extensions: { pgcrypto } });
  const startedAt = process.hrtime.bigint();
  const versionRow = await first(db, 'SELECT version() AS server_version');
  const report = {
    files,
    engine: String(versionRow.server_version).split(' on ')[0],
    pass1: null,
    checks: [],
    second: null,
    replays: [],
    diagnostic: null,
    checksElapsed: 0,
    idempotencyElapsed: 0,
    totalElapsed: 0
  };
  try {
    console.log('迁移真引擎验证（PGlite：WASM 编译的真实 PostgreSQL，不是静态分析）');
    console.log(`引擎：${report.engine}\n迁移：db/migrations 下 ${files.length} 个文件，按文件名顺序执行`);
    console.log('说明：0001 需要 pgvector；PGlite 未提供时按「无法验证」处理并降级继续，见摘要。');
    await bootstrapMigrationTable(db);
    report.pass1 = await applyPass(db, files, '一、空库按文件名顺序执行全部迁移');
    if (!report.pass1.ok) {
      report.totalElapsed = elapsedMs(startedAt);
      printSummary(report, false);
      process.exit(1);
    }
    const checksStartedAt = process.hrtime.bigint();
    await runBehaviourChecks(db, report);
    report.checksElapsed = elapsedMs(checksStartedAt);
    const idempotencyStartedAt = process.hrtime.bigint();
    await runIdempotencyChecks(db, files, report);
    report.idempotencyElapsed = elapsedMs(idempotencyStartedAt);
  } finally {
    await db.close();
  }
  report.totalElapsed = elapsedMs(startedAt);
  const checksOk = report.checks.length > 0 && report.checks.every((check) => check.ok);
  const secondOk = Boolean(report.second && report.second.ok);
  const replaysOk = report.replays.every((item) => item.ok);
  printSummary(report, checksOk && secondOk && replaysOk);
  process.exit(checksOk && secondOk && replaysOk ? 0 : 1);
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
