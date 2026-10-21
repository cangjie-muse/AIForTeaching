-- 0012：可见范围按课程分别设置 + 知识图谱人工审核
--
-- 两件事：
--
-- 1) 口径从"全平台一个"变成"全局默认 + 课程覆盖"。
--    表里加 course_id：NULL 的行是全局默认（id='default'），非 NULL 的行是某门课的覆盖
--    （id='course:<课程号>'）。查询时"这门课有覆盖就用覆盖，否则跟随全局"。
--    这样"数学分析严格、试点课全放"不需要两套代码。
--
-- 2) 知识图谱（knowledge_node）也需要人工审核，但只有人工这一档——没有 Lean，
--    因为它不是定理，看不出"形式化验证"这回事。所以记录 human_verified_at，
--    并给可见范围加一列 graph_tier：all（全放，默认）或 human（仅人工审核过的节点）。
--
-- 教材片段（textbook_chunk）与知识点正文不设审核：它们由原教材切片生成，
-- 属于"原文转载"而不是"我们的论断"，审的是作者而不是我们。

BEGIN;

-- ---- 1. 作用域 ----
ALTER TABLE visibility_policy
  ADD COLUMN course_id text REFERENCES course(id) ON DELETE CASCADE;

ALTER TABLE visibility_policy
  ADD COLUMN graph_tier text NOT NULL DEFAULT 'all'
  CONSTRAINT visibility_policy_graph_tier_check CHECK (graph_tier IN ('human', 'all'));

-- 原来只允许 id='default' 这一行；现在要有每门课覆盖行
ALTER TABLE visibility_policy DROP CONSTRAINT visibility_policy_id_check;

-- 一门课只能有一行覆盖
CREATE UNIQUE INDEX visibility_policy_course_uniq
  ON visibility_policy (course_id) WHERE course_id IS NOT NULL;

-- 全局只能有一行（用常量表达式做部分唯一索引）
CREATE UNIQUE INDEX visibility_policy_global_uniq
  ON visibility_policy ((true)) WHERE course_id IS NULL;

COMMENT ON COLUMN visibility_policy.course_id IS
  'NULL = 全局默认；非 NULL = 这门课的覆盖。查询时课程覆盖优先。';
COMMENT ON COLUMN visibility_policy.graph_tier IS
  '知识图谱只有一档人工审核：all = 全放（默认，与引入前一致）；human = 只放人工审核过的节点。';

-- ---- 2. 知识图谱人工审核 ----
ALTER TABLE knowledge_node
  ADD COLUMN human_verified_at timestamptz,
  ADD COLUMN human_verified_by text;

CREATE INDEX knowledge_node_verified_idx
  ON knowledge_node (course_id)
  WHERE human_verified_at IS NULL;

COMMENT ON COLUMN knowledge_node.human_verified_at IS
  '知识图谱的人工审核时间。教材切片正文不审；审的是"这条图谱挂得对不对"。';

COMMIT;
