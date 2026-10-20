-- =============================================================================
-- 智慧武大 · 数学 AI 教学智能体 —— 数据库结构
-- PostgreSQL 16 + pgvector
--
-- 三条设计原则（来自对现有 14 门课程数据的实测，见 docs/数据模型与数据库设计.md）：
--   1. 主键一律复合 (course_id, id)：现有 4485 个节点里只有 3909 个唯一 ID，
--      4807 道题里只有 4120 个唯一 ID，跨课程重名是常态而非异常。
--   2. 章节提升为一等实体：现有数据用 level=2 的节点兼任章节，关系靠 chapterId
--      指回自身；数据库里显式建模，避免"节点表里查章节"。
--   3. 教材锚点独立成表 node_chunk：这是问答可溯源的命门，不能寄存在自由文本里。
--
-- 幂等：本文件可重复执行（drop 后重建），仅用于开发与初始化。
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- 0. 扩展与类型
-- -----------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;      -- 需 pgvector/pgvector:pg16 镜像

DROP SCHEMA IF EXISTS public CASCADE;
CREATE SCHEMA public;

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
  'unknown'
);

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
  review_status       review_status NOT NULL DEFAULT 'draft',
  content_version     int NOT NULL DEFAULT 1,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (course_id, id),
  FOREIGN KEY (course_id, chapter_id) REFERENCES chapter (course_id, id)
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

-- -----------------------------------------------------------------------------
-- 8. 教学：班级、作业、提交、成绩
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
-- 9. AI：会话、引用、模型调用计量
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
-- 10. 审计（谁改了什么，为什么）
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
-- 11. updated_at 维护
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

COMMIT;
