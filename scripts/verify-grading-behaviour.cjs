'use strict';

// 0037 的真实行为断言：成绩只能追加、逐项判定不可改、练习投影与正式成绩隔离。
//
// 三条都对应"事后改数就能改成绩"的风险，必须在数据库层拦住（服务层之外的第二道门）。
// 由 scripts/verify-migrations.cjs 的 buildChecks 调用。
// 拆成两个函数只为满足 eslint max-lines-per-function(120)，断言本身是同一条链路。
const assert = require('node:assert/strict');
const crypto = require('node:crypto');

const sha = (seed) => crypto.createHash('sha256').update(String(seed)).digest('hex');

const STUDENT = 'bb000000-0000-4000-8000-000000000009'; // 与 0036 断言用不同学生
const ATTEMPT = 'aa000000-0000-4000-8000-000000000030';
const SUBMISSION = 'dd000000-0000-4000-8000-000000000030';
const SUBMISSION_ITEM = 'dd000000-0000-4000-8000-000000000031';
const GRADE = 'ff000000-0000-4000-8000-000000000030';
const REVISION = 'ff000000-0000-4000-8000-000000000031';
const DECISION = 'ff000000-0000-4000-8000-000000000032';
const LEARNING = '1e000000-0000-4000-8000-000000000030';
const LEARNING_LEGACY = '1e000000-0000-4000-8000-000000000031';

async function writeFixtures(db, ids) {
  await db.query(
    `INSERT INTO assessment.paper_attempt
       (id, assignment_id, paper_version_id, student_id, attempt_policy_version_id,
        attempt_no, mode, status, started_at, server_deadline_at, worker_due_at,
        submitted_at, close_reason)
     VALUES ($1, $2, $3, $4, $5, 1, 'normal', 'submitted', now(),
             now() + interval '1 hour', now() + interval '1 hour', now(), 'manual')`,
    [ATTEMPT, ids.assignment, ids.paperDraft, STUDENT, ids.policy]
  );
  await db.query(
    `INSERT INTO assessment.submission
       (id, attempt_id, student_id, trigger_kind, submitted_at, item_count,
        unanswered_count, invalid_count, response_manifest_hash, receipt_hash)
     VALUES ($1, $2, $3, 'manual', now(), 2, 0, 0, $4, $5)`,
    [SUBMISSION, ATTEMPT, STUDENT, sha('m30'), sha('r30')]
  );
  await db.query(
    `INSERT INTO assessment.submission_item
       (id, submission_id, assignment_item_id, paper_slot_id, parse_status)
     VALUES ($1, $2, $3, $4, 'parsed')`,
    [SUBMISSION_ITEM, SUBMISSION, ids.assignmentItem, ids.slot1]
  );
  await db.query(
    "INSERT INTO assessment.grade (id, submission_id, status) VALUES ($1, $2, 'draft')",
    [GRADE, SUBMISSION]
  );
  await db.query(
    `INSERT INTO assessment.grade_revision
       (id, grade_id, submission_id, revision_no, total_score, status, created_by)
     VALUES ($1, $2, $3, 1, 88.00, 'draft', $4)`,
    [REVISION, GRADE, SUBMISSION, ids.actor]
  );
  const revisionRow = (await db.query(
    `INSERT INTO assessment.response_revision
       (id, attempt_id, paper_slot_id, revision_no, client_revision_id,
        raw_response, parse_status, server_received_at)
     VALUES (gen_random_uuid(), $1, $2, 1, 'grading-check', '{"text":"1"}'::jsonb,
             'parsed', now()) RETURNING id`,
    [ATTEMPT, ids.slot1]
  )).rows[0];
  return revisionRow.id;
}

