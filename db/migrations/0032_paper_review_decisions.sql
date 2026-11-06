-- 0032 · 整卷双复核的逐人证据 + 提交时冻结的校验报告
--
-- 对应文档：
--   docs/superpowers/plans/2026-11-05-paper-01-manual-fixed-papers.md  任务 7
--   docs/superpowers/specs/2026-11-04-paper-assembly-and-online-attempts-design.md
--     §9（审核流程）、§10.5（paper_version 的 manifest 与 review_manifest_hash）
--
-- 为什么不改 0027—0031：
--   0027 的 paper_review_round 只有"这一轮审核"的壳：manifest、需要几人、状态、
--   提交人与关闭时间。它没有"谁签的、签的是哪份卷面、证据哈希是什么"，也没有
--   提交时冻结的 report_hash / edit_seq。若把两人决定塞进 round 的一个 jsonb 列，
--   数据库就无法表达"同一轮同一人只能签一次"，也无法让 append-only 触发器
--   逐条保护证据。所以：
--     1. 新增 assessment.paper_review_decision（逐人证据，append-only）；
--     2. 给既有的 paper_review_round 只**追加**两列：
--        validation_report_hash（提交时冻结的整卷校验报告哈希）与
--        submitted_edit_seq（提交时的编辑序号）。
--   两处都是 CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS / DROP ... IF
--   EXISTS 后重建，重复执行安全；不删除、不改写任何既有对象。
--
-- 证据失效（manifest 变了旧审核仍算通过）是这里最重要的防线：
--   * decision.manifest_hash 记录"签字时绑定的卷面"；
--   * 服务批准前要求 decision.manifest_hash = round.manifest_hash =
--     paper.submitted_manifest_hash = paper.manifest_hash；
--   * CHECK 保证一条 approved 记录必须 human + authorized + 非 contributor。
--
-- RLS：与 paper_version 同口径。paper_review_decision 开 FORCE ROW LEVEL SECURITY，
-- 课程范围经 paper_version.course_id 判定；服务账号写路径仍统一走
-- db.transaction(assessment_service=True)。

BEGIN;

CREATE SCHEMA IF NOT EXISTS assessment;

-- ---------------------------------------------------------------------------
-- 一、提交时冻结：report_hash 与 edit_seq
--
-- 可空的原因：0027/0030 之前可能已有手工写入的 round 行（本仓库没有，但迁移
-- 不能假设生产库为空）。新写入路径（任务 7 起）永远填这两个值；读取接口对
-- 空的旧行返回 null，而不是伪造一个"当时校验过"的证据。
-- ---------------------------------------------------------------------------

ALTER TABLE assessment.paper_review_round
  ADD COLUMN IF NOT EXISTS validation_report_hash char(64);

ALTER TABLE assessment.paper_review_round
  ADD COLUMN IF NOT EXISTS submitted_edit_seq integer;

COMMENT ON COLUMN assessment.paper_review_round.validation_report_hash IS
  '提交审核时冻结的整卷校验 report_hash；复核人复算同一份卷面应得到同一值';

COMMENT ON COLUMN assessment.paper_review_round.submitted_edit_seq IS
  '提交审核时冻结的 paper_version.edit_seq；提交后卷面不可编辑';

-- 旧行可能为空；新行必须像 sha256 一样是 64 位小写十六进制
ALTER TABLE assessment.paper_review_round
  DROP CONSTRAINT IF EXISTS ck_review_round_report_hash;
ALTER TABLE assessment.paper_review_round
  ADD CONSTRAINT ck_review_round_report_hash CHECK (
    validation_report_hash IS NULL
    OR validation_report_hash ~ '^[0-9a-f]{64}$'
  );

-- ---------------------------------------------------------------------------
-- 二、逐人复核证据：一张不可变的签字表
--
-- (review_round_id, reviewer_user_id) 唯一 = "同一轮同一人只能签一次"；
-- (paper_version_id, decision_seq) 唯一 = 签署顺序在整份卷子内不重复，重放/审计
-- 都能稳定排序。decision 只允许三种终态；approved 记录额外要求 human、
-- authorized 且非 contributor——数据库层不允许"贡献者自己批自己"。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assessment.paper_review_decision (
  id                 uuid PRIMARY KEY,
  paper_version_id   uuid NOT NULL REFERENCES assessment.paper_version(id),
  review_round_id    uuid NOT NULL REFERENCES assessment.paper_review_round(id),
  reviewer_user_id   uuid NOT NULL,
  -- 签字时绑定的卷面；与 round / paper 的 manifest 不一致即证据失效
  manifest_hash      char(64) NOT NULL,
  decision           text NOT NULL
                     CHECK (decision IN ('approved','rejected','revoked')),
  -- 复核人自己给出的证据哈希（检查表/签署件等），服务只校验形状，不解释内容
  evidence_hash      char(64) NOT NULL
                     CHECK (evidence_hash ~ '^[0-9a-f]{64}$'),
  decision_seq       integer NOT NULL CHECK (decision_seq >= 1),
  human              boolean NOT NULL,
  authorized         boolean NOT NULL,
  contributor        boolean NOT NULL,
  note               text NULL CHECK (note IS NULL OR length(note) <= 500),
  created_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (review_round_id, reviewer_user_id),
  UNIQUE (paper_version_id, decision_seq),
  CHECK (decision <> 'approved' or (human and authorized and not contributor))
);

COMMENT ON TABLE assessment.paper_review_decision IS
  '整卷复核的逐人签字证据：manifest / 证据哈希 / 是否人类 / 是否授权 / 是否贡献者；UPDATE/DELETE 被触发器拒绝';

CREATE INDEX IF NOT EXISTS paper_review_decision_version_idx
    ON assessment.paper_review_decision (paper_version_id, manifest_hash);
CREATE INDEX IF NOT EXISTS paper_review_decision_round_idx
    ON assessment.paper_review_decision (review_round_id);

-- 审计事实不可变：任何绕过服务的 SQL 也改不了已签的字
CREATE OR REPLACE FUNCTION assessment.guard_paper_review_decision() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'paper_review_decision is append-only: % 被拒绝', TG_OP;
END;
$$;

DROP TRIGGER IF EXISTS paper_review_decision_guard
    ON assessment.paper_review_decision;
CREATE TRIGGER paper_review_decision_guard
  BEFORE UPDATE OR DELETE ON assessment.paper_review_decision
  FOR EACH ROW EXECUTE FUNCTION assessment.guard_paper_review_decision();

-- ---------------------------------------------------------------------------
-- 三、RLS：服务账号全量；教师/学生按 paper_version.course_id 的课程范围
-- ---------------------------------------------------------------------------

ALTER TABLE assessment.paper_review_decision ENABLE ROW LEVEL SECURITY;
ALTER TABLE assessment.paper_review_decision FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS review_decision_course_scope
    ON assessment.paper_review_decision;
CREATE POLICY review_decision_course_scope ON assessment.paper_review_decision
  USING (
    current_setting('app.assessment_service', true) = 'on'
    OR EXISTS (
      SELECT 1 FROM assessment.paper_version p
      WHERE p.id = paper_version_id
        AND p.course_id::text = ANY (
          string_to_array(current_setting('app.course_ids', true), ',')
        )
    )
  );

COMMIT;
