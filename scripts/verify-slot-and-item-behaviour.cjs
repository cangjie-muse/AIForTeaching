'use strict';

// paper_slot 的唯一性 + assignment_item 的"只允许追加"：两条都是数据库层约束，
// 只能靠真引擎证明。从 verify-migrations.cjs 的 buildChecks 调用。
//
// 为什么从主文件搬出来：verify-migrations.cjs 逼近 eslint max-lines(800) 上限，
// 继续往里塞断言会让后来的人第一反应是调高上限，而不是想清楚该不该拆。
const assert = require('node:assert/strict');

module.exports = async function checkSlotAndItemRules(db, ids, expectSqlError, hash64) {
  // 1) 同一分节内重复 slot_no → UNIQUE(section_id, slot_no) 必须拦下（负数也不例外）。
  const duplicate = await expectSqlError(
    db,
    `INSERT INTO assessment.paper_slot (id, section_id, code, slot_no, score, content_hash)
     VALUES ($1, $2, $3, $4, $5, $6)`,
    ['2a2a2a2a-2a2a-4a2a-8a2a-2a2a2a2a2a2a', ids.section, 'A3', -1, 10.00, hash64('slot3')]
  );
  assert.equal(duplicate.code, '23505',
    `同一 section 内重复 slot_no 应触发 23505，实际 SQLSTATE ${duplicate.code}`);

  // 2) assignment_item 一旦写入就不能改：发布终检的依据不能事后被挪走。
  const mutation = await expectSqlError(
    db,
    'UPDATE assessment.assignment_item SET score = score + 1 WHERE id = $1',
    [ids.assignmentItem]
  );
  assert.equal(mutation.code, 'P0001',
    `assignment_item 的 UPDATE 应被触发器以 P0001 拒绝，实际 SQLSTATE ${mutation.code}`);
  assert.ok(/append-only/i.test(mutation.message),
    `报错应含 append-only，实际：${mutation.message}`);

  return '同 section 重复 slot_no 被 23505 拒绝（负数也在唯一索引里）；'
    + 'assignment_item 的 UPDATE 被 append-only 触发器拒绝';
};
