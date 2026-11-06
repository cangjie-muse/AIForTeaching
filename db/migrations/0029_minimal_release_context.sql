-- 0029 · 最小 Release Context：固定取题范围 + 内容寻址题目版本
--
-- 对应文档：
--   docs/superpowers/plans/2026-11-05-paper-01-manual-fixed-papers.md  任务 3
--   docs/superpowers/specs/2026-11-04-paper-assembly-and-online-attempts-design.md
--     §6.2（候选资格是前置硬门槛）、§10.11（item_exposure_event）、§14.2（保密卷与曝光）
--
-- 为什么必须新开一个迁移：计划书假定 `release_context / release_item /
-- exercise_version / exercise_instance` 已经由前置计划建好，但本仓库**一张都没有**
-- （db/schema.sql 与 0001—0028 全部核对过：没有 release、没有 exercise_version、
-- 没有 license 表、没有 governance.review_decision_item）。候选资格的第一条硬门槛
-- 是"属于固定 Release Context"；没有成员关系表，这条门槛只能由调用方传一个布尔值，
-- 那不是"实现门禁"，是假装门禁存在。
--
-- 为什么只建两张表，不建 exercise_version：
--   题目版本用**内容寻址**：exercise_version_id = uuid5(固定命名空间,
--   "exercise_version:{course}:{exercise}:{sha256(canonical_json(prompt, answer))}")。
--   任何一次编辑都会换哈希 → 换版本 id，所以不需要版本表；同一个内容在开发库、
--   测试库、生产都得到同一个 id，候选快照的哈希因此可跨环境复现。
--   已知限制：release_item 只存版本 id + 内容哈希，不存全文，所以"从快照回放旧版本
--   正文"做不到；补历史只能遍历 exercise_revision 逐版本重算哈希去匹配，而它不覆盖
--   许可与审核状态，也无法做依赖失效传播。这条限制写进了任务 3 的交付说明。
--
-- 为什么不做 RLS：0027 只对 paper_version / paper_section / paper_slot / paper_render
-- 开了 FORCE ROW LEVEL SECURITY；release_* 与 blueprint_version / candidate_snapshot
-- 一样是服务账号读写的元数据表。写路径仍然统一走
-- db.transaction(assessment_service=True)（见 server/app/releases.py 与 candidates.py），
-- 以后若给这些表加策略，不需要改调用方。
--
-- 幂等：CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS；没有删改既有对象
-- （本项目只做加法，0027/0028 一个字都不改）。

BEGIN;

CREATE SCHEMA IF NOT EXISTS assessment;

-- ---------------------------------------------------------------------------
-- 一、Release Context：一次"把当前内容固定下来"的事实
--
-- base_release_id 让"基础发布 + 小修订"不必复制整份清单：候选读取沿 base 链
-- 递归解析，最近一层覆盖旧层（见 _PostgresCandidateUnitOfWork.load_records）。
-- status：
--   open    正在编，内容还可能变 —— 只允许练习/作业卷引用
--   frozen  已冻结 —— 正式保密卷的唯一合法状态
--   retired 整份退役
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assessment.release_context (
  id              uuid PRIMARY KEY,
  -- 与 assessment.paper_blueprint_version.course_id 同口径（uuid5 映射，见 0028）
  course_id       uuid NOT NULL,
  -- 文本课程号：release_item 靠它 join public.exercise（那边是 text 主键）
  course_ref      text NOT NULL CHECK (length(course_ref) BETWEEN 1 AND 200),
  offering_id     uuid,
  offering_ref    text CHECK (offering_ref IS NULL OR length(offering_ref) BETWEEN 1 AND 200),
  purpose         text NOT NULL CHECK (purpose IN ('formal', 'coursework', 'practice')),
  status          text NOT NULL DEFAULT 'frozen' CHECK (status IN ('open', 'frozen', 'retired')),
  base_release_id uuid REFERENCES assessment.release_context(id),
  -- 本次冻结的规范清单哈希（科目/班级/用途/状态/基础/每条 (题号, 版本 id, 内容哈希, 状态)）
  content_hash    char(64) NOT NULL,
  label           text,
  created_by      uuid NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  -- 自己当自己的基础会让递归解析立刻成环；这里先挡一层，深度上限在查询里再挡一层
  CONSTRAINT release_context_not_its_own_base
    CHECK (base_release_id IS NULL OR base_release_id <> id)
);

COMMENT ON TABLE assessment.release_context IS
  '固定的取题范围（Release Context）。候选资格的第一条硬门槛：题目必须属于它或它的有效 base 链';

COMMENT ON COLUMN assessment.release_context.base_release_id IS
  '有效 base 链的上游；候选读取按"最近一层优先"解析，最多 32 层（环保护）';

-- "这门课有哪些 Release" 是唯一的列表方向
CREATE INDEX IF NOT EXISTS release_context_course_idx
    ON assessment.release_context (course_id, status, created_at DESC);

-- ---------------------------------------------------------------------------
-- 二、Release Item：某个 Release 固定了哪一道题的哪一版
--
-- 两条唯一键各自防一类事故：
--   (release_context_id, exercise_version_id)：同一范围不能把同一版本固定两次
--   (release_context_id, exercise_id)        ：同一范围不能同时固定同一道题的两个版本
--                                              ——否则"固定"就没有意义了
-- exercise_version_id 是内容寻址 id（见文件头），因此它同时承担"版本身份"与
-- "内容是否被改过"的判据：当前内容算出的 id 与这里不同，就说明题目被原地编辑过。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assessment.release_item (
  id                  uuid PRIMARY KEY,
  release_context_id  uuid NOT NULL REFERENCES assessment.release_context(id) ON DELETE CASCADE,
  -- 上游来源 Release；本仓库没有上游链，冻结时写本 Release 自己的 id
  source_release_id   uuid NOT NULL,
  exercise_id         uuid NOT NULL,
  exercise_version_id uuid NOT NULL,
  content_hash        char(64) NOT NULL,
  -- 同族题 id：曝光冲突、去重、必选/排除题都按它算，不按版本
  exercise_family_id  uuid NOT NULL,
  course_ref          text NOT NULL CHECK (length(course_ref) BETWEEN 1 AND 200),
  exercise_ref        text NOT NULL CHECK (length(exercise_ref) BETWEEN 1 AND 200),
  -- included：可用；excluded：本 Release 明确排除；withdrawn：严重撤回（任何用途都不得再用）
  state               text NOT NULL DEFAULT 'included'
                      CHECK (state IN ('included', 'excluded', 'withdrawn')),
  decided_by          uuid NOT NULL,
  decided_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (release_context_id, exercise_version_id),
  UNIQUE (release_context_id, exercise_id)
);

COMMENT ON TABLE assessment.release_item IS
  'Release 固定的题目版本。版本 id 是内容寻址的，所以"题目被原地编辑"能被判出来（content_drift）';

COMMENT ON COLUMN assessment.release_item.exercise_version_id IS
  '内容寻址版本 id = uuid5(命名空间, course:exercise:sha256(canonical_json(prompt, answer)))；改一个字就换 id';

-- "这道题的哪些版本被哪些 Release 固定过"——曝光回放与漂移巡检都走这个方向
CREATE INDEX IF NOT EXISTS release_item_version_idx
    ON assessment.release_item (exercise_version_id);
CREATE INDEX IF NOT EXISTS release_item_family_idx
    ON assessment.release_item (exercise_family_id);
CREATE INDEX IF NOT EXISTS release_item_exercise_idx
    ON assessment.release_item (course_ref, exercise_ref);

COMMIT;
