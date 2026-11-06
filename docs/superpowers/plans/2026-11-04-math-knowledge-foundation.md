# 数学知识库与溯源问答第一阶段实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 在一个新工程边界内跑通《数学分析 I》「教材上传 → 解析 → 语义切片 → 知识草稿 → 人工审核 → 发布 → 本地向量检索 → 有页码引用的智能体问答」。

**架构：** 新建 `apps/api`、`apps/web`、`apps/worker` 与 `modules/*`，保留旧仓库只读，不在旧表上继续堆功能。PostgreSQL 同时保存业务、版本、审核与向量；对象存储保存文件；异步任务使用数据库 job/outbox，Redis 仅作 Celery broker。每个任务以独立测试交付，禁止用前端标签代替数据库审核与发布门禁。

**技术栈：** Python 3.12、FastAPI、SQLAlchemy 2、Alembic、PostgreSQL 16+、pgvector、MinIO、Redis/Celery、Next.js/React/TypeScript、Tailwind、Docling、PaddleOCR-VL、BGE-M3、BGE-Reranker-v2-m3、pytest、Vitest、Docker Compose。

---

## 0. 实施边界与验收

### 本期必须完成

- 开发/测试环境一键启动，数据库迁移、对象存储、worker 与健康检查可用。
- 用户登录与管理员/课程建设者/审核员/普通师生范围权限。
- 文字版 PDF 优先；扫描 PDF 走 OCR 候选解析，低置信度内容不得自动发布。
- 原文块、语义切片、知识点、图谱关系、题干、答案、解析和量规均有来源、版本和人工审核。
- 公共知识必须经过独立审核；本班材料由任课教师确认，不与公共知识混用。
- 发布固定完整 Release Context 和索引版本；未审核、撤销、依赖失效内容不能被普通师生检索。
- 本地 BGE-M3 向量、关键词/概念召回、Rerank 和原页引用。
- 学生可用一句话查询定义，得到答案、证据、模型标识和后续追问入口。
- 管理员配置模型密钥，用户仅选择已授权生成模型；Embedding/Rerank 不开放用户切换。

### 本期明确不做

- 完整证明步骤 DAG、参数化题库、Lean runner、复杂作业批改、PPT 生产、GraphRAG 社区摘要。
- Neo4j、Kubernetes、独立向量数据库、任意 Python 代码沙箱。
- 整门旧系统一次性数据迁移；先使用一小章真实教材建立垂直链路。

### 阶段验收样例

1. 上传含「连续」「一致连续」定义的教材章节，解析得到可定位原文块。
2. AI/导入产生的「一致连续」知识点和图谱边均为草稿，普通学生查询不到。
3. 审核员审核并发布后，学生问「什么是一致连续？」获得定义、书页/PDF 页、原文切片与 AI 实时生成标识。
4. 修改定义或撤销审核后，普通师生查询不再返回该内容；历史发布与操作记录仍可审计。
5. 重建 Embedding 索引不使原内容审核失效，历史 Release Context 不随索引重建漂移。

## 1. 文件结构

