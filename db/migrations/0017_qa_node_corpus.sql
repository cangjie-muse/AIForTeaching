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
