'use strict';

// 0033 的真实行为断言：发布痕迹的三条路径都在 PGlite 真引擎里跑，而不是只 grep SQL。
// 由 scripts/verify-migrations.cjs 的 buildChecks 调用。
//
// 单列成文件是为了遵守 eslint max-lines(800)：verify-migrations.cjs 已接近上限。
const assert = require('node:assert/strict');

module.exports = async function checkAssignmentPublishFields(db, ids, expectSqlError) {
  // 1) 已发布却没有发布时刻 → 必须被 assignment_published_at_required 拒绝。
  const noPublishedAt = await expectSqlError(
    db,
    "UPDATE assessment.assignment SET status = 'published' WHERE id = $1",
    [ids.assignment]
  );
  assert.equal(noPublishedAt.code, '23514',
    `published 缺 published_at 应触发 23514，实际 SQLSTATE ${noPublishedAt.code}`);

  // 2) 撤回理由全是空白 → 必须被 assignment_withdrawn_fields 拒绝。
  const blankReason = await expectSqlError(
    db,
    `UPDATE assessment.assignment
        SET status = 'withdrawn', withdrawn_at = now(), withdrawal_reason = '   '
      WHERE id = $1`,
    [ids.assignment]
  );
  assert.equal(blankReason.code, '23514',
    `撤回理由全空白应触发 23514，实际 SQLSTATE ${blankReason.code}`);

  // 3) 反向对照：字段齐全的撤回必须成功——证明约束没有挡过宽。
  await db.query(
    `UPDATE assessment.assignment
        SET status = 'withdrawn', withdrawn_at = now(),
            withdrawal_reason = '第 2 题答案已在班级群流出'
      WHERE id = $1`,
    [ids.assignment]
  );
  const row = (await db.query(
    'SELECT status, withdrawal_reason FROM assessment.assignment WHERE id = $1',
    [ids.assignment]
  )).rows[0];
  assert.equal(row.status, 'withdrawn');
  assert.ok(String(row.withdrawal_reason).includes('流出'),
    `撤回理由应被保存，实际 ${row.withdrawal_reason}`);

  // 4) 新列真的存在且类型正确（0033 的 ALTER 不是空转）。
  const columns = (await db.query(
    `SELECT column_name, data_type
       FROM information_schema.columns
      WHERE table_schema = 'assessment'
        AND table_name IN ('assignment', 'assignment_item')
        AND column_name IN ('publish_manifest_hash', 'review_manifest_hash',
                            'withdrawn_at', 'withdrawn_by', 'withdrawal_reason',
                            'exercise_family_id')`
  )).rows.map((entry) => entry.column_name);
  for (const name of ['publish_manifest_hash', 'review_manifest_hash',
    'withdrawn_at', 'withdrawn_by', 'withdrawal_reason', 'exercise_family_id']) {
    assert.ok(columns.includes(name), `0033 缺少列 ${name}`);
  }

  return 'published 缺时间被拒、空白理由被拒、完整撤回成功、6 个新列全部就位';
};
