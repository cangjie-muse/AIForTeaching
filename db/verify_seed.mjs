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

// ---------------------------------------------------------------------------
// 0003 多语言：教材语言标记、切片继承、术语对照
// ---------------------------------------------------------------------------
console.log('\n多语言支持：');

const { rows: langDefault } = await db.query(
  `SELECT language FROM exercise WHERE course_id=$1 AND id=$2`,
  [target.course_id, target.exercise_id]
);
report('存量题目默认语言为 zh', langDefault[0].language === 'zh', langDefault[0].language);

// 插入一本英文教材 + 中英混排切片，验证切片可单独覆盖语言
await db.query(
  `INSERT INTO textbook (id, course_id, title, language, original_title, pdf_sha256)
   VALUES ('analysis-rudin-3e', 'analysis', '数学分析原理', 'en',
           'Principles of Mathematical Analysis', 'deadbeef')`
);
await db.query(
  `INSERT INTO textbook_chunk (textbook_id, seq, page_from, page_to, content_md, embedding_model)
   VALUES ('analysis-rudin-3e', 1, 1, 1, '**Definition** A sequence converges if ...', 'bge-m3'),
          ('analysis-rudin-3e', 2, 2, 2, '定理 3.1（此处为中文批注）', 'bge-m3')`
);
await db.query(
  `UPDATE textbook_chunk SET language='zh'
   WHERE textbook_id='analysis-rudin-3e' AND seq=2`
);

const { rows: langs } = await db.query(
  `SELECT chunk_id, language FROM v_chunk_language
   WHERE textbook_id='analysis-rudin-3e' ORDER BY chunk_id`
);
report(
  '切片语言：未标注的继承教材(zh→en)，标注的用自身',
  langs.length === 2 && langs[0].language === 'en' && langs[1].language === 'zh',
  langs.map((r) => r.language).join('/')
);

// 术语对照：一题中文、教材英文时靠它做查询扩展
await db.query(
  `INSERT INTO glossary_term (course_id, term_zh, term_en, aliases, node_id)
   VALUES ('analysis', '一致收敛', 'uniform convergence', ARRAY['均匀收敛'], NULL)`
);
const { rows: pairs } = await db.query(
  `SELECT term, counterpart, lang FROM v_glossary_pair WHERE course_id='analysis' ORDER BY lang`
);
report(
  '术语可双向展开',
  pairs.length === 2 &&
    pairs.some((r) => r.term === '一致收敛' && r.counterpart === 'uniform convergence') &&
    pairs.some((r) => r.term === 'uniform convergence' && r.counterpart === '一致收敛'),
  pairs.map((r) => `${r.term}→${r.counterpart}`).join(' | ')
);

// ---------------------------------------------------------------------------
// 0004 模型端点：数据不出校的允许清单
// ---------------------------------------------------------------------------
console.log('\n模型端点（供应商允许清单）：');

const { rows: ep } = await db.query(
  `INSERT INTO model_endpoint (name, kind, base_url, model, data_scope, max_context)
   VALUES ('campus-gateway', 'chat', 'http://10.0.0.8:8001/v1', 'qwen2.5-14b-instruct', 'internal', 32768)
   RETURNING name, data_scope`
);
report('可登记校内端点', ep.length === 1, ep[0]?.name ?? '');

// 外部端点必须写明理由，否则半年后没人知道它为什么存在
let extBlocked = false;
try {
  await db.query(
    `INSERT INTO model_endpoint (name, kind, base_url, model, data_scope)
     VALUES ('deepseek', 'chat', 'https://api.deepseek.com', 'deepseek-flash', 'external')`
  );
} catch {
  extBlocked = true;
}
report('外部端点缺说明被拒绝', extBlocked);

// embedding 端点必须写维度：没有维度就无法和 vector(1024) 列核对
let dimBlocked = false;
try {
  await db.query(
    `INSERT INTO model_endpoint (name, kind, base_url, model, data_scope)
     VALUES ('emb-nodim', 'embedding', 'http://127.0.0.1:8002/v1', 'bge-m3', 'internal')`
  );
} catch {
  dimBlocked = true;
}
report('embedding 端点缺维度被拒绝', dimBlocked);

const { rows: emb } = await db.query(
  `INSERT INTO model_endpoint (name, kind, base_url, model, data_scope, dim)
   VALUES ('local-bge-m3', 'embedding', 'http://127.0.0.1:8002/v1', 'bge-m3', 'internal', 1024)
   RETURNING dim`
);
report('embedding 端点维度可登记且为 1024', emb[0].dim === 1024, String(emb[0].dim));

// 同一时刻只能启用一个 chat 端点，避免"到底在用哪个模型"说不清
let dupBlocked = false;
try {
  await db.query(
    `INSERT INTO model_endpoint (name, kind, base_url, model, data_scope)
     VALUES ('local-vllm-2', 'chat', 'http://10.0.0.9:8001/v1', 'qwen2.5-32b', 'internal')`
  );
} catch {
  dupBlocked = true;
}
report('同时只能启用一个 chat 端点', dupBlocked);

// 0005：数据可出校后，外部端点成为常态，但必须留痕（说明 + 批准人）
await db.query(
  `INSERT INTO model_endpoint
     (name, kind, base_url, model, data_scope, notes, approved_by, approved_at, enabled)
   VALUES ('deepseek-chat', 'chat', 'https://api.deepseek.com', 'deepseek-flash',
           'external', '学院同意使用国内大模型 API，教材片段可出校', '信息化专员', now(), false)`
);
const { rows: ext } = await db.query(
  `SELECT name, approved_by FROM v_external_endpoint_audit WHERE name = 'deepseek-chat'`
);
report('外部端点登记后可审计', ext.length === 1, ext[0]?.approved_by ?? '');

// ---------------------------------------------------------------------------
// 0006 配置界面：公开视图不得泄露密钥路径
// ---------------------------------------------------------------------------
console.log('\n配置界面（0006）：');

const { rows: publicCols } = await db.query(
  `SELECT column_name FROM information_schema.columns
   WHERE table_name = 'v_model_endpoint_public'`
);
const publicNames = publicCols.map((r) => r.column_name);
report('公开视图不含密钥路径', !publicNames.includes('api_key_path'), `${publicNames.length} 列`);
report('公开视图给出 key_configured 标记', publicNames.includes('key_configured'));

await db.query(
  `UPDATE model_endpoint
   SET api_key_path = 'deepseek-chat.key', api_key_updated_at = now(), updated_by = '信息化专员'
   WHERE name = 'deepseek-chat'`
);
const { rows: configured } = await db.query(
  `SELECT key_configured FROM v_model_endpoint_public WHERE name = 'deepseek-chat'`
);
report('配置密钥后标记为已配置', configured[0].key_configured === true);

// 自检结果要能落到界面显示的三个字段上
await db.query(
  `UPDATE model_endpoint
   SET last_test_at = now(), last_test_ok = false, last_test_note = 'HTTP 401: invalid api key'
   WHERE name = 'deepseek-chat'`
);
const { rows: tested } = await db.query(
  `SELECT last_test_ok, last_test_note, updated_by
   FROM v_model_endpoint_public WHERE name = 'deepseek-chat'`
);
report(
  '自检结果可展示',
  tested[0].last_test_ok === false && tested[0].last_test_note.includes('401'),
  tested[0].last_test_note
);

process.exit(failed === 0 ? 0 : 1);