```text
AIForTeaching/
├─ apps/
│  ├─ api/
│  │  ├─ main.py                 # FastAPI 组合、健康检查、全局错误
│  │  ├─ deps.py                 # 数据库、当前用户、权限上下文
│  │  └─ routers/
│  │     ├─ auth.py              # 登录与当前用户
│  │     ├─ documents.py         # 上传、解析、原页访问
│  │     ├─ review.py            # 审核、退回、撤销
│  │     ├─ releases.py          # 构建与发布
│  │     ├─ agent.py             # 对话运行与 SSE
│  │     └─ models.py            # 管理员模型目录
│  ├─ web/
│  │  ├─ app/page.tsx            # 纯对话工作台
│  │  ├─ app/admin/models/page.tsx
│  │  ├─ components/AgentComposer.tsx
│  │  ├─ components/ModelPicker.tsx
│  │  ├─ components/EvidenceCard.tsx
│  │  └─ components/ReviewDiff.tsx
│  └─ worker/main.py             # Celery worker 与队列路由
├─ modules/
│  ├─ identity/models.py         # 用户、角色、范围授权
│  ├─ identity/policy.py         # 服务端权限判定
│  ├─ source/models.py           # 文件、教材、原文块、语义切片
│  ├─ source/parser.py           # Docling 与 OCR 适配器
│  ├─ source/chunker.py          # 数学语义切片
│  ├─ source/service.py          # 上传、解析、切片应用服务
│  ├─ knowledge/models.py        # 概念、课程节点、图谱边
│  ├─ exercise/models.py         # 题干、答案、解析、量规
│  ├─ governance/models.py       # 审核、Release、依赖、Release Context
│  ├─ governance/hashing.py      # canonical JSON 与内容哈希
│  ├─ governance/service.py      # 审核、发布、撤销事务
│  ├─ retrieval/models.py        # chunk、embedding、index_version
│  ├─ retrieval/embedder.py      # BGE-M3 固定配置与归一化
│  ├─ retrieval/indexer.py       # 独立索引构建
│  ├─ retrieval/search.py        # 混合召回、RRF、重排
│  ├─ agent/models.py            # 对话、运行、证据与论断
│  ├─ agent/service.py           # 受控问答工具调用
│  ├─ integration/models.py      # 模型目录、密钥引用、访问策略
│  └─ integration/gateway.py     # 出校与生成模型调用
├─ packages/contracts/           # API 与 Artifact JSON Schema
├─ db/migrations/                # Alembic 前向迁移
├─ tests/
│  ├─ unit/                      # 切片、哈希、权限、检索与模型路由
│  ├─ integration/               # PostgreSQL/MinIO/worker 事务与 RLS
│  ├─ e2e/                       # 上传—审核—发布—问答
│  └─ fixtures/textbooks/        # 合成、可公开的数学教材测试页
├─ evals/                        # 初始定义/定理检索金标准
├─ infra/
│  ├─ compose.dev.yml
│  ├─ compose.staging.yml
│  ├─ Dockerfile.api
│  ├─ Dockerfile.web
│  └─ Dockerfile.worker
└─ docs/operations/              # 开发、备份、发布、故障恢复
```

## 2. 实施顺序

### 任务 1：新工程与可重复开发环境

**交付物：** 新工程边界、Linux 容器、PostgreSQL/pgvector、MinIO、Redis、API/Web/worker 健康检查与 CI。旧目录保留只读，不删除或改写旧业务数据。

- [ ] 建立 `apps/`、`modules/`、`packages/contracts/`、`db/migrations/`、`tests/`、`infra/` 目录与 Python/Node 锁定依赖。
- [ ] 在 `infra/compose.dev.yml` 配置 PostgreSQL 16+pgvector、MinIO、Redis、API、worker、Web；密钥从 `.env.local` 或 secret 文件读取，禁止进入仓库。
- [ ] 在 `apps/api/main.py` 暴露 `/health/live` 与 `/health/ready`；ready 必须实际执行 `SELECT 1`、检查对象存储与 worker heartbeat。
- [ ] 编写 `tests/integration/test_health.py`：数据库不可用、对象存储不可用、worker 过期时 ready 失败，live 仍能区分进程存活。
- [ ] 配置 pytest、Vitest、Ruff、TypeScript、Alembic upgrade/downgrade 测试；CI 使用 Linux 容器，不使用 Windows 本机差异作为豁免。
- [ ] 创建 `modules/ops/models.py` 与 `modules/ops/service.py`，在初始迁移中落地 `ops.job`、`job_attempt`、`outbox_event`、`idempotency_key`；outbox 保存 kind/business_key/payload/created_at/published_at，`(kind, business_key)` 唯一，idempotency 保存作用域、请求键、请求哈希和响应快照。
- [ ] 编写 `tests/integration/test_ops_outbox.py`：业务事务回滚不产生 outbox；相同业务键重复入队返回原 job；worker 重试不重复副作用；连接池归还后清理用户与范围上下文。
- [ ] 添加 `docs/operations/development.md`，写明开发启动、测试、迁移和 R720 仅作开发测试机的约束。

### 任务 2：身份、范围权限与默认拒绝

**交付物：** 用户、角色、课程/教学班范围授权和数据库 RLS 基线。

