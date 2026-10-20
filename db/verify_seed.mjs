// 用 PGlite（WASM PostgreSQL 16）真实执行 db/schema.sql 与 db/seed.sql。
// 目的：验证 DDL 语法、外键插入顺序、以及现有数据能否原样入库。
// 局限：PGlite 没有 pgvector，所以向量列降级为 jsonb、hnsw 索引跳过。
//       这里验证的是"结构与数据"，不是"向量检索"。

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { PGlite } from '@electric-sql/pglite';

const root = fileURLToPath(new URL('..', import.meta.url));
const schemaRaw = readFileSync(`${root}/db/schema.sql`, 'utf8');
const seedRaw = readFileSync(`${root}/db/seed.sql`, 'utf8');

const schema = schemaRaw
  .replace(/CREATE EXTENSION IF NOT EXISTS pgcrypto;/g, '')
  .replace(/CREATE EXTENSION IF NOT EXISTS vector;[^\n]*/g, '')
  .replace(/vector\(1024\)/g, 'jsonb')
  .replace(/CREATE INDEX textbook_chunk_embedding_idx[\s\S]*?WHERE embedding IS NOT NULL;/g, '');

if (/vector\(/.test(schema)) {
  console.error('仍有未降级的 vector 类型');
  process.exit(1);
}

const db = new PGlite();
console.log('执行 schema.sql …');
await db.exec(schema);
console.log('schema 执行成功');

console.log('执行 seed.sql（约 12 MB）…');
const t0 = Date.now();
await db.exec(seedRaw);
console.log(`seed 执行成功，耗时 ${((Date.now() - t0) / 1000).toFixed(1)}s`);

const checks = [
  ['course', 14],
  ['chapter', 173],
  ['knowledge_node', 4485],
  ['exercise', 4807],
  ['exercise_node', 5287],
  ['course_asset', 169],
];

let failed = 0;
for (const [table, expected] of checks) {
  const { rows } = await db.query(`SELECT count(*)::int AS n FROM ${table}`);
  const actual = rows[0].n;
  const ok = actual === expected;
  if (!ok) failed += 1;
  console.log(`  ${ok ? '✓' : '✗'} ${table.padEnd(16)} ${String(actual).padStart(5)} / 期望 ${expected}`);
}

// 抽查三件事：复合主键真的隔离了跨课程重名、视图能算、溯源列有值
const { rows: dup } = await db.query(
  "SELECT count(*)::int AS n FROM knowledge_node WHERE id = 'group-ch01'"
);
console.log(`  跨课程同名节点 group-ch01 独立行数：${dup[0].n}（应 >1，证明复合主键生效）`);

const { rows: view } = await db.query('SELECT count(*)::int AS n FROM v_contains');
console.log(`  v_contains 视图行数：${view[0].n}`);

const { rows: prov } = await db.query(
  `SELECT provenance, count(*)::int AS n FROM exercise GROUP BY provenance ORDER BY n DESC`
);
console.log('  题目来源分布：', prov.map((r) => `${r.provenance}=${r.n}`).join(', '));

const { rows: pages } = await db.query(
  'SELECT count(*)::int AS n FROM exercise WHERE source_page IS NOT NULL'
);
console.log(`  解析出精确书页的题目：${pages[0].n}`);

const { rows: status } = await db.query(
  `SELECT review_status, count(*)::int AS n FROM exercise GROUP BY review_status`
);
console.log('  审核状态分布：', status.map((r) => `${r.review_status}=${r.n}`).join(', '));

// ---------------------------------------------------------------------------
// 审核模型的三条硬约束：必须真的挡住，而不能只是写在注释里
// ---------------------------------------------------------------------------
console.log('\n审核模型约束检查：');

function report(name, ok, detail = '') {
  if (!ok) failed += 1;
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
}

// 1. 三档可信等级：无审核时全部应为 none
const { rows: tiers } = await db.query(
  `SELECT tier, count(*)::int AS n FROM v_exercise_verification GROUP BY tier`
);
report(
  '未审核题库全部为 none',
  tiers.length === 1 && tiers[0].tier === 'none' && tiers[0].n === 4807,
  tiers.map((r) => `${r.tier}=${r.n}`).join(', ')
);

// 2. Lean 编译通过但形式化未经人工确认时，不得算作已验证
await db.query(
  `INSERT INTO lean_proof (course_id, exercise_id, statement_lean, compiled, faithful_status)
   VALUES ('analysis', 'CJX-I-XLSX-r234', 'theorem ...', true, 'draft')`
);
let { rows: t2 } = await db.query(
  `SELECT tier FROM v_exercise_verification WHERE course_id='analysis' AND id='CJX-I-XLSX-r234'`
);
report('Lean 已编译但忠实性未确认 → tier 仍为 none', t2[0].tier === 'none', t2[0].tier);

// 3. 形式化经教师确认后，才算 lean
await db.query(
  `UPDATE lean_proof SET faithful_status='teacher-reviewed'
   WHERE course_id='analysis' AND exercise_id='CJX-I-XLSX-r234'`
);
({ rows: t2 } = await db.query(
  `SELECT tier FROM v_exercise_verification WHERE course_id='analysis' AND id='CJX-I-XLSX-r234'`
));
report('形式化经确认 → tier=lean', t2[0].tier === 'lean', t2[0].tier);

// 4. 再叠人工审核 → strongest
await db.query(
  `UPDATE exercise SET human_verified_at = now(), human_verified_by = '审查员A'
   WHERE course_id='analysis' AND id='CJX-I-XLSX-r234'`
);
({ rows: t2 } = await db.query(
  `SELECT tier FROM v_exercise_verification WHERE course_id='analysis' AND id='CJX-I-XLSX-r234'`
));
report('Lean + 人工审核 → strongest', t2[0].tier === 'strongest', t2[0].tier);

// 5. 修订历史：改动前自动落一版旧值
const { rows: rev } = await db.query(
  `SELECT version, human_verified_at FROM exercise_revision
   WHERE course_id='analysis' AND exercise_id='CJX-I-XLSX-r234'`
);
report('更新前已快照旧版本', rev.length === 1 && rev[0].version === 1, `版本数 ${rev.length}`);

// 6. 网上搜集题必须带出处
let webBlocked = false;
try {
  await db.query(
    `INSERT INTO exercise (course_id, id, prompt_md, provenance)
     VALUES ('analysis', 'WEB-TEST-1', '网上题的题干', 'web')`
  );
} catch {
  webBlocked = true;
}
report('web 来源缺 source_url 被数据库拒绝', webBlocked);

// 7. 未发布的题不会进入可组卷视图
const { rows: pub } = await db.query('SELECT count(*)::int AS n FROM v_publishable_exercise');
report('无可发布题目（当前全部未审核）', pub[0].n === 0, `可发布 ${pub[0].n} 道`);

// ---------------------------------------------------------------------------
// 审核队列端到端：建队 → 查队列 → 发布 → 修订留痕
// 这里跑的是 server/app/review.py 里那几条真实 SQL（仅把 psycopg 的 %% 还原成 %）
// ---------------------------------------------------------------------------
console.log('\n审核队列流程：');

const { rows: created } = await db.query(
  `INSERT INTO review_task (course_id, target, target_id, priority)
   SELECT e.course_id, 'exercise', e.id,
          CASE
            WHEN e.source_raw LIKE '%待核%' THEN 1
            WHEN NOT EXISTS (SELECT 1 FROM exercise_node n
                              WHERE n.course_id = e.course_id AND n.exercise_id = e.id) THEN 2
            WHEN e.provenance = 'unknown' THEN 2
            WHEN e.source_page IS NULL THEN 3
            ELSE 4
          END
   FROM exercise e
   WHERE e.review_status IN ('draft', 'machine-checked')
   ORDER BY e.course_id, e.id
   ON CONFLICT (course_id, target, target_id) DO NOTHING
   RETURNING id`
);
report('建队：未发布题目全部入队', created.length === 4807, `入队 ${created.length} 条`);

const { rows: issues } = await db.query(
  `WITH fresh AS (
     SELECT t.id, t.course_id, t.target_id
     FROM review_task t
     WHERE t.state = 'pending'
       AND NOT EXISTS (SELECT 1 FROM review_issue i WHERE i.task_id = t.id)
   )
   INSERT INTO review_issue (task_id, category, severity, detail, raised_by)
   SELECT f.id, cat.category, cat.severity, cat.detail, 'import-audit'
   FROM fresh f
   JOIN exercise e ON e.course_id = f.course_id AND e.id = f.target_id
   CROSS JOIN LATERAL (
     SELECT 'missing-node'::review_category AS category, 'major' AS severity, '题目未挂任何知识点' AS detail
     WHERE NOT EXISTS (SELECT 1 FROM exercise_node n
                        WHERE n.course_id = e.course_id AND n.exercise_id = e.id)
     UNION ALL
     SELECT 'source-missing'::review_category, 'major', '溯源无法判定'
     WHERE e.provenance = 'unknown'
     UNION ALL
     SELECT 'page-imprecise'::review_category, 'minor', '无精确书页，仅章节范围或缺失'
     WHERE e.source_page IS NULL
     UNION ALL
     SELECT 'content-error'::review_category, 'minor', '原文含"待核"标记，需人工确认'
     WHERE e.source_raw LIKE '%待核%'
   ) AS cat
   RETURNING id`
);
report('自动生成错误清单', issues.length > 0, `${issues.length} 条 issue`);

const { rows: issueDist } = await db.query(
  `SELECT category, count(*)::int AS n FROM review_issue GROUP BY category ORDER BY n DESC`
);
console.log('      错误分类分布：', issueDist.map((r) => `${r.category}=${r.n}`).join(', '));

const { rows: q } = await db.query(
  `SELECT t.id AS task_id, t.course_id, t.target_id AS exercise_id,
          t.state, t.priority, e.provenance, v.tier,
          coalesce(array_agg(DISTINCT n.node_id) FILTER (WHERE n.node_id IS NOT NULL), '{}') AS node_ids,
          coalesce(json_agg(DISTINCT jsonb_build_object(
            'category', i.category, 'severity', i.severity, 'detail', i.detail))
            FILTER (WHERE i.id IS NOT NULL), '[]') AS issues
   FROM review_task t
   JOIN exercise e ON e.course_id = t.course_id AND e.id = t.target_id
   LEFT JOIN v_exercise_verification v ON v.course_id = e.course_id AND v.id = e.id
   LEFT JOIN exercise_node n ON n.course_id = e.course_id AND n.exercise_id = e.id
   LEFT JOIN review_issue i ON i.task_id = t.id AND i.resolved_at IS NULL
   WHERE t.state = 'pending'
   GROUP BY t.id, e.course_id, e.id, v.tier
   ORDER BY t.priority, t.created_at
   LIMIT 3`
);
report('队列查询返回结构化行', q.length === 3 && Array.isArray(q[0].issues), `样本 ${q.length} 行`);
if (q.length) {
  const first = q[0];
  console.log(
    `      首行：${first.exercise_id} tier=${first.tier} 挂载=${first.node_ids.length} 个 issue=${first.issues.length} 条`
  );
}

// 发布一条，验证三道闸门同时打开
const target = q[0];
await db.query(
  `UPDATE review_task SET state='published', decided_at=now(), assignee='审查员A' WHERE id=$1`,
  [target.task_id]
);
await db.query(
  `UPDATE exercise SET review_status='published',
          human_verified_at=coalesce(human_verified_at, now()),
          human_verified_by=coalesce(human_verified_by, '审查员A')
   WHERE course_id=$1 AND id=$2`,
  [target.course_id, target.exercise_id]
);

const { rows: afterPub } = await db.query('SELECT count(*)::int AS n FROM v_publishable_exercise');
report('发布后进入可组卷视图', afterPub[0].n === 1, `可发布 ${afterPub[0].n} 道`);

const { rows: revAfter } = await db.query(
  'SELECT count(*)::int AS n FROM exercise_revision WHERE course_id=$1 AND exercise_id=$2',
  [target.course_id, target.exercise_id]
);
report('发布动作也留下修订快照', revAfter[0].n >= 1, `${revAfter[0].n} 版`);

// 审计写入：与 server/app/review.py 的 publish 分支保持同一语句形状。
// 这里曾把列名写成 entity_id（实际是 entity_key），接口一调用就 500，故固化为测试。
let auditOk = false;
try {
  await db.query(
    `INSERT INTO audit_log (actor, action, entity, entity_key, detail)
     VALUES ($1, 'publish-exercise', 'exercise', $2, jsonb_build_object('note', $3::text))`,
    ['审查员A', `${target.course_id}/${target.exercise_id}`, '首轮发布']
  );
  auditOk = true;
} catch (err) {
  console.log('      审计写入失败：', err.message);
}
const { rows: auditRows } = await db.query(
  `SELECT detail FROM audit_log WHERE action = 'publish-exercise'`
);
report('发布写入审计日志', auditOk && auditRows.length === 1, `${auditRows.length} 条`);

// 0002 迁移带来的学伴/管线表必须一起建好
const { rows: newTables } = await db.query(
  `SELECT table_name FROM information_schema.tables
   WHERE table_schema='public'
     AND table_name IN ('pipeline_job','exercise_attempt','node_mastery',
                        'student_profile','qa_eval_case','qa_eval_run','qa_eval_result')
   ORDER BY table_name`
);
report('学伴与管线表齐备', newTables.length === 7, `${newTables.length}/7`);

process.exit(failed === 0 ? 0 : 1);
