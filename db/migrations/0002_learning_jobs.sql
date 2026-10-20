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

BEGIN;

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

COMMIT;
