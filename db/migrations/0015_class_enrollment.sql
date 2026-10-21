-- 班级与选课：把"谁在哪门课的哪个班上"变成真数据。
--
-- teaching_class / enrollment 是 0001 就建好的空壳（只有课程、班级名、学期），
-- 从没被任何接口用过。这次把它们接上：
--   * 班级要能查到是谁建的、用什么口径排课（学期）；
--   * 选课要能追到"谁把学生加进来的"，否则学期末对不上名单时没人说得清；
--   * student_id 要真正指向 app_account，否则删了账号会留下幽灵选课记录。
--
-- 注意：这里不做"学生只能看自己选的课"的限制。当前阶段班级是名单与归属，
-- 学生可见范围仍由课程发布状态 + 可见范围口径决定。

ALTER TABLE teaching_class
  ADD COLUMN IF NOT EXISTS created_by text,
  ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN IF NOT EXISTS note text;

-- 同一门课、同一学期不允许重名班级：重名会让"名单到底交哪一份"变成猜谜。
CREATE UNIQUE INDEX IF NOT EXISTS teaching_class_course_term_name_idx
  ON teaching_class (course_id, term, name);

CREATE INDEX IF NOT EXISTS teaching_class_course_idx
  ON teaching_class (course_id, term);

ALTER TABLE enrollment
  ADD COLUMN IF NOT EXISTS added_by text,
  ADD COLUMN IF NOT EXISTS note text;

CREATE INDEX IF NOT EXISTS enrollment_student_idx
  ON enrollment (student_id);

-- 历史选课里可能有对不上账号的脏行（这个表以前没有约束）。先清掉孤儿再上外键，
-- 否则迁移会在生产库上直接失败。删之前把它们记进审计，留着可查。
INSERT INTO audit_log (actor, action, entity, entity_key, detail)
SELECT 'migration-0015', 'enrollment.prune-orphan', 'enrollment', e.class_id || ':' || e.student_id,
       jsonb_build_object('class_id', e.class_id, 'student_id', e.student_id)
FROM enrollment e
LEFT JOIN app_account a ON a.id = e.student_id
WHERE a.id IS NULL;

DELETE FROM enrollment e
USING (
  SELECT e2.class_id, e2.student_id
  FROM enrollment e2
  LEFT JOIN app_account a ON a.id = e2.student_id
  WHERE a.id IS NULL
) orphan
WHERE e.class_id = orphan.class_id AND e.student_id = orphan.student_id;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'enrollment_student_fk'
  ) THEN
    ALTER TABLE enrollment
      ADD CONSTRAINT enrollment_student_fk
      FOREIGN KEY (student_id) REFERENCES app_account(id) ON DELETE CASCADE;
  END IF;
END $$;

COMMENT ON TABLE teaching_class IS '教学班：一门课在一个学期里的一个班，名单见 enrollment';
COMMENT ON COLUMN teaching_class.teacher_id IS '任课教师账号 id（app_account.id）；可为空，表示尚未指定';
COMMENT ON COLUMN teaching_class.term IS '学期，格式 2026-2027-1';
COMMENT ON COLUMN enrollment.added_by IS '把学生加进班的账号 id，用于学期末核对名单来源';
