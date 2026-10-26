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
