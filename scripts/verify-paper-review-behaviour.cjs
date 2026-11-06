'use strict';

// 0032 的真实行为断言：唯一键、CHECK、append-only 触发器都在 PGlite 真引擎里跑，
// 而不是只 grep SQL 文本。由 scripts/verify-migrations.cjs 的 buildChecks 调用。
//
// 单列成文件是为了遵守 eslint max-lines(800)：verify-migrations.cjs 已经接近上限。
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');

const h = (value) => createHash('sha256').update(value).digest('hex');

module.exports = async function checkReviewEvidence(db, ids, expectSqlError) {
  const round = '3a3a3a3a-3a3a-4a3a-8a3a-3a3a3a3a3a3a';
  const reviewerA = '3b3b3b3b-3b3b-4b3b-8b3b-3b3b3b3b3b3b';
  const reviewerB = '3c3c3c3c-3c3c-4c3c-8c3c-3c3c3c3c3c3c';
  const insert = `INSERT INTO assessment.paper_review_decision
      (id, paper_version_id, review_round_id, reviewer_user_id, manifest_hash,
       decision, evidence_hash, decision_seq, human, authorized, contributor)
    VALUES ($1, $2, $3, $4, $5, 'approved', $6, $7, $8, $9, $10)`;
  const values = (id, reviewer, evidence, seq, human, authorized, contributor) => [
    id, ids.paperApproved, round, reviewer, h('manifest'), evidence, seq,
    human, authorized, contributor
  ];
  const expectCode = async (label, params, code) => {
    const error = await expectSqlError(db, insert, params);
    assert.equal(error.code, code, `${label}：预期 SQLSTATE ${code}，实际 ${error.code}`);
  };

  await db.query(
    `INSERT INTO assessment.paper_review_round
       (id, paper_version_id, manifest_hash, required_approvals, status, submitted_by,
        validation_report_hash, submitted_edit_seq)
     VALUES ($1, $2, $3, 2, 'open', $4, $5, 1)`,
    [round, ids.paperApproved, h('manifest'), ids.actor, h('report')]);

  await expectCode('非人类账号的 approved 记录应被 CHECK 拒绝',
    values('3d3d3d3d-3d3d-4d3d-8d3d-3d3d3d3d3d3d', reviewerA, h('a'), 1, false, true, false), '23514');
  await expectCode('非 64 位小写十六进制的 evidence_hash 应被 CHECK 拒绝',
    values('3e3e3e3e-3e3e-4e3e-8e3e-3e3e3e3e3e3e', reviewerA, 'abc', 1, true, true, false), '23514');

  await db.query(insert,
    values('3f3f3f3f-3f3f-4f3f-8f3f-3f3f3f3f3f3f', reviewerA, h('a'), 1, true, true, false));
  await expectCode('同一轮同一人第二次签字应被唯一键拒绝',
    values('4a4a4a4a-4a4a-4a4a-8a4a-4a4a4a4a4a4a', reviewerA, h('a2'), 2, true, true, false), '23505');
  await expectCode('贡献者自批应被 CHECK 拒绝',
    values('4b4b4b4b-4b4b-4b4b-8b4b-4b4b4b4b4b4b', reviewerB, h('b'), 2, true, true, true), '23514');

  const mutated = await expectSqlError(
    db, 'UPDATE assessment.paper_review_decision SET note = $2 WHERE id = $1',
    ['3f3f3f3f-3f3f-4f3f-8f3f-3f3f3f3f3f3f', '改证据']);
  assert.equal(mutated.code, 'P0001',
    `复核证据的 UPDATE 应被触发器以 P0001 拒绝，实际 ${mutated.code}`);
  assert.ok(/append-only/i.test(mutated.message),
    `报错应含 append-only，实际：${mutated.message}`);

  return '逐人唯一 23505、非人类/贡献者/短哈希 23514、UPDATE 被 P0001 拒绝；round 的 report_hash/edit_seq 可写';
};