- [ ] 建 `iam.user_account`、`role`、`permission`、`role_assignment`、`auth_session`；`role_assignment` 的 course/offering/owner 范围使用互斥 CHECK。
- [ ] 定义管理员、课程建设者、审核员、任课教师、学生权限矩阵；公共知识审核与班级教师确认分离。
- [ ] 编写负向测试：学生读取草稿表、教师读取其他班、审核员审核自己创建的版本均失败；非 owner 数据库角色不能绕过 RLS。
- [ ] `apps/api/deps.py` 在事务内设置用户与范围上下文；连接归还前清理上下文，不能污染连接池下一个请求。
- [ ] 登录支持开发测试账号与学校 SSO 接口边界；生产 SSO 未接入前使用受控本地账号并限制范围。

### 任务 3：文件、教材版本与原文块

**交付物：** 可追溯的文件上传、页码和原文块存储。

- [ ] 建 `source.asset`、`document`、`document_version`、`document_page`、`source_block`、`source_license`；文件使用 SHA-256 去重，物理去重与每课程授权分离。
- [ ] `apps/api/routers/documents.py` 限制 PDF 类型、大小、页数，校验文件签名、病毒/恶意结构和存储路径；异步解析返回 job_id。
- [ ] `modules/source/parser.py` 提供统一 `ParsedPage/RawBlock` 类型：页码、坐标、阅读顺序、块类型、原文、语言、置信度、解析器版本。
- [ ] 文字层以 Docling 为主，轻量抽取交叉检查；乱码、覆盖不足、阅读顺序异常转 OCR；扫描页适配 PaddleOCR-VL，保存原页与置信度。
- [ ] 以合成多栏、跨页定理、公式、中文/英文混排 PDF 做 golden fixture，测试原始页坐标与块顺序不丢失。
- [ ] 原页访问默认裁剪引用区域，检查 license 有效期、用途和范围权限，禁止通过连续请求拼出整本受限教材。

### 任务 4：数学语义切片与多语言版本

**交付物：** 可审核 `content_slice_version`，不是按字符数切割的纯文本块。

- [ ] 建 `content_slice`、`content_slice_version`，字段包含 `content_role`、`origin_kind`、`origin_run_id`、`parent_slice_version_id`、语言、翻译关联、来源依赖和 `content_hash`。
- [ ] `modules/source/chunker.py` 实现定义、定理、证明、例题、普通段落和表格的规则：定理条件/结论原子化，长证明保留父定理，普通段落目标 300—800 tokens。
- [ ] 对以下 fixture 写测试：跨页定理条件不丢；定义不与下一题混合；公式 LaTeX 与原图并存；子问继承公共题干；新原文与旧翻译不能形成新 Release。
- [ ] `normalized_content` 只做受控 Unicode/空白处理，禁止改变上下标和数学语义；原文、审核翻译、AI 解释分角色与来源。
- [ ] 内容哈希使用 canonical JSON + SHA-256；测试 JSON 键重排不改变哈希，修改定理条件、来源、翻译或父版本会改变哈希。

### 任务 5：知识、图谱、习题与独立版本

**交付物：** 草稿态概念、课程节点、图谱关系、题干、答案、解析与量规。

- [ ] 建 `knowledge.concept/concept_version`、课程节点与 `relation/relation_version/relation_evidence`；边有独立版本与来源，不能由节点审核自动通过。
- [ ] 建 `exercise.exercise_version`、`answer_version`、`solution_version`、`rubric_version`；exercise_version 不保存答案副本，教材印刷答案只作来源证据。
- [ ] 所有实体使用稳定 UUID + 不可变版本；已发布版本禁止 UPDATE/DELETE，`base_version_id` 必须属于同一实体。
- [ ] 编写审核草稿生成器：从已解析切片产生候选知识点、关系和题目，不直接写入正式知识；保存模型/prompt/输入哈希与人工修改记录。
- [ ] 建最小 `math.convention_version`、`statement_version`、`symbol_binding`；首期只支持 scoped formula_ast 与符号绑定，不实现完整 proof DAG。

### 任务 6：审核、依赖失效与原子发布

**交付物：** 未审核内容绝不对普通师生可见的数据库与事务规则。

