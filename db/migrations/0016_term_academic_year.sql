-- 学期改成学校口径：学年 + 秋/春/夏，而不是 2026-2027-1 这种内部编号。
--
-- 原来的 `term` 是一个自造格式（2026-2027-1），只有我认识，老师看不懂。武大的学期
-- 说法是"2026年秋季学期""2027年春季学期"。这里把学期拆成两个字段存：
--
--   academic_year  2026-2027   学年
--   term_kind      秋 / 春 / 夏
--
-- 为什么拆开存、而不是直接存"2026年秋季学期"这个字符串：
--   1. 显示文案会变（学院可能改叫"第一学期"），存储不该跟着改；文案在代码里生成；
--   2. 排序要按时间，不能按字符串——"2027年春季学期"和"2026年秋季学期"谁在前，
--      字符串比较靠不住（"夏"的码点比"春""秋"都小）；
--   3. 唯一约束用结构字段比用文案更稳。
--
-- 夏季学期（短学期）在这里是允许的：武大曾设夏季短学期，留一个枚举值不花代价，
-- 学校真用上了就是改个下拉项，不用再迁移。

ALTER TABLE teaching_class
  ADD COLUMN IF NOT EXISTS academic_year text,
  ADD COLUMN IF NOT EXISTS term_kind text;

-- 先把历史数据翻译过来。开发库里只有演示班，e2e 库里是临时夹具，但脚本要能
-- 处理两种来源：老的 2026-2027-1，以及万一已经写成 2026年秋季学期 的。
UPDATE teaching_class SET
  term_kind = CASE
    WHEN term ~ '-1$' THEN '秋'
    WHEN term ~ '-2$' THEN '春'
    WHEN term ~ '-3$' THEN '夏'
    WHEN term ~ '秋季学期' THEN '秋'
    WHEN term ~ '春季学期' THEN '春'
    WHEN term ~ '夏季学期' THEN '夏'
  END,
  academic_year = CASE
    WHEN term ~ '^\d{4}-\d{4}' THEN substring(term from '^\d{4}-\d{4}')
    -- "2026年秋季学期"：秋季学期属于 2026-2027 学年
    WHEN term ~ '^(\d{4})年[秋]季学期' THEN
      substring(term from '^(\d{4})') || '-' || (substring(term from '^(\d{4})')::int + 1)::text
    -- "2027年春季学期/夏季学期"：属于 2026-2027 学年
    WHEN term ~ '^(\d{4})年[春夏]季学期' THEN
      (substring(term from '^(\d{4})')::int - 1)::text || '-' || substring(term from '^(\d{4})')
  END
WHERE academic_year IS NULL;

-- 认不出来的旧值不该被静默丢掉：先让它报错，人工看一遍再改。
DO $$
DECLARE bad integer;
BEGIN
  SELECT count(*) INTO bad FROM teaching_class WHERE academic_year IS NULL OR term_kind IS NULL;
  IF bad > 0 THEN
    RAISE EXCEPTION '有 % 个班级的学期无法识别，请先人工处理 teaching_class.term', bad;
  END IF;
END $$;

ALTER TABLE teaching_class
  ALTER COLUMN academic_year SET NOT NULL,
  ALTER COLUMN term_kind SET NOT NULL;

ALTER TABLE teaching_class
  ADD CONSTRAINT teaching_class_term_kind_check CHECK (term_kind IN ('秋', '春', '夏')),
  ADD CONSTRAINT teaching_class_academic_year_check
    CHECK (academic_year ~ '^\d{4}-\d{4}$'
           AND split_part(academic_year, '-', 2)::int = split_part(academic_year, '-', 1)::int + 1);

DROP INDEX IF EXISTS teaching_class_course_term_name_idx;
DROP INDEX IF EXISTS teaching_class_course_idx;

ALTER TABLE teaching_class DROP COLUMN term;

CREATE UNIQUE INDEX teaching_class_course_term_name_idx
  ON teaching_class (course_id, academic_year, term_kind, name);

CREATE INDEX teaching_class_course_idx
  ON teaching_class (course_id, academic_year, term_kind);

COMMENT ON COLUMN teaching_class.academic_year IS '学年，如 2026-2027';
COMMENT ON COLUMN teaching_class.term_kind IS '学期：秋（9月—次年1月）、春（2月—6月）、夏（夏季短学期）';
