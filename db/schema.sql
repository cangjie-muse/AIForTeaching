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

-- ---------------------------------------------------------------
-- 0012_visibility_scope_and_graph.sql
-- ---------------------------------------------------------------
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

-- ---------------------------------------------------------------
-- 0013_accounts_and_sessions.sql
-- ---------------------------------------------------------------
-- 0013：账号与会话（本地账号密码登录），为统一认证留同一张网
--
-- 为什么要有这张表：
--   之前身份只来自请求头（AUTH_MODE=dev），能跑但等于没有登录——浏览器里
--   谁都能把自己写成 admin。要给学生/教师/管理员分权限，先得有一个"人"的实体。
--
-- 为什么现在就把 CAS 的位置留出来：
--   app_account.password_hash 允许为 NULL，表示"这个人只能走统一认证"。
--   接 CAS 时只做两件事：回调里按 uid 找到（或自动创建）account 行，然后
--   签发同一种 app_session。权限判断、审计、页面闸门一行都不用改——
--   它们都只看 Actor.roles，不关心身份从哪来。
--
-- 角色只有三个：student / teacher / admin。
--   审核相关的旧代码里写着 require("reviewer")，由 Actor.has() 把 teacher
--   映射成 reviewer（见 server/app/auth.py），不在这里多造一个人工角色。


