-- 0004：模型端点登记表（数据不出校的允许清单）
--
-- 学院要求：数据不出校，但校外可以访问网站。
-- 这两件事不矛盾，矛盾的是"随手调用外部 API"——RAG 每次问答都把教材片段
-- 塞进提示词，只要 base_url 指向校外，数据就已经出去了，而且没有任何提示。
--
-- 所以端点必须登记在库里：谁能用、是不是校内、用的是哪个模型。
-- 代码里的 ALLOW_EXTERNAL_MODELS 是开关，这张表是账本。两者都要有：
-- 开关防止误调，账本让审计能回答"到底用过哪些模型"。

BEGIN;

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

COMMIT;
