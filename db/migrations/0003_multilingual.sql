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

BEGIN;

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

COMMIT;
