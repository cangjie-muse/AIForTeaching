-- 0009：资源包导入与"整体更新"
--
-- 背景：课程导入原来是纯前端的——解析三件套后存在浏览器 IndexedDB 里，只对这个浏览器有效；
-- 而教材生成写的是数据库。两套内容各活各的，老师改一份，另一份不变。这一版把导入也落到库里，
-- 让数据库成为唯一真源。
--
-- 关键决定：老师导出 Excel 改完再导入时，"文件里已经没有的旧知识点/旧习题"标记为 retired，
-- 不做物理删除。理由：学生作答、审核决定、修订历史都挂在这些行上，删了就是不可恢复的数据丢失；
-- retired 既能让学生端与课程包不再出现，又保留了追溯能力。
--
-- 为了知道"哪些行是上一次导入带进来的"，每行记一个 content_batch。
-- 只退休"上一个批次"的行，不动模型生成的 draft（content_batch 为空）——
-- 老师导入一份包，不该顺手把他还没审的 AI 草稿全部下架。

BEGIN;

-- -----------------------------------------------------------------------------
-- 1. 批次：一次导入一个 batch id
-- -----------------------------------------------------------------------------
ALTER TABLE course
  ADD COLUMN content_source     text
    CHECK (content_source IN ('import', 'generated', 'mixed')),
  ADD COLUMN content_batch      text,
  ADD COLUMN content_updated_at timestamptz;

ALTER TABLE chapter        ADD COLUMN content_batch text;
ALTER TABLE knowledge_node ADD COLUMN content_batch text;
ALTER TABLE exercise       ADD COLUMN content_batch text;

COMMENT ON COLUMN course.content_batch IS
  '最近一次导入了哪个资源包批次。用它区分"导入的内容"和"模型生成的内容"。';

-- 知识点标签（知识节点.xlsx 的"知识点标签"列）。原来只存在源文件里，
-- 入库后丢失了，导出时就还原不出来。
ALTER TABLE knowledge_node ADD COLUMN tags text[] NOT NULL DEFAULT '{}';

CREATE INDEX exercise_content_batch_idx ON exercise (course_id, content_batch);
CREATE INDEX knowledge_node_batch_idx   ON knowledge_node (course_id, content_batch);

-- -----------------------------------------------------------------------------
-- 2. 导入记录：谁在什么时候导入了哪份包，结果如何
-- -----------------------------------------------------------------------------
CREATE TABLE content_import (
  id           bigserial PRIMARY KEY,
  course_id    text NOT NULL,          -- 不设外键：课程删了，导入记录也要留下
  batch        text NOT NULL,
  actor        text NOT NULL,
  mode         text NOT NULL CHECK (mode IN ('replace', 'merge')),
  nodes_upserted     int NOT NULL DEFAULT 0,
  nodes_retired      int NOT NULL DEFAULT 0,
  exercises_upserted int NOT NULL DEFAULT 0,
  exercises_retired  int NOT NULL DEFAULT 0,
  warnings     jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX content_import_course_idx ON content_import (course_id, created_at DESC);

COMMENT ON TABLE content_import IS
  '资源包导入的审计记录。整体更新退掉了多少条，必须能查到——否则"题怎么少了"无法回答。';

COMMIT;