CREATE TABLE app_account (
  id                    text PRIMARY KEY,
  username              text NOT NULL UNIQUE,          -- 登录名，统一小写
  display_name          text NOT NULL,
  role                  text NOT NULL
    CONSTRAINT app_account_role_check CHECK (role IN ('student', 'teacher', 'admin')),
  password_hash         text,                          -- NULL = 只能走统一认证
  password_updated_at   timestamptz,
  must_change_password  boolean NOT NULL DEFAULT false,
  -- 连续失败与锁定：破密码要先过这一关。次数够了锁一段时间，
  -- 成功登录清零。不锁的后果是脚本半夜跑字典，日志里只有几千条 401。
  failed_attempts       integer NOT NULL DEFAULT 0,
  locked_until          timestamptz,
  disabled_at           timestamptz,
  last_login_at         timestamptz,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX app_account_role_idx ON app_account (role) WHERE disabled_at IS NULL;

CREATE TRIGGER app_account_touch BEFORE UPDATE ON app_account
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TABLE app_session (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  -- 只存令牌的 sha256：库泄漏时拿不到能直接用的 cookie 值
  token_hash    text NOT NULL UNIQUE,
  account_id    text NOT NULL REFERENCES app_account(id) ON DELETE CASCADE,
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_seen_at  timestamptz,
  expires_at    timestamptz NOT NULL,
  revoked_at    timestamptz,
  ip            text,
  user_agent    text
);

CREATE INDEX app_session_account_idx ON app_session (account_id);
CREATE INDEX app_session_expiry_idx  ON app_session (expires_at);

COMMENT ON TABLE  app_account IS
  '登录账号。role 三选一：student / teacher / admin；password_hash 为空表示只能走统一认证。';
COMMENT ON TABLE  app_session IS
  '登录会话。cookie 里是原文令牌，这里只存 sha256；退出即 revoked_at。';
COMMENT ON COLUMN app_account.must_change_password IS
  '首次登录或管理员重置后置 true，前端提示改密（不强制拦截，避免演示时锁死）。';

-- ---------------------------------------------------------------
-- 0014_backfill_approved_verification.sql
-- ---------------------------------------------------------------
-- 0014: 补齐"任务已通过、题目却没有人工校验标记"的历史数据
--
-- 背景：在「通过」只改工作流状态的那段时间里点过的通过，没有写
-- exercise.human_verified_at，于是总表的可信等级、课程内容页的徽标、
-- 可见范围「仅人工审核」那一档的题量都和实际审核进度对不上。
--
-- 判定口径：这道题最近一条审核任务已经是 approved / published，但题目
-- 没有人工校验标记。只补这两类状态——draft 的题不动，避免把"还没人看"
-- 的题误标成已审。
--
-- 幂等：只处理 human_verified_at IS NULL 的行，重复执行不会再改任何东西。

UPDATE exercise e
   SET human_verified_at = now(),
       human_verified_by = coalesce((
         SELECT rt.assignee
           FROM review_task rt
          WHERE rt.course_id = e.course_id AND rt.target_id = e.id
            AND rt.state IN ('approved', 'published')
          ORDER BY rt.decided_at DESC NULLS LAST, rt.created_at DESC
          LIMIT 1
       ), '历史补录'),
       review_status = CASE
         WHEN e.review_status = 'published' THEN e.review_status
         ELSE 'teacher-reviewed'::review_status
       END
 WHERE e.human_verified_at IS NULL
   AND e.review_status <> 'retired'
   AND EXISTS (
     SELECT 1
       FROM review_task t
      WHERE t.course_id = e.course_id
        AND t.target_id = e.id
        AND t.state IN ('approved', 'published')
   );

COMMENT ON COLUMN exercise.human_verified_at IS
  '人工审核通过时间。审核页点「通过」即写入（发布也会写）；总表与可见范围据此判定 human 档。';

-- ---------------------------------------------------------------
-- 0015_class_enrollment.sql
-- ---------------------------------------------------------------
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

-- ---------------------------------------------------------------
-- 0016_term_academic_year.sql
-- ---------------------------------------------------------------
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

-- ---------------------------------------------------------------
-- 0017_qa_node_corpus.sql
-- ---------------------------------------------------------------
-- 知识点正文当问答语料：让「教材还没切片」的课也能被检索、被引用。
--
-- 背景（为什么要这个迁移）：
--   问答链路只认 textbook_chunk —— 每条片段都挂在某本教材上，引用显示"第 N 页"。
--   但 14 门课一本教材都没入库，chunk_count 全是 0，于是每次提问都被拒答。
--   而知识点正文其实已经在 knowledge_node.content_md 里，合计 138 万字，其中 10 门
--   课是教材级长文（单节点上千字，带 $LaTeX$ 和书页锚点）。
--
--   所以这里做的是"同一张表、两种来源"，不是另起一套检索：
--     source_kind = 'pdf'   教材切片（有页码，将来 PDF 入库走这条）
--     source_kind = 'node'  知识点正文切块（页码可能没有，引用改标知识点路径）
--   问答代码、引用表、拒答逻辑都不变，只是引用文案按来源区分。
--
-- 三处结构变化：
--   1. textbook.source_kind —— 标出来源，引用渲染和将来的"换回真切片"都靠它；
--   2. textbook.pdf_sha256 允许为空 —— 知识点语料没有 PDF 可哈希；
--   3. textbook_chunk.page_from/page_to 允许为空 —— 知识点没有精确页码，
--      硬填 0 会让引用显示"第 0 页"，比不写页码更糟。
--
-- 版权状态加一档 'self-authored'：知识点正文是我们自己整理的，不是转载，
-- 既不该冒充 'public-domain'，也不能标成 'internal-only'——后者会让
-- 外传策略把整门课拦下，问答永远只在本机转圈。

ALTER TABLE textbook
  DROP CONSTRAINT IF EXISTS textbook_copyright_status_check;

ALTER TABLE textbook
  ADD CONSTRAINT textbook_copyright_status_check
  CHECK (copyright_status IN ('internal-only', 'licensed', 'public-domain', 'self-authored'));

ALTER TABLE textbook
  ADD COLUMN IF NOT EXISTS source_kind text NOT NULL DEFAULT 'pdf'
  CHECK (source_kind IN ('pdf', 'node'));

COMMENT ON COLUMN textbook.source_kind IS
  'pdf=教材原件切片；node=知识点正文切块。引用渲染按它决定标页码还是标知识点路径。';

ALTER TABLE textbook ALTER COLUMN pdf_sha256 DROP NOT NULL;

ALTER TABLE textbook_chunk ALTER COLUMN page_from DROP NOT NULL;
ALTER TABLE textbook_chunk ALTER COLUMN page_to   DROP NOT NULL;

COMMENT ON COLUMN textbook_chunk.page_from IS
  '书页码。知识点语料没有精确页时为空，引用改标 section_path。';

-- 一个知识点可以切出多块，重建时按 (textbook_id, seq) 整体替换；
-- 这里给"按节点找块"一条索引，问答要拿 node_id 回填引用。
CREATE INDEX IF NOT EXISTS node_chunk_chunk_idx ON node_chunk (chunk_id);

-- ---------------------------------------------------------------
-- 0018_ai_message_basis.sql
-- ---------------------------------------------------------------
-- 回答的"依据"来源。课程材料里找不到依据时，允许改用模型的通用知识，
-- 但必须留下痕迹：前端会显示「通用知识」标记，回答正文开头也有一段免责说明。
ALTER TABLE ai_message ADD COLUMN basis text NOT NULL DEFAULT 'course'
  CHECK (basis IN ('course', 'model'));

COMMENT ON COLUMN ai_message.basis IS
  'course=依据课程材料（有引用）；model=知识库无依据，模型通用知识（正文已带未验证标注）';

-- ---------------------------------------------------------------
-- 0019_app_setting.sql
-- ---------------------------------------------------------------
-- 平台级开关：让管理员在前端改行为，而不是每次登服务器改 .env。
--
-- 只放"改了立刻生效、且需要留痕"的开关。密钥、白名单这类仍然只走 .env：
-- 那些改错了会让平台对外发数据，不适合给一个网页按钮。
CREATE TABLE app_setting (
  key        text PRIMARY KEY,
  value      jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by text REFERENCES app_account(id)
);

COMMENT ON TABLE app_setting IS '平台开关与运行期配置；读取方见 server/app/settings.py';

-- ---------------------------------------------------------------
-- 0020_approved_host.sql
-- ---------------------------------------------------------------
-- 出校域名：管理员可以在前端加，不必每次登服务器改 .env 再重启。
--
-- 独立成表而不是塞进 app_setting 的 jsonb：
--   · 每条要单独留痕（谁在什么时候放行了哪个域名）；
--   · 域名天然是主键，重复添加应该被数据库挡住，而不是靠应用层去重。
--
-- 注意：这张表只是"追加"。.env 的 MODEL_ALLOWED_HOSTS 仍然是基线，
-- MODEL_EGRESS=internal-only 仍然一票否决（此时谁都不能外发）。
CREATE TABLE approved_host (
  host       text PRIMARY KEY,
  note       text NOT NULL DEFAULT '',
  added_by   text REFERENCES app_account(id),
  created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE approved_host IS '管理员批准的出校域名；与 .env 的 MODEL_ALLOWED_HOSTS 取并集，判定见 server/app/egress.py';
COMMENT ON COLUMN approved_host.note IS '为什么允许它接收课程内容，只有一句话也要写，事后要能解释';

-- ---------------------------------------------------------------
-- 0021_blocked_host.sql
-- ---------------------------------------------------------------
-- 出校域名黑名单：把内置白名单里的域名也搬进前端可管理的范围。
--
-- 为什么不能只靠"删掉 approved_host"：.env 里的基线删不掉，而真实需求常常是
-- "某家供应商出了合规问题，先从可用列表里拿掉"——这件事不该等到下次登录服务器。
--
-- 优先级：黑名单 > 白名单（含 .env 基线）。屏蔽永远安全（少发数据），
-- 所以 internal-only 模式下也允许屏蔽，只是不允许放开。
CREATE TABLE blocked_host (
  host       text PRIMARY KEY,
  note       text NOT NULL DEFAULT '',
  blocked_by text REFERENCES app_account(id),
  created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE blocked_host IS '管理员屏蔽的出校域名；优先于 .env 与 approved_host，判定见 server/app/egress.py' ;
COMMENT ON COLUMN blocked_host.note IS '为什么屏蔽；出处不明的合规要求也要写下来，否则以后没人敢恢复';

-- ---------------------------------------------------------------
-- 0022_course_meta.sql
-- ---------------------------------------------------------------
-- 课程名称与学科方向可以改。
--
-- 原来这两件事都没有入口：
--   · 名称只能靠重新导入改（会重写整门课、把审核打回 draft，为三个字赔掉几百道题的审核记录）；
--   · 学科方向数据库里根本没有这一列，课程中心的分组靠前端静态表 js/course-catalog.js，
--     新导入的课一律进"待归类"，要归位只能改代码再部署。
--
-- direction 存的是课程中心那六个方向的 id（analysis/algebra/geometry/pde/probability/computing），
-- 留空表示待归类；前端遇到不认识的 id 也按待归类处理，不会让课凭空消失。
ALTER TABLE course ADD COLUMN IF NOT EXISTS direction text;

-- 谁导入的。用于"教师改自己导入的课"这条规则；
-- 早期导入的课没有这一列的值，视为无主，教师可改。
ALTER TABLE course ADD COLUMN IF NOT EXISTS created_by text;

-- ---------------------------------------------------------------
-- 0023_course_direction.sql
-- ---------------------------------------------------------------
-- 学科分类从代码里搬进数据库。
--
-- 原来六个方向硬编码在 js/course-catalog.js 与 server/app/course_meta.py 两处：
-- 想多加一个「数理逻辑」，得同时改前端、改后端、重新部署。分类属于运营数据，
-- 不是程序结构，管理员在界面上加一条就应该生效。
--
-- 课程表上的 course.direction 存的就是这里的 id；没有外键约束：
-- 方向被删掉时课不该跟着消失，读不到 id 就按「待归类」显示。
CREATE TABLE IF NOT EXISTS course_direction (
    id          text PRIMARY KEY,
    label       text NOT NULL UNIQUE,
    description text,
    ord         int  NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  text
);

INSERT INTO course_direction (id, label, description, ord) VALUES
    ('analysis',    '分析',       '极限、连续、微分与积分，以及测度与泛函', 1),
    ('algebra',     '代数与数论', '线性结构、群环域与数论',               2),
    ('geometry',    '几何与拓扑', '空间、流形与不变量',                   3),
    ('pde',         '微分方程',   '常微分方程、偏微分方程与数学建模',     4),
    ('probability', '概率统计',   '随机现象、统计推断与金融应用',         5),
    ('computing',   '计算与应用', '数值方法、机器学习与运筹',             6)
ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------
-- 0024_homework_grading.sql
-- ---------------------------------------------------------------
-- 0024：作业拍照批改
--
-- 原来的结构里，submission 必须挂在一个 assignment 上（教师布置、题号固定）。
-- 但现实里学生最常见的动作是"我把今天的作业拍下来，让它看看对不对"——没有布置、
-- 没有题号表，题目是拍完才被识别出来的。所以这一版把 submission 放开成两种来源：
--
--   有 assignment  ：班级作业，题号由 assignment_item.ord 决定（原样不动）
--   没有 assignment：自由作业，course_id 必填，题目由识别结果决定
--
-- 为什么不另建一张 homework 表：批改状态机、AI 初评分与教师确认分、审计全都在
-- submission/submission_item 上，另起一张表等于把"AI 初评不进成绩"这条规则抄两遍，
-- 早晚有一遍会失效。
--
-- 另外：作业批改是分钟级任务（一页一次视觉模型调用），必须走 pipeline_job，
-- 所以 kind 里补 'grade-homework'；视觉模型要能单独配一个端点，model_endpoint.kind
-- 里补 'vision'。


-- -----------------------------------------------------------------------------
-- 1. 任务类型：作业批改
-- -----------------------------------------------------------------------------
ALTER TABLE pipeline_job DROP CONSTRAINT IF EXISTS pipeline_job_kind_check;
ALTER TABLE pipeline_job
  ADD CONSTRAINT pipeline_job_kind_check
  CHECK (kind IN ('upload','probe','extract','chunk','anchor','embed','ocr',
                  'generate','generate-book','grade-homework'));

-- -----------------------------------------------------------------------------
-- 2. 模型端点：视觉
-- -----------------------------------------------------------------------------
-- 视觉能力要能单独指向某个模型：即使同一个供应商既给文本又给视觉，
-- 两件事的出错方式完全不同（转写错 vs 判分错），配置上也该分得开。
-- 没有单独配时，调用层回落到对话端点。
ALTER TABLE model_endpoint DROP CONSTRAINT IF EXISTS model_endpoint_kind_check;
ALTER TABLE model_endpoint
  ADD CONSTRAINT model_endpoint_kind_check
  CHECK (kind IN ('chat','embedding','rerank','vision'));

-- -----------------------------------------------------------------------------
-- 3. submission 放开：允许"学生自己拍照交上来的作业"
-- -----------------------------------------------------------------------------
ALTER TABLE submission ALTER COLUMN assignment_id DROP NOT NULL;
ALTER TABLE submission
  ADD COLUMN course_id  text REFERENCES course(id) ON DELETE CASCADE,
  ADD COLUMN title      text,                  -- 学生自己起的名字，可空
  ADD COLUMN page_count int NOT NULL DEFAULT 0,
  ADD COLUMN model      text,                  -- 这次批改用的视觉/文本模型
  ADD COLUMN error      text,                  -- 批改失败原因，学生能看到
  ADD COLUMN job_id     bigint;                -- 对应的 pipeline_job

-- 原来的唯一约束要求 (assignment_id, student_id) 全表唯一；
-- 自由作业 assignment_id 是 NULL，多条记录会被当成同一个 NULL 而互相冲突。
ALTER TABLE submission DROP CONSTRAINT IF EXISTS submission_assignment_id_student_id_key;
CREATE UNIQUE INDEX submission_assignment_student_uniq
  ON submission (assignment_id, student_id)
  WHERE assignment_id IS NOT NULL;
CREATE INDEX submission_student_idx ON submission (student_id, submitted_at DESC);
CREATE INDEX submission_course_idx  ON submission (course_id, submitted_at DESC);

-- 两种来源至少要有一个：既没布置也没课程，这批作业没法判分，也没处显示
ALTER TABLE submission ADD CONSTRAINT submission_needs_target
  CHECK (assignment_id IS NOT NULL OR course_id IS NOT NULL);

-- 批改失败要能说出来。原状态机只有"提交/已初评/教师确认/退回"，
-- 失败只能停在 submitted，学生看到的是"一直在批改中"。
ALTER TABLE submission DROP CONSTRAINT IF EXISTS submission_status_check;
ALTER TABLE submission
  ADD CONSTRAINT submission_status_check
  CHECK (status IN ('submitted','grading','ai-graded','teacher-confirmed','returned','failed'));

-- -----------------------------------------------------------------------------
-- 4. 作业页面图
-- -----------------------------------------------------------------------------
-- 一张图一行，而不是把图塞进 submission_item：一页纸上常有好几道题，
-- 图片属于页面，题目属于页面上的条目，两者是 1:N。
CREATE TABLE submission_page (
  submission_id uuid NOT NULL REFERENCES submission(id) ON DELETE CASCADE,
  page_no       int  NOT NULL CHECK (page_no BETWEEN 1 AND 20),
  sha256        text NOT NULL,
  bytes         int  NOT NULL,
  mime          text NOT NULL,
  transcript    jsonb,                        -- 视觉模型对这一页的原始输出，留证用
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (submission_id, page_no)
);

COMMENT ON COLUMN submission_page.transcript IS
  '视觉模型的原始输出。判分有争议时要能回看"当初识别成了什么"，只存最终分数是查不清的。';

-- -----------------------------------------------------------------------------
-- 5. submission_item 补"自由作业"需要的列
-- -----------------------------------------------------------------------------
-- 有 assignment 时题号来自 assignment_item；自由作业没有那张表，
-- 题号、题干、学生作答、匹配到的题库题都得自己记。
ALTER TABLE submission_item
  ADD COLUMN exercise_id      text,           -- 匹配到的题库题；空表示没匹配上
  ADD COLUMN page_no          int,
  ADD COLUMN question_no      text,           -- 卷面上的题号，如 "3(2)"
  ADD COLUMN question_md      text,           -- 识别出的题干
  ADD COLUMN student_answer_md text,          -- 识别出的学生作答（含步骤）
  ADD COLUMN confidence       numeric(3,2),   -- 模型自评置信度，低于 0.6 提醒教师看
  ADD COLUMN auto_comment     text;           -- AI 点评（一段话，指明第一处问题）

COMMENT ON COLUMN submission_item.confidence IS
  '模型自评置信度。它不是准确率，只用来排序——教师复核时先看低置信的那几题。';

-- ---------------------------------------------------------------
-- 0025_courseware.sql
-- ---------------------------------------------------------------
-- 0025：按章/节生成课件（PPT）与迭代修订
--
-- 原来课件是一组离线产物：courseware/<课程>/<章>/vN/slides.pptx 直接提交进仓库，
-- 由 scripts/courseware-index.cjs 校验后生成 data/courseware.js。教师看到的
-- 「课件」页只有下载和预览，没有任何生成入口——prompts/courseware-*.md 写好了
-- 规则，但"接口预留"。
--
-- 这一版把课件变成数据库里的数据：
--
--   courseware_deck     一次「这一章/这一节要有一份课件」，一门课一个章/节只有一份
--   courseware_version  每次生成或修订一个版本，plan 与 pptx 都留档，旧版本不覆盖
--
-- 为什么不把 pptx 直接放进仓库：一份 6MB，一学期几十次修订就是几百 MB，
-- 而且 PPT 是二进制，进 Git 之后每次 diff 都是新增一整份文件。
--
-- 状态：pending（排队/生成中）→ needs-review（生成完成，待教师审核）
--       → approved（教师确认）；失败留在 failed，并写 error。
-- 只有 approved 才能拿去上课；needs-review 也能下载，避免"生成完不能看"。

ALTER TABLE pipeline_job DROP CONSTRAINT IF EXISTS pipeline_job_kind_check;
ALTER TABLE pipeline_job
  ADD CONSTRAINT pipeline_job_kind_check
  CHECK (kind IN ('upload','probe','extract','chunk','anchor','embed','ocr',
                  'generate','generate-book','grade-homework',
                  'courseware-generate','courseware-revise'));

CREATE TABLE courseware_deck (
  id            text PRIMARY KEY,
  course_id     text NOT NULL REFERENCES course(id) ON DELETE CASCADE,
  chapter_id    text NOT NULL,                     -- knowledge_node.id（章或节）
  chapter_title text NOT NULL,
  node_level    int  NOT NULL DEFAULT 3,           -- 2=章 3=节
  title         text NOT NULL,
  audience      text,
  created_by    text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  -- 一个章/节一份课件：换课就换 deck，不在这里堆历史
  UNIQUE (course_id, chapter_id)
);

CREATE TABLE courseware_version (
  id            text PRIMARY KEY,
  deck_id       text NOT NULL REFERENCES courseware_deck(id) ON DELETE CASCADE,
  number        int  NOT NULL,
  based_on      text,                              -- 修订自哪个版本
  status        text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','needs-review','approved','failed')),
  title         text NOT NULL,
  instruction   text,                              -- 教师这次提的建议原文
  page_target   int  NOT NULL DEFAULT 12,           -- 生成时要求的目标页数
  base_plan     jsonb,                             -- 修订时的基础计划（快照，不跟随基础版本变化）
  slide_plan    jsonb NOT NULL DEFAULT '{}'::jsonb,
  change_log    jsonb NOT NULL DEFAULT '[]'::jsonb,
  slide_count   int  NOT NULL DEFAULT 0,
  pptx_path     text,                              -- 相对于 UPLOAD_DIR
  pptx_bytes    bigint,
  sha256        text,
  model         text,
  source_sha256 text,                              -- 生成时的资料来源指纹
  error         text,
  created_by    text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (deck_id, number)
);

CREATE INDEX courseware_version_deck_idx ON courseware_version (deck_id, number DESC);
CREATE INDEX courseware_version_status_idx ON courseware_version (status);

COMMENT ON TABLE courseware_deck IS '一份课件对应一门课的一个章/节；不存内容，只做归组与权限';
COMMENT ON TABLE courseware_version IS '版本不可变：修订产生新行，旧行保留，教师确认只改 status';
COMMENT ON COLUMN courseware_version.slide_plan IS '结构化页面计划（title/blocks/equations/sourceNodeIds/speakerNote），pptx 由它渲染';
COMMENT ON COLUMN courseware_version.change_log IS '教师建议 → 实际改动，逐条记录；界面要能回答"建议了什么、改了什么"';

-- ---------------------------------------------------------------
-- 0026_job_heartbeat.sql
-- ---------------------------------------------------------------
-- 任务心跳：worker 跑任务时定时刷新，用来区分「真的在跑」和「进程早没了」。
--
-- 为什么需要：任务被领走后状态就是 running，只有跑完才会变。worker 被重启、
-- 被杀、或者某次模型调用一直挂着，这一行就永远停在 running —— 前端转圈，
-- 队列里 queued = 0，health 看着还"正常"。2026-10-26 那次两个任务卡 48 分钟
-- 就是这么来的，除了干等没有别的办法。
--
-- heartbeat_at 由 worker 每 15 秒刷一次；超过 STALE 秒没刷就说明跑它的进程
-- 已经不在了，可以安全地重新排队。

ALTER TABLE pipeline_job ADD COLUMN IF NOT EXISTS heartbeat_at timestamptz;

-- 查"哪些 running 已经没人管"是每次 worker 启动和巡检都要做的事，给它个索引
CREATE INDEX IF NOT EXISTS pipeline_job_running_heartbeat_idx
    ON pipeline_job (heartbeat_at)
    WHERE status = 'running';

-- ---------------------------------------------------------------
-- 0027_assessment_baseline.sql
-- ---------------------------------------------------------------
-- =============================================================================
-- 0027 · 阶段 A 数据基线：固定卷、曝光、打印作业，以及最小通用 Artifact
--
-- 对应文档：
--   docs/superpowers/plans/2026-11-05-paper-01-manual-fixed-papers.md  任务 1
--   docs/superpowers/specs/2026-11-04-paper-assembly-and-online-attempts-design.md
--     §10.1—§10.12（数据模型）与 §14.1（学生投影白名单）
--
-- 为什么单独一套命名空间：固定卷的对象是 UUID + NUMERIC(8,2) + JSONB + char(64)
-- 哈希，和既有 public 表（text id、num(5,1) 分）不是同一套口径。硬塞进 public
-- 会让 homework 的 assignment 语义被改（本仓库只做加法，不改既有列语义），
-- 所以阶段 A 的全部对象落在 assessment.*，文件凭证落在 agent.*。
--
-- 与计划书的差异（按本仓库现有技术栈改写）：
--   * 不用 Alembic，这里是 db/migrations/NNNN_*.sql，由 db/migrate.py 应用；
--   * 不用 ops.migration_owned_object 记录归属：migrate.py 是只前进 + 校验和的，
--     没有 downgrade 概念，所以本文件不写 downgrade 段；
--   * 全部语句幂等（IF NOT EXISTS / DROP ... IF EXISTS 后再建），重复执行不报错；
--   * public.assignment 一行都不动：阶段 A 的打印作业是 assessment.assignment。
--
-- 幂等写法说明：CREATE TABLE IF NOT EXISTS 已覆盖"表已存在"；
-- 策略、触发器这类没有 IF NOT EXISTS 语法的对象先 DROP IF EXISTS 再建。
--
-- ⚠ RLS 提醒：paper_version / paper_section / paper_slot / paper_render 开了
-- FORCE ROW LEVEL SECURITY，非超级用户（含表 owner）默认读不到，除非同一事务里
-- `SET LOCAL app.assessment_service = 'on'`（服务账号）或
-- `SET LOCAL app.course_ids = '<uuid>,<uuid>'`（教师/学生范围）。
-- 本次任务没有任何代码读这些表，所以不影响现有功能；阶段 A 后续服务必须显式设置。
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS assessment;
CREATE SCHEMA IF NOT EXISTS agent;


-- ---------------------------------------------------------------------------
-- 一、最小通用 Artifact（plan 任务 1：从基础计划任务 9 抽出）
--
-- 如果并行工作已经建过 agent.artifact_version，这里的 IF NOT EXISTS 会跳过，
-- 后续列兼容检查由 server/tests/test_assessment_migration.py 之外的集成测试负责
-- （本次未连真库，见交付说明）。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS agent.artifact (
  id            uuid PRIMARY KEY,
  artifact_kind text NOT NULL,
  created_by    uuid NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent.artifact_version (
  id                   uuid PRIMARY KEY,
  artifact_id          uuid NOT NULL REFERENCES agent.artifact(id),
  version_no           integer NOT NULL,
  release_context_id   uuid NOT NULL,
  -- private：仅本人/服务；class_shared：本 offering；course_published：整个 Release Context
  visibility           text NOT NULL CHECK (visibility IN ('private','class_shared','course_published')),
  contains_solution    boolean NOT NULL,
  contains_answer_test boolean NOT NULL,
  media_kind           text NOT NULL,
  mime_type            text NOT NULL,
  -- s3://<bucket>/<media_kind>/<sha256>：内容寻址，不可由题号/卷号猜出，不含签名参数
  storage_uri          text NOT NULL,
  byte_size            bigint NOT NULL CHECK (byte_size >= 0),
  output_hash          char(64) NOT NULL,
  created_by           uuid NOT NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  UNIQUE (artifact_id, version_no),
  -- 重复注册同一份内容（同创建者、同媒体类型）复用同一版本：重试不产生第二份文件
  UNIQUE (output_hash, media_kind, created_by)
);

CREATE TABLE IF NOT EXISTS agent.artifact_access_event (
  id                  uuid PRIMARY KEY,
  artifact_version_id uuid NOT NULL REFERENCES agent.artifact_version(id),
  actor_id            uuid NOT NULL,
  action              text NOT NULL CHECK (action IN ('open','download')),
  occurred_at         timestamptz NOT NULL DEFAULT now()
);

-- 「谁在什么时候导出过这份保密卷」的审计查询按版本走
CREATE INDEX IF NOT EXISTS artifact_access_event_version_idx
    ON agent.artifact_access_event (artifact_version_id, occurred_at DESC);


-- ---------------------------------------------------------------------------
-- 二、蓝图与候选快照（§10.1、§10.2 的手工子集；不建 assembly_run）
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assessment.paper_blueprint_version (
  id                 uuid PRIMARY KEY,
  blueprint_id       uuid NOT NULL,
  version_no         integer NOT NULL CHECK (version_no > 0),
  course_id          uuid NOT NULL,
  offering_id        uuid NULL,
  release_context_id uuid NOT NULL,
  purpose            text NOT NULL CHECK (purpose IN ('formal','coursework','practice')),
  assembly_mode      text NOT NULL CHECK (assembly_mode IN ('manual','constraint_assisted')),
  total_score        numeric(8,2) NOT NULL CHECK (total_score > 0),
  duration_seconds   integer NOT NULL CHECK (duration_seconds > 0),
  blueprint_ast      jsonb NOT NULL,
  base_version_id    uuid NULL REFERENCES assessment.paper_blueprint_version(id),
  content_hash       char(64) NOT NULL,
  change_set_id      uuid NOT NULL,
  created_by         uuid NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (blueprint_id, version_no),
  UNIQUE (content_hash, blueprint_id)
);

CREATE TABLE IF NOT EXISTS assessment.candidate_snapshot (
  id                       uuid PRIMARY KEY,
  blueprint_version_id     uuid NOT NULL REFERENCES assessment.paper_blueprint_version(id),
  candidate_manifest       jsonb NOT NULL,
  candidate_manifest_hash  char(64) NOT NULL,
  created_by               uuid NOT NULL,
  created_at               timestamptz NOT NULL DEFAULT now(),
  -- 同一候选清单可复用于两个蓝图；同一蓝图重复清单由复合键去重
  UNIQUE (blueprint_version_id, candidate_manifest_hash)
);


-- ---------------------------------------------------------------------------
-- 三、固定卷本体（§10.3—§10.5）
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assessment.paper_version (
  id                     uuid PRIMARY KEY,
  paper_id               uuid NOT NULL,
  version_no             integer NOT NULL CHECK (version_no > 0),
  blueprint_version_id   uuid NOT NULL REFERENCES assessment.paper_blueprint_version(id),
  candidate_snapshot_id  uuid NOT NULL REFERENCES assessment.candidate_snapshot(id),
  -- 手工卷恒为空；列保留是为了与规格对齐且能明确拒绝约束组卷
  assembly_run_id        uuid NULL,
  selected_solution_index integer NULL,
  base_paper_version_id  uuid NULL REFERENCES assessment.paper_version(id),
  course_id              uuid NOT NULL,
  offering_id            uuid NULL,
  release_context_id     uuid NOT NULL,
  paper_kind             text NOT NULL CHECK (paper_kind IN ('formal','coursework','practice')),
  variant_group_id       uuid NULL,
  variant_label          text NULL,
  total_score            numeric(8,2) NOT NULL,
  duration_seconds       integer NOT NULL,
  status                 text NOT NULL CHECK (status IN ('draft','in_review','approved','withdrawn')),
  edit_seq               integer NOT NULL DEFAULT 1,
  manifest_hash          char(64) NOT NULL,
  submitted_manifest_hash char(64) NULL,
  review_manifest_hash   char(64) NULL,
  created_by             uuid NOT NULL,
  created_at             timestamptz NOT NULL DEFAULT now(),
  approved_at            timestamptz NULL,
  withdrawn_by           uuid NULL,
  withdrawn_at           timestamptz NULL,
  withdrawal_reason      text NULL,
  UNIQUE (paper_id, version_no),
  UNIQUE (manifest_hash, paper_id),
  -- 本阶段不组卷：手工卷不允许出现求解器字段
  CONSTRAINT ck_paper_manual_only CHECK (assembly_run_id IS NULL AND selected_solution_index IS NULL),
  CHECK (status <> 'approved' OR approved_at IS NOT NULL),
  CHECK ((status = 'withdrawn') = (withdrawn_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS assessment.paper_section (
  id              uuid PRIMARY KEY,
  paper_version_id uuid NOT NULL REFERENCES assessment.paper_version(id),
  code            text NOT NULL,
  title           text NOT NULL,
  instruction     text NOT NULL,
  sort_order      integer NOT NULL,
  section_score   numeric(8,2) NOT NULL,
  display_config  jsonb NOT NULL DEFAULT '{}'::jsonb,
  content_hash    char(64) NOT NULL,
  UNIQUE (paper_version_id, code),
  UNIQUE (paper_version_id, sort_order)
);

CREATE TABLE IF NOT EXISTS assessment.paper_slot (
  id                      uuid PRIMARY KEY,
  section_id              uuid NOT NULL REFERENCES assessment.paper_section(id),
  code                    text NOT NULL,
  slot_no                 integer NOT NULL,
  exercise_id             uuid NULL,
  exercise_version_id     uuid NULL,
  exercise_instance_id    uuid NULL,
  exercise_family_id      uuid NULL,
  source_release_id       uuid NULL,
  score                   numeric(8,2) NOT NULL CHECK (score >= 0),
  estimated_seconds       integer NULL,
  display_config          jsonb NOT NULL DEFAULT '{}'::jsonb,
  dependency_manifest_hash char(64) NULL,
  content_hash            char(64) NOT NULL,
  UNIQUE (section_id, slot_no),
  UNIQUE (section_id, code),
  -- 参数化题必须有版本；静态题可以没有实例
  CHECK (exercise_instance_id IS NULL OR exercise_version_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS assessment.paper_review_round (
  id                 uuid PRIMARY KEY,
  paper_version_id   uuid NOT NULL REFERENCES assessment.paper_version(id),
  manifest_hash      char(64) NOT NULL,
  required_approvals integer NOT NULL DEFAULT 2 CHECK (required_approvals >= 1),
  status             text NOT NULL CHECK (status IN ('open','approved','returned','invalidated')),
  submitted_by       uuid NOT NULL,
  submitted_at       timestamptz NOT NULL DEFAULT now(),
  closed_at          timestamptz NULL,
  -- 一个 manifest 只审一次；manifest 变了就开新一轮，旧轮次不能再返回 approved
  UNIQUE (paper_version_id, manifest_hash)
);


-- ---------------------------------------------------------------------------
-- 四、作答策略与曝光（§10.6、§10.11）
--
-- 本阶段不建 attempt / response_revision：阶段 A 没有任何在线作答行为，
-- 这里只把阶段 C/D 要直接引用的策略与曝光表建出来，避免以后重复建表。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assessment.attempt_policy_version (
  id                         uuid PRIMARY KEY,
  policy_id                  uuid NOT NULL,
  version_no                 integer NOT NULL CHECK (version_no > 0),
  policy_kind                text NOT NULL CHECK (policy_kind IN ('formal','coursework','practice')),
  content_hash               char(64) NOT NULL UNIQUE,
  max_attempts               integer NULL CHECK (max_attempts IS NULL OR max_attempts > 0),
  behavior                   text NOT NULL CHECK (behavior IN ('deferred','adaptive_practice')),
  time_limit_seconds         integer NULL,
  grace_seconds              integer NOT NULL DEFAULT 0,
  late_policy                text NOT NULL CHECK (late_policy IN ('reject','read_only_grace','auto_submit')),
  show_syntax_errors         boolean NOT NULL,
  show_math_interpretation   boolean NOT NULL,
  show_input_preview         boolean NOT NULL,
  correctness_release        text NOT NULL,
  score_release              text NOT NULL,
  hint_release               text NOT NULL,
  solution_release           text NOT NULL,
  answer_release             text NOT NULL,
  allow_item_check           boolean NOT NULL,
  max_hint_level             integer NOT NULL DEFAULT 0,
  allow_retry                boolean NOT NULL,
  practice_penalty           numeric(5,2) NOT NULL DEFAULT 0,
  allow_flag                 boolean NOT NULL,
  allow_review               boolean NOT NULL,
  allow_upload               boolean NOT NULL,
  autosave_debounce_ms       integer NOT NULL DEFAULT 800,
  autosave_max_interval_ms   integer NOT NULL DEFAULT 5000,
  conflict_policy            text NOT NULL DEFAULT 'per_slot',
  created_at                 timestamptz NOT NULL DEFAULT now(),
  UNIQUE (policy_id, version_no),
  -- 不限次数只允许出现在练习策略上
  CHECK (max_attempts IS NOT NULL OR policy_kind = 'practice')
);

CREATE TABLE IF NOT EXISTS assessment.item_exposure_event (
  id                  uuid PRIMARY KEY,
  exercise_version_id uuid NOT NULL,
  exercise_instance_id uuid NULL,
  exercise_family_id  uuid NOT NULL,
  paper_version_id    uuid NULL REFERENCES assessment.paper_version(id),
  assignment_id       uuid NULL,   -- 外键在 assessment.assignment 建好后补（见文件末尾）
  scope_offering_id   uuid NULL,
  event_kind          text NOT NULL CHECK (event_kind IN ('secure_assigned','student_opened','practice_answer_released','formal_answer_released','withdrawn')),
  visible_from        timestamptz NOT NULL,
  occurred_at         timestamptz NOT NULL DEFAULT now(),
  metadata_hash       char(64) NOT NULL
);

-- 曝光冲突检查的典型查询：同族题目最近一次放答案的时间
CREATE INDEX IF NOT EXISTS exposure_family_kind_idx
    ON assessment.item_exposure_event (exercise_family_id, event_kind, occurred_at DESC);


-- ---------------------------------------------------------------------------
-- 五、打印渲染（§10.10）：文件本身在 agent.artifact_version
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assessment.paper_render (
  id                    uuid PRIMARY KEY,
  paper_version_id      uuid NOT NULL REFERENCES assessment.paper_version(id),
  render_kind           text NOT NULL CHECK (render_kind IN ('student','answer','rubric')),
  paper_manifest_hash   char(64) NOT NULL,
  template_version      text NOT NULL,
  language_code         text NOT NULL,
  artifact_version_id   uuid NOT NULL REFERENCES agent.artifact_version(id),
  renderer_version      text NOT NULL,
  font_manifest_hash    char(64) NOT NULL,
  -- 学生卷的 security_manifest_hash 只允许包含题干与公开资产（§14.1）
  security_manifest_hash char(64) NOT NULL,
  output_hash           char(64) NOT NULL,
  status                text NOT NULL CHECK (status IN ('ready','failed','blocked')),
  created_by            uuid NOT NULL,
  created_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (paper_version_id, render_kind, output_hash)
);


-- ---------------------------------------------------------------------------
-- 六、打印作业与不可变作业项（§10.9 的 assessment 命名空间版本）
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assessment.assignment (
  id                        uuid PRIMARY KEY,
  course_id                 uuid NOT NULL,
  offering_id               uuid NOT NULL,
  paper_version_id          uuid NOT NULL REFERENCES assessment.paper_version(id),
  attempt_policy_version_id uuid NOT NULL REFERENCES assessment.attempt_policy_version(id),
  release_context_id        uuid NOT NULL,
  visible_at                timestamptz NOT NULL,
  due_at                    timestamptz NOT NULL,
  hard_close_at             timestamptz NOT NULL,
  distribution_kind         text NOT NULL CHECK (distribution_kind = 'print'),
  status                    text NOT NULL CHECK (status IN ('draft','scheduled','published','closed','withdrawn')),
  created_by                uuid NOT NULL,
  created_at                timestamptz NOT NULL DEFAULT now(),
  published_at              timestamptz NULL,
  CHECK (visible_at <= due_at AND due_at <= hard_close_at)
);

CREATE TABLE IF NOT EXISTS assessment.assignment_item (
  id                  uuid PRIMARY KEY,
  assignment_id       uuid NOT NULL REFERENCES assessment.assignment(id),
  paper_slot_id       uuid NOT NULL REFERENCES assessment.paper_slot(id),
  ord                 integer NOT NULL,
  release_context_id  uuid NOT NULL,
  source_release_id   uuid NOT NULL,
  exercise_version_id uuid NOT NULL,
  exercise_instance_id uuid NULL,
  score               numeric(8,2) NOT NULL,
  content_hash        char(64) NOT NULL,
  UNIQUE (assignment_id, paper_slot_id),
  UNIQUE (assignment_id, ord)
);

-- item_exposure_event 建表时 assignment 还不存在，这里补外键；用 DO 块保证幂等
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'exposure_assignment_fk'
      AND conrelid = 'assessment.item_exposure_event'::regclass
  ) THEN
    ALTER TABLE assessment.item_exposure_event
      ADD CONSTRAINT exposure_assignment_fk
      FOREIGN KEY (assignment_id) REFERENCES assessment.assignment(id);
  END IF;
END $$;


-- ---------------------------------------------------------------------------
-- 七、不可变性守卫（§0.3 第 4 条）
--
-- approved 之后题目、分值、顺序、manifest 不可 UPDATE；普通纠错建新 paper 版本，
-- 严重纠错走撤回。assignment_item 只允许 INSERT。
-- 用触发器而不是应用层判断，是因为"改卷"这件事必须挡在数据库这一层。
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION assessment.guard_fixed_projection() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_TABLE_NAME = 'paper_version' THEN
    IF OLD.status = 'withdrawn' THEN
      RAISE EXCEPTION 'withdrawn paper is immutable';
    END IF;
    IF OLD.status = 'approved' AND NEW.status <> 'withdrawn' THEN
      RAISE EXCEPTION 'approved paper is immutable';
    END IF;
    IF OLD.status <> 'draft'
       AND (NEW.manifest_hash <> OLD.manifest_hash OR NEW.edit_seq <> OLD.edit_seq) THEN
      RAISE EXCEPTION 'submitted manifest is immutable';
    END IF;
    IF NEW.status = 'approved'
       AND (OLD.status <> 'in_review' OR NEW.review_manifest_hash IS NULL) THEN
      RAISE EXCEPTION 'dual review manifest required';
    END IF;
  ELSIF TG_TABLE_NAME = 'assignment_item' THEN
    RAISE EXCEPTION 'assignment_item is append-only';
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS paper_version_guard ON assessment.paper_version;
CREATE TRIGGER paper_version_guard BEFORE UPDATE ON assessment.paper_version
  FOR EACH ROW EXECUTE FUNCTION assessment.guard_fixed_projection();

DROP TRIGGER IF EXISTS assignment_item_guard ON assessment.assignment_item;
CREATE TRIGGER assignment_item_guard BEFORE UPDATE OR DELETE ON assessment.assignment_item
  FOR EACH ROW EXECUTE FUNCTION assessment.guard_fixed_projection();


-- ---------------------------------------------------------------------------
-- 八、行级安全（§14.2）
--
-- 保密卷、学生卷与答案卷不允许普通学生角色读到。服务账号在同一事务里
-- SET LOCAL app.assessment_service = 'on'；课程范围用 app.course_ids。
-- paper_render 只允许服务账号读：三类卷的差异本身就是答案信息。
-- ---------------------------------------------------------------------------

ALTER TABLE assessment.paper_version ENABLE ROW LEVEL SECURITY;
ALTER TABLE assessment.paper_version FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS paper_course_scope ON assessment.paper_version;
CREATE POLICY paper_course_scope ON assessment.paper_version
  USING (
    current_setting('app.assessment_service', true) = 'on'
    OR course_id::text = ANY (string_to_array(current_setting('app.course_ids', true), ','))
  );

ALTER TABLE assessment.paper_section ENABLE ROW LEVEL SECURITY;
ALTER TABLE assessment.paper_section FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS section_course_scope ON assessment.paper_section;
CREATE POLICY section_course_scope ON assessment.paper_section
  USING (
    current_setting('app.assessment_service', true) = 'on'
    OR EXISTS (
      SELECT 1 FROM assessment.paper_version p
      WHERE p.id = paper_version_id
        AND p.course_id::text = ANY (string_to_array(current_setting('app.course_ids', true), ','))
    )
  );

ALTER TABLE assessment.paper_slot ENABLE ROW LEVEL SECURITY;
ALTER TABLE assessment.paper_slot FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS slot_course_scope ON assessment.paper_slot;
CREATE POLICY slot_course_scope ON assessment.paper_slot
  USING (
    current_setting('app.assessment_service', true) = 'on'
    OR EXISTS (
      SELECT 1 FROM assessment.paper_section s
      JOIN assessment.paper_version p ON p.id = s.paper_version_id
      WHERE s.id = section_id
        AND p.course_id::text = ANY (string_to_array(current_setting('app.course_ids', true), ','))
    )
  );

ALTER TABLE assessment.paper_render ENABLE ROW LEVEL SECURITY;
ALTER TABLE assessment.paper_render FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS render_service_only ON assessment.paper_render;
CREATE POLICY render_service_only ON assessment.paper_render
  USING (current_setting('app.assessment_service', true) = 'on');

-- ---------------------------------------------------------------
-- 0028_assessment_identity_bridge.sql
-- ---------------------------------------------------------------
-- 阶段 A 任务 2 的两张辅助表：幂等记录 + 文本 id 桥接。
--
-- 为什么必须新开 0028 而不是塞进 0027：0027 已提交，是本阶段数据基线的历史；
-- 这两张表是任务 2 的实现细节（幂等键、身份映射），不属于规格 §10 的领域表。
-- 0027 只做了 11 张领域表 + 3 张 Artifact 表，且没有任何幂等存储。
--
-- 一、assessment.idempotency_record
--   写接口要求 Idempotency-Key（计划书 §0.3 规则 3）。只在内存里记是不够的：
--   进程重启、多 worker、HTTP 超时后的重试都会绕过内存。记录必须与版本插入在
--   同一个事务里落库，唯一键 (scope, idempotency_key, actor_id) 保证同一用户
--   的同一个键只被消费一次；键按用户隔离，避免 A 的重试读到 B 的蓝图。
--   response 存当时的响应快照：重放时原样返回，不重新计算（否则"重放"会变成
--   "按现在的库状态再生成一份"，两者在并发下可能不同）。
--
-- 二、assessment.id_ref
--   0027 的 assessment 表把 course_id / created_by 定义为 uuid，而本仓库既有的
--   身份真源是文本：course.id 形如 `analysis`，app_account.id 形如 `acc-1a2b3c4d5e6f`，
--   teaching_class.id / teacher_id 同样是 text。两边不可能直接相等。
--   应用层用固定命名空间的 uuid5 做确定性映射（见 server/app/blueprints.py 的
--   ref_uuid），这张表保存 subject(uuid) ↔ (kind, ref) 的反向映射，让读取接口
--   能把文本 id 还原出来给前端。唯一键 (kind, ref) 保证一个文本 id 永远对应
--   同一个 uuid；subject 是主键，保证一个 uuid 不会被记成两个不同的文本 id。
--
-- 三、RLS
--   0027 只对 paper_version / paper_section / paper_slot / paper_render 开了
--   FORCE ROW LEVEL SECURITY；蓝图版本表与这两张新表都没有策略，因此本迁移不
--   添加策略，也不改变 0027 的行级安全姿态。写路径仍然统一走
--   db.transaction(assessment_service=True)：任务 3/4 写 paper_* 时同一段代码
--   已经满足 SET LOCAL 要求，不会出现"事务里忘了设作用域"的返工。


CREATE SCHEMA IF NOT EXISTS assessment;

-- ---------------------------------------------------------------------------
-- 幂等记录
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assessment.idempotency_record (
  id              uuid PRIMARY KEY,
  -- 作用域写成 `资源.动作`，同一把键可以在不同接口复用而互不干扰
  scope           text NOT NULL CHECK (length(scope) BETWEEN 1 AND 80),
  idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 200),
  -- 幂等键按用户隔离：不同用户的同名字符串不是同一次重试
  actor_id        uuid NOT NULL,
  -- 载荷指纹（canonical JSON 的 sha256）：同键不同载荷必须 409，而不是返回旧结果
  request_hash    char(64) NOT NULL,
  -- 当时的响应快照；重放原样返回，不按当前库状态重算
  response        jsonb NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (scope, idempotency_key, actor_id)
);

COMMENT ON TABLE assessment.idempotency_record IS
  '写接口幂等键：与业务写入同事务落库，同键同载荷重放、同键不同载荷 409';

-- "清理多久以前的幂等记录"是唯一的批量查询方向
CREATE INDEX IF NOT EXISTS idempotency_record_created_idx
    ON assessment.idempotency_record (created_at);

-- ---------------------------------------------------------------------------
-- 文本 id ↔ 确定性 uuid 的桥接
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assessment.id_ref (
  subject    uuid PRIMARY KEY,
  -- 与 server/app/blueprints.py 的 REF_KINDS 一一对应
  kind       text NOT NULL CHECK (kind IN ('course', 'offering', 'account', 'release_context')),
  ref        text NOT NULL CHECK (length(ref) BETWEEN 1 AND 200),
  created_at timestamptz NOT NULL DEFAULT now(),
  -- 一个文本 id 只能映射到一个 uuid（uuid5 本身就是确定的，这里再加一道库级保证）
  UNIQUE (kind, ref)
);

COMMENT ON TABLE assessment.id_ref IS
  'assessment 表的 uuid 身份 ↔ 本仓库文本 id（course/app_account/teaching_class）的确定性映射';

-- 反向查询（uuid → 文本）走主键；这里补的是"某类身份有哪些"的巡检方向
CREATE INDEX IF NOT EXISTS id_ref_kind_idx ON assessment.id_ref (kind);

-- ---------------------------------------------------------------
-- 0029_minimal_release_context.sql
-- ---------------------------------------------------------------
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