- [ ] 建 `review_decision`、`review_decision_item`、`change_set_contribution`、`dependency_decision_manifest`、`quality_check/finding`。
- [ ] 实现 `knowledge_approval` 与 `teaching_confirmation` 两种决策，使用单调 `decision_seq`；最新终态 approved/rejected/revoked 决定有效性，不允许只查历史 approved。
- [ ] 公共知识 `reviewer_user_id != version.created_by` 且不得有内容贡献；审核人修改任何内容哈希字段后必须由另一审核员复审。
- [ ] 建 `release`、`release_item`、`release_context`，覆盖学院/课程/班级三类范围；上下文固定 release 与具体 index_version，历史不随当前指针变化。
- [ ] `governance/service.py` 采用两阶段发布：事务外构建，切换时按学院→课程→班级顺序获取 advisory lock，校验 base、完整依赖、许可与最新决策后只更新对应指针。
- [ ] 编写并发测试：双发布者、撤销在终检前后提交、撤销节点导致关联边隐藏、撤销 rubric 使题目失效、发布期间读取不撕裂。
- [ ] 撤销与隔离同事务写入；旧索引残留项也不能返回。单项撤销影响进行中作业时冻结并通知教师。

### 任务 7：本地向量索引与混合检索

**交付物：** 可重建索引、模型版本隔离、权限前置的混合召回。

- [ ] 建 `retrieval.chunk`、`chunk_source`、`chunk_embedding`、`index_version`；`retrieval_text` 仅存 chunk，记录 `retrieval_template_version`，不进入内容审核哈希。
- [ ] `modules/retrieval/embedder.py` 固定 BGE-M3 revision、tokenizer、维度 1,024、L2 归一化、FP32 和 cosine；测试输出维度、有限数值和相同输入哈希复用。
- [ ] `modules/retrieval/indexer.py` 仅读取已审核 Release 内容，创建独立 index_version 与 HNSW `vector_cosine_ops`；不同模型/模板不混用，历史 Release Context 不漂移。
- [ ] `modules/retrieval/search.py` 实现概念/别名精确召回、语言感知稀疏召回、dense 召回、RRF 与 BGE-Reranker-v2-m3；参数集中配置并版本化。
- [ ] 测试草稿、未审核、撤销、越权内容无法进入候选；多语言问题能返回正确原文/已审核翻译；低召回时不能伪造页码。
- [ ] 在 R720 只做兼容性/小规模基准，记录 5,000 块 Embedding、100 对重排和 10 万向量索引耗时；生产参数由校内 staging 重新压测。

### 任务 8：模型治理与生成调用

**交付物：** 管理员配置密钥与目录，用户选择已授权生成模型，密钥不下发浏览器。

- [ ] 建 `model_provider`、`model_endpoint`、`model_secret_ref`、`model_access_policy`、`user_model_preference`、`model_call`。
- [ ] 管理界面支持供应商、Base URL、密钥写入/轮换、能力、可见范围、数据级别、额度和上下架；密钥保存后不可回读，只显示引用和轮换时间。
- [ ] `integration/gateway.py` 校验 endpoint、角色、任务、数据分类、许可、预算和模型状态；构造 `context_envelope` 最小上下文，受限 workspace 整体禁止云端。
- [ ] 用户选择只影响后续 agent_run；Embedding/Rerank/OCR 无用户切换入口；篡改 endpoint_id、选择下架模型或超额度均由后端拒绝。
- [ ] 测试日志、API、SSE、导出和错误信息无密钥；云端不可用时返回可恢复错误与已检索证据，不伪造回答。

### 任务 9：EvidenceBundle、MathAnswer 与纯智能体问答

**交付物：** 一句话问定义，得到可定位证据和明确来源的回答。

- [ ] 建 `agent.conversation/message/agent_run/tool_call`、`evidence_bundle_version`、`answer_claim`、`artifact_version`；每次运行固定 release_context、模型修订、策略版本和输入哈希。
- [ ] `agent/service.py` 只允许受控检索与生成工具，不允许模型执行 SQL/shell；实现服务端教学策略判定，作业/考试场景限制跨工具绕过。
- [ ] MathAnswer 论断类型支持教材原文、审核翻译、教师指导、模型推断、计算和形式验证；每个类型有强制引用字段，缺证据不得标为教材事实。
- [ ] `POST /agent/runs` + SSE 输出简短状态、回答、引用和 Artifact；断线重连使用 run_id/event_id 恢复，不重复执行有副作用工具。
- [ ] `apps/web` 实现纯对话、课程上下文、历史任务和按需证据面板，不做功能广场；问定义直接给定义与教材原页，AI 实时生成不继承人工已审核标签。
- [ ] 移动端单栏、键盘操作、MathML、图表替代文本、SSE 不抢焦点；模型选择器只显示授权目录。

