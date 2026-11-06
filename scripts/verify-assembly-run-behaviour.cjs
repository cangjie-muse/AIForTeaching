'use strict';

// 0034 的真实行为断言：求解运行的数据库级语义在 PGlite 真引擎里跑一遍，
// 而不是只 grep SQL 文件里有没有那几行字。
//
// 为什么必须在数据库层再钉一次：服务里的"没有方案就不建卷"是一条注释和一次 if；
// 而 INFEASIBLE 带方案、paper_version 只填一半来源这两件事，任何绕过服务的脚本、
// 手工修数或将来某个新入口都能造出来。数据库 CHECK 是最后一道不会忘的门。
//
// 由 scripts/verify-migrations.cjs 的 buildChecks 调用；单列成文件是为了遵守
// eslint max-lines(800)（verify-migrations.cjs 已接近上限）。
const assert = require('node:assert/strict');
const crypto = require('node:crypto');

const RUN_WITH_SOLUTIONS = '7a1b0000-0000-4000-8000-000000000001';
const RUN_FAILED = '7a1b0000-0000-4000-8000-000000000002';

const sha = (seed) => crypto.createHash('sha256').update(String(seed)).digest('hex');

function writer(db, ids) {
  return (values) => db.query(
    `INSERT INTO assessment.assembly_run (
       id, blueprint_version_id, actor_id, idempotency_key, candidate_manifest,
       candidate_manifest_hash, candidate_snapshot_id, solver_name, solver_version,
       solver_config, status, solution_manifest, completed_at
     ) VALUES ($1, $2, $3, $4, '{"candidates":[]}'::jsonb, $5, $6, 'cp-sat', '9.11.4210',
               '{"random_seed":7}'::jsonb, $7, $8::jsonb, $9)`,
    [
      values.id, ids.blueprintVersion, ids.actor, values.key, sha('candidate'),
      ids.candidateSnapshot, values.status, values.solutions,
      values.completed === undefined ? new Date().toISOString() : values.completed
    ]
  );
}

async function checkRunLifecycle(db, ids, expectSqlError) {
  const write = writer(db, ids);

  // 1) 反向对照：INFEASIBLE 且方案为空必须能写进去——证明下面的约束没有挡过宽。
  await write({ id: RUN_WITH_SOLUTIONS, key: 'assembly-run-1', status: 'infeasible',
    solutions: '[]' });
  const stored = (await db.query(
    'SELECT status, jsonb_array_length(solution_manifest) AS n, solver_name, solver_version'
    + ' FROM assessment.assembly_run WHERE id = $1',
    [RUN_WITH_SOLUTIONS]
  )).rows[0];
  assert.equal(stored.status, 'infeasible');
  assert.equal(stored.n, 0);
  assert.equal(stored.solver_name, 'cp-sat');
  assert.equal(stored.solver_version, '9.11.4210');

  // 2) INFEASIBLE 带方案 → 必须被 ck_assembly_no_solutions_without_solve 拒绝。
  const solutionsOnInfeasible = await expectSqlError(
    db,
    'UPDATE assessment.assembly_run SET solution_manifest = $2::jsonb WHERE id = $1',
    [RUN_WITH_SOLUTIONS, JSON.stringify([{ variant_label: 'single' }])]
  );
  assert.equal(solutionsOnInfeasible.code, '23514',
    `INFEASIBLE 带方案应触发 23514，实际 SQLSTATE ${solutionsOnInfeasible.code}`);

  // 3) FAILED 同理：失败运行留着"方案"会让 UI 以为能选卷。
  await write({ id: RUN_FAILED, key: 'assembly-run-2', status: 'failed', solutions: '[]' });
  const solutionsOnFailed = await expectSqlError(
    db,
    'UPDATE assessment.assembly_run SET solution_manifest = $2::jsonb WHERE id = $1',
    [RUN_FAILED, JSON.stringify([{ variant_label: 'single' }])]
  );
  assert.equal(solutionsOnFailed.code, '23514');

  // 4) 最多 3 个方案（规格 §7.3）：第 4 个必须被 ck_assembly_max_3 拒绝。
  const four = JSON.stringify([
    { variant_label: 'a' }, { variant_label: 'b' },
    { variant_label: 'c' }, { variant_label: 'd' }
  ]);
  const tooMany = await expectSqlError(
    db,
    `UPDATE assessment.assembly_run
        SET status = 'feasible', solution_manifest = $2::jsonb, completed_at = now()
      WHERE id = $1`,
    [RUN_WITH_SOLUTIONS, four]
  );
  assert.equal(tooMany.code, '23514', `第 4 个方案应触发 23514，实际 ${tooMany.code}`);

  // 5) 三个方案可以写入（上限是 3 而不是 2）。
  await db.query(
    `UPDATE assessment.assembly_run
        SET status = 'feasible',
            solution_manifest = $2::jsonb,
            best_bound = 100,
            optimality_gap_milli = 0,
            completed_at = now()
      WHERE id = $1`,
    [RUN_WITH_SOLUTIONS, JSON.stringify([
      { variant_label: 'a' }, { variant_label: 'b' }, { variant_label: 'c' }
    ])]
  );

  // 6) 终态必须有完成时间：status='optimal' 但 completed_at 为空 → 拒绝。
  const missingCompleted = await expectSqlError(
    db,
    'UPDATE assessment.assembly_run SET status = $2, completed_at = NULL WHERE id = $1',
    [RUN_WITH_SOLUTIONS, 'optimal']
  );
  assert.equal(missingCompleted.code, '23514',
    `终态缺 completed_at 应触发 23514，实际 ${missingCompleted.code}`);

  // 7) 同一操作者 + 同一把幂等键只能有一次运行（重试返回同一行）。
  const duplicateKey = await expectSqlError(
    db,
    `INSERT INTO assessment.assembly_run (
       id, blueprint_version_id, actor_id, idempotency_key, candidate_manifest,
       candidate_manifest_hash, solver_name, solver_version, solver_config, status
     ) VALUES ($1, $2, $3, 'assembly-run-1', '{}'::jsonb, $4, 'cp-sat', '9.11.4210',
               '{}'::jsonb, 'queued')`,
    [RUN_FAILED, ids.blueprintVersion, ids.actor, sha('other')]
  );
  assert.equal(duplicateKey.code, '23505',
    `重复幂等键应触发 23505，实际 ${duplicateKey.code}`);

  // 8) RLS：assembly_run 必须 FORCE（候选清单里含"这门课考什么"）。
  const rls = (await db.query(
    `SELECT relrowsecurity AS enabled, relforcerowsecurity AS forced
       FROM pg_class WHERE oid = 'assessment.assembly_run'::regclass`
  )).rows[0];
  assert.equal(rls.enabled, true, 'assembly_run 必须启用 RLS');
  assert.equal(rls.forced, true, 'assembly_run 必须 FORCE RLS');

  return 'INFEASIBLE/FAILED 不能带方案、方案上限 3、终态需完成时间、幂等键唯一、RLS 强制开启';
}

