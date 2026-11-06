'use strict';

// 0036 的真实行为断言：在线作答的表与不变量（PGlite / PostgreSQL 18）。
//
// 这些约束每一条都对应一次"开考之后才发现就来不及了"的事故：
//   * 同一学生同时开两份卷 → 部分唯一索引；
//   * 保存重放插出第二条响应 → (attempt_id, client_revision_id) 唯一；
//   * 交卷后被偷偷改写 → append-only 触发器；
//   * 学生读到别人的卷 → FORCE RLS。
// 由 scripts/verify-migrations.cjs 的 buildChecks 调用。
const assert = require('node:assert/strict');
const crypto = require('node:crypto');

const ATTEMPT_ONE = 'aa000000-0000-4000-8000-000000000001';
const ATTEMPT_TWO = 'aa000000-0000-4000-8000-000000000002';
const ATTEMPT_THREE = 'aa000000-0000-4000-8000-000000000003';
const STUDENT = 'bb000000-0000-4000-8000-000000000001';
const REVISION_ONE = 'cc000000-0000-4000-8000-000000000001';
const REVISION_TWO = 'cc000000-0000-4000-8000-000000000002';
const sha = (seed) => crypto.createHash('sha256').update(String(seed)).digest('hex');

module.exports = async function checkAttemptBehaviour(db, ids, expectSqlError) {
  const insertAttempt = (values) => db.query(
    `INSERT INTO assessment.paper_attempt
       (id, assignment_id, paper_version_id, student_id, attempt_policy_version_id,
        attempt_no, mode, status, started_at, server_deadline_at, worker_due_at,
        submitted_at, close_reason)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now(), now() + interval '2 hours',
             now() + interval '2 hours', $9, $10)`,
    [values.id, ids.assignment, ids.paperDraft, values.student || STUDENT,
      ids.policy, values.attempt_no || 1, values.mode || 'normal',
      values.status || 'in_progress', values.submitted_at || null,
      values.close_reason || null]
  );

  // 1) distribution_kind 现在允许 online / both（阶段 A 只允许 print）。
  await db.query(
    "UPDATE assessment.assignment SET distribution_kind = 'online' WHERE id = $1",
    [ids.assignment]
  );
  const bogusKind = await expectSqlError(
    db,
    "UPDATE assessment.assignment SET distribution_kind = 'video' WHERE id = $1",
    [ids.assignment]
  );
  assert.equal(bogusKind.code, '23514', `未允许的 distribution_kind 应触发 23514，实际 ${bogusKind.code}`);

  // 2) 第一份进行中的卷可以插入。
  await insertAttempt({ id: ATTEMPT_ONE });

  // 3) 同一学生同一作业的第二个"进行中"卷必须被部分唯一索引拒绝。
  const secondActive = await expectSqlError(
    db,
    `INSERT INTO assessment.paper_attempt
       (id, assignment_id, paper_version_id, student_id, attempt_policy_version_id,
        attempt_no, mode, status, started_at, server_deadline_at, worker_due_at)
     VALUES ($1, $2, $3, $4, $5, 2, 'normal', 'in_progress', now(),
             now() + interval '2 hours', now() + interval '2 hours')`,
    [ATTEMPT_TWO, ids.assignment, ids.paperDraft, STUDENT, ids.policy]
  );
  assert.equal(secondActive.code, '23505', `第二个进行中 attempt 应触发 23505，实际 ${secondActive.code}`);

  // 4) 但"已交卷"的历史 attempt 可以存在（部分索引只管 in_progress/finalizing）。
  await insertAttempt({
    id: ATTEMPT_TWO, attempt_no: 2, status: 'submitted',
    submitted_at: new Date().toISOString(), close_reason: 'student_finished',
  });
  const rows = (await db.query(
    'SELECT count(*)::int AS n FROM assessment.paper_attempt WHERE assignment_id = $1',
    [ids.assignment]
  )).rows[0];
  assert.equal(rows.n, 2, '同一学生应允许一份进行中 + 一份已交卷');

  // 5) 交卷状态必须有 submitted_at；进行中状态不能有（CHECK）。
  const missingSubmitted = await expectSqlError(
    db,
    `UPDATE assessment.paper_attempt SET status = 'submitted', submitted_at = NULL
      WHERE id = $1`,
    [ATTEMPT_ONE]
  );
  assert.equal(missingSubmitted.code, '23514', `submitted 缺 submitted_at 应触发 23514，实际 ${missingSubmitted.code}`);

  // 6) 逐槽响应：client_revision_id 唯一（重放不会插出第二条），revision_no >= 1。
  // 第三个 attempt 用另一个学生：同一学生此刻已有一份进行中，会撞部分唯一索引
  // （那正是第 3 步要证的事，不能在这里重复踩）。
  const OTHER_STUDENT = 'bb000000-0000-4000-8000-000000000002';
  await insertAttempt({ id: ATTEMPT_THREE, attempt_no: 1, status: 'in_progress',
    student: OTHER_STUDENT });
  const insertRevision = (id, revision_no, client_id) => db.query(
    `INSERT INTO assessment.response_revision
       (id, attempt_id, paper_slot_id, revision_no, client_revision_id,
        raw_response, parse_status, server_received_at)
     VALUES ($1, $2, $3, $4, $5, '{"type":"text","value":"x"}'::jsonb, 'parsed', now())`,
    [id, ATTEMPT_ONE, ids.slot1, revision_no, client_id]
  );
  await insertRevision(REVISION_ONE, 1, 'client-1');
  const duplicateClient = await expectSqlError(
    db,
    `INSERT INTO assessment.response_revision
       (id, attempt_id, paper_slot_id, revision_no, client_revision_id,
        raw_response, parse_status, server_received_at)
     VALUES ($1, $2, $3, 2, 'client-1', '{}'::jsonb, 'empty', now())`,
    [REVISION_TWO, ATTEMPT_ONE, ids.slot1]
  );
  assert.equal(duplicateClient.code, '23505', `同一 client_revision_id 重放应触发 23505，实际 ${duplicateClient.code}`);

  const zeroRevision = await expectSqlError(
    db,
    `INSERT INTO assessment.response_revision
       (id, attempt_id, paper_slot_id, revision_no, client_revision_id,
        raw_response, parse_status, server_received_at)
     VALUES ($1, $2, $3, 0, 'client-zero', '{}'::jsonb, 'empty', now())`,
    ['cc000000-0000-4000-8000-0000000000ff', ATTEMPT_ONE, ids.slot1]
  );
  assert.equal(zeroRevision.code, '23514', `revision_no=0 应触发 23514，实际 ${zeroRevision.code}`);

  // 7) append-only：已提交的响应、提交项、交卷本身都不能被 UPDATE/DELETE。
  const revisionUpdate = await expectSqlError(
    db,
    'UPDATE assessment.response_revision SET parse_status = $2 WHERE id = $1',
    [REVISION_ONE, 'invalid']
  );
  assert.equal(revisionUpdate.code, 'P0001', `response_revision 的 UPDATE 应被触发器拒绝，实际 ${revisionUpdate.code}`);

  await assertSubmissionImmutable(db, ids, expectSqlError);
  await assertAttemptRls(db);
  return 'online/both 可分发、同学生仅一份进行中、client_revision_id 唯一、'
    + 'revision_no>=1、交卷与响应 append-only、五表 FORCE RLS';
};

