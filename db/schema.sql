-- =============================================================================
-- 智慧武大 · 数学 AI 教学智能体 —— 数据库结构（由 db/migrate.py --concat 生成）
--
-- 不要手改本文件。改 db/migrations/NNNN_*.sql，然后重新生成：
--     python db/migrate.py --concat
--
-- 本文件是**非破坏性**的：只建，不删。要清空开发库请显式执行 db/reset.dev.sql。
-- 三条设计原则见 docs/数据模型与数据库设计.md。
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------
-- 0001_init.sql
-- ---------------------------------------------------------------
-- 0001 基线：由 db/schema.sql 拆分而来，内容为最初的完整结构。
-- 已移除 DROP SCHEMA 与事务包裹，事务由 db/migrate.py 统一管理。

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;      -- 需 pgvector/pgvector:pg16 镜像


CREATE TYPE review_status AS ENUM (
  'draft',              -- 导入但未校验
  'machine-checked',    -- 通过脚本校验（公式可渲染、外键完整）
  'teacher-reviewed',   -- 教师逐条确认
  'published',          -- 对学生可见
  'retired'             -- 已被新版本取代
);

CREATE TYPE exercise_provenance AS ENUM (
  'textbook',           -- 教材原题
  'colleague-export',   -- 同事导出表
  'self-authored',      -- 课程自拟
  'ai-generated',       -- 模型生成
  'web',                -- 网上搜集（需附 source_url 与 license，默认不可发布）
  'unknown'
);

-- 可信等级：与 review_status（工作流状态）正交。
--   unverified     未经审核（网上搜集的默认落在这里）
--   lean-verified  已有 Lean 形式化证明
--   human-verified 人工逐题审核过
-- 两维都达标（人工审核 + Lean）由视图计算为 strongest。
CREATE TYPE verification_tier AS ENUM ('unverified', 'lean-verified', 'human-verified');

CREATE TYPE ocr_status AS ENUM ('clean', 'ocr-checked', 'needs-review');

CREATE TYPE relation_type AS ENUM (
  'prerequisite',       -- 先修
  'derived',            -- 推导
  'example-of',         -- 例证
  'application-of',     -- 应用
  'cross-course'        -- 跨课程同一概念
);

-- 说明：'contains'（层级隶属）不建边表，由 knowledge_node.parent_id 派生，见 v_contains。

