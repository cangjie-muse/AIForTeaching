# 数据管线

负责把**学院教材**变成可检索、可溯源的数据库内容。当前只落地了入口与契约，切片实现待教材到位后开工。

## 目录

```
pipeline/
  probe_pdf.py        PDF 入门体检（现在可用）
  requirements.txt    管线依赖
  （待建）extract.py   页级抽取：PDF → 带页码锚点的 Markdown
  （待建）chunk.py     按知识点边界切片
  （待建）anchor.py    知识点 ↔ 教材片段 的锚点匹配
  （待建）embed.py     向量化
```

## 教材放哪里

`textbooks/` 已在 `.gitignore` 中，**永不进入仓库**（版权）。

```
textbooks/
  analysis/                         ← 课程 id
    chenjixiu-3e-vol1.pdf           ← 教材原件
    chenjixiu-3e-vol1.json          ← 可选元数据
```

元数据字段：

```json
{
  "title": "数学分析",
  "edition": "第三版（上册）",
  "authors": "陈纪修、於崇华、金路",
  "publisher": "高等教育出版社",
  "copyright_status": "internal-only",
  "page_offset": 20
}
```

`page_offset` 很关键：PDF 第 21 页才是书页 1 时，`page_offset = 20`，入库统一存书页码，另存 `pdf_page` 以便核对。

## 第一步：体检（现在就能跑）

```bash
pip install -r pipeline/requirements.txt
python pipeline/probe_pdf.py textbooks/analysis/chenjixiu-3e-vol1.pdf
```

三种结论，对应三条完全不同的路线：

| 结论 | 含义 | 后续 |
| --- | --- | --- |
| `direct-extract` | 电子版，有文字层 | 直接抽文字，公式靠 `$` 与数学字体识别 |
| `needs-ocr` | 纯扫描件 | 必须先 OCR（公式 OCR 是硬骨头，需 10%–20% 人工校对） |
| `mixed` | 部分页是图 | 逐页判断，先跑抽取再看缺口 |

**不要跳过这一步。** 你手上有电子版也有扫描件，"平均每页字符数"直接决定 S1 是两周还是两个月。

## 切片产出的契约（约定好，实现时照着填）

后续 `extract.py` + `chunk.py` 的输出必须长这样，才能直接灌进 `textbook_chunk` 表：

```json
{
  "textbook_id": "analysis-chenjixiu-3e-vol1",
  "seq": 42,
  "section_path": ["第2章 数列极限", "§1 数列极限概念"],
  "page_from": 27,
  "page_to": 28,
  "content_md": "**定义 2.1** 设 $\\{a_n\\}$ 为数列……",
  "has_formula": true,
  "ocr_status": "clean",
  "token_count": 412
}
```

三条硬约束：

1. **页码必须是书页**，不是 PDF 页（用 `page_offset` 换算）。
2. **不能跨知识点边界切**。按 512 token 硬切会把定理条件切到上一块，检索出来"看着相关但缺前提"——这是数学 RAG 最常见的失败模式。
3. **公式保留 LaTeX**，同时另存一句自然语言释义（供检索用）。`$\\int_0^1 f(x)dx$` 在向量空间里几乎不可检索。

## 与数据库的关系

`db/schema.sql` 里的 `textbook` / `textbook_chunk` 表就是这些产物的落点；`node_chunk` 表存"知识点 → 教材片段"的锚点。**锚点覆盖率低于 80% 的课程不应开放问答**，用 `v_anchor_coverage` 视图可以直接查。