// RLS 断言单独成函数：主断言函数已到 eslint max-lines-per-function(120) 上限。
async function assertAttemptRls(db) {
  // 9) RLS：五张表都 FORCE。
  const rls = (await db.query(
    `SELECT c.relname AS name, c.relrowsecurity AS on_, c.relforcerowsecurity AS forced
       FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'assessment'
        AND c.relname IN ('paper_attempt','paper_attempt_item','response_revision',
                          'submission','submission_item')`
  )).rows;
  assert.equal(rls.length, 5, '五张在线作答表都应存在');
  for (const row of rls) {
    assert.equal(row.on_, true, `${row.name} 必须启用 RLS`);
    assert.equal(row.forced, true, `${row.name} 必须 FORCE RLS`);
  }
}
// 交卷与提交项的不可变性：单独成函数（同上，为满足 max-lines-per-function）。
async function assertSubmissionImmutable(db, ids, expectSqlError) {
  const STUDENT = 'bb000000-0000-4000-8000-000000000001';
  const ATTEMPT_ONE = 'aa000000-0000-4000-8000-000000000001';
  const ATTEMPT_TWO = 'aa000000-0000-4000-8000-000000000002';
  const sha = (seed) => crypto.createHash('sha256').update(String(seed)).digest('hex');
  // 8) 交卷 + 提交项：先插，再验"改不动"。
  const submissionId = 'dd000000-0000-4000-8000-000000000001';
  await db.query(
    `INSERT INTO assessment.submission
       (id, attempt_id, student_id, trigger_kind, submitted_at, item_count,
        unanswered_count, invalid_count, response_manifest_hash, receipt_hash)
     VALUES ($1, $2, $3, 'manual', now(), 2, 0, 0, $4, $5)`,
    [submissionId, ATTEMPT_ONE, STUDENT, sha('manifest'), sha('receipt')]
  );
  const submissionUpdate = await expectSqlError(
    db,
    'UPDATE assessment.submission SET item_count = 3 WHERE id = $1',
    [submissionId]
  );
  assert.equal(submissionUpdate.code, 'P0001', `submission 的 UPDATE 应被触发器拒绝，实际 ${submissionUpdate.code}`);

  const badCounts = await expectSqlError(
    db,
    `INSERT INTO assessment.submission
       (id, attempt_id, student_id, trigger_kind, submitted_at, item_count,
        unanswered_count, invalid_count, response_manifest_hash, receipt_hash)
     VALUES ($1, $2, $3, 'deadline', now(), 1, 1, 1, $4, $5)`,
    ['dd000000-0000-4000-8000-000000000002', ATTEMPT_TWO, STUDENT, sha('m2'), sha('r2')]
  );
  assert.equal(badCounts.code, '23514', `未答+无效超过总项应触发 23514，实际 ${badCounts.code}`);
}