### 任务 10：审核工作台与端到端试点

**交付物：** 建设者、审核员和任课教师可在同一智能体工作台完成内容建设与发布。

- [ ] `ReviewDiff` 支持原文/切片对照、知识点与图谱边 diff、习题/答案/解析/rubric 独立审核、低置信度 OCR 队列、退回意见与修改后复审。
- [ ] 批量操作只展示效率入口，数据库仍逐项保存人工决策；禁止跨类型一键全选通过，审核人修改后强制独立复审。
- [ ] 用《数学分析 I》真实授权章节做端到端测试：上传→解析→切片→候选知识/图谱/题目→审核→发布→学生问答→修改→撤销→检索不可见。
- [ ] 建立首批至少 50 个开发用定义/定理问题，固定金标准证据、答案与教师标注，验证页码、条件完整性与拒答；按总体方案逐步扩充至 400 问及隐藏集。
- [ ] 完成迁移回滚、任务重试、数据库/对象恢复、模型超时、索引构建失败与权限负向测试。
- [ ] 编写 `docs/operations/staging.md`：校内 staging 部署、备份、恢复、模型额度、发布审批和 go/no-go 清单。

## 3. 每个任务的 TDD 循环

所有任务按同一小步执行，不先写实现再补测试：

- [ ] **步骤 1：写失败测试。** 用本任务规定的输入、权限、预期状态和错误信息建立最小失败用例。
- [ ] **步骤 2：运行测试确认失败。** 确认失败原因确实是功能不存在，而非 fixture、环境或测试本身错误。
- [ ] **步骤 3：实现最少代码。** 只实现当前测试所需接口与规则，不把后续完整功能提前塞进当前模块。
- [ ] **步骤 4：运行测试确认通过。** 同时运行相关单元、数据库和权限测试，不能只跑单个 happy path。
- [ ] **步骤 5：检查数据库与来源约束。** 版本、哈希、来源、审核、Release Context 与权限均有数据库/服务层约束，不依赖前端显示。
- [ ] **步骤 6：提交独立变更。** 使用 `feat:`、`fix:`、`test:` 等清晰提交信息，提交前不夹带旧数据删除或无关重构。

建议命令入口：

```bash
# 后端单元与集成测试
pytest tests/unit tests/integration -q

# 数据库迁移与空库重建
alembic upgrade head
pytest tests/integration/test_migrations.py -q

# Web 契约与可访问性测试
npm --prefix apps/web run test
npm --prefix apps/web run lint

# 关键垂直链路
pytest tests/e2e/test_textbook_to_grounded_answer.py -q
```

## 4. 开发中不得违反的规则

1. 不把旧表结构、旧状态或「历史已审」标记直接当新平台有效审核。
2. 不允许模型直接写正式表、执行任意 SQL/shell、选择任意 URL 或绕过人工发布。
3. 不用「release 已发布」代替逐项人工审核，不用 Lean 通过代替人工审核。
4. 不在内容表中保存随模型/模板变化的检索文本，避免换模型触发全量复审。
5. 不在浏览器保存 API Key，不让用户任意指定 Base URL 或未授权 endpoint。
6. 不把云端模型不可用的教材/学生数据自动转发到其他云供应商。
7. 不把 R720 的性能数字写成校内生产服务器的并发承诺。
8. 不在一个迁移中直接重命名/删除已上线列；采用 expand/contract 与生产规模演练。
9. 不把原始 PDF、OCR、工具输出或网页当作可信系统指令。
10. 不以功能页面数量、模型演示或课程数量作为验收标准。

## 5. 计划自检

- **范围：** 本计划只覆盖第一阶段垂直链路；教学增量、Lean、参数化题库、完整证明 DAG 按设计文档后续阶段单独计划。
- **数据一致性：** 原文、语义切片、检索投影、审核、Release Context 和向量索引分别建模，避免双真源与换模型导致复审。
- **权限：** 草稿、公共知识、班级材料、模型密钥和隐藏评测集分别授权，均有负向测试要求。
- **可验收性：** 每个任务都有可测试交付物，最终用真实授权章节验证学生问定义与撤销不可见，而非仅验证服务能启动。
- **环境：** R720 仅作开发测试，生产使用学校本地高配服务器，开发与生产使用同一容器镜像和迁移流程。