-- -----------------------------------------------------------------------------
-- 1. 课程
-- -----------------------------------------------------------------------------
CREATE TABLE course (
  id              text PRIMARY KEY,
  title           text NOT NULL,
  audience        text,
  description     text,
  subject         text,
  status          text,
  schema_version  text,
  resource_base   text,                        -- course-packs 下的相对目录
  source_files    text[],                      -- 原始三件套文件名
  review_status   review_status NOT NULL DEFAULT 'draft',
  source_imported_at timestamptz,              -- 来源数据里的 importedAt
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE course IS '课程。id 与数据中的 course.id 一致，同时等于该课程根节点 id。';

-- -----------------------------------------------------------------------------
-- 2. 教材（版权材料，只在服务器内使用，不入 git）
-- -----------------------------------------------------------------------------
CREATE TABLE textbook (
  id                text PRIMARY KEY,          -- 如 analysis-chenjixiu-3e-vol1
  course_id         text NOT NULL REFERENCES course(id) ON DELETE CASCADE,
  title             text NOT NULL,
  edition           text,
  authors           text,
  publisher         text,
  isbn              text,
  copyright_status  text NOT NULL DEFAULT 'internal-only'
                    CHECK (copyright_status IN ('internal-only', 'licensed', 'public-domain')),
  page_offset       int NOT NULL DEFAULT 0,    -- 书页码 = PDF 页 - page_offset
  pdf_sha256        text NOT NULL,             -- 同一个文件重复上传时去重
  pdf_bytes         bigint,
  page_count        int,
  has_text_layer    boolean,                   -- false 表示纯扫描件，需 OCR
  uploaded_by       text,
  uploaded_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (course_id, pdf_sha256)
);

COMMENT ON TABLE textbook IS '教材元数据。原件存对象存储，库内只存哈希与路径，避免数据库膨胀。';

-- 教材切片：RAG 的检索单元，也是知识点溯源的落点
CREATE TABLE textbook_chunk (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  textbook_id   text NOT NULL REFERENCES textbook(id) ON DELETE CASCADE,
  seq           int NOT NULL,                  -- 阅读顺序，全文唯一
  section_path  text[] NOT NULL DEFAULT '{}',  -- 章节路径，用于回答里显示位置
  page_from     int NOT NULL,                  -- 书页码（已换算）
  page_to       int NOT NULL,
  pdf_page_from int,                           -- 原始 PDF 页，保留以便核对
  content_md    text NOT NULL,                 -- Markdown + $LaTeX$
  has_formula   boolean NOT NULL DEFAULT false,
  ocr_status    ocr_status NOT NULL DEFAULT 'clean',
  token_count   int,
  embedding     vector(1024),                  -- 维度与所选 embedding 模型对齐
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (textbook_id, seq),
  CHECK (page_to >= page_from)
);

CREATE INDEX textbook_chunk_textbook_page_idx ON textbook_chunk (textbook_id, page_from);
CREATE INDEX textbook_chunk_embedding_idx
  ON textbook_chunk USING hnsw (embedding vector_cosine_ops)
  WHERE embedding IS NOT NULL;

-- -----------------------------------------------------------------------------
-- 3. 章节
-- -----------------------------------------------------------------------------
CREATE TABLE chapter (
  id          text NOT NULL,
  course_id   text NOT NULL REFERENCES course(id) ON DELETE CASCADE,
  title       text NOT NULL,
  ord         int NOT NULL,                    -- 课程内顺序，来自原始层级
  path        text[] NOT NULL DEFAULT '{}',
  parent_id   text,                            -- 章节可嵌套（现有数据只有一层）
  PRIMARY KEY (course_id, id),
  FOREIGN KEY (course_id, parent_id) REFERENCES chapter (course_id, id)
);

COMMENT ON TABLE chapter IS '章节。由原数据 level=2 的节点提升而来，id 沿用该节点 id 以保证稳定。';

-- -----------------------------------------------------------------------------
-- 4. 知识点（含目录容器节点）
-- -----------------------------------------------------------------------------
CREATE TABLE knowledge_node (
  course_id       text NOT NULL REFERENCES course(id) ON DELETE CASCADE,
  id              text NOT NULL,
  chapter_id      text,
  parent_id       text,
  title           text NOT NULL,
  level           int NOT NULL,
  kind            text NOT NULL CHECK (kind IN ('container', 'knowledge')),
  path            text[] NOT NULL DEFAULT '{}',   -- 原数据冗余路径，保留用于显示
  content_md      text NOT NULL DEFAULT '',       -- 正文，Markdown + $LaTeX$
  exercise_anchor text,                           -- 原数据锚点，等于 id
  source_raw      text,                           -- 原自由文本溯源，原样保留
  review_status   review_status NOT NULL DEFAULT 'draft',
  content_version int NOT NULL DEFAULT 1,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (course_id, id),
  FOREIGN KEY (course_id, parent_id) REFERENCES knowledge_node (course_id, id),
  FOREIGN KEY (course_id, chapter_id) REFERENCES chapter (course_id, id)
);

CREATE INDEX knowledge_node_chapter_idx ON knowledge_node (course_id, chapter_id);
CREATE INDEX knowledge_node_parent_idx  ON knowledge_node (course_id, parent_id);
CREATE INDEX knowledge_node_review_idx  ON knowledge_node (review_status);

COMMENT ON COLUMN knowledge_node.content_md IS '数学正文。$...$ 与 $$...$$ 由前端 KaTeX 渲染，入库不做转换。';

-- 知识点 → 教材片段：问答引用的落点
CREATE TABLE node_chunk (
  course_id    text NOT NULL,
  node_id      text NOT NULL,
  chunk_id     bigint NOT NULL REFERENCES textbook_chunk(id) ON DELETE CASCADE,
  method       text NOT NULL DEFAULT 'manual'
               CHECK (method IN ('manual', 'heading-match', 'embedding', 'llm-proposed')),
  confidence   numeric(3,2) CHECK (confidence BETWEEN 0 AND 1),
  reviewed_by  text,
  PRIMARY KEY (course_id, node_id, chunk_id),
  FOREIGN KEY (course_id, node_id) REFERENCES knowledge_node (course_id, id) ON DELETE CASCADE
);

COMMENT ON TABLE node_chunk IS '知识点-教材锚点。有了它，问答才能回答"教材第 78 页"而不是"模型认为"。';

-- 课程资源（原数据 attachments 里的相对路径，课程级资源库）
CREATE TABLE course_asset (
  course_id  text NOT NULL REFERENCES course(id) ON DELETE CASCADE,
  path       text NOT NULL,                    -- 相对 course-packs/<resourceBase> 的路径
  kind       text NOT NULL DEFAULT 'image',
  sha256     text,
  PRIMARY KEY (course_id, path)
);

-- -----------------------------------------------------------------------------
-- 5. 习题
-- -----------------------------------------------------------------------------
CREATE TABLE exercise (
  course_id           text NOT NULL REFERENCES course(id) ON DELETE CASCADE,
  id                  text NOT NULL,
  chapter_id          text,
  prompt_md           text NOT NULL,
  answer_md           text,
  difficulty          smallint CHECK (difficulty BETWEEN 1 AND 5),
  source_raw          text,                    -- 原自由文本，原样保留
  source_book         text,                    -- 结构化溯源：教材
  source_page         int,                     -- 书页码
  source_pdf_page     int,                     -- PDF 页
  source_no           text,                    -- 原题号
  provenance          exercise_provenance NOT NULL DEFAULT 'unknown',
  provenance_note     text,
  source_url          text,                    -- 网上搜集题的原始地址
  retrieved_at        date,                    -- 抓取日期
  license             text,                    -- 版权状态；网上题默认 unknown
  -- 可信等级（与 review_status 正交，见 v_exercise_verification）
  lean_verified_at    timestamptz,             -- 有编译通过的 Lean 证明
  human_verified_at   timestamptz,             -- 人工逐题审核通过
  human_verified_by   text,
  review_status       review_status NOT NULL DEFAULT 'draft',
  content_version     int NOT NULL DEFAULT 1,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (course_id, id),
  FOREIGN KEY (course_id, chapter_id) REFERENCES chapter (course_id, id),
  -- 网上搜集的题必须留下出处，否则无法评估版权与可信度
  CONSTRAINT web_exercise_needs_url
    CHECK (provenance <> 'web' OR source_url IS NOT NULL)
);

CREATE INDEX exercise_chapter_idx  ON exercise (course_id, chapter_id);
CREATE INDEX exercise_provenance_idx ON exercise (provenance);
CREATE INDEX exercise_review_idx   ON exercise (review_status);

COMMENT ON COLUMN exercise.source_raw IS '原始溯源文本，含"待核"等人工标注，不做清洗，便于审计。';
COMMENT ON COLUMN exercise.provenance_note IS '从 source_raw 解析出的置信说明，如"页码为章节范围，非精确页"。';

-- 习题 ↔ 知识点（多对多）
CREATE TABLE exercise_node (
  course_id   text NOT NULL,
  exercise_id text NOT NULL,
  node_id     text NOT NULL,
  PRIMARY KEY (course_id, exercise_id, node_id),
  FOREIGN KEY (course_id, exercise_id) REFERENCES exercise (course_id, id) ON DELETE CASCADE,
  FOREIGN KEY (course_id, node_id)     REFERENCES knowledge_node (course_id, id) ON DELETE CASCADE
);

CREATE INDEX exercise_node_node_idx ON exercise_node (course_id, node_id);

-- 同 ID 不同表述（现有数据 variants 全为 0，保留结构以防将来）
CREATE TABLE exercise_variant (
  course_id   text NOT NULL,
  exercise_id text NOT NULL,
  ord         int NOT NULL,
  prompt_md   text NOT NULL,
  answer_md   text,
  PRIMARY KEY (course_id, exercise_id, ord),
  FOREIGN KEY (course_id, exercise_id) REFERENCES exercise (course_id, id) ON DELETE CASCADE
);

-- Lean 形式化验证证据链。
-- 核心矛盾：Lean 编译通过只证明"形式化后的命题"为真。若形式化时漏了条件，
-- 证明再漂亮也救不回原题。因此 faithful_* 这几列必须由人填，且不可与 compiled 混为一谈。
CREATE TABLE lean_proof (
  course_id       text NOT NULL,
  exercise_id     text NOT NULL,
  ord             smallint NOT NULL DEFAULT 1,
  statement_lean  text NOT NULL,               -- 形式化后的命题（Lean 源码）
  proof_file      text,                        -- 证明文件在 lean 仓库内的路径
  proof_sha256    text,
  toolchain       text,                        -- 如 leanprover/lean4:v4.12.0 + mathlib@<commit>
  build_log_sha256 text,
  compiled        boolean NOT NULL DEFAULT false,
  compiled_at     timestamptz,
  -- 形式化忠实性：独立于"证明是否通过"的第二道关
  faithful_status review_status NOT NULL DEFAULT 'draft',
  faithful_note   text,                        -- 与原题的条件/结论差异
  faithful_by     text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (course_id, exercise_id, ord),
  FOREIGN KEY (course_id, exercise_id) REFERENCES exercise (course_id, id) ON DELETE CASCADE
);

COMMENT ON COLUMN lean_proof.compiled IS '仅表示 Lean 编译通过，不表示原题正确。';
COMMENT ON COLUMN lean_proof.faithful_status IS '形式化命题与原题是否等价，需人工确认；未确认为 published 时不得作为验证依据。';


-- -----------------------------------------------------------------------------
-- 6. 学术关系（不含 contains，contains 由 parent_id 派生）
-- -----------------------------------------------------------------------------
CREATE TABLE relation (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  course_id     text NOT NULL REFERENCES course(id) ON DELETE CASCADE,
  from_node     text NOT NULL,
  to_node       text NOT NULL,
  type          relation_type NOT NULL,
  evidence      text,                          -- 依据：教材页、定理号、教师说明
  review_status review_status NOT NULL DEFAULT 'draft',
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (course_id, from_node, to_node, type),
  FOREIGN KEY (course_id, from_node) REFERENCES knowledge_node (course_id, id) ON DELETE CASCADE,
  FOREIGN KEY (course_id, to_node)   REFERENCES knowledge_node (course_id, id) ON DELETE CASCADE,
  CHECK (from_node <> to_node)
);

COMMENT ON TABLE relation IS '只存不能派生的关系。层级隶属请看 v_contains，共题关系请看 v_shared_exercise。';

-- -----------------------------------------------------------------------------
-- 7. 派生视图（替代原数据里的冗余数组）
-- -----------------------------------------------------------------------------
CREATE VIEW v_contains AS
  SELECT course_id, parent_id AS from_node, id AS to_node
  FROM knowledge_node
  WHERE parent_id IS NOT NULL;

CREATE VIEW v_shared_exercise AS
  SELECT a.course_id, a.node_id AS from_node, b.node_id AS to_node, a.exercise_id
  FROM exercise_node a
  JOIN exercise_node b
    ON a.course_id = b.course_id AND a.exercise_id = b.exercise_id AND a.node_id < b.node_id;

-- 教材锚点覆盖率：判断一门课能不能开放问答
CREATE VIEW v_anchor_coverage AS
  SELECT k.course_id,
         count(*) FILTER (WHERE k.kind = 'knowledge')                       AS knowledge_nodes,
         count(DISTINCT n.chunk_id)                                         AS anchored_nodes,
         round(100.0 * count(DISTINCT n.node_id)
               / NULLIF(count(*) FILTER (WHERE k.kind = 'knowledge'), 0), 1) AS coverage_pct
  FROM knowledge_node k
  LEFT JOIN node_chunk n ON n.course_id = k.course_id AND n.node_id = k.id
  GROUP BY k.course_id;

-- 可信等级：把"人工审核"与"Lean 验证"两个正交维度和成一个可展示的标签。
--   strongest = 既有人工审核又有 Lean 证明（两证齐全）
--   lean      = 只有 Lean 证明（形式化忠实性仍需人工确认，见 lean_proof.faithful_status）
--   human     = 只有人工审核
--   none      = 都没有（网上搜集的题默认落在这里）
CREATE VIEW v_exercise_verification AS
  SELECT e.course_id,
         e.id,
         e.provenance,
         e.human_verified_at,
         e.lean_verified_at,
         EXISTS (
           SELECT 1 FROM lean_proof p
           WHERE p.course_id = e.course_id AND p.exercise_id = e.id
             AND p.compiled AND p.faithful_status = 'teacher-reviewed'
         ) AS lean_confirmed,
         CASE
           WHEN e.human_verified_at IS NOT NULL AND EXISTS (
                  SELECT 1 FROM lean_proof p
                  WHERE p.course_id = e.course_id AND p.exercise_id = e.id
                    AND p.compiled AND p.faithful_status = 'teacher-reviewed'
                ) THEN 'strongest'
           WHEN e.human_verified_at IS NOT NULL THEN 'human'
           WHEN EXISTS (
                  SELECT 1 FROM lean_proof p
                  WHERE p.course_id = e.course_id AND p.exercise_id = e.id
                    AND p.compiled AND p.faithful_status = 'teacher-reviewed'
                ) THEN 'lean'
           ELSE 'none'
         END AS tier
  FROM exercise e;

-- 可发布/可组卷的题：三档里只有人工审核过的才允许进正式作业与考试。
-- 网上搜集（web）与 AI 生成（ai-generated）即便人工审核过，也要求显式填写 license。
CREATE VIEW v_publishable_exercise AS
  SELECT e.course_id, e.id, e.prompt_md, e.difficulty
  FROM exercise e
  JOIN v_exercise_verification v ON v.course_id = e.course_id AND v.id = e.id
  WHERE v.tier IN ('human', 'strongest')
    AND e.review_status = 'published'
    AND (e.provenance NOT IN ('web', 'ai-generated') OR e.license IS NOT NULL);

-- -----------------------------------------------------------------------------
-- 8. 审核工作流：队列、错误分类、不可变修订历史
-- -----------------------------------------------------------------------------
CREATE TYPE review_target AS ENUM ('exercise', 'knowledge_node', 'anchor', 'course');

CREATE TYPE review_state AS ENUM (
  'pending',             -- 待审核（机器校验已过）
  'in-review',           -- 有人认领
  'changes-requested',   -- 退回修改，附错误清单
  'approved',            -- 审核通过，待发布
  'published',           -- 已发布，学生可见
  'rejected'             -- 不采用（重复题、版权不明等）
);

CREATE TYPE review_category AS ENUM (
  'content-error',       -- 题干错误
  'answer-error',        -- 答案/解析错误
  'answer-missing',      -- 缺解答
  'format-error',        -- 公式或排版渲染失败
  'wrong-node',          -- 挂错知识点
  'missing-node',        -- 该挂未挂
  'over-linked',         -- 挂载过多，实为单一知识点
  'source-missing',      -- 溯源缺失
  'source-wrong',        -- 溯源错误
  'page-imprecise',      -- 只有章节范围，无精确页
  'difficulty-mismatch', -- 难度标定失真
  'duplicate',           -- 重复或同源异文
  'hierarchy-error',     -- 知识点层级错误
  'other'
);

CREATE TABLE review_task (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  course_id   text NOT NULL REFERENCES course(id) ON DELETE CASCADE,
  target      review_target NOT NULL,
  target_id   text NOT NULL,
  state       review_state NOT NULL DEFAULT 'pending',
  priority    smallint NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
  assignee    text,
  claimed_at  timestamptz,
  decided_at  timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (course_id, target, target_id)
);

CREATE INDEX review_task_queue_idx ON review_task (state, priority, created_at);
CREATE INDEX review_task_course_idx ON review_task (course_id, state);

CREATE TABLE review_issue (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  task_id    bigint NOT NULL REFERENCES review_task(id) ON DELETE CASCADE,
  category   review_category NOT NULL,
  severity   text NOT NULL DEFAULT 'major' CHECK (severity IN ('blocker', 'major', 'minor')),
  detail     text,
  raised_by  text NOT NULL,
  raised_at  timestamptz NOT NULL DEFAULT now(),
  resolved_at timestamptz,
  resolution text
);

CREATE INDEX review_issue_task_idx ON review_issue (task_id);

-- 不可变修订历史：审核改了什么都留底，可逐版回滚
CREATE TABLE exercise_revision (
  course_id    text NOT NULL,
  exercise_id  text NOT NULL,
  version      int NOT NULL,
  prompt_md    text NOT NULL,
  answer_md    text,
  difficulty   smallint,
  node_ids     text[] NOT NULL DEFAULT '{}',
  source_page  int,
  provenance   exercise_provenance,
  human_verified_at timestamptz,
  changed_by   text NOT NULL,
  changed_at   timestamptz NOT NULL DEFAULT now(),
  change_note  text,
  PRIMARY KEY (course_id, exercise_id, version),
  FOREIGN KEY (course_id, exercise_id) REFERENCES exercise (course_id, id) ON DELETE CASCADE
);

CREATE OR REPLACE FUNCTION snapshot_exercise_revision() RETURNS trigger AS $$
DECLARE
  node_ids text[];
BEGIN
  SELECT coalesce(array_agg(node_id ORDER BY node_id), '{}') INTO node_ids
  FROM exercise_node WHERE course_id = OLD.course_id AND exercise_id = OLD.id;

  INSERT INTO exercise_revision (
    course_id, exercise_id, version, prompt_md, answer_md, difficulty,
    node_ids, source_page, provenance, human_verified_at, changed_by, change_note
  ) VALUES (
    OLD.course_id, OLD.id, OLD.content_version, OLD.prompt_md, OLD.answer_md, OLD.difficulty,
    node_ids, OLD.source_page, OLD.provenance, OLD.human_verified_at,
    coalesce(current_setting('app.actor', true), 'unknown'),
    coalesce(current_setting('app.change_note', true), 'before update')
  ) ON CONFLICT DO NOTHING;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER exercise_snapshot_on_update
  BEFORE UPDATE ON exercise
  FOR EACH ROW EXECUTE FUNCTION snapshot_exercise_revision();

COMMENT ON TABLE exercise_revision IS '题目修订历史。触发器在每次 UPDATE 前快照旧值；操作者取自 app.actor 会话变量。';

-- -----------------------------------------------------------------------------
-- 9. 教学：班级、作业、提交、成绩
-- -----------------------------------------------------------------------------
CREATE TABLE teaching_class (
  id          text PRIMARY KEY,
  course_id   text NOT NULL REFERENCES course(id) ON DELETE CASCADE,
  name        text NOT NULL,
  term        text NOT NULL,                   -- 如 2026-2027-1
  teacher_id  text,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE enrollment (
  class_id    text NOT NULL REFERENCES teaching_class(id) ON DELETE CASCADE,
  student_id  text NOT NULL,
  enrolled_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (class_id, student_id)
);

CREATE TABLE assignment (
  id          text PRIMARY KEY,
  class_id    text NOT NULL REFERENCES teaching_class(id) ON DELETE CASCADE,
  title       text NOT NULL,
  kind        text NOT NULL DEFAULT 'homework'
              CHECK (kind IN ('homework', 'quiz', 'exam')),
  rubric      jsonb,                           -- 评分细则，模型评分时按此执行
  due_at      timestamptz,
  published_at timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE assignment_item (
  assignment_id text NOT NULL REFERENCES assignment(id) ON DELETE CASCADE,
  exercise_id   text NOT NULL,
  course_id     text NOT NULL,
  ord           int NOT NULL,
  points        numeric(5,1) NOT NULL DEFAULT 10,
  PRIMARY KEY (assignment_id, ord),
  FOREIGN KEY (course_id, exercise_id) REFERENCES exercise (course_id, id)
);

CREATE TABLE submission (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  assignment_id text NOT NULL REFERENCES assignment(id) ON DELETE CASCADE,
  student_id    text NOT NULL,
  submitted_at  timestamptz NOT NULL DEFAULT now(),
  status        text NOT NULL DEFAULT 'submitted'
                CHECK (status IN ('submitted', 'ai-graded', 'teacher-confirmed', 'returned')),
  UNIQUE (assignment_id, student_id)
);

CREATE TABLE submission_item (
  submission_id   uuid NOT NULL REFERENCES submission(id) ON DELETE CASCADE,
  ord             int NOT NULL,
  answer_md       text,
  answer_image    text,                        -- 对象存储路径；拍照手写题用
  auto_verdict    text CHECK (auto_verdict IN ('correct', 'wrong', 'partial', 'unjudged')),
  auto_score      numeric(5,1),
  auto_evidence   jsonb,                       -- CAS 判定输入输出 / 模型评分理由
  teacher_score   numeric(5,1),                -- 只有这一列进成绩
  teacher_comment text,
  graded_by       text,
  graded_at       timestamptz,
  PRIMARY KEY (submission_id, ord)
);

COMMENT ON COLUMN submission_item.auto_score IS 'AI 建议分。只有 teacher_score 可进入正式成绩。';

-- -----------------------------------------------------------------------------
-- 10. AI：会话、引用、模型调用计量
-- -----------------------------------------------------------------------------
CREATE TABLE ai_conversation (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  course_id  text REFERENCES course(id) ON DELETE SET NULL,
  node_id    text,
  user_ref   text,                             -- 学号/工号，按隐私要求可存哈希
  title      text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ai_message (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  conversation_id uuid NOT NULL REFERENCES ai_conversation(id) ON DELETE CASCADE,
  role            text NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
  content_md      text NOT NULL,
  refused         boolean NOT NULL DEFAULT false,  -- 检索无依据时拒答
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ai_message_conv_idx ON ai_message (conversation_id, id);

CREATE TABLE ai_citation (
  message_id bigint NOT NULL REFERENCES ai_message(id) ON DELETE CASCADE,
  ord        int NOT NULL,
  chunk_id   bigint REFERENCES textbook_chunk(id) ON DELETE SET NULL,
  course_id  text,
  node_id    text,
  page       int,
  excerpt    text,
  PRIMARY KEY (message_id, ord)
);

CREATE TABLE model_call (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  purpose       text NOT NULL,                  -- chat / grading / embedding / slicing
  provider      text NOT NULL,
  model         text NOT NULL,
  prompt_tokens int,
  completion_tokens int,
  cost_usd      numeric(10,6),
  latency_ms    int,
  succeeded     boolean NOT NULL,
  error         text,
  prompt_version text,                          -- 提示词版本，便于回归
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX model_call_created_idx ON model_call (created_at);

-- -----------------------------------------------------------------------------
-- 11. 审计（谁改了什么，为什么）
-- -----------------------------------------------------------------------------
CREATE TABLE audit_log (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  actor      text NOT NULL,
  action     text NOT NULL,                     -- import / review / publish / grade
  entity     text NOT NULL,                     -- knowledge_node / exercise / submission
  entity_key text NOT NULL,
  detail     jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- 12. updated_at 维护
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER course_touch        BEFORE UPDATE ON course
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER knowledge_node_touch BEFORE UPDATE ON knowledge_node
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER exercise_touch      BEFORE UPDATE ON exercise
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- ---------------------------------------------------------------
-- 0002_learning_jobs.sql
-- ---------------------------------------------------------------
-- 0002：为「问答」与「AI 学伴」补上缺的三块
--
-- 为什么现在加而不是以后加：
--   1. pipeline_job —— 教材切片/OCR/向量化是分钟级任务，不能塞进 HTTP 请求。
--      没有任务表，服务器上第一次上传教材就会超时，然后被迫改成同步脚本，
--      再然后就没有进度、重试、失败原因。
--   2. exercise_attempt / node_mastery —— 学伴的"因材施教"全靠这两张表。
--      没有它们，学伴只能猜学生水平，或者退化成通用聊天。
--   3. qa_eval_* —— 没有评测集，换了模型或提示词之后无法回答"变好了还是变差了"。
--      评测集只能在系统上线初期积累，事后补是补不出来的。
--
-- 另：textbook_chunk.embedding_model —— 记录每块向量是用哪个模型算的。
--     换 embedding 模型时若不分列记录，新旧向量混在一起检索，结果会悄悄变差。


-- -----------------------------------------------------------------------------
-- 1. 教材切片：记录向量来源
-- -----------------------------------------------------------------------------
ALTER TABLE textbook_chunk
  ADD COLUMN embedding_model text,          -- 如 bge-m3 / text-embedding-3-small
  ADD COLUMN embedded_at     timestamptz;

COMMENT ON COLUMN textbook_chunk.embedding_model IS
  '生成该向量的模型标识。换模型时必须重算，不能新旧混用。';

CREATE INDEX textbook_chunk_embedding_model_idx ON textbook_chunk (embedding_model);

-- -----------------------------------------------------------------------------
-- 2. 管线任务（教材上传 → 抽取 → 切片 → 锚点 → 向量化）
-- -----------------------------------------------------------------------------
CREATE TABLE pipeline_job (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind        text NOT NULL CHECK (kind IN ('upload','probe','extract','chunk','anchor','embed','ocr')),
  status      text NOT NULL DEFAULT 'queued'
              CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
  -- 幂等键：同一份教材重复上传时不要重复跑
  idem_key    text,
  payload     jsonb NOT NULL DEFAULT '{}',   -- 输入：textbook_id / 页码范围 / 模型
  result      jsonb,                          -- 输出：产出条数、文件路径、耗时
  error       text,
  progress    smallint NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
  attempts    smallint NOT NULL DEFAULT 0,
  created_by  text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  started_at  timestamptz,
  finished_at timestamptz,
  UNIQUE (idem_key)
);

CREATE INDEX pipeline_job_status_idx ON pipeline_job (status, created_at);
CREATE INDEX pipeline_job_kind_idx   ON pipeline_job (kind, created_at DESC);

COMMENT ON TABLE pipeline_job IS
  '异步任务。API 只入队并立即返回 job_id，worker 领取执行；前端轮询进度。';

-- -----------------------------------------------------------------------------
-- 3. 学伴所需的学习数据
-- -----------------------------------------------------------------------------

-- 每一次作答都是一条原始证据，不做覆盖写
CREATE TABLE exercise_attempt (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  student_id    text NOT NULL,
  course_id     text NOT NULL,
  exercise_id   text NOT NULL,
  class_id      text REFERENCES teaching_class(id) ON DELETE SET NULL,
  attempt_no    smallint NOT NULL DEFAULT 1,
  answer_md     text,
  duration_sec  int,
  submitted_at  timestamptz NOT NULL DEFAULT now(),
  -- AI 初评与教师评分分列，只有后者能进成绩（与 submission_item 保持一致）
  auto_verdict  text,
  auto_score    numeric(5,2),
  auto_evidence jsonb,
  teacher_score numeric(5,2),
  teacher_comment text,
  UNIQUE (student_id, course_id, exercise_id, attempt_no),
  FOREIGN KEY (course_id, exercise_id) REFERENCES exercise (course_id, id) ON DELETE CASCADE
);

CREATE INDEX exercise_attempt_student_idx ON exercise_attempt (student_id, course_id, submitted_at DESC);
CREATE INDEX exercise_attempt_exercise_idx ON exercise_attempt (course_id, exercise_id);

-- 掌握度：由重算作业得出，不由人手工填
CREATE TABLE node_mastery (
  student_id   text NOT NULL,
  course_id    text NOT NULL,
  node_id      text NOT NULL,
  mastery      numeric(4,3) NOT NULL DEFAULT 0 CHECK (mastery BETWEEN 0 AND 1),
  attempts     smallint NOT NULL DEFAULT 0,
  correct      smallint NOT NULL DEFAULT 0,
  last_seen_at timestamptz,
  computed_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (student_id, course_id, node_id),
  FOREIGN KEY (course_id, node_id) REFERENCES knowledge_node (course_id, id) ON DELETE CASCADE
);

COMMENT ON TABLE node_mastery IS
  '掌握度快照。由 exercise_attempt 聚合重算，可随时丢弃重建——所以它是缓存，不是事实。';

-- 学伴需要知道"为谁服务"，且学生个人数据必须有同意与保留期
CREATE TABLE student_profile (
  student_id     text PRIMARY KEY,
  display_name   text,
  preferences    jsonb NOT NULL DEFAULT '{}',   -- 语言偏好、解释详略、是否要提示而非答案
  consented_at   timestamptz,                    -- 同意使用 AI 学伴的时间
  retention_until date,                          -- 到期清理
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE student_profile IS
  '个人数据。consented_at 为空时不得保存对话记忆；retention_until 到期应清理。';

-- -----------------------------------------------------------------------------
-- 4. 问答评测集：衡量"答得好不好"的唯一手段
-- -----------------------------------------------------------------------------
CREATE TABLE qa_eval_case (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  course_id      text REFERENCES course(id) ON DELETE CASCADE,
  question       text NOT NULL,
  expected_points text[] NOT NULL DEFAULT '{}',  -- 答案必须覆盖的要点
  must_cite      boolean NOT NULL DEFAULT true,  -- 是否必须有教材引用
  tags           text[] NOT NULL DEFAULT '{}',   -- 如 定义 / 定理应用 / 计算
  created_by     text,
  created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE qa_eval_run (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  label           text NOT NULL,                 -- 如 prompt-v3-bge-m3
  model           text NOT NULL,
  prompt_version  text,
  corpus_snapshot text,                          -- 教材切片版本/日期
  started_at      timestamptz NOT NULL DEFAULT now(),
  finished_at     timestamptz,
  metrics         jsonb                          -- 引用命中率、要点覆盖率、拒答率
);

CREATE TABLE qa_eval_result (
  run_id       bigint NOT NULL REFERENCES qa_eval_run(id) ON DELETE CASCADE,
  case_id      bigint NOT NULL REFERENCES qa_eval_case(id) ON DELETE CASCADE,
  answer_md    text,
  cited_chunks bigint[] NOT NULL DEFAULT '{}',
  score        numeric(4,2),
  passed       boolean,
  note         text,
  PRIMARY KEY (run_id, case_id)
);

COMMENT ON TABLE qa_eval_case IS
  '评测集。上线前先写 50 条真问题；之后每次换模型/改提示词都跑一遍，否则无法判断是否退步。';

-- ---------------------------------------------------------------
-- 0003_multilingual.sql
-- ---------------------------------------------------------------
-- 0003：多语言教材支持（英文教材、双语班、译本）
--
-- 为什么现在补：语言字段一旦缺，后面每加一种语言都要改三处——
--   1. 全文检索：中文要 pg_bigm 二元组，英文要用 PG 内置 english 词典分词，
--      没有 language 列就无法按行选择配置，只能全库用同一种，两头都不准。
--   2. 术语对照：数学教材中英术语（一致收敛 / uniform convergence）不统一，
--      学生用中文问，教材是英文，检索必然落空。
--   3. 检索范围：混排教材一句话里中英夹杂，按整本教材的 language 走会判错。
--
-- 维度不变：1024 维对多语言够用，前提是选多语言模型（BGE-M3 或 qwen 系列）。
-- 维度和语言能力无关，语言能力是模型给的。


-- -----------------------------------------------------------------------------
-- 1. 教材与切片：语言标记
-- -----------------------------------------------------------------------------
ALTER TABLE textbook
  ADD COLUMN language        text NOT NULL DEFAULT 'zh',  -- BCP-47 主语言：zh / en
  ADD COLUMN original_title  text,                        -- 外文教材原名，如 Principles of Mathematical Analysis
  ADD COLUMN translated_from text;                        -- 译本的原语言：zh 译本填 en

COMMENT ON COLUMN textbook.language IS
  '教材主语言。决定全文检索用哪套分词配置（zh→pg_bigm，en→english）。';

ALTER TABLE textbook_chunk
  ADD COLUMN language text;   -- NULL = 继承 textbook.language；中英混排段才单独填

COMMENT ON COLUMN textbook_chunk.language IS
  'NULL 表示继承所属教材的语言。仅当该片段语言与教材主语言不同时才填。';

CREATE INDEX textbook_language_idx ON textbook (course_id, language);

-- 取片段实际语言的统一入口，避免各处重复写 coalesce
CREATE VIEW v_chunk_language AS
  SELECT c.id AS chunk_id,
         c.textbook_id,
         c.seq,
         coalesce(c.language, t.language) AS language,
         t.course_id,
         c.page_from,
         c.page_to
  FROM textbook_chunk c
  JOIN textbook t ON t.id = c.textbook_id;

-- -----------------------------------------------------------------------------
-- 2. 习题也可能有外文版（双语班、原版教材习题）
-- -----------------------------------------------------------------------------
ALTER TABLE exercise
  ADD COLUMN language text NOT NULL DEFAULT 'zh';

COMMENT ON COLUMN exercise.language IS
  '题干主语言。同一道题的中英文版本应视为两条不同 exercise，而不是变体。';

-- -----------------------------------------------------------------------------
-- 3. 术语对照表：跨语言检索真正要靠的东西
-- -----------------------------------------------------------------------------
-- 为什么不是"让模型自己翻"：
--   通用翻译会把 "uniform convergence" 译成"均匀收敛"，
--   而数学界固定译法是"一致收敛"。术语表要人工维护，不能用通用翻译代替。
CREATE TABLE glossary_term (
  id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  course_id text NOT NULL REFERENCES course(id) ON DELETE CASCADE,
  term_zh   text NOT NULL,
  term_en   text NOT NULL,
  aliases   text[] NOT NULL DEFAULT '{}',   -- 缩写、旧译名、符号读法
  node_id   text,                            -- 对应知识点，便于反查
  note      text,                            -- 易混术语的区分说明
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (course_id, term_zh, term_en),
  FOREIGN KEY (course_id, node_id) REFERENCES knowledge_node (course_id, id) ON DELETE SET NULL
);

CREATE INDEX glossary_term_zh_idx ON glossary_term (course_id, term_zh);
CREATE INDEX glossary_term_en_idx ON glossary_term (course_id, lower(term_en));
CREATE INDEX glossary_term_aliases_idx ON glossary_term USING gin (aliases);

COMMENT ON TABLE glossary_term IS
  '中英数学术语对照。用于查询扩展：学生用中文问、教材是英文时，先把术语翻成英文再检索。';

-- 检索用的双向展开视图：一个中文词能查到对应英文，反之亦然
CREATE VIEW v_glossary_pair AS
  SELECT course_id, node_id, term_zh AS term, term_en AS counterpart, 'zh'::text AS lang
  FROM glossary_term
  UNION ALL
  SELECT course_id, node_id, term_en AS term, term_zh AS counterpart, 'en'::text AS lang
  FROM glossary_term;

-- ---------------------------------------------------------------
-- 0004_model_endpoint.sql
-- ---------------------------------------------------------------
-- 0004：模型端点登记表（数据不出校的允许清单）
--
-- 学院要求：数据不出校，但校外可以访问网站。
-- 这两件事不矛盾，矛盾的是"随手调用外部 API"——RAG 每次问答都把教材片段
-- 塞进提示词，只要 base_url 指向校外，数据就已经出去了，而且没有任何提示。
--
-- 所以端点必须登记在库里：谁能用、是不是校内、用的是哪个模型。
-- 代码里的 ALLOW_EXTERNAL_MODELS 是开关，这张表是账本。两者都要有：
-- 开关防止误调，账本让审计能回答"到底用过哪些模型"。


CREATE TABLE model_endpoint (
  name       text PRIMARY KEY,                 -- campus-gateway / local-vllm-bge-m3
  kind       text NOT NULL CHECK (kind IN ('chat', 'embedding', 'rerank')),
  base_url   text NOT NULL,                    -- OpenAI 兼容地址，如 http://10.0.0.8:8001/v1
  model      text NOT NULL,                    -- 模型标识，如 qwen2.5-14b-instruct
  data_scope text NOT NULL DEFAULT 'internal'
             CHECK (data_scope IN ('internal', 'external')),
  dim        int,                              -- embedding 维度；chat 为 NULL
  max_context int,                             -- 上下文窗口，用于估算提示词预算
  enabled    boolean NOT NULL DEFAULT true,
  notes      text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  -- 外部端点必须写明理由，否则半年后没人知道为什么允许它存在
  CONSTRAINT external_needs_note CHECK (data_scope = 'internal' OR notes IS NOT NULL),
  CONSTRAINT embedding_needs_dim  CHECK (kind <> 'embedding' OR dim IS NOT NULL)
);

COMMENT ON TABLE model_endpoint IS
  '模型端点允许清单。data_scope=external 的端点默认不可用于含校内内容的请求。';

CREATE UNIQUE INDEX model_endpoint_one_enabled_chat
  ON model_endpoint (kind) WHERE enabled AND kind = 'chat';

CREATE UNIQUE INDEX model_endpoint_one_enabled_embedding
  ON model_endpoint (kind) WHERE enabled AND kind = 'embedding';

-- vector 列维度必须与启用的 embedding 端点一致，否则写进去的向量没法比较
CREATE VIEW v_embedding_dim_check AS
  SELECT e.name,
         e.model,
         e.dim                                   AS endpoint_dim,
         (SELECT atttypmod FROM pg_attribute
           WHERE attrelid = 'textbook_chunk'::regclass AND attname = 'embedding') AS column_typmod
  FROM model_endpoint e
  WHERE e.kind = 'embedding' AND e.enabled;

CREATE TRIGGER model_endpoint_touch BEFORE UPDATE ON model_endpoint
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- 教材原文不得以附件形式外流：检索只返回必要片段与页码，不提供 PDF 下载入口。
-- 这里记一条策略，便于后面写接口时对照。
COMMENT ON COLUMN textbook.copyright_status IS
  '版权状态。internal-only 时禁止对外提供全文或下载，只允许回答中引用必要片段。';

-- ---------------------------------------------------------------
-- 0005_egress_policy.sql
-- ---------------------------------------------------------------
-- 0005：模型端点语义调整（数据可出校，改用国内 API）
--
-- 背景变化（2026-10-20 学院确认）：
--   0004 写的时候假设"数据不出校"，所以把外部端点当成例外，要求必须写说明。
--   现在确认可以出校、使用国内大模型 API，所以外部端点变成常态。
--
-- 但约束不放松，只换方向：
--   原来防"数据被发出去" → 现在防"发给了没批准的供应商"。
--   具体落在两处：代码里的 allowlist（server/app/llm.py），和这张表里的审批人。
--
-- 表结构改动很小，主要是把"谁批准了这个供应商"记下来。
-- 半年后审计会问：为什么教材内容会出现在这家公司的服务器上？


ALTER TABLE model_endpoint
  ADD COLUMN approved_by text,                 -- 谁批准的（校内责任人）
  ADD COLUMN approved_at timestamptz;          -- 何时批准

COMMENT ON TABLE model_endpoint IS
  '模型端点登记表。记录用了哪个供应商、哪个模型、内容是否会离开校园网，以及谁批准的。';

COMMENT ON COLUMN model_endpoint.data_scope IS
  'internal=校内服务，内容不出内网；external=外部供应商，请求会离开校园网，'
  '需先确认合规并在 approved_by 留痕。';

COMMENT ON COLUMN model_endpoint.approved_by IS
  '批准使用该外部供应商的责任人。外部端点必须填写，否则半年后无人能回答"为什么允许"。';

-- 外部端点必须同时具备说明与批准人：这是审计唯一能追的两条线索
CREATE OR REPLACE VIEW v_external_endpoint_audit AS
  SELECT name, model, base_url, notes, approved_by, approved_at, enabled
  FROM model_endpoint
  WHERE data_scope = 'external';

-- ---------------------------------------------------------------
-- 0006_model_config.sql
-- ---------------------------------------------------------------
-- 0006：模型接口配置界面所需字段
--
-- 密钥的存放方式（重要）：
--   不把密钥放进数据库。原因有两个，都不是洁癖：
--     1. 数据库备份、只读副本、讲师临时取的 dump，都会连带把密钥带走；
--     2. 加密入库要依赖 pgcrypto 之类的扩展，还得自己管密钥轮换；
--        这些复杂度换不来多少实际收益，不如让密钥根本不进库。
--   所以密钥存服务器文件（0600），库里只存相对路径。接口永不回显密钥，
--   只返回"是否已配置"。
--
-- 这样做的代价：密钥与数据库不在同一个备份里。迁移服务器时要单独搬 secrets/ 目录
-- ——这条写进部署指南。


ALTER TABLE model_endpoint
  ADD COLUMN api_key_path       text,          -- 相对 SECRETS_DIR，如 deepseek.key
  ADD COLUMN api_key_updated_at timestamptz,
  ADD COLUMN updated_by         text,          -- 最后一次修改人（谁配的）
  ADD COLUMN last_test_at       timestamptz,   -- 最近一次连通性自检
  ADD COLUMN last_test_ok       boolean,
  ADD COLUMN last_test_note     text;

COMMENT ON COLUMN model_endpoint.api_key_path IS
  '密钥文件名（相对 SECRETS_DIR）。库里不存密钥本身，只存"密钥在哪"。';
COMMENT ON COLUMN model_endpoint.last_test_note IS
  '自检失败原因摘要，截断到 300 字。成功时为空。';

-- 给配置界面用的读视图。刻意不包含 api_key_path：
-- 前端不需要知道路径，知道路径只会让攻击者少查一步。
CREATE VIEW v_model_endpoint_public AS
  SELECT name,
         kind,
         base_url,
         model,
         data_scope,
         dim,
         max_context,
         enabled,
         notes,
         approved_by,
         approved_at,
         updated_by,
         updated_at,
         (api_key_path IS NOT NULL) AS key_configured,
         api_key_updated_at,
         last_test_at,
         last_test_ok,
         last_test_note
  FROM model_endpoint;

COMMENT ON VIEW v_model_endpoint_public IS
  '配置界面的读取来源。不含密钥路径，只给 key_configured 布尔值。';

-- 启用状态只能有一个 chat / 一个 embedding（0004 已建索引，这里补个可读的约束说明）
COMMENT ON INDEX model_endpoint_one_enabled_chat IS
  '同一时刻只能启用一个对话端点，避免"到底在用哪个模型"说不清。';

-- ---------------------------------------------------------------
-- 0007_generation_jobs.sql
-- ---------------------------------------------------------------
-- 0007：教材上传 → 排队 → 生成资源包（第一章垂直切片）
--
-- 0002 已经把 pipeline_job 建好了（queued/running/succeeded/failed、progress、attempts、idem_key），
-- 但 kind 里没有 "generate"，也没有地方记"这个任务是哪门课、哪本教材的"。
-- 这一版补上，并把 PDF 抽出来的文本落到 textbook 上，避免每次生成都重新抽一遍。
--
-- 不做的事：不给 pipeline_job.course_id 加外键。课程表是导入产物，重建课程时
-- 外键会让历史任务连带删除——任务日志的价值恰恰在于课程被删之后还能看出发生过什么。


-- -----------------------------------------------------------------------------
-- 1. 生成任务
-- -----------------------------------------------------------------------------
ALTER TABLE pipeline_job DROP CONSTRAINT IF EXISTS pipeline_job_kind_check;
ALTER TABLE pipeline_job
  ADD CONSTRAINT pipeline_job_kind_check
  CHECK (kind IN ('upload','probe','extract','chunk','anchor','embed','ocr','generate'));

ALTER TABLE pipeline_job
  ADD COLUMN course_id    text,   -- 目标课程；为空表示"新建课程"
  ADD COLUMN textbook_id  text,   -- 输入教材
  ADD COLUMN requested_by text,   -- 谁点的生成，出了问题能找到人
  ADD COLUMN parent_id    bigint REFERENCES pipeline_job(id) ON DELETE SET NULL;

COMMENT ON COLUMN pipeline_job.parent_id IS
  '子任务指向父任务。整本书的生成会拆成每章一个子任务，失败只重跑那一章。';

CREATE INDEX pipeline_job_course_idx   ON pipeline_job (course_id, created_at DESC);
CREATE INDEX pipeline_job_textbook_idx ON pipeline_job (textbook_id);

-- -----------------------------------------------------------------------------
-- 2. 教材文本（抽一次就存下来）
-- -----------------------------------------------------------------------------
ALTER TABLE textbook
  ADD COLUMN text_path   text,    -- 抽取出的 Markdown，带 <!-- page N --> 页标记
  ADD COLUMN text_chars  int,
  ADD COLUMN extracted_by text,   -- 抽取引擎：pypdf / pdftotext / 手工
  ADD COLUMN extracted_at timestamptz;

COMMENT ON COLUMN textbook.text_path IS
  '抽取后的纯文本。PDF 解析库不是到处都装得上，抽好的文本要能复用，不能每次重抽。';

-- ---------------------------------------------------------------
-- 0008_book_generation.sql
-- ---------------------------------------------------------------
-- 0008：整本书生成——按章拆子任务，失败只重跑那一章
--
-- 0007 让"一章"能跑。老师真正要的是上传一个 PDF 就完事，所以这一版把一本书
-- 拆成每章一个子任务：父任务负责切章与汇总进度，子任务各跑一次模型调用。
--
-- 关键约束：同一本书的同一章，不能因为重跑父任务就再建一个子任务——
-- 那会把同一章的钱和审核工作量翻倍。用 (parent_id, chapter_key) 唯一索引顶住，
-- 重跑父任务只会把失败的子任务重新排队。
--
-- 为什么把 chapter_key 存成列而不是塞进 payload：唯一索引要能建在列上，
-- 而且运维查"这一章跑了几次"时不用解 JSON。


ALTER TABLE pipeline_job DROP CONSTRAINT IF EXISTS pipeline_job_kind_check;
ALTER TABLE pipeline_job
  ADD CONSTRAINT pipeline_job_kind_check
  CHECK (kind IN ('upload','probe','extract','chunk','anchor','embed','ocr',
                  'generate','generate-book'));

ALTER TABLE pipeline_job
  ADD COLUMN chapter_key   text,   -- ch-3 / part-2，父任务内唯一
  ADD COLUMN chapter_title text;   -- 章标题，展示用

CREATE UNIQUE INDEX pipeline_job_chapter_uniq
  ON pipeline_job (parent_id, chapter_key)
  WHERE parent_id IS NOT NULL AND chapter_key IS NOT NULL;

CREATE INDEX pipeline_job_parent_idx ON pipeline_job (parent_id);

-- ---------------------------------------------------------------
-- 0009_content_import.sql
-- ---------------------------------------------------------------
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

-- ---------------------------------------------------------------
-- 0010_publish.sql
-- ---------------------------------------------------------------
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

-- ---------------------------------------------------------------
-- 0011_visibility_policy.sql
-- ---------------------------------------------------------------
-- 0011：学生可见范围（验证等级闸）
--
-- 审核不是二元的。一道题的可信程度至少有四档（见 v_exercise_verification）：
--   none       导入进来、没人看过
--   lean       Lean 证明编译通过，且形式化忠实性经人工确认
--   human      人工逐题审核通过（human_verified_at）
--   strongest  两者都有
--
-- 学校在不同阶段要的口径不一样：开发期只能全放，正式上课要"只放人工审核过的"。
-- 这一版把"口径"变成库里的一个开关，而不是散落在各个页面里的 if。
--
-- 与发布闸的关系：review_status = 'published' 决定这道题"能不能出现"，
-- visibility_policy 决定"哪一档可信度才允许出现"。两者是 AND，不是替代。
--
-- 默认 'all'：与引入本表之前的行为完全一致，升级不会让学生端突然空掉。


CREATE TABLE visibility_policy (
  id            text PRIMARY KEY DEFAULT 'default' CHECK (id = 'default'),
  exercise_tier text NOT NULL DEFAULT 'all' CHECK (exercise_tier IN ('human', 'verified', 'all')),
  qa_tier       text NOT NULL DEFAULT 'all' CHECK (qa_tier IN ('human', 'verified', 'all')),
  note          text,
  updated_at    timestamptz NOT NULL DEFAULT now(),
  updated_by    text
);

COMMENT ON TABLE visibility_policy IS
  '学生可见范围。exercise_tier 管习题与测验，qa_tier 管 AI 问答的完整解答模式；默认 all，与引入本表之前一致。';

COMMENT ON COLUMN visibility_policy.exercise_tier IS
  'human = 仅人工审核通过；verified = Lean 验证或人工审核；all = 不设门槛（含未验证）。';

INSERT INTO visibility_policy (id) VALUES ('default') ON CONFLICT (id) DO NOTHING;

COMMIT;
