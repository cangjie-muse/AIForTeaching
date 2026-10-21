---
name: course-building
description: Build or update Chinese course resources from textbooks, exercise sheets, exams, or research papers in any language. Produce a linked knowledge graph workbook, knowledge Markdown with a terminology glossary, and exercise Markdown with sketch solutions. Use for course construction, pilots, additions or revisions, and format-only conversion of existing course Markdown to the Symplectic Markdown v1 standard; not for standalone summaries or exercise solving.
---

# Course Building

**Format specification: Symplectic Markdown v1 (`symplectic-markdown-v1`).** The complete specification is included in the [output contract](#output-contract) below. It fixes Markdown serialization, not graph depth, and applies to new files, ordinary updates, and explicitly requested format conversions.

## Portable use

This single file contains the complete workflow, output schemas, entry templates, and acceptance checklist. Give it to an assistant together with the books, papers, exams, exercise sheets, or existing course resources to process, and specify an output folder. It can be used as an instruction document or saved as `SKILL.md` in a `course-building` skill folder in an environment that supports skills.

The recipient supplies the source materials and file-processing capabilities. No files from the author's computer, earlier conversations, other installed skills, accounts, or private services are needed. Relative filenames inside the templates describe future course inputs and outputs; they are examples, not files that accompany or must be bundled with this skill. Replace example names, dates, sources, page ranges, and anchors with values verified for the actual materials.

## Select the operation

For requests such as “统一格式”, “转换旧格式”, “normalize Markdown”, or “convert to Symplectic Markdown v1”, use the contract's **Format-only conversion** workflow. Read the existing graph for mapping and normalize the requested existing Markdown files throughout their requested scope. A course-wide conversion defaults to `知识点.md` and `习题.md`; include `额外习题.md` only when requested. Preserve mathematics, graph hierarchy, anchors, source identities, answers, ratings, and raw blanks. Do not build missing deliverables, add exercises, reread textbooks for mathematical verification, or launch source research. The construction and content-verification steps below apply only to a build/content-update request. Report structural conversion separately from mathematical verification.

Turn the supplied teaching sources directly into three linked deliverables in the user's chosen folder:

1. `知识节点.xlsx`.
2. `知识点.md`.
3. `习题.md`.

Use these exact filenames, without subject prefixes or pilot suffixes. Distinguish courses by their folders and pilots by a dedicated pilot subfolder and scope notes inside the files.

Read [the output contract](#output-contract) before authoring. It defines the workbook hierarchy columns (six by default, extensible), fixed Markdown structure, and reference syntax. The three files should be portable together, with relative Markdown paths and explicit anchors. Anchors provide the references.

Follow the required document and entry templates in that contract: use `##` for chapter/theme containers and `###` for substantive knowledge points in both Markdown files. Each knowledge entry has separate `图谱路径`, `教材范围`, and `习题` paragraphs, followed by content under descriptive bold labels. Each exercise section has `图谱路径` and `知识内容`; each numbered question has a stable `source-exercise` comment, a concise `答案`, then difficulty, question source, and solution provenance. Put optional editorial notes after that metadata and confirmed errata last. Preserve blank lines, list indentation, and matching anchors. These are required formats, not merely illustrative layouts; adapt the mathematical content and semantic labels to the subject.

Use the spreadsheet and document-reading capabilities available in the receiving environment. Create and inspect a genuine `.xlsx` workbook, verify its exported cell values and hierarchy, and preview its layout. For scanned PDFs, render pages, use OCR for navigation, and inspect the images for mathematical details. No separate spreadsheet skill, PDF skill, scripts, templates, or fixed software installation is required by these instructions. If a needed capability is unavailable, state the specific limitation and do not claim the affected reading, export, or verification succeeded. The output contract is mandatory unless the user's current request explicitly specifies a different Markdown format. An existing file's inconsistent formatting or a supplied graph is not an override. Ordinary updates normalize structural wrappers throughout each Markdown file being edited, preserving its content; honor an explicitly narrower user scope and report any inconsistency left outside it.

## Establish scope from the sources

- Inspect the supplied books, exercise sheets, exams, papers, and any current graph/template. A supplied graph is a reference for names and hierarchy; inspect the sources to establish actual content and coverage. Do not assume generated graph entries are correct.
- When several inputs are supplied, select one as the main source without asking the user to choose, unless the user has already designated one. Any supplied input may serve; prefer the one with the clearest structure and broadest coverage of the intended course. Use it to guide organization, sequencing, notation, and terminology. Treat all remaining inputs as additions: integrate their relevant knowledge, results, examples, exercises, solutions, and corrections into the same course, adding branches where needed. Identify the main source and supplementary sources in the Markdown source lists. Main-source status does not override mathematical correctness or justify omitting supplementary material in scope; resolve substantive conflicts or ask when they remain ambiguous.
- Default to the full supplied scope. For a requested first-N pilot, count substantive knowledge entries in graph/source order, excluding course and chapter containers. A parent entry with real mathematical content counts. State which entries the pilot covers inside the files; retain the fixed filenames in a dedicated pilot subfolder.
- Accept sources in any language, including mixed-language collections. Write the workbook labels and content, knowledge explanations, questions, solutions, and correction notes in Chinese. Preserve mathematical notation, source identifiers, and original bibliographic titles where needed for accurate attribution. Translate the mathematical meaning faithfully, including hypotheses and quantifiers.
- End `知识点.md` with `重要术语译名对照`: a list of important original-language terms and their Chinese translations, identifying the language and linking each to the relevant knowledge point. Follow established translations and the existing course glossary consistently. Distinguish context-dependent meanings and document a consequential translation choice briefly. Include terms encountered in exercises as well. For Chinese-only sources, retain this final section and state that no foreign-language terms required translation; do not invent foreign originals.
- Identify editions, chapter/section structure, exercises, appendices, and answer/hint sections. For papers and exams, identify theorem/problem numbering and document-specific source labels. Obtain missing information only when it affects reliable attribution or interpretation.
- Ask about ambiguity that changes a theorem's meaning or an exercise's answer. Continue independent entries while waiting. Do not let an uncertain transcription become an asserted correction.

## Add to or update an existing course

For build/content-update invocations, inspect the chosen course folder for `知识节点.xlsx`, `知识点.md`, and `习题.md`. When they exist, treat them as the current course and apply the requested additions or revisions to those same files. Read their current contents, including user edits, before planning changes. If only some exist, complete the missing deliverables and reconcile them with the existing ones. If the user supplies an older course under different filenames, use it as the baseline and deliver the three fixed names with all references updated. If several folders or course versions could be intended and context does not identify one, ask which course to update.

- Match existing concepts by their stable anchors, full hierarchy, meaning, and glossary; match exercises by source identity, original numbering, and mathematical content. Extend an existing entry when appropriate rather than duplicating it merely because the wording or source language differs.
- Integrate new sources, points, examples, exercises, and corrections into the existing hierarchy and Markdown sections. Retain unrelated content and user edits. Do not rebuild or discard the course simply because new inputs cover a smaller scope. Repeating the same inputs without substantive changes should not create duplicate nodes, questions, or glossary entries.
- For an existing course, retain its established main source and treat newly supplied inputs as additions unless the user requests a change. If no main source was previously identified, infer it from the current course and record the choice; do not reorganize an established course merely to select a new main source.
- Preserve anchors for retained concepts. If a requested reorganization changes a path or requires merging/splitting a concept, update every affected workbook reference, Markdown link, and exercise association together; do not leave stale targets. Keep all relevant provenance when merging.
- Maintain the source lists, contents, coverage/count summaries, correction notes, and final terminology glossary across both existing and added material. Keep translated names consistent in all three outputs. A supplied answer may be translated into Chinese while preserving its mathematics and provenance; translation alone is not an erratum.
- Validate the complete set of references and the changed content after the update. Report briefly what was added or revised and any unresolved source conflicts. Invocation authorizes the requested course updates; ask only about consequential ambiguities that cannot be resolved from the files and request.

## Read and reconcile the material

Use extracted text or OCR for navigation. Confirm mathematical notation, hypotheses, diagrams, and suspected errors against the source page images. In scanned books, the rendered page is authoritative over the OCR. Inspect all sections within the requested scope, rather than relying on the table of contents or exercise tags alone.

Maintain precise provenance:

- Use printed page numbers and PDF page numbers when both exist; establish their relationship for each document rather than assuming a universal offset.
- Include section, theorem/proposition/example, or exercise numbers where available. For exams, use the paper/session and question number; for research papers, use the paper title/version and numbered result or page.
- When combining sources, use a consistent short source label that resolves to full bibliographic/file details in each Markdown's source list.
- If a task depends on a diagram, preserve the needed information through a faithful mathematical description. If that cannot make the exercise self-contained, retain a local figure asset only when needed and cite its source; ask when the necessary diagram is unavailable. Never invent a missing figure's content.

## Build a useful hierarchy

Organize the subject into course/topic, chapters or themes, and substantive knowledge points. Graph depth has no fixed maximum; use meaningful nesting and the extensible hierarchy columns in the contract. Preserve an existing hierarchy during format-only conversion. Include necessary parent nodes and distinguish navigation containers from content-bearing parents. Graph depth never determines Markdown heading depth: every point or intermediate node below a chapter/theme uses `###`, with its full depth expressed in `图谱路径` and navigation links.

Group equivalent concepts across sources when their meanings agree, preserving their multiple references. Keep genuinely different meanings separate. Resolve repeated names such as `光滑映射`, `分配律`, or `结合律` using the full hierarchy and mathematical context, not leaf-name equality alone.

For each substantive point, write enough content to explain it without reopening the source:

- A brief description and the essential definitions/notation.
- Important theorems, propositions, lemmas, and conditions, with accurate quantifiers, regularity assumptions, exceptional cases, and source numbers. Avoid presenting a special case as the full statement.
- Representative examples from the source and the feature they illustrate. Identify an unnumbered construction as such rather than inventing an example number. Label any added teaching example as an addition.
- Precise source ranges and a link to the matching exercise section.

Keep proofs optional and concise unless needed to explain a correction or requested by the user. Narrow property nodes can be short; broad nodes such as tensor or exterior algebra need more content. For a point inferred from exercise-only material, clearly distinguish the agent's synthesized explanation from a statement found in the source.

## Corrections and uncertainty

Use the corrected statement/question/solution in the main content when the source contains a confirmed error. At the **end of that knowledge point or exercise**, add a concise `勘误` note with the source location, the original problematic wording/formula, the correction, and its mathematical reason or supporting reference. Distinguish an error in the source from OCR/transcription noise; fixing OCR to match the page is not a textbook erratum.

Check that corrected hypotheses and notation are carried through every affected occurrence, including duplicate exercise listings. Correct demonstrably erroneous supplied solutions as well, with the same end note. If several plausible repairs change the intended problem, ask rather than selecting one silently. Mark any unresolved item explicitly; do not claim complete mathematical verification while it remains unresolved.

## Assemble exercises and sketch solutions

- Cover every exercise in scope, including relevant exercises in appendices, sheets, and exams. For a knowledge-point pilot, include every supplied exercise meaningfully related to those points. Preserve the original numbered problem and its subparts as one exercise entry.
- Use an existing exercise workbook's question text, answer, difficulty, and knowledge tags as the starting point. Check coverage and meaningful associations against the sources. Preserve the mathematical content of usable supplied answers and their difficulty ratings unless correction is warranted; translate non-Chinese answers into Chinese. Do not silently replace a supplied answer with a newly generated one.
- Look for solutions or hints at the end of the book or in supplied answer sheets before generating a solution. Use or concisely adapt a valid available solution; otherwise provide a mathematically sufficient sketch: the key construction, argument, or calculation and conclusion. A theorem name alone is sufficient only when its application is evident. Mark an agent-generated sketch as `自拟简解` in the final metadata line; distinguish source-provided or source-adapted solutions.
- Keep all assumptions and subparts needed to solve the problem. Preserve LaTeX. A spreadsheet's stem-length limit does not apply to Markdown; do not truncate a Markdown question to 500 characters. If a supplied shortened stem lost essential content, restore it from the source and note a substantive correction when appropriate.
- For missing difficulty ratings, assign an integer from 1 to 5: 1 direct definition/routine calculation; 2 a standard short argument; 3 several connected ideas; 4 a substantial construction or non-obvious proof; 5 unusually demanding or highly technical. Judge relative to the course's level, not just solution length.
- Categorize by the graph's full knowledge-point identity. Allow multiple associations. Repeat the complete question and sketch under each directly relevant point so each section is independently usable; state distinct-question and displayed-entry counts. Do not automatically copy all descendant exercises into every ancestor.
- Keep distinct exam/book occurrences identifiable. Exact duplicate questions can share an entry within a category only when all original source locations remain recorded; do not merge different hypotheses or subparts.
- Put each question before its solution in a numbered list, never a table. Move an existing source prefix out of the question to the metadata after the solution. Put difficulty, source, and solution provenance there; put any `勘误` note last.
- Where no exercise corresponds to a knowledge point, retain its anchored exercise section and state `所给材料中未找到对应习题。` Do not manufacture exercises or claim no exercises exist beyond the supplied material.

The requested exercise deliverable is Markdown. Do not add an exercise-import workbook unless requested. If an exercise-import workbook is separately requested, follow the supplied template. Unless the user or template specifies otherwise, default to 10 points per question, concise answers, and a blank optional `解析` field.

## Link and verify the deliverables

- Give each knowledge point a unique, stable explicit anchor in **both** Markdown files. Use semantic names with chapter/topic context. Preserve existing anchors during extensions when their meanings remain the same.
- Fill `知识点标签` with the two relative file-and-anchor references specified in the contract. Keep `知识说明` as source ranges, with the substantive material in the knowledge Markdown. Container rows may link to corresponding overview/navigation sections.
- Before delivering, reconcile source sections and numbered exercises with the requested scope. Check that every graph content node has both Markdown sections, all references resolve uniquely, each included exercise has a solution and difficulty, and each source prefix has moved after its solution.
- Check formulas and source ranges, mathematical consistency of sketch solutions, repeated-name disambiguation, and synchronized corrections. Compare imported exercise answers with their sources to detect accidental alterations.
- Run the complete structural acceptance checklist in the output contract over each delivered Markdown file, not just a sample or the newly appended chapters.
- Verify that all three files have their exact fixed names, the outputs are in Chinese, and `知识点.md` ends with the maintained terminology glossary. Verify the exported workbook has the exact header order and the requested hierarchy; render the changed view to check that references are readable. Inspect representative Markdown sections, including a long formula, subparts, multiple-source entry, and correction note when present.
- Deliver the three files with a brief scope/count summary. Report remaining ambiguities or unavailable source material explicitly. A pilot does not authorize a full-scale run.

<a id="output-contract"></a>
## Output contract

**Format specification: Symplectic Markdown v1 (`symplectic-markdown-v1`).** This embedded contract is normative for the outputs described in this file. Its structure is mandatory, while mathematical wording and semantic content labels remain subject-specific. Supplementary/raw rules apply only when those existing resources are in the requested scope. Graph depth is unrestricted.

The three exact output filenames are `知识节点.xlsx`, `知识点.md`, and `习题.md`. All teaching content is in Chinese, regardless of input language. Use separate course or pilot folders rather than adding filename prefixes or suffixes. Updates modify these existing files and preserve consistent cross-references.

### Knowledge graph workbook

Use one worksheet, named for the subject when practical. Keep six hierarchy columns by default for compatibility. When a graph needs more levels, add `7级节点`, `8级节点`, and so on immediately before `知识说明`; there is no fixed depth limit. The general header order is `1级节点` through `N级节点`, then `知识说明`, then `知识点标签`. Read the last two columns by their headers, not hard-coded G/H positions. For the default six-level capacity, the exact header is:

| Column | Header | Meaning |
| --- | --- | --- |
| A | 1级节点 | Course or top-level topic |
| B | 2级节点 | Chapter or theme |
| C | 3级节点 | Knowledge point or next level |
| D | 4级节点 | Next level, if needed |
| E | 5级节点 | Next level, if needed |
| F | 6级节点 | Next level, if needed |
| G | 知识说明 | Source document, section/result, and page ranges |
| H | 知识点标签 | Two precise Markdown references |

Each row represents one node. Populate its complete path from A through the node's level and leave all deeper levels blank. Include a row for each required parent. Preserve the existing number of hierarchy columns when it accommodates the graph; extend it only when actual deeper nodes require this. Do not add unrelated metadata columns. A Markdown format conversion does not authorize changing the workbook or graph depth.

Example `知识说明`:

```text
[教材作者]《教材名称》[版本]，第一章§4定理4.1，书页29（PDF页43）。
```

Example `知识点标签` (two actual lines within the cell):

```text
知识内容：知识点.md#kp-ch01-tensor-distributivity
习题：习题.md#kp-ch01-tensor-distributivity
```

Resolve these paths relative to the workbook's folder. Use exact delivered filenames. Store references as literal text; do not use formulas that depend on the author's absolute filesystem location. Keep `知识点.md` and `习题.md` beside `知识节点.xlsx`, including in a pilot folder.

### Shared Markdown document format

The following structure and entry formats are required for new outputs and for the whole Markdown file being edited during ordinary updates. Preserve existing anchors and user content, but normalize old structural wrappers instead of perpetuating mixed formats in untouched chapters. An explicit user restriction to a smaller scope takes precedence; report remaining inconsistencies outside it. Existing formatting is not an implicit template override. All bracketed placeholders below must be replaced with actual content or omitted with their optional paragraph. Intentional raw or preserved legacy blanks follow the exceptions below.

Use one `#` title: `# [课程名称] 知识点` or `# [课程名称] 习题与简解`. Follow it with separate paragraphs in this order:

```markdown
**版本日期。** YYYY-MM-DD。

**范围与状态。** [实际覆盖范围及审核状态，不声称未进行的审核已完成]。

[章节/附录数、实质知识节点数及习题数量。区分不同题数与按知识点重复展示的条目数。]

## 来源与页码

- **主教材A**：[作者、原文书名、版本等]；[材料](资料/教材.pdf)。
- **补充材料B**：[实际补充材料的书目与相对文件链接或来源网址；没有则省略]。
- [说明各来源的书页与PDF页定位方法，以及无编号习题的定位规则。]

## 目录

- [第1章 主题](#chapter-01)

<a id="course-subject"></a>
## 课程总览

[简短范围介绍及导航。]

<a id="chapter-01"></a>
## 第1章 主题

[知识点名称](#kp-ch01-concept)；[其他实际知识点链接]。
```

The heading mapping is fixed and independent of workbook depth:

| Document role | Required syntax |
| --- | --- |
| Document title | Exactly one `#` heading, with a space between course name and document kind |
| Source list, contents, course overview | Exactly `## 来源与页码`, `## 目录`, `## 课程总览`, in that order |
| Chapter/theme/appendix container | `##` followed by its existing course-specific name |
| Every knowledge-point entry below a chapter/theme, including content-bearing parents and intermediate navigation nodes | `###` followed by its existing name, regardless of graph depth |
| Definitions, theorems, examples, and other content inside an entry | Bold paragraph labels; no heading level `####` or deeper |
| Knowledge-file glossary | `## 重要术语译名对照`, the final section |

Do not use `##` for leaf knowledge points, recursively increase heading levels with graph depth, or use different heading rules in different chapters. Keep chapter names and graph order; a format conversion does not invent or flatten graph nodes. Group subordinate `###` entries under their existing chapter/theme, representing their complete hierarchy in `图谱路径`. Where a deeper parent needs navigation, place `**下级知识点：**` followed by links after its content (before any final erratum); its children still use `###`.

Place each `<a id="existing-or-semantic-anchor"></a>` on its own line immediately before its heading, with no blank line between them and one blank line after the heading. Containers referenced by the workbook have anchors in both main Markdown files. Course/chapter containers contain brief overview prose and navigation; chapter navigation is an inline sequence of actual links separated by `；`. Required entry metadata belongs to the `###` entries. During conversion, retain useful legacy container metadata as overview prose instead of discarding its information.

Use the front-matter paragraphs and order exactly as shown above. Keep `版本日期。` and `范围与状态。` as separate bold-labelled paragraphs, followed by a separate count paragraph. Move existing extra front-matter explanations into the appropriate source section or course-overview prose using descriptive bold labels; preserve their information rather than adding alternate structural headings such as `来源与约定` or `来源与记号`. Source labels such as A, T, or G are identities and must be preserved consistently, not renamed merely to match an example. Do not fabricate metadata or infer a universal page offset. Counts describe the actual scope and distinguish source/selected items from displayed copies.

### Knowledge Markdown

Each substantive entry must follow the field order and paragraph spacing below. Use the full graph path, precise source range(s), and the matching exercise anchor. Write content as paragraphs under bold semantic labels ending in `。`, such as `简述。`, `定义。`, `存在唯一性。`, `教材例子。`, or the source's numbered theorem label. These labels may vary with the mathematics; the three metadata labels may not. Include essential definitions, important results with hypotheses, and representative source examples when applicable, without inventing results or filling irrelevant empty sections. Adapt length to the point.

```markdown
<a id="kp-ch01-tensor-distributivity"></a>
### 分配律

**图谱路径：** 示例课程 / 预备知识 / 张量 / 张量积 / 分配律

**教材范围：** A：第一章§4定理4.1，书页29（PDF第43页）。

**习题：** [题目与简解](习题.md#kp-ch01-tensor-distributivity)

**简述。** 张量积对加法满足左右分配律。

**定理4.1(1)。** 对同型张量 $\alpha_1,\alpha_2$ 及适当类型的 $\beta$，

$$ (\alpha_1+\alpha_2)\otimes\beta=\alpha_1\otimes\beta+\alpha_2\otimes\beta. $$

右侧因子的加法同样满足分配律。

**教材应用。** 对两个向量的基展开分别使用分配律，得到其张量积的基展开。（书页30）
```

The mathematics and page numbers above illustrate the layout; verify all actual source statements and locations. Separate semantic labels into their own paragraphs rather than running two labelled blocks together. An entry may also use optional `**学习层次：** ...`; retain it when present, and include it in new content only when meaningful. Do not invent learning-level judgments during conversion. If an editorial clarification is needed, add `**整理说明：** ...` after content and optional learning-level/navigation paragraphs. For a confirmed source error, append a final paragraph of the form `**勘误：** [source location and original issue]；改为[correction]；理由：[brief justification]。` Use the corrected content above it. Omit either paragraph when unnecessary; a translation or repaired OCR is not a source erratum.

#### Final terminology glossary

At the end of `知识点.md`, after every knowledge section and its correction note, include `## 重要术语译名对照`. Use a list, for example:

```markdown
## 重要术语译名对照

- **tensor product**（英语）→ **张量积**；见[张量积](#kp-ch01-tensor-product)。
- **pullback**（英语）→ **拉回**；在线性映射作用于外形式的语境使用，见[线性映射的拉回](#kp-ch01-linear-pullback)。
```

These are examples only: list the important terms actually encountered in the supplied sources, with verified target anchors. Include original spellings, source language, chosen Chinese translation, and context when meanings differ. Reconcile synonyms without losing language-specific originals. On updates, extend or correct the existing glossary and keep it the final section. If no foreign-language terminology occurs, state `本次材料为中文，无需列出外语术语译名。` in that section.

### Exercise Markdown

Use the shared document format above, explaining repeated listings and stating distinct-exercise and displayed-entry counts. Use the same anchor for a point as in the knowledge Markdown, unique within each file. Every point section must have the following two metadata paragraphs followed by a numbered question list. Restart visible numbering at 1 in each point section and increment for subsequent questions. Do not use a table.

The following is a format illustration, not a claim about a source exercise:

```markdown
<a id="kp-ch01-tensor-distributivity"></a>
### 分配律

**图谱路径：** 示例课程 / 预备知识 / 张量 / 张量积 / 分配律

**知识内容：** [定义、结论与例子](知识点.md#kp-ch01-tensor-distributivity)

<!-- source-exercise:A-p029-1 -->
1. 证明张量积满足左分配律。

   **答案：** 在任意一组自变量上求值，结论由实数乘法的分配律得到。

   **难度：2/5；出处：** [实际来源及题号、页码]；**解答来源：** 自拟简解。
```

Replace the bracketed source placeholder with verified attribution in real outputs. Preserve numbered subparts within an exercise. Use `答案` for all question types, including proof sketches, numeric answers, choices, and fill-ins. An additional detailed `解析` section is unnecessary.

Each exercise must follow this order:

1. An HTML source marker `<!-- source-exercise:[stable-source-key] -->` immediately before its numbered question. It identifies the source occurrence, not a knowledge-node or department-system ID. Derive new keys from source label plus original exercise identifier or printed page and occurrence order, for example `A-p029-1`. Preserve existing keys such as `p011-2`; disambiguate new sources without renaming retained keys. Repeat the same key when the same exercise is displayed under another directly related point. Different source occurrences need distinct keys or explicitly preserved joint provenance.
2. The Chinese question, without a prefixed book/page attribution. Retain all mathematical hypotheses and subparts. If essential, place a relative image link with descriptive alt text in a separate indented paragraph between the question and answer.
3. A separate indented paragraph `**答案：** [concise solution]`.
4. A separate indented metadata paragraph in the exact order `**难度：[1–5]/5；出处：** [source label, section/question number and printed/PDF pages or other precise locator]；**解答来源：** [provenance]。` For an unnumbered problem, state that it is unnumbered and identify its order on the page; a project locator must not be presented as an original exercise number.
5. Optional indented `**整理说明：** ...` for a material editorial clarification, followed by optional indented `**勘误：** ...` for a confirmed source error. An erratum must be last and identify the original issue, corrected content, location, and reason. Do not add empty note fields.

For a retained Excel answer, use `解答来源：所给习题表`; for an adapted textbook hint/solution, cite its actual location. Use `自拟简解` for a generated solution; identify both contributions for a mixed solution. Do not label a retained answer as a newly checked textbook solution. Repeat the complete question, answer, metadata, and notes under each directly related point; count these as multiple displays of one source exercise.

If no supplied exercise corresponds to a point, retain its anchor, heading, and the two section metadata paragraphs, then write `所给材料中未找到对应习题。` as plain text instead of a numbered placeholder.

Use blank lines between all entry paragraphs. Indent answers, figures, display mathematics, metadata, and notes to remain inside the numbered item (three spaces for `1. `; adjust for wider number prefixes). A source comment between list items may interrupt automatic list numbering, so write the intended visible numbers explicitly. Use `$...$` for inline mathematics and `$$...$$` for displayed mathematics. Use explicit HTML anchors so locating sections does not depend on a Markdown renderer's automatic treatment of Chinese headings. All cross-file links use relative filenames and matching anchors.

### Supplementary and raw exercise files

`额外习题.md` uses the same document skeleton, anchors, `##` chapter containers, `###` point entries, two section metadata paragraphs, numbered lists, source comments, spacing, and attribution order as `习题.md`. It remains a separate file; do not merge it with the main collection or change workbook links to point to it.

- Verified search title: `# [课程名称] 额外习题与简解`.
- Raw search title: `# [课程名称] 额外习题`. For an existing mixed collection, retain the title that accurately describes the whole file and identify batch status in `范围与状态。`; do not alternate titles on successive appends.
- Use `## 来源与页码` for the actual online documents and their direct links/locators. Do not invent a main textbook for a supplementary collection. Reuse existing source labels.
- State selected-item and displayed-entry counts, without claiming mathematical uniqueness or duplicate checking. A raw collection's status must not claim correctness verification.
- Include the selected scope's chapter navigation and knowledge-point sections; do not create empty sections for every point in the entire course just to imitate full-course coverage. Existing sections are retained during conversion.
- Each point has `**图谱路径：**` and `**知识内容：** [定义、结论与例子](知识点.md#same-anchor)` as separate paragraphs. Use the actual knowledge filename if different. If a target does not exist, keep the field as plain text `**知识内容：** 该知识点尚无对应知识内容条目。`, retain the full graph path, and report the mapping gap; do not make a broken link.
- Every exercise has a source-occurrence comment immediately before its numbered item, including raw collections. Markers are structural bookkeeping, not difficulty judgments, correctness verification, or content duplicate checking. Preserve known keys; derive missing keys from recorded source identity and locator. The same already-identified source occurrence keeps its key across displays. Never use a department node ID as an exercise key.

Raw collection has exactly the same field names and order, with these intentional value differences:

```markdown
<a id="kp-ch01-concept"></a>
### 实际知识点名称

**图谱路径：** 实际课程 / 实际章节 / 实际知识点

**知识内容：** [定义、结论与例子](知识点.md#kp-ch01-concept)

<!-- source-exercise:A-sheet01-q03 -->
1. 实际中文题干，保留全部条件与分问。

   **答案：**

   **难度：；出处：** 实际来源及精确定位；**解答来源：**
```

When a source supplies an accompanying answer, fill `答案` with its faithful transcription/translation and set the value of `解答来源` exactly to `原题库`. Keep its precise location in `出处`. Difficulty remains blank, with no `/5`, zero, or placeholder. Do not create empty optional notes. Do not generate solutions, ratings, editorial judgments, or errata. Existing answers, ratings, notes, and genuinely blank fields in mixed collections survive both appends and conversions; raw rules never erase previously populated fields. Preserve already supplied detailed analysis under a bold paragraph label if present, even though new entries do not require it.

### Format-only conversion

An explicit request to normalize, standardize, or convert existing formatting selects this mode. Do not apply construction, search, or mathematical-verification workflows in this mode.

1. Read the existing target Markdown in full and the graph/knowledge mapping as needed. A course-wide request covers the two main Markdown files unless the user names a different scope. Include an existing `额外习题.md` only when requested. Do not create missing deliverables or a new retrieval ledger merely for conversion.
2. Record existing anchors, complete graph paths, source identities, question/subpart and displayed-entry counts, statements, answers, ratings, provenance, figures, notes, and intentional blanks. Preserve the hierarchy, sequence, and mathematical content. Do not deduplicate, merge, split, retag, translate anew, or replace substantive prose to imitate the illustrative subject matter.
3. Apply the skeleton and role-to-heading mapping throughout the authorized files. Normalize metadata labels and spacing: bare knowledge backlinks become `**知识内容：** [定义、结论与例子](...)`; exercise `来源` becomes `出处`; `**难度：** 2／5；**来源：** ...` becomes `**难度：2/5；出处：** ...`. Keep the actual values. Move source prefixes after answers without dropping locators. Keep optional notes after metadata and confirmed errata last. Preserve pending/unprocessed notices; never relabel them as a claim that the source contains no exercises.
4. Preserve every existing anchor and source marker. Add a missing exercise marker using recorded source label/document plus original problem number or another explicit locator; this is provenance bookkeeping, not content-similarity checking. Do not invent source information to manufacture a key. If provenance is insufficient, complete the unambiguous formatting and report that item as an unresolved format/provenance gap. Do not claim full conformance while required information remains missing. Preserve unknown answers, difficulty, and solution provenance as blanks and report pre-existing gaps instead of filling them.
5. Update contents/navigation and the version date truthfully. State in `范围与状态。` that the change is a format-only conversion to `symplectic-markdown-v1`, preserving the original coverage and verification status. Counts must not change merely because formatting changed. Leave the workbook unchanged when anchors and paths are preserved. A user-requested filename/anchor change requires updating the affected references together; raw source-history locators need changes only if their actual destination changed, without inventing searches or inspections.
6. Run the structural checklist below and compare the before/after inventories for content loss. Report converted files, applied format version, and remaining gaps. Do not claim a mathematical audit or a completed search. Repeating the conversion should make no further structural changes; a genuinely unchanged rerun need not update the version date.

### Structural acceptance checklist

Check the whole authorized Markdown scope, excluding fenced examples and LaTeX contents from heading/list parsing. Sampling only newly written chapters is insufficient.

- Exactly one title; fixed opening paragraph order and structural section names; `##` containers and `###` points uniformly applied; no heading level 4 or deeper. Graph depth and node count remain independent of heading depth.
- Every explicit anchor is unique within its file and immediately precedes its heading. Retained anchors are unchanged. Each workbook node has corresponding anchors in both main files; all actual internal and cross-file anchor links resolve. Supplementary checks cover its included nodes, not an invented full-course quota.
- Each knowledge `###` entry has separate `图谱路径`, `教材范围`, and `习题` paragraphs in that order. Each exercise `###` section has separate `图谱路径` and `知识内容` paragraphs in that order. Deeper navigation-only nodes use the same section fields plus navigation, without manufactured content or placeholder questions.
- Every displayed exercise has a source marker, complete numbered question/subparts, separate answer paragraph, and one metadata paragraph ordered `难度；出处；解答来源`. Use the exact punctuation/bold boundaries in the examples and an ASCII `/` for populated 1–5 ratings. Raw/legacy blanks are checked as blanks, not automatically filled. Missing required source information is reported, not silently accepted.
- Visible exercise numbering restarts at 1 in each point section and increments explicitly. Continuation paragraphs, figures, display mathematics, metadata, and notes are indented to the question text column: three spaces after `1. ` through `9. `, four after `10. ` through `99. `, and so on. Source comments stay at the top level immediately before their question. Blank lines separate paragraphs. Subparts stay inside the same item.
- Optional `整理说明` follows metadata; `勘误` is last. Useful existing notes/analysis and figures survive conversion. The knowledge glossary is the final section, with working links.
- The inventory of original mathematical statements, answers, ratings, source identities, subparts, displayed copies, and intentional blanks is preserved in format-only mode. Explain any unresolved legacy omissions. These are structural/content-preservation checks, not mathematical correctness or duplicate checks.