async function checkGradeAppendOnly(db, ids, expectSqlError) {
  // 1) 成绩分数不可原位改：直接堵住"事后改数即改成绩"。
  const scoreUpdate = await expectSqlError(
    db, 'UPDATE assessment.grade_revision SET total_score = 0 WHERE id = $1', [REVISION]);
  assert.equal(scoreUpdate.code, 'P0001', `改分数应被触发器拒绝，实际 SQLSTATE ${scoreUpdate.code}`);
  assert.ok(/grade_revision_score_is_append_only/.test(scoreUpdate.message),
    `报错应含 grade_revision_score_is_append_only，实际：${scoreUpdate.message}`);

  // 2) 但"发布"必须允许：状态/发布人/发布时间可以变。
  await db.query(
    `UPDATE assessment.grade_revision
        SET status = 'published', published_by = $2, published_at = now()
      WHERE id = $1`, [REVISION, ids.actor]);
  const published = (await db.query(
    'SELECT status, published_at FROM assessment.grade_revision WHERE id = $1', [REVISION]
  )).rows[0];
  assert.equal(published.status, 'published');
  assert.ok(published.published_at, '发布时刻应被记下');

  // 3) 发布不是"开锁"：发布之后再改分数仍被拒。
  const afterPublish = await expectSqlError(
    db, 'UPDATE assessment.grade_revision SET total_score = 99.00 WHERE id = $1', [REVISION]);
  assert.equal(afterPublish.code, 'P0001');

  // 4) 逐项判定 append-only：改主意要追加新决定。
  await db.query(
    `INSERT INTO assessment.item_grade_decision
       (id, submission_item_id, grade_revision_id, decision_seq, score, decided_by, decided_at)
     VALUES ($1, $2, $3, 1, 88.00, $4, now())`,
    [DECISION, SUBMISSION_ITEM, REVISION, ids.actor]);
  const decisionUpdate = await expectSqlError(
    db, 'UPDATE assessment.item_grade_decision SET score = 0 WHERE id = $1', [DECISION]);
  assert.equal(decisionUpdate.code, 'P0001');
  const decisionDelete = await expectSqlError(
    db, 'DELETE FROM assessment.item_grade_decision WHERE id = $1', [DECISION]);
  assert.equal(decisionDelete.code, 'P0001');

  // 5) 检查器三态：fail 必须带 failure_kind（判不出来也要留证据）。
  await expectSqlError(db,
    `INSERT INTO assessment.answer_test_run
       (id, response_revision_id, input_hash, engine_name, engine_version, outcome)
     VALUES (gen_random_uuid(), $1, $2, 'sympy', '1.13', 'fail')`,
    [await latestRevision(db), sha('input')]);
  return '成绩分数不可原位改（发布可）、逐项判定 append-only、fail 必带原因';
}

async function latestRevision(db) {
  const row = (await db.query(
    "SELECT id FROM assessment.response_revision WHERE attempt_id = $1 LIMIT 1", [ATTEMPT]
  )).rows[0];
  assert.ok(row, '夹具里的 response_revision 应该存在');
  return row.id;
}

async function checkProjectionAndRls(db, ids, expectSqlError) {
  const revisionId = await latestRevision(db);

  // 6) 练习投影：带来源的行不可改；历史行（来源为空）不受约束。
  await db.query(
    `INSERT INTO learning.exercise_attempt
       (id, exercise_version_id, input_status, source_response_revision_id, paper_attempt_id)
     VALUES ($1, $2, 'parsed', $3, $4)`,
    [LEARNING, ids.exerciseVersion, revisionId, ATTEMPT]);
  const learningUpdate = await expectSqlError(
    db, "UPDATE learning.exercise_attempt SET input_status = 'invalid' WHERE id = $1", [LEARNING]);
  assert.equal(learningUpdate.code, 'P0001',
    `带来源的练习投影应 append-only，实际 SQLSTATE ${learningUpdate.code}`);

  await db.query(
    `INSERT INTO learning.exercise_attempt (id, exercise_version_id, input_status)
     VALUES ($1, $2, 'parsed')`, [LEARNING_LEGACY, ids.exerciseVersion]);
  await db.query(
    "UPDATE learning.exercise_attempt SET input_status = 'invalid' WHERE id = $1", [LEARNING_LEGACY]);
  const legacy = (await db.query(
    'SELECT input_status FROM learning.exercise_attempt WHERE id = $1', [LEARNING_LEGACY]
  )).rows[0];
  assert.equal(legacy.input_status, 'invalid', '历史学习记录不该被锁死');

  // 7) RLS：成绩与申诉相关表都 FORCE。
  const rls = (await db.query(`
    SELECT c.relname AS name, c.relrowsecurity AS on_, c.relforcerowsecurity AS forced
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'assessment'
       AND c.relname IN ('grade','grade_revision','item_grade_decision','answer_test_run',
                         'grade_appeal','regrade_run','regrade_item','grading_suggestion')`
  )).rows;
  assert.equal(rls.length, 8, '八张评分相关表都应存在');
  for (const row of rls) {
    assert.equal(row.on_, true, `${row.name} 必须启用 RLS`);
    assert.equal(row.forced, true, `${row.name} 必须 FORCE RLS`);
  }
  return '练习投影带来源不可改、历史行不受锁、八表 FORCE RLS';
}

module.exports = async function checkGradingBehaviour(db, ids, expectSqlError) {
  await writeFixtures(db, ids);
  const first = await checkGradeAppendOnly(db, ids, expectSqlError);
  const second = await checkProjectionAndRls(db, ids, expectSqlError);
  return `${first}；${second}`;
};
