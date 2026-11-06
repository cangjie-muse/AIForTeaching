'use strict';

// 阶段 B 落卷的两条新门禁在真引擎里再钉一次（0034 的 CHECK 是数据库层，这里
// 钉的是"服务层真的用了那两列"）。
//
// 为什么要单独一条：`papers.py` 的 INSERT 同时服务手工卷与组卷。手工卷那两列必须
// 是 NULL（0027 的语义），组卷那两列必须成对。任何一次改 INSERT 语句都可能把
// 手工卷写成"组卷"，而单测用的是内存仓储，看不出来。
const assert = require('node:assert/strict');
const crypto = require('node:crypto');

const ASSEMBLY_RUN = '7a1b0000-0000-4000-8000-0000000000ff';
const SOLUTION_PAPER = '8b2c0000-0000-4000-8000-000000000001';
const MANUAL_PAPER = '8b2c0000-0000-4000-8000-000000000002';
const sha = (seed) => crypto.createHash('sha256').update(String(seed)).digest('hex');
// paper_id 也是 uuid：不能用 ${id}-paper 这种拼串。
const variantPaperId = (id) => id.replace('0a1', '0d1').replace('0b1', '0d2');

module.exports = async function checkPaperProvenance(db, ids) {
  // 一个装配运行（0034 已建 assembly_run）供组卷卷子引用。
  // `ON CONFLICT DO NOTHING`：这条断言可能与本文件外的夹具共用同一行。
  await db.query(
    `INSERT INTO assessment.assembly_run (
       id, blueprint_version_id, actor_id, idempotency_key, candidate_manifest,
       candidate_manifest_hash, candidate_snapshot_id, solver_name, solver_version,
       solver_config, status, solution_manifest, completed_at
     ) VALUES ($1, $2, $3, 'provenance-run', '{"candidates":[]}'::jsonb, $4, $5,
               'cp-sat', '9.11.4210', '{"random_seed":7}'::jsonb, 'optimal',
               $6::jsonb, now())
     ON CONFLICT (id) DO NOTHING`,
    [ASSEMBLY_RUN, ids.blueprintVersion, ids.actor, sha('candidate'),
      ids.candidateSnapshot, JSON.stringify([{ variant_label: 'single' }])]
  );

  const insertPaper = (id, group, runId, index) => db.query(
    `INSERT INTO assessment.paper_version
       (id, paper_id, version_no, blueprint_version_id, candidate_snapshot_id,
        assembly_run_id, selected_solution_index, course_id, offering_id,
        release_context_id, paper_kind, total_score, duration_seconds, status,
        manifest_hash, created_by)
     VALUES ($1, $2, 1, $3, $4, $5, $6, $7, $8, $9, 'formal', 100.00, 5400,
             'draft', $10, $11)`,
    [id, group, ids.blueprintVersion, ids.candidateSnapshot, runId, index,
      ids.course, ids.offering, ids.releaseContext, sha(String(id)), ids.actor]
  );

  // 1) 组卷卷子：两列成对写入必须成功，并且读回来一模一样（来源链真的落库）。
  await insertPaper(SOLUTION_PAPER, '9c3d0000-0000-4000-8000-000000000001',
    ASSEMBLY_RUN, 1);
  const solved = (await db.query(
    `SELECT assembly_run_id::text AS run, selected_solution_index AS idx
       FROM assessment.paper_version WHERE id = $1`,
    [SOLUTION_PAPER]
  )).rows[0];
  assert.equal(solved.run, ASSEMBLY_RUN);
  assert.equal(solved.idx, 1);

  // 2) 手工卷子：两列都为 NULL 仍然合法（阶段 A 的路径不能被堵死）。
  await insertPaper(MANUAL_PAPER, '9c3d0000-0000-4000-8000-000000000002',
    null, null);
  const manual = (await db.query(
    `SELECT assembly_run_id, selected_solution_index
       FROM assessment.paper_version WHERE id = $1`,
    [MANUAL_PAPER]
  )).rows[0];
  assert.equal(manual.assembly_run_id, null);
  assert.equal(manual.selected_solution_index, null);

  // 3) 两组来源混写（一个卷子既有 run 又有空序号）由 0034 的 CHECK 拒绝；
  //    这里直接改已存在的手工卷行，确认约束对 UPDATE 也生效。
  const half = await db.query(
    `UPDATE assessment.paper_version SET assembly_run_id = $2 WHERE id = $1`,
    [MANUAL_PAPER, ASSEMBLY_RUN]
  ).then(() => null).catch((error) => error);
  assert.ok(half, '手工卷只补 assembly_run_id 必须被 CHECK 拒绝');
  assert.equal(half.code, '23514');

  // 4) A/B 身份：同一分组的 A、B 两份卷可以各自落库，且分组/字母都读得回来。
  const VARIANT_GROUP = '5e5e0000-0000-4000-8000-0000000000aa';
  const insertVariant = (id, label) => db.query(
    `INSERT INTO assessment.paper_version
       (id, paper_id, version_no, blueprint_version_id, candidate_snapshot_id,
        course_id, offering_id, release_context_id, paper_kind, total_score,
        duration_seconds, status, manifest_hash, created_by, variant_group_id,
        variant_label)
     VALUES ($1, $2, 1, $3, $4, $5, $6, $7, 'formal', 100.00, 5400, 'draft',
             $8, $9, $10, $11)`,
    [id, variantPaperId(id), ids.blueprintVersion, ids.candidateSnapshot, ids.course,
      ids.offering, ids.releaseContext, sha(String(id)), ids.actor,
      VARIANT_GROUP, label]
  );
  await insertVariant('8b2c0000-0000-4000-8000-0000000000a1', 'A');
  await insertVariant('8b2c0000-0000-4000-8000-0000000000b1', 'B');
  const pair = (await db.query(
    `SELECT label.variant_label, label.variant_group_id::text AS grp
       FROM assessment.paper_version label
      WHERE label.variant_group_id = $1
      ORDER BY label.variant_label`,
    [VARIANT_GROUP]
  )).rows;
  assert.equal(pair.length, 2, 'A/B 两份卷都要落库');
  assert.deepEqual(pair.map((item) => item.variant_label), ['A', 'B']);
  assert.ok(pair.every((item) => item.grp === VARIANT_GROUP), '两卷必须共享同一分组');

  // 5) 反向对照：没有任何约束禁止"只有字母没有分组"——所以这一条必须由服务层挡。
  //    这里断言的是数据库**允许**裸字母，用来提醒后来人不要以为库会兜底。
  await db.query(
    `INSERT INTO assessment.paper_version
       (id, paper_id, version_no, blueprint_version_id, candidate_snapshot_id,
        course_id, offering_id, release_context_id, paper_kind, total_score,
        duration_seconds, status, manifest_hash, created_by, variant_label)
     VALUES ('8b2c0000-0000-4000-8000-0000000000c1', '8b2c0000-0000-4000-8000-0000000000c2',
             1, $1, $2, $3, $4, $5, 'formal', 100.00, 5400, 'draft', $6, $7, 'A')`,
    [ids.blueprintVersion, ids.candidateSnapshot, ids.course, ids.offering,
      ids.releaseContext, sha('lone-label'), ids.actor]
  );

  return '组卷卷子两列成对落库、手工卷两列仍为 NULL、单边补写被 CHECK 拒绝、'
    + 'A/B 同组两份卷可落库（裸字母由服务层挡，数据库不兜底）';
};