async function checkPaperProvenance(db, ids, expectSqlError) {
  // 9) paper_version 只填 assembly_run_id 不填 solution_index → 拒绝（来源不完整）。
  const halfSource = await expectSqlError(
    db,
    'UPDATE assessment.paper_version SET assembly_run_id = $2 WHERE id = $1',
    [ids.paperDraft, RUN_WITH_SOLUTIONS]
  );
  assert.equal(halfSource.code, '23514',
    `只填运行不填方案序号应触发 23514，实际 ${halfSource.code}`);

  // 10) 方案序号越界（0..2）→ 拒绝。
  const outOfRange = await expectSqlError(
    db,
    `UPDATE assessment.paper_version
        SET assembly_run_id = $2, selected_solution_index = 3
      WHERE id = $1`,
    [ids.paperDraft, RUN_WITH_SOLUTIONS]
  );
  assert.equal(outOfRange.code, '23514',
    `方案序号 3 应触发 23514，实际 ${outOfRange.code}`);

  // 11) 反向对照：两个字段同时给值必须成功——组卷来源链真的能落库。
  await db.query(
    `UPDATE assessment.paper_version
        SET assembly_run_id = $2, selected_solution_index = 2
      WHERE id = $1`,
    [ids.paperDraft, RUN_WITH_SOLUTIONS]
  );
  const paper = (await db.query(
    `SELECT assembly_run_id::text AS run, selected_solution_index AS idx
       FROM assessment.paper_version WHERE id = $1`,
    [ids.paperDraft]
  )).rows[0];
  assert.equal(paper.run, RUN_WITH_SOLUTIONS);
  assert.equal(paper.idx, 2);

  // 12) 阶段 A 的旧闸门必须已被 0034 解除：0027 的 ck_paper_manual_only 不允许
  //     这两列有值，若它还在，第 11 步就已经报错了。这里显式断言它不存在。
  const manualOnly = (await db.query(
    `SELECT count(*)::int AS n FROM pg_constraint
      WHERE conname = 'ck_paper_manual_only'
        AND conrelid = 'assessment.paper_version'::regclass`
  )).rows[0];
  assert.equal(manualOnly.n, 0, 'ck_paper_manual_only 必须被 0034 移除，否则组卷无法落卷');

  return '组卷来源两列必须成对、方案序号 0..2、旧的手工卷闸门已解除';
}

module.exports = async function checkAssemblyRunBehaviour(db, ids, expectSqlError) {
  const run = await checkRunLifecycle(db, ids, expectSqlError);
  const paper = await checkPaperProvenance(db, ids, expectSqlError);
  return `${run}；${paper}`;
};
