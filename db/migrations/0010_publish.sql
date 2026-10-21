-- 0010：学生端发布出口
--
-- 到这一步为止，导入/生成的内容都停在数据库与审核队列里，学生端读的还是静态课程包。
-- 这一版补上出口：课程级发布 + 公开的目录接口。
--
-- 两级门禁，缺一不可：
--   1. 课程级：course.review_status = 'published'。没发布的课，公开目录里根本不出现。
--   2. 条目级：exercise.review_status = 'published'。课程发布了，也只会露出老师逐题
--      点过"发布"的题；仍是 draft / teacher-reviewed 的题不会因为课程发布而被顺带放出去。
--
-- 知识点不设条目级门禁：知识点的正文在「课程内容」页由老师直接改，改完即是定稿；
-- 再要求逐条发布只会让树结构缺枝。retired 的知识点仍然隐藏。

BEGIN;

ALTER TABLE course
  ADD COLUMN published_at   timestamptz,
  ADD COLUMN published_by   text,
  ADD COLUMN publish_note   text;

COMMENT ON COLUMN course.published_at IS
  '课程级发布时间。为空表示学生端不可见；条目级可见性另看 exercise.review_status。';

CREATE INDEX course_published_idx ON course (review_status) WHERE published_at IS NOT NULL;

-- 发布时留下了哪一版：内容改过之后再发布，老师能看出学生端现在跑的是哪一版。
CREATE TABLE publish_event (
  id          bigserial PRIMARY KEY,
  course_id   text NOT NULL,
  actor       text NOT NULL,
  action      text NOT NULL CHECK (action IN ('publish', 'unpublish')),
  nodes       int NOT NULL DEFAULT 0,
  exercises   int NOT NULL DEFAULT 0,
  note        text,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX publish_event_course_idx ON publish_event (course_id, created_at DESC);

COMMENT ON TABLE publish_event IS
  '发布/撤回记录。学生端看到的是哪一版、谁放的，必须能查。';

COMMIT;
