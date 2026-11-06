# 武汉大学数学学院 AI 教育智能体平台设计

- 日期：2026-11-03
- 文档类型：总体产品与技术设计（最终设计基线 v1.3.3，含数学语义、多语言与校内生产部署规范）
- 建设基准：基于现有资产升级，但不受旧代码和旧表结构约束
- 首期范围：《数学分析 I》一门课完整教学闭环
- 部署约束：R720 仅作开发测试；正式部署在学校本地高配服务器，生产规模经校内 staging 压测确定，云端生成模型优先并可切换本地模型
- 审查状态：v1.3 为历史基线；2026-11-05 由 `2026-11-05-platform-blueprint-v2-design.md` 补充全范围扩展与现行 db-v2/API 约定，实现后仍须按第 18 章及 E01—E18 实测验收

## 1. 执行摘要

本项目不是把现有课程网站增加几个 AI 页面，而是建设一个长期演进的数学教育智能体平台。平台以统一对话为入口，以教材、知识点、知识图谱、习题和学习记录为证据，以图形、课件、批改结果和课程版本为产物。

核心技术决策如下：

1. **采用模块化单体起步，而不是直接微服务化。** Web、API、worker 分开部署，业务在代码和数据库 schema 层隔离；负载出现后可将检索、批改、渲染等模块独立拆出。
2. **PostgreSQL 是唯一业务真源。** 业务数据、知识图谱、版本、审核和权限均存入 PostgreSQL；pgvector 只承担向量检索，对象存储只承担文件，不形成第二套业务真相。
3. **所有正式内容均版本化并强制人工审核。** 稳定身份与内容版本分离，AI 只生成草稿；知识点、图谱节点与关系、习题、答案量规及公开教材切片必须具有与当前内容哈希匹配的人工审核通过记录。未审核内容对普通教师和学生不可见，审核通过后生成不可变 release，并原子切换到师生可见版本。习题另设独立的 Lean 形式化验证标签，不能用 Lean 替代人工审核，也不能用人工审核冒充 Lean 验证。
4. **智能体通过版本化工具调用业务能力。** 不允许模型直接写数据库，不允许绕过权限、审核和发布流程。
5. **前端是纯智能体工作台。** 默认只有对话、课程上下文和任务历史；图形、PPT、原文、图谱和审核差异按需在同一页面打开。
6. **先完成一门课的真实闭环，再复制到全院。** 不用课程数量代替质量，不用模型演示代替教学验收。
7. **多语言保留原文与审核翻译。** 按原文块识别语言，语义切片与检索投影分离；BGE-M3 作为多语言向量基线，其他模型只在真实评测达标后切换。
8. **服务器实测决定运行参数。** CPU/GPU/内存/磁盘和出网只影响 OCR、向量化和部署并发，不影响审核与版本架构；全量导入前先完成目标硬件基准测试。

现有课程包、题库、公式转换、来源审计和可验证的工程组件可以复用；旧数据库只作为迁移输入，不作为新平台 schema 的设计约束。

## 2. 建设目标与边界

### 2.1 建设目标

- 教材原文可解析、可定位、可切片、可向量化，回答能回到书页和原文块。
- 形成课程知识结构、跨课程概念、语义关系、习题和学习证据的关联网络。
- 学生用一句话完成问定义、学证明、做练习、改错、绘图和复习。
- 教师用一句话完成备课、课件、作业、批改、学情分析和内容纠错。
- 建设者能够批量导入教材并生成课程草稿，审核者能够对照原文修改和发布。
- 平台能够持续增加新工具、新课程、新模型和新产物类型，而不修改核心交互和核心发布机制。
- 所有关键行为可审计、可回放、可评测、可回滚。

### 2.2 首期不做

- 不做多智能体自由讨论或复杂 Agent 社会；一个编排器配合确定性工具更可控。
- 不引入 Neo4j、Kubernetes 和独立向量数据库；当前规模没有足够收益。
- 不让 AI 自动发布课程、自动给正式成绩或自动向校外发送内容。
- 不把完整教材、学生名单和含身份信息的作业图片默认发送给云端模型。
- 不做大而全的门户、资讯、商城、论坛和复杂数据大屏。
- 不用模型生成结果直接覆盖旧知识库。

### 2.3 当前系统基线与复用决策

本方案基于对现有仓库的只读盘点，不把项目当作从零开始。仓库历史清单记录 14 套标准课程包、4,146 个实质知识点、4,807 道题和 169 张引用图片；这些数字是迁移审计起点，不代表线上当前数量或全部已人工实审。历史代码还记录了 33 门线上已发布课程与 36 门规划课程的不同口径，第 0 阶段必须对账。

| 当前能力 | 最终处理 |
|---|---|
| 原生 HTML/JS 课程、图谱、题库与离线包 | 内容和可复用算法保留，导航与页面壳替换为智能体工作台；离线包明确非正式数据 |
| FastAPI、PostgreSQL、worker 和模型调用 | 保留 Python 技术积累，按新模块边界渐进重构，不一次性搬入旧代码 |
| 旧数据库与三件套导入 | 旧库只读快照，经 `legacy_ref` 映射进入新草稿区，不继承旧表约束和虚假实审状态 |
| 关键词 ILIKE 问答与知识点正文切片 | 替换为已审核内容切片、混合检索、向量与引用评测，不宣称现有 vector 列已完成向量化 |
| PDF 上传与 AI 课程生成 | 复用任务、进度、重试和来源思想，新增忠实教材解析与可审核 content_slice 产线 |
| 拍照转写与 AI 初评 | 保留转写/判分分离、AI 分/教师分分离，补班级闭环、CAS 和严格复核 |
| PPT 页面计划、OMML 和版本 diff | 优先复用成熟组件，接入 Artifact 协议和 PowerPoint/WPS 验收 |
| 模型白名单与密钥服务端保存 | 安全审查后复用，补全所有模型类型的数据出校矩阵、SSRF 和成本硬限额 |
| 历史测试、来源审计和部署脚本 | 作为迁移守门员保留并更新，历史测试通过不等于真实教学效果已验证 |

## 3. 外部产品借鉴与本地化取舍

| 产品 | 可借鉴做法 | 本平台的取舍 |
|---|---|---|
| ClawsGO Science | 工作区、运行记录、真实文件、代码和图表产物、可追溯科研过程 | 引入统一产物区和沙箱运行记录，但围绕数学教学而非科研项目管理 |
| ChatGPT Study Mode | 苏格拉底式提问、分步提示、知识检查，而非直接给答案 | 学伴默认引导；自由学习允许退出引导，作业策略由服务端统一控制 |
| Khanmigo | 学生导师与教师备课、题目、量规和学情工具并行 | 使用同一智能体入口，按角色加载不同工具，不建设两套孤立系统 |
| Gemini Guided Learning / Study Notebooks | 诊断、分步解释、小测、交互图和学习进度 | 增加可点击数学图、诊断后短讲解与再测，不把进度仪表盘作为默认首页 |
| NotebookLM / Gemini Notebook | 限定资料来源、逐条引用、学习指南、闪卡和测验 | 支持「只根据选定教材/讲义回答」，引用回原页；生成知识仍需审核 |
| ALEKS | Knowledge Space、初始诊断、可学习主题和周期性再测 | 用先修图计算学习准备度，加入遗忘复习；不照搬其具体算法或宣传效果 |
| Carnegie MATHia | 技能级反馈、教师 LiveLab 干预和详细学习证据 | 给教师共性困难与需帮助提示，不做隐蔽监控或单凭在线时长判断学习 |
| MagicSchool / SchoolAI | 教师配置学生空间、工具边界、教学目标和班级洞察 | 形成版本化班级 policy；私人对话默认不可见，课程指导空间须事先告知 |
| 松鼠 AI | 知识图谱、知识追踪、错因诊断、学习路径推荐 | 图谱必须服务检索、推荐和诊断，不能只用于可视化 |
| 雨课堂、超星、学堂在线 | 课前课中课后闭环、AI 备课、题库、作业、学情和课程建设 | 保留完整教学闭环，但去掉传统功能菜单，以智能体任务串起来 |
| LoreGraph 等课程构建工具 | 上传资料、AI 生成初稿、教师审核、发布 | 增加数学公式、教材页码、版本 diff、原子发布和回滚能力 |

教学策略同时依据学习科学：检索练习与间隔复习用于保持记忆，示例讲解用于新手并逐步撤去支架，及时反馈用于修正错误。平台不宣称使用某个功能就必然提升成绩；真实效果由试点数据评估。

公开资料用于产品方法参考，本轮未对所有产品逐一进行账号内实测；不代表本平台已经复现其全部功能，也不采用厂商宣传数据作为验收依据。涉及认证、隐私或效果的内容均按供应商自述处理，正式采购或对接前必须独立核验。

## 4. 角色、权限与责任边界

### 4.1 角色模型

权限判断采用「角色 + 范围 + 动作」，范围包括全院、课程、教学班和个人。一个用户可以在不同课程拥有不同角色。

| 角色 | 主要能力 | 明确禁止 |
|---|---|---|
| 学生 | 使用已发布内容、个人学伴、练习、作业、绘图和个人产物 | 读取草稿、查看他人作业、修改正式知识 |
| 普通教师 | 使用已发布内容备课、管理本人教学班、生成个人教学产物 | 默认读取知识库草稿、越权管理其他教师班级 |
| 课程建设者 | 新建课程、上传教材、运行解析、编辑草稿、提交审核 | 发布本人建设的正式知识、给正式成绩 |
| 审核员 | 查看授权课程的草稿、对照原文、修改、退回、审核 | 越过发布校验、修改历史审核记录 |
| 任课教师兼审核员 | 在课程授权范围内完成教学审核和发布 | 审核无授权课程 |
| 课程负责人 | 分配课程建设和审核人员、管理课程发布策略、紧急下线 | 删除审计记录、绕过发布流程 |
| 平台管理员 | 账号、角色、模型、存储、预算、安全和系统配置 | 因系统管理身份自动获得课程内容审核权 |

AI 生成的公共知识默认执行建设与审核分离。个人 PPT、个人练习题等不进入公共知识库的教学产物，可由教师自行使用，不受双人审核限制。

### 4.2 四类资源与审核范围

| 资源类别 | 默认可见范围 | 分享前要求 | 审核责任 |
|---|---|---|---|
| 个人产物 | 仅创建者 | 不分享时保持 AI 草稿状态 | 创建者自行判断，不显示公共知识标签 |
| 班级教学材料 | 指定教学班 | 生成班级 teaching release | 任课教师逐项确认后方可向学生发布，显示「教师已审核」 |
| 学院/课程公共知识 | 授权范围全体师生 | 生成学院目录或课程公共 knowledge release | 建设者与独立审核员分离，执行全项人工审核 |
| 正式评价材料 | 指定班级与授权教师 | 作业/测验发布和成绩发布分别确认 | 任课教师负责；AI 不得自动发布试题或正式成绩 |

教师可以读取和修改本人创建的私人草稿或纠错草稿，但不能因此读取其他建设草稿。私人产物被分享到班级时必须进入班级 teaching release，不能临时增加 `visible=true` 绕过审核。班级材料若进一步进入学院公共知识库，必须重新经过独立审核。

公共知识强制 `change_set.creator != reviewer`。审核员只要修改任何进入 `content_hash` 的字段，就会成为内容贡献者，原审核结论失效且必须由另一名授权审核员复审。任课教师对本人班级材料的人工确认属于 `teaching_confirmation`，不冒充学院公共知识的 `knowledge_approval`。

### 4.3 内容可见性与质量标签

**普通师生可见的唯一条件：内容属于其可见范围内的已发布 release，当前具体内容版本具有与该 release 类型匹配的最新终态人工决策，版本、依赖哈希及 dependency_decision_manifest 均匹配，且许可有效、未被撤回或隔离。** 只有 release 状态为 `published` 不够，只有单项审核通过但尚未发布也不够。

- 学院/课程公共知识 release 只接受 `knowledge_approval`：必须由未创建或修改该内容的独立授权审核员签署，显示「人工已审核」。
- 班级 teaching release 只接受 `teaching_confirmation`：必须由该教学班授权任课教师逐项确认，显示「教师已审核」。教师可以确认本人创建的班级材料，但该决策永远不能用于学院或课程公共知识发布。
- 历史/进行中教学实例沿用其固定 release 的决策类型；后续 `rejected/revoked`、严重撤回或许可失效会覆盖历史访问。
- `draft`、`in_review`、`rejected`、未审核和审核失效：仅授权所有者、建设者、审核员或课程负责人按范围可见，普通班级学生不可见。
- 人工决策标签覆盖知识点、图谱节点、每条图谱关系、习题、答案、量规、知识正文和公开教材切片。不能整门课或整班打一个标签就掩盖其中未审核项目。
- 「Lean 已验证」只表示指定习题依赖闭包中的形式化命题与证明在记录的环境中通过 Lean 检查。它是独立标签，不是人工审核的替代品，也不自动证明自然语言题目与形式化命题完全等价；形式化对应关系仍须人工核对。
- 未完成 Lean 验证的习题在所需人工决策通过后能否发布，由课程质量策略决定；无论是否发布，都必须如实显示 Lean 状态。
- 检索权限必须在召回之前生效，不能先检索草稿再靠提示词要求模型保密。
- 草稿索引与正式索引按 release/可见范围物理或逻辑隔离；正式索引只能包含人工决策有效且随 release 发布的内容。
- 云端模型只接收当前动作所需的最小上下文，并再次执行发送范围校验。

### 4.4 可执行权限模型与数据库默认拒绝

`iam.role_assignment` 使用明确外键表示范围：`course_id`、`offering_id`、`owner_user_id` 按全局、课程、教学班和个人范围满足互斥 CHECK，不使用任意字符串 scope。角色撤销后使相关会话和缓存授权失效。

- 普通师生数据库账号只访问已发布视图和受控函数，不直接读底层版本表；建设、审核、worker、迁移账号使用不同数据库角色。
- 敏感表启用 RLS 与 `FORCE ROW LEVEL SECURITY`，连接池在事务内 `SET LOCAL` 用户和范围上下文。测试必须使用非 owner、非 superuser，防止测试绕过 RLS。
- 文件签名 URL、教材原页、Artifact 下载、SSE 创建与重连、导出和检索日志执行同一授权判定，使用短时 URL，不根据 obscure UUID 放行。
- Artifact 明确 `private / class_shared / course_published` 可见性；`class_shared` 必须绑定同学院/课程/教学班的有效 teaching release，`course_published` 必须绑定公共知识 release。`assignment_item` 保存 `(release_context_id, source_release_id, exercise_version_id, exercise_instance_id nullable)`：`source_release_id` 可以是上下文中的课程公共 release（公共教材题）或本班 teaching release（班级自建题），参数化作业还必须固定该来源 release 中已审核的具体 exercise_instance 与 seed，但必须是该来源 release 中决策匹配的题目，教师生成个人题再布置给学生的绕过路径必须有负向测试。
- Artifact 的打开、渲染、下载和签名 URL 续期也经过同一 Policy Decision Point，并绑定当前 activity context。artifact_version 记录 derived_from_exercise、contains_solution/answer_test 和来源上下文；评价期禁止打开由被考查题派生或含解答的产物，防止考前创建求解器、考中直接打开。
- 每次工具调用重新计算权限，不能因模型在对话前部获得过授权就永久继承。

### 4.5 Release 类型与检索合成

`release.release_kind` 分为 `college_catalog`、`course_knowledge`、`class_teaching`，每类有唯一匹配的范围指针和决策类型。课程公共 release 固定 `base_college_release_id`；班级 teaching release 固定 `base_course_release_id`，其 manifest 只包含本班新增材料，不把班级内容复制进课程公共 release。

`governance.release_context` 是不可变行，保存 `(college_release_id, course_release_id, class_release_id, college_index_version_id, course_index_version_id, class_index_version_id, context_hash)`，并按 context_hash 唯一去重、get-or-create，避免每次请求制造重复行。解析班级上下文时先读本班当前 teaching release，再沿 `base_course_release_id → base_college_release_id` 固定基线；解析课程公共上下文时先读课程当前 release，再固定其学院基线。开始固定后不得再读当前指针，因此新课程或公共版本发布不会使历史班级、作业或回放漂移；教师通过新 teaching release 显式采用公共更新。

每个 release 可拥有多个重建的 index_version，`release.active_index_version_id` 指向当前浏览使用的索引；release_context 保存解析当时的确切索引版本，历史任务不随索引重建改变。检索在 release_context 允许的索引集合内分别召回，再合并、重排和生成，权限过滤先于任何模型上下文。解析和发布都校验完整 base 链的 published/未撤回状态、最新决策和许可；上级撤回会使依赖上下文失效并隔离，而不是继续服务。失效时明确显示「内容已撤回或暂不可用」，不能自动回退到另一版或未经审核的内容。

## 5. 纯智能体产品设计

### 5.1 统一工作台

默认页面只包含：

1. 顶部：平台名称、当前课程、用户身份；课程上下文可收起。
2. 左侧：新任务和最近任务，可完全折叠，不设置传统功能树。
3. 中部：对话、证据、工具执行状态和连续追问。
4. 右侧：按需打开的产物区；没有产物时不显示。
5. 底部：统一输入框，支持文本、LaTeX、语音、图片、PDF、数据和代码附件。

建设与审核界面统一显示「未审核」「审核中」「人工已审核」「教师已审核」「审核失效」标签；习题额外显示「Lean 验证中」「Lean 已验证」「Lean 未通过」或「未做 Lean 验证」。普通师生只能看到人工审核有效的正式内容，其习题同时展示真实 Lean 状态。点击标签可查看审核人或验证环境、时间和证据，不显示无法核验的装饰性徽章。

**实时 AI 回答不继承来源的「人工已审核」标签。** 它应标记为「AI 实时生成，基于以下来源」，并与教材原话、教师课程口径、模型推断和计算工具结果区分。实时回答若要成为公共知识，必须转成草稿并重新审核。

学生私人对话默认不对教师开放。教师只查看课程作业、学生主动分享的对话，或在开始即明确告知可见性的课程指导空间；班级学情优先使用最小化聚合事件，不做隐蔽监控。

纯对话不等于所有任务都只能用自然语言。批量审核、成绩复核、图谱 diff 和紧急撤回在同一工作台中提供可预测的结构化产物面板，自然语言与表单/键盘操作是等价入口，不增加传统功能迷宫。

用户不需要知道「问答、图谱、绘图、批改、PPT」分别位于哪个菜单。系统根据任务调用工具，并在原对话中返回结果。

### 5.2 结果类型

智能体输出采用版本化 Artifact 协议，而不是把任何结果都渲染成 Markdown：

- `Answer`：定义、解释、证明、对比和简短结论。
- `MathAnswer`：结构化论断、假设、证据、计算和验证状态，避免只有不可审计的自然语言。
- `Citation`：教材版本、书页、PDF 页、章节、原文块和知识点。
- `EvidenceBundle`：供智能体使用的内部证据包，固定 release_context、切片版本、哈希、许可和图路径。
- `Figure`：SVG、PNG、PDF、Plotly 交互图及对应代码和数据。
- `InteractiveMath`：公式、参数、图形和数据联动的可编辑数学实验。
- `KnowledgeGraph`：可展开的概念关系图，可回到证据。
- `ExerciseSet`：题目、提示、作答区、量规和反馈。
- `HomeworkReview`：转写、逐步判分、教师复核和成绩。
- `ProofReview`：证明步骤、依赖、第一处可疑错误和教师复核。
- `Slides`：可编辑 PPTX、逐页预览、讲稿和版本差异。
- `Document`：讲义、试卷、学习计划和报告。
- `CourseDraft`：课程、知识点、关系、习题和质量问题集合。
- `ReviewDiff`：原文对照、前后差异、自动检查和发布动作。

新增产物类型只需增加 renderer、schema 和查看器，不新增一套一级导航。

### 5.3 三条典型链路

**学生问定义：**「什么是一致连续？」→ 识别课程与概念 → 优先读取已发布定义和教材证据 → 直接给定义 → 补充与普通连续的关键差异 → 提供原页和一级提示，不强制进入练习流程。

**学生绘图：**「画出 sin(x)/x 与导数并标出可去间断点」→ 沙箱生成并运行代码 → 校验输出 → 右侧显示图形 → 可继续用自然语言修改 → 可导出 SVG、PDF、PNG 和代码。

**教师纠错：**「定理 3.2 的条件错了，把原页给我看」→ 打开当前正式版本和教材证据 → 教师提出修改 → 生成新草稿和 diff → 授权审核者通过 → 新 release 原子替换，旧版本仍可追溯。

### 5.4 无障碍与移动端是首期要求

以 WCAG 2.2 AA 为目标，覆盖完整键盘操作、焦点管理、高对比、缩放、减少动画和非颜色状态提示。SSE 更新使用克制的 live region，不抢焦点。公式同时提供 MathML/语义化朗读和文本说明；图形提供数据表与文字摘要；图谱提供等价列表和键盘路径；扫描原页提供可读取文本。审核 diff、Lean 标签和成绩状态不能只靠颜色或 hover。

移动端采用单栏对话与产物切换，支持相机上传、LaTeX 编辑、断线恢复和低带宽模式。生成 PPT/PDF 检查标题结构、阅读顺序、alt text 和 tagged PDF。测试包含 NVDA、VoiceOver/TalkBack 和真实用户操作，不以自动扫描代替验收。

## 6. 总体技术架构

### 6.1 技术选型

| 层 | 决策 | 理由 |
|---|---|---|
| Web | Next.js、React、TypeScript | 流式对话、产物渲染、组件生态和长期维护能力 |
| UI | Tailwind CSS、Radix UI、KaTeX/MathJax、Plotly | 克制样式、可访问性、数学公式和交互图支持 |
| API | Python、FastAPI、Pydantic | 与数学、AI、文档处理生态一致，接口契约清晰 |
| ORM 与迁移 | SQLAlchemy 2、db-v2 SQL 迁移、psycopg 3 | 明确模型边界、可靠迁移和 PostgreSQL 支持；不引入 Alembic 双真源 |
| 数据库 | PostgreSQL 16+、pgvector | 事务、JSONB、递归图查询、向量和全文能力统一 |
| 缓存与队列 | Redis、Celery | 限流、短期状态和可靠异步任务 |
| 文件 | S3 兼容对象存储，首期单节点 MinIO | 统一文件接口、版本和生命周期管理 |
| 智能体 | 持久化运行状态机 + 内部工具注册表 | 支持暂停、审批、恢复、审计，避免框架锁定 |
| 文档 | Docling、PaddleOCR-VL；MinerU 对照评测 | 文字层/扫描件分流，多语言数学 OCR，保留原页与坐标 |
| 检索模型 | BGE-M3、BGE-Reranker-v2-m3；Qwen3 对照评测 | 多语言向量与重排，模型修订和索引版本绑定 |
| 数学计算 | SymPy、NumPy、SciPy、Matplotlib、Plotly | 符号计算、数值计算和科研绘图 |
| 部署 | Docker Compose、nginx、HTTPS | 单服务器可运维，未来可拆分 |
| 观测 | OpenTelemetry、结构化日志、Prometheus/Grafana | 统一追踪 API、任务、模型和成本 |

图数据库、独立 BM25 引擎和 Kubernetes 只作为达到明确性能阈值后的扩展，不在首期部署。

### 6.2 代码结构

```text
AIForTeaching/
├─ apps/
│  ├─ web/                 # 对话与产物工作台
│  ├─ api/                 # FastAPI 入口、鉴权、路由
│  └─ worker/              # 解析、向量化、渲染、发布任务
├─ modules/
│  ├─ identity/            # 用户、角色、范围权限
│  ├─ course/              # 课程、教学班、课程结构
│  ├─ source/              # 文件、教材、页和原文块
│  ├─ knowledge/           # 概念、别名、语义关系
│  ├─ math/                # 数学约定、命题、符号和证明语义
│  ├─ exercise/            # 习题、答案、量规、来源
│  ├─ retrieval/           # 切片、向量、混合检索
│  ├─ assessment/          # 作业、提交、批改、成绩
│  ├─ learning/            # 作答、掌握度、复习
│  ├─ agent/               # 编排、工具、运行和产物
│  ├─ governance/          # 草稿、审核、发布、审计
│  ├─ evaluation/          # 金标准、标注、评测和指标
│  └─ integration/         # 模型、存储和外部服务
├─ packages/
│  ├─ contracts/           # OpenAPI、JSON Schema、Artifact 协议
│  ├─ ui/                  # 共享 UI 和产物查看器
│  └─ math/                # 公式规范化、数学校验
├─ db/
│  ├─ migrations/          # 只能追加的前向迁移
│  └─ fixtures/            # 测试数据
├─ evals/                  # 检索、问答、批改和智能体评测
├─ tests/
└─ infra/                  # Compose、nginx、备份和监控
```

模块只能通过应用服务接口协作，不允许其他模块绕过服务直接修改其表。数据库使用同一实例和不同 schema，不等于允许跨模块任意 SQL。governance 发布服务是跨模块发布事务的唯一编排者，各领域提供可在同一 Unit of Work 中运行的校验器。

### 6.3 开放标准与可迁移性

- 公式交换保存 LaTeX 与 MathML，PPT 使用可编辑 OMML，避免公式全部变成图片。
- 客观题支持 QTI 3 导入导出；证明、Lean 证据和课程图谱使用版本化扩展 manifest，不能为了迁就 QTI 丢失内部语义。
- 后续通过 LTI 1.3/Advantage 对接学校 LMS，采用其安全框架、 Deep Linking、名单与成绩服务；对接前单独做安全评审。
- 学习事件可映射到 Caliper 或 xAPI 导出，默认去标识并遵循最小化，不把完整私人对话变成分析事件。
- 每门课程可导出带 `schema_version`、release、内容哈希、来源和资产清单的开放 JSON + 文件包，保证可迁移和可重建。
- 开发、测试和生产统一使用 Linux Dev Container/WSL2 或等效 Linux 环境，避免 Windows 与 Linux 在路径、字体、Lean、OCR 和 shell 上产生差异。

### 6.4 模型网关、管理员密钥与用户切换

#### 管理员专属配置

模型供应商、Base URL、API Key、模型上下架、能力类型、数据出校级别、默认模型、角色/课程可见范围、用户额度和全院预算只在管理员控制台配置。API Key 采用写入后不可回读：管理界面只显示密钥是否存在、版本和轮换时间，不能显示原文或末位以外的内容。

密钥原文只进入服务端 Docker secret、Vault 或等效密钥存储，数据库 `model_secret_ref` 仅保存引用、版本和轮换元数据。浏览器、API 响应、日志、Trace、SSE、导出、错误信息和审计内容均不得包含密钥。连接测试由后端发起最小请求并脱敏，供应商地址仍需 allowlist、重定向和 SSRF 校验。

#### 用户自由切换的边界

学生和教师可在输入框附近选择「自动（推荐）」或管理员已上架且与当前任务兼容的 chat/vision 生成模型，选择保存在 `user_model_preference`；每次 `agent_run` 固定实际模型修订。切换只影响后续运行，不重写历史回答和回放上下文。

「自由切换」不等于任意 Base URL、任意 API Key 或任意未审核模型。后端根据用户、课程、任务能力、数据分类、教学策略和预算重新过滤可选目录，即使前端篡改 endpoint_id 也不能越权调用。受限教材/草稿只允许合规模型；考试或教师指定任务可锁定模型。用户可以看到模型是校内/云端、适用任务和速度/推理特点，但看不到供应商密钥。

Embedding、Rerank、OCR 模型不开放给用户切换，因为它们直接决定索引一致性、OCR 证据和检索质量，只能由管理员经评测和索引构建流程更换。模型停用立即阻止新任务，排队任务重新校验；历史 agent_run 仍保留原模型记录。

#### 模型治理表

- `model_provider`：供应商、协议类型、Base URL、状态和允许数据区域。
- `model_endpoint`：模型名称/revision、chat/vision/embedding/rerank/OCR 能力、上下文限制、价格、保留策略和上下架状态。
- `model_secret_ref`：密钥引用、版本、轮换时间和创建者，不保存密钥原文。
- `model_access_policy`：角色/课程/教学班可见范围、允许任务、数据级别、token/并发/费用额度和默认优先级。
- `agent.user_model_preference`：用户选择的 endpoint 或自动路由偏好，不保存任何凭据。
- `model_call`：实际模型修订、脱敏输入摘要、token、费用、延迟和结果状态，不记录密钥。

## 7. 数据库总体设计

### 7.1 核心原则

1. **稳定身份与内容版本分离。** 课程、概念、习题有永久 UUID；修改产生新版本，不覆盖已发布记录。
2. **业务真源关系化。** 标题、课程、页码、关系、权限和状态使用明确列与外键；JSONB 只保存有 JSON Schema 约束的可变结构，如题目 AST、量规和渲染配置。
3. **原始资料不可变。** 上传文件、原页和解析原文保留哈希；规范化文本和 AI 内容另存，不能覆盖原件。
4. **派生数据可重建。** 切片、向量、搜索 token、图谱投影均记录生成器版本和输入哈希。
5. **所有内容有来源。** 教材页、原文块、课程包、教师录入或模型 run 至少有一种明确来源；AI 来源不能伪装成教材原文。
6. **所有时间使用 `timestamptz`。** 内部主键使用应用生成的 UUIDv7，展示编号另设可读 code。
7. **删除以停用和撤回为主。** 原始文件与审计按保留策略处理，学生个人数据支持到期清理和删除申请。
8. **迁移只追加。** 已部署迁移不得修改；每次迁移包含结构、数据校验和回滚说明。

### 7.2 Schema 划分

| Schema | 职责 | 主要表 |
|---|---|---|
| `iam` | 身份、范围授权与审核资格 | `user_account`、`role`、`permission`、`role_permission`、`role_assignment`、`reviewer_qualification`、`auth_session` |
| `course` | 学院目录、课程、学习目标和教学实例 | `college_catalog`、`course`、`course_offering`、`offering_member`、`chapter`、`chapter_version`、`knowledge_node`、`knowledge_node_version`、`learning_outcome`、`learning_outcome_version`、`outcome_concept`、`outcome_assessment` |
| `source` | 原始文件、教材结构和可审核语义切片 | `asset`、`document`、`document_version`、`document_page`、`source_block`、`content_slice`、`content_slice_version`、`source_license` |
| `knowledge` | 跨课程概念和语义图谱 | `concept`、`concept_version`、`concept_alias`、`concept_alias_version`、`relation`、`relation_version`、`relation_evidence` |
| `math` | 数学语义、符号、约定和证明结构 | `convention_profile`、`convention_version`、`statement`、`statement_version`、`symbol_binding`、`proof`、`proof_version`、`proof_step`、`proof_step_dependency` |
| `exercise` | 题目、答案、解析、量规、数学判题和形式化验证 | `exercise`、`exercise_version`、`answer`、`answer_version`、`solution`、`solution_version`、`answer_test`、`answer_test_version`、`exercise_template`、`template_version`、`exercise_instance`、`exercise_concept`、`exercise_source`、`rubric`、`rubric_version`、`formal_verification` |
| `retrieval` | 可重建检索投影、向量和索引版本 | `chunk`、`chunk_source`、`chunk_embedding`、`index_version`、`query_log` |
| `assessment` | 教学过程和正式评价 | `assignment`、`assignment_item`、`submission`、`submission_item`、`grade` |
| `learning` | 学习证据和个性化 | `exercise_attempt`、`mastery_snapshot`、`review_item`、`study_plan` |
| `agent` | 对话、教学策略、证据、工具运行和产物 | `workspace`、`conversation`、`message`、`user_model_preference`、`policy_profile`、`policy_version`、`evidence_bundle`、`evidence_bundle_version`、`answer_claim`、`agent_run`、`run_step`、`tool_call`、`artifact`、`artifact_version` |
| `governance` | 建设、审核、发布、质量与审计 | `change_set`、`change_set_contribution`、`review_task`、`review_decision`、`review_decision_item`、`dependency_decision_manifest`、`publishable_type`、`release`、`release_item`、`release_context`、`quality_check`、`quality_finding`、`audit_event` |
| `evaluation` | 金标准、标注、仲裁、评测运行和指标 | `dataset`、`dataset_version`、`dataset_access_grant`、`eval_case`、`annotation`、`adjudication`、`eval_run`、`case_result`、`metric_result` |
| `integration` | 模型、密钥引用、外部系统和迁移映射 | `model_provider`、`model_endpoint`、`model_secret_ref`、`model_access_policy`、`embedding_model_version`、`model_call`、`external_identity`、`tool_config`、`legacy_ref` |
| `ops` | 可靠异步执行 | `job`、`job_attempt`、`outbox_event`、`idempotency_key` |

核心关系均使用严格外键。`release_item` 采用受控 `entity_type + entity_version_id`，`audit_event` 采用 `entity_type + entity_id`；它们只用于编排和审计，不替代核心业务外键。`governance.publishable_type` 注册每种可发布对象的版本表、canonicalizer、审核规则和运行时解析器；release 清单通过严格触发器或分类型关联表校验类型与版本，禁止悬空 ID，并禁止硬删除被 release 引用的版本。

Release 必须封闭所有影响公开语义的对象：公开教材 document_version、课程章节、知识节点、学习目标、概念、别名、关系及证据、数学约定、statement_version、symbol_binding、proof_version、proof_step、proof_step_dependency、exercise_version、answer_version、solution_version、answer_test_version、rubric_version、题目—概念关联、template_version、已审题目实例、知识正文和公开内容切片。`rubric_version`、`exercise_concept`、`relation_evidence` 等不可变子对象必须随父聚合固定并计入依赖哈希，运行时禁止读取它们的「最新版本」。任何新增公开类型未注册并进入 release manifest 时，发布必须失败。

### 7.3 通用字段约定

稳定身份表：

```text
id                UUID PRIMARY KEY
code              可读业务编码，按范围唯一
created_at        TIMESTAMPTZ NOT NULL
created_by        UUID NULL
archived_at       TIMESTAMPTZ NULL
```

版本表：

```text
id                UUID PRIMARY KEY
entity_id         UUID NOT NULL REFERENCES 稳定身份表
version_no        INTEGER NOT NULL
base_version_id   UUID NULL
change_set_id     UUID NOT NULL
content_hash      TEXT NOT NULL
schema_version    INTEGER NOT NULL
created_at        TIMESTAMPTZ NOT NULL
created_by        UUID NULL
UNIQUE(entity_id, version_no)
```

版本更新使用 `base_version_id` 做乐观并发控制；它必须有指向同一稳定实体的外键与同实体验证。审核期间出现新基线时必须重新生成 diff，不能静默覆盖他人修改。已发布版本通过数据库权限和触发器禁止 UPDATE/DELETE。

`content_hash` 使用 SHA-256 和版本化 canonical serialization，明确 JSON 键排序、Unicode/空白、LaTeX AST 和附件字节的处理。哈希覆盖所有影响展示、答案、评分、权限和机器解释的字段；跨表依赖以「依赖类型 + 版本 ID + 哈希」清单计入。关系端点或关系类型变化会改变边的身份，应创建新 `relation`，而不是修改原边版本。纯内部运营元数据不计入展示哈希，但必须单独审计。

每个 publishable 版本另有 `dependency_decision_manifest`，逐项记录 `(dependency_type, dependency_version_id, content_hash, decision_kind, decision_seq)` 并建立反向依赖索引。发布和运行时可见性都要求每个依赖的最新终态决策仍通过、序号匹配且未隔离。依赖被撤销时，其依赖对象自动进入隔离/待复审集合；例如撤销节点会隐藏依赖它的边，撤销边不会反向隐藏节点，撤销学院概念会使采用它的课程/班级上下文失效。

「人工已审核」不是一个可由前端随意切换的布尔字段。`review_decision` 是追加写的审核批次，`review_decision_item` 逐项固定 `decision_kind`、实体版本、依赖哈希、审核人、时间、授权范围和终态决策，并以单调递增 `decision_seq` 消除同时间戳歧义。对同一对象和决策类型以序号最新的终态 `approved/rejected/revoked` 决策为准；后续退回或撤销会使旧通过失效，不能仅凭历史上「存在 approved」放行。`knowledge_approval` 与 `teaching_confirmation` 分开取最新决策，不能互相满足。

签署人必须是具有有效授权的人类用户，模型、worker 和服务账号不能签署人工审核或教师确认。公共知识的创建者和任何修改过 `content_hash` 字段的人均不能审核该版本，数据库/服务层校验 `reviewer_user_id != version.created_by` 且无内容贡献记录。班级 `teaching_confirmation` 允许任课教师确认本人材料，但只能用于其授权教学班。每个 publishable 版本可保存由同一事务维护的有效审核缓存引用，但它不是第二真源，发布仍须逐项复核决策明细。缺少匹配的决策时，发布预检直接失败，管理员也不能绕过。

### 7.4 教材与原文模型

- `source.asset`：存储任何二进制文件。关键字段包括 `storage_uri`、`sha256`、`mime_type`、`byte_size`、`visibility`、`encryption_status`。文件按内容哈希去重。
- `source.document`：教材的逻辑身份，如某本《数学分析》。标题变化不新建教材身份。
- `source.document_version`：版次、ISBN、作者、出版社、原始 asset、解析器版本、书页与 PDF 页偏移。
- `source.document_page`：`pdf_page_no`、`printed_page_no`、页面尺寸、文字层/OCR 状态、页面图像 asset。
- `source.source_block`：按阅读顺序排列的标题、段落、定义、定理、证明、公式、图、表和例题。保存原始文本、规范文本、LaTeX、页面坐标、解析置信度和父块；另存 BCP 47 `language_code`、`script`、`text_direction`、OCR 引擎/版本和语言置信度。数学公式可标记为 `zxx`（无自然语言内容），不能按页面语言强行覆盖块语言。
- `source.source_license`：授权范围、允许用途、是否允许云端处理、生效/到期时间、撤回状态，以及原页 `crop/full_page/disabled` 展示模式。默认只允许显示与引用切片对应的裁剪区域，不允许通过逐页打开拼出全书。

一个块可以跨页，一个页面也有多个块。跨页定理通过块关系连接，不能简单按页截断。教材原页公开访问仅对来源、版权和页码校准已审核的 `document_version` 开放；上传成功本身不等于允许师生查看。每次原页、切片、检索和导出请求都在运行时检查许可有效期、用途和撤回状态，到期或撤回立即进入隔离，不等待下一次发布。默认由服务端生成引用区域的短时裁剪图，只有许可明确允许 `full_page` 时才开放整页。

### 7.5 知识点与知识图谱

采用「全局概念 + 课程节点 + 语义关系 + 证据」四层结构：

1. **全局概念 `knowledge.concept`**：如「一致连续」，可跨课程复用，有稳定 UUID。
2. **概念版本 `concept_version`**：规范名称、定义、数学表述、符号、摘要和概念类型。
3. **课程节点 `course.knowledge_node_version`**：概念在某课程中的章节位置、教学顺序、课程化讲解、学时和教学要求。全局定义与教师课程口径不混在一列。
4. **概念别名 `concept_alias` / `concept_alias_version`**：中文、英文、历史叫法、符号和常见误写，用于精确检索，不重复创建概念；别名变更同样进入版本与审核。
5. **语义关系 `relation` / `relation_version`**：`prerequisite`、`implies`、`equivalent`、`generalizes`、`contrasts`、`example_of`、`misconception_of` 等。`relation` 保存稳定身份，`relation_version` 保存有向端点、关系类型、适用范围和版本内容；关系类型存受控字典，不使用难以迁移的数据库 enum。
6. **关系证据 `relation_evidence`**：连接原文块、题目或教师说明，保存摘录和定位信息。

跨课程共享不意味着一门课修改后自动影响其他课程。概念、关系和 change set 明确标记学院或课程 `owner_scope`；课程建设者不能编辑学院共享概念来变相修改其他课程。学院公共目录有独立发布流程，课程 release 固定「学院概念版本 + 本课程节点版本 + 本课程采用审核」。关系两端、证据和关联对象必须处于授权范围并形成 release 闭包；跨课程关系需要学院级授权，不能指向另一课程的草稿。

课程章节树与语义图分开存储。`parent_id + sort_order` 表达课程结构，`relation_version` 表达学术关系；「共同出现在一道题」只能作为学习证据，不能冒充先修或推导关系。

图谱审核精确到节点版本和边版本。一个节点已审核不会使其相邻边自动通过；一条边已审核也不会使两个节点自动通过。师生图谱只投影当前 release 中人工审核通过的节点和边，未审核边在后台显示为草稿，在师生端完全不存在。

PostgreSQL 递归 CTE 支持先修链、影响范围和学习路径查询；物化视图只作为读取投影。只有图分析需求经基准测试证明 PostgreSQL 不足时，才增加 Neo4j 同步投影。

### 7.6 内容切片与向量索引模型

可审核语义内容与可重建检索投影必须分开：

- `source.content_slice / content_slice_version`：稳定语义切片及其版本，保存 `content_role`（source_original / reviewed_translation / editorial_explanation）、`origin_kind`（human / OCR / AI）、`origin_run_id`、`display_text`、规范化正文 `normalized_content`、LaTeX、来源页与原文块依赖。原文与 AI/教师解释必须是不同切片版本，不能在同一记录中打包后冒充教材原文；source_quote 只能引用 source_original，翻译引用必须标记 reviewed_translation。另存 `language_code`、`translation_of_slice_version_id`、`translation_kind` 和 `parent_slice_version_id`，父版本进入依赖清单。`retrieval_text` 不在此处保存。
- `retrieval.chunk`：某个 `index_version` 的检索投影，保存 token 数、chunker/tokenizer 版本和输入切片哈希，不是独立人工审核对象。
- `retrieval.chunk_source`：连接检索 chunk 与一个或多个 `content_slice_version` 及原文块；每个公开 chunk 都能回到已审核切片和原文。
- `integration.embedding_model_version`：不可变模型修订，记录提供商、模型名/版本、维度、预处理、归一化和距离函数，不能只引用可修改的 endpoint。
- `retrieval.chunk_embedding`：绑定 `index_version_id + chunk_id + embedding_model_version_id + content_hash`，可追溯到一次具体构建。
- `retrieval.index_version`：多个索引版本可属于同一 release，记录 pgvector 版本、HNSW 参数、模型修订、构建清单、输入数量和状态。`release.active_index_version_id` 只控制新解析的当前浏览；历史 release_context 固定具体 index_version，不再维护独立的「当前索引别名」真源。
- `retrieval.query_log`：记录检索参数、候选和引用结果，默认保存脱敏文本并执行保留期限。

公开教材解析批次和内容切片需要有效人工审核。审核可以按章节或批次完成，但 `review_decision_item` 必须逐项记录覆盖对象和哈希。Embedding、检索 chunk、搜索 token 等无语义人工改写的投影不重复人工审核；只有已审核且属于 release 的内容切片才能进入师生索引。chunker 或 Embedding 升级只重建索引，不无故制造新的内容审核义务。

向量列不绑定一个永久的全局维度。每个索引版本只包含同一模型、同一维度的向量；不同 release 和模型使用独立物理分区与 HNSW 索引，确保权限过滤发生在候选扫描范围内，避免先取 Top-K 再过滤导致泄露或召回不足。旧索引保留到回滚窗口结束，垃圾回收必须确认无 release、历史作业或回放引用。

### 7.7 习题模型

- `exercise.exercise`：题目稳定身份。
- `exercise.exercise_version`：只保存题型、题干 AST、规范文本、子问结构、难度标称、预计时长、生成来源和内容哈希，不另存答案或解析副本。
- `exercise.answer / answer_version`：结构化标准答案及其独立版本、来源、审核和内容哈希。
- `exercise.solution / solution_version`：结构化解析、证明或步骤讲解及其独立版本、来源、审核和内容哈希；保存 `method_label` 与 `solution_role`，支持多解法、示例和分级提示。
- `exercise.exercise_concept`：题目与概念的关系及角色，如考查、先修、补救；保存权重。
- `exercise.exercise_source`：教材版本、书页、PDF 页、题号、原块或外部来源；教材印刷答案/解析只作为 source content_slice 证据引用，不能成为第二套结构化答案真源。
- `exercise.rubric_version`：步骤分、关键条件、可接受解法、常见错误和扣分规则。
- `exercise.answer_test / answer_test_version`：借鉴 STACK 的「先输入校验、后数学评价」。静态题显式绑定 `answer_version_id`，参数化模板显式绑定 `template_version_id` 与 parameter_schema，二者互斥；测试表达式引用权威答案，不另建第二答案真源。保存测试类型、变量域/假设、convention_version、数值容差、CAS 引擎与版本和反馈。发布时必须用固定 CAS/约定验证权威答案通过自身测试，rubric 的可接受解法只能引用同 release 的 answer/solution 版本，矛盾即阻断。
- `exercise_template / template_version / exercise_instance`：保存参数 schema、约束、模板、随机种子、具体参数和确定性验证清单。exercise_instance 是带 content_hash 的**不可变行**，其 id 在 publishable_type 中作为不可变版本标识使用；模板版本、seed、参数、exercise_version 和验证清单一旦写入不得修改，变化必须生成新实例。模板审核不等于实例公开；每个具体实例仍需匹配的人工决策。assignment_item、submission_item 和 exercise_attempt 均固定 exercise_instance_id、template_version_id 与 exercise_version_id，学生已收到的实例不受后续编辑或重新随机影响。参数化功能后置到教学增量阶段，首期每模板每 release 最多预生成 50 个实例，班级作业只从该已审实例库抽取，超出上限不得发布。
- `exercise.formal_verification`：绑定具体 `exercise_version_id`、目标声明名、完整 `release_dependency_hash` 和 `dependency_decision_manifest_hash`；参数化题还必须绑定不可变 `exercise_instance_id` 和实例化形式命题，不能只验证模板就宣称所有随机实例通过。前者覆盖 exercise_version、answer_version、solution_version、answer_test_version、rubric_version、template_version、具体实例、题目—概念关联、形式命题及相关资产，后者固定自然语言/形式化对应关系等依赖决策的 decision_seq。保存 `not_started/running/environment_failed/timeout/check_failed/passed` 状态、Lean 与 mathlib 工具链、依赖 revision、编译选项、形式化命题及完整源文件哈希、证明 artifact、容器镜像 digest、验证 job、公理依赖、日志、受信 attestation 和验证时间。显示和发布时重算内容依赖与决策 manifest，只有完全匹配的 `passed` attestation 才能显示「Lean 已验证」。
- `learning.exercise_attempt`：学生每次作答不可变记录，不能覆盖历史。
- `assessment.grade`：AI 建议分与教师正式分分开，只有教师确认分能进入正式成绩。

题干和答案采用分题型 JSON Schema。选择题、数值题、表达式题、证明题和代码题不共享一个含糊的 `answer` 字符串。Release 中的习题聚合固定 exercise_version、answer_version、solution_version、answer_test_version、rubric_version、具体实例（若适用）和关联对象，每一项可独立 diff 与审核，缺少任一必需项的有效决策都不得发布。结构化答案/解析与教材证据通过 exercise_source 和依赖 manifest 关联，任一侧变化都触发相应复核。

表达式等价可调用 SymPy；证明题只能提供辅助评价，不能仅凭模型相似度给正式成绩。

AI 生成的变式题保存 `derived_from` 和生成 run，不继承原题的人工审核状态。

Lean runner 在干净、离线、锁定依赖的沙箱中从源码重建目标声明，不能以项目整体构建退出码代替目标验证。必须拒绝 `sorry/admit` 和策略外公理，检查目标声明的公理依赖；`native_decide` 等额外信任基础需要课程策略显式允许并展示。失败或超时不表示命题为假，只表示本次未获得有效机器证明。标签旁固定说明「只验证指定形式命题与证明，不等于自然语言原题、所有解法或评分均已验证」。形式化命题与自然语言题意的对应关系仍需人工确认。

Lean 文件及其 elaboration 过程同样按不可信代码处理。Lean 进程无网络、无数据库凭据、无共享可写报告路径，只能写隔离临时目录。沙箱外的受信 wrapper 对输入重新计算哈希，核对目标声明、公理依赖和构建结果，并生成包含 runner 镜像 digest、输入哈希和 job ID 的签名 attestation。wrapper 通过受限存储过程写入验证结果，Lean 进程和普通 worker 不能自行插入 `passed`；展示端校验签名、release_dependency_hash 和 dependency_decision_manifest_hash。

Lean runner 未通过独立验收前，所有习题只能显示「未做 Lean 验证」，禁止写入或迁移生成「Lean 已验证」标签。

### 7.8 数学语义、约定与证明结构

数学知识不能只存自然语言段落和 LaTeX 展示字符串。新增 `math` schema，在不替换教材原文的前提下逐步建立可计算的语义层：

- `convention_profile / convention_version`：记录教材或课程约定，例如自然数是否含 0、log 默认底数、「递增」是否严格、测度与拓扑术语约定。课程 release 固定采用版本，避免把不同教材约定误判为矛盾。
- `statement / statement_version`：定义、公理、定理、引理等结构化语义，显式关联 `content_slice_version_id` 原文证据、主 concept 和 convention_version（raw 可为空）。核心字段是**单一自包含的 scoped `formula_ast`**，binder 节点直接包住其作用公式，可区分 `∀x(P(x)→Q(x))` 与 `(∀xP(x))→Q(x)`；定义使用 `definition` 节点表示被定义符号与定义公式，不再用分离的 quantifier/assumption/conclusion 字段拼接语义。另存 canonical LaTeX 与可选 Content MathML/OpenMath。
- `symbol_binding`：是 statement_version 的**不可变版本子对象**，通过 `binder_node_id` 指向 formula_ast 中的作用域节点，保存符号、自由/约束角色、类型/定义域节点和来源位置。子对象 `content_hash` 计入父 statement_version 哈希和依赖清单，批准后修改必须产生新 statement_version。
- `proof / proof_version`：显式保存 `statement_version_id` 与证明 `content_slice_version_id`，固定被证明命题版本，禁止运行时读取命题最新版本。
- `proof_step / proof_step_dependency`：是 proof_version 的不可变版本子对象，分别保存论断/理由与步骤/命题依赖及各自 content_hash。步骤依赖必须为 DAG，禁止循环；被引用 statement_version 必须在同一 release 中且审核有效，步骤、依赖和命题闭包全部计入 proof_version 哈希与 release manifest。

语义完整度分为 `raw → structured → formal`。首期只对试点章节的定义、定理条件、结论和关键符号做 structured 标注，不要求把整本教材形式化；未形式化不阻碍原文学习，但不能宣称已完成 Lean 或机器证明。所有公开语义对象及其不可变子对象都在 publishable_type 注册，进入 release 和人工审核，解析置信度与来源位置必须保留。完整 proof_step/proof_step_dependency 实现后置到数学增量阶段，不进入阶段 1 垂直切片。

### 7.9 评测数据集与结果数据库

评测不能只是仓库里几个临时 JSON 文件。`evaluation` schema 将金标准作为受控数据资产：

- `dataset / dataset_version`：每个版本只包含一个 `development` 或 `hidden_test` split，记录用途、语言、题型、冻结时间、案例清单哈希和访问策略；eval_case 继承该 split，不允许同版本混合。
- `dataset_access_grant`：按 dataset_version、用户/角色、权限级别、批准人和有效期授权；eval_case、annotation、adjudication 均执行 RLS，冻结后 append-only。eval_run、case_result、metric_result 通过 eval_run.dataset_version_id 继承同一 grant 与 RLS，隐藏结果仅授权评测者可见。
- `eval_case`：保存问题、标准答案、金标准证据、难度、语言、数学约定和对抗类型，如相似定理、符号冲突、错误 OCR、无依据问题。
- `annotation / adjudication`：保存教师独立标注、资格快照、分歧和仲裁结果，可计算一致性，不能由模型替代金标准。
- `eval_run`：固定 dataset_version、release_context（含索引）、模型修订、prompt、agent policy 和评测配置，支持复现，不另建一套索引上下文。
- `case_result / metric_result`：保存逐题检索/回答/工具证据、错误类别，以及分子、分母、指标值、单位、置信区间和算法版本。

隐藏测试集有独立权限，不能进入训练、prompt 示例、检索语料或教师日常页面。隐藏评测请求使用专用 `purpose=evaluation_hidden`，禁止写入普通 query_log 的文本与候选内容，只在 evaluation schema 保存受权限控制的哈希和结果。真实学生作答进入评测前必须去标识并获得合法授权。通用 LLM 评分只能辅助，数学正确性、证明逻辑和证据支持仍以确定性检查与教师盲评/仲裁为准。

### 7.10 学习、对话与产物

- 掌握度是由作答证据计算的快照，不是教师或模型随意填写的标签。保存模型版本、输入证据、计算时间、最低证据量和置信度；初期采用可解释规则与先修图，不在数据不足时引入黑箱深度知识追踪。周期性再测用于检验保持情况，推荐显示理由并允许教师纠正，低置信度不得做高影响分流。
- 对话消息与工具调用分开；一条消息可触发多个工具，工具结果不伪装成用户消息。
- `agent.agent_run` 保存目标、状态、模型策略、预算、当前步骤和恢复点。
- `agent.tool_call` 保存工具版本、脱敏输入摘要、权限决策、结果摘要、耗时和错误。
- `agent.artifact_version` 保存产物类型、schema、文件 asset、源 run、父版本和渲染器版本。
- 图形的代码、输入数据、依赖锁文件和运行日志均关联产物，确保可复现。

### 7.11 后续扩展规则

新增能力按以下顺序判断：

1. 能否作为新工具和新 Artifact 类型加入，而不改核心表。
2. 是否需要独立领域模块和 schema，而不是向 `agent` 或 `course` 塞字段。
3. 是否产生公共知识；若是，必须接入 change set、审核和 release。
4. 是否产生个人数据；若是，必须定义所有者、保留期限和删除策略。
5. 是否调用外部服务；若是，必须接入模型/工具网关、预算和审计。

禁止为每个新功能增加一张自由 JSON 配置表，也禁止在现有表添加无所有者的通用 `extra_data` 作为长期方案。

## 8. 教材到知识库的生产流水线

### 8.1 流水线

```text
上传与版权检查
→ 文件哈希、病毒扫描、文字层检测
→ 文字解析或数学 OCR
→ 页码校准、阅读顺序、公式和图表识别
→ 定义/定理/证明/例题可审核语义切片（content_slice_version）
→ 知识点、别名、关系和习题候选抽取
→ 去重、来源校验、数学结构校验
→ 生成草稿与质量报告
→ 人工对照、修改、退回或审核
→ 构建新 release 和独立索引
→ 全量发布校验
→ 原子切换正式版本
```

### 8.2 数学与多语言切片规范

#### 8.2.1 先解析结构，再切语义，最后生成检索投影

切片分三层，不能直接从 PDF 按字符数切完就写向量：

1. **原文块 source_block**：保留页面、坐标、阅读顺序、原始文字、公式、图片和 OCR 证据，不可变。
2. **可审核语义切片 content_slice_version**：由确定阅读顺序的原文块组成，承载定义、定理、证明、例题和翻译等正式内容。
3. **检索 chunk**：按某个 index_version 对语义切片加检索上下文、分词和向量，可随时重建，不改变原文或审核状态。

文字版 PDF 以 Docling 的版面、阅读顺序和结构结果为基线，并用轻量文字层抽取交叉检查；扫描页以本地 PaddleOCR-VL 为 OCR 基线。文字层乱码、字体映射异常、覆盖率低或阅读顺序校验失败时自动转 OCR，不能把复制出来的乱码当作可检索正文。MinerU 等解析器可参加同一金标准评测，但不能因 vendor 宣称支持某语言就直接上线。所有 OCR 结果同时保留原页裁剪和置信度，未审核不得成为正式知识。

#### 8.2.2 语义切片类型与尺寸

| 内容 | 切片规则 | 初始检索尺寸 |
|---|---|---|
| 章节/节 | 形成父切片，保存标题、路径和摘要，不与子段混成一块 | 仅作上下文和父级检索 |
| 定义 | 编号、名称、全部条件、符号约定和必要脚注作为原子单元 | 通常 100—800 tokens，不为凑长度切断 |
| 定理/引理/推论 | 编号、前提、结论和引用完整保留；短证明可随定理 | 通常 200—1,200 tokens，超长原子命题单列并预警 |
| 长证明 | 与父定理链接，按证明步骤切分；每个子块带定理编号和必要上下文 | 400—900 tokens，上下文约 50—150 tokens |
| 普通叙述 | 在段落/句子边界切分，保留节标题路径 | 目标 300—800 tokens，重叠 10%—15% |
| 例题 | 题干、全部公共条件和设问为父块；子问可继承公共题干 | 不跨题拼接，不重复复制公共条件造成假新题 |
| 习题与解答 | 教材印刷题干/答案/解析可作为 source content_slice 证据；平台权威题干、答案、解析分别只存 exercise_version、answer_version、solution_version | 各项独立审核，release 固定聚合，不保存第二份答案副本 |
| 公式 | 保留原始 LaTeX/OCR、规范化 AST、原图和相邻语义说明 | 公式不单独脱离所属定义/定理 |
| 图/表 | 保留图像、标题、表头、注释和正文引用；长表按行组并重复表头 | 图像向量不是首期文本向量替代品 |
| 参考文献/脚注 | 保留与正文的链接，不把整页参考文献混进定义 | 独立类型，按需召回 |

Token 数必须按当前 Embedding 模型的 tokenizer 计算，不能用中文字符数代替。尺寸是初始参数，不是高于数学完整性的硬切割线；超过 1,200 tokens 的完整定理可保留原子单元并走长上下文路线。chunker 或 Embedding 变化只生成新索引版本，不覆盖旧索引。

#### 8.2.3 多语言与跨语言规则

- 按 source_block 识别语言，而不是整本 PDF 只存一个语言字段。支持中英混排、希腊符号、脚注转语言和公式块；低置信度由人工指定。
- 保留 BCP 47 语言、文字系统、阅读方向和 OCR 引擎版本，特别处理阿拉伯语/希伯来语 RTL、中文竖排、多栏、上下标、天城文及西里尔字母。
- 原始 Unicode 永存；检索副本只做受控 NFC 和空白规范化，不用可能改变数学语义的激进 NFKC、全半角或上下标替换。
- `original` 与 `translation` 是不同 content_slice_version，通过 `translation_of_slice_version_id` 关联，并把该原文版本列入翻译的 dependency_decision_manifest 硬依赖。原文出新版本后，旧翻译在新 release 中标记为 `stale_for_new_source`，必须重新翻译/复审；历史 release 可保留原文与当时翻译的固定配对，原文撤销也会使翻译失效。
- 翻译或 OCR 审核人除课程授权外，还必须具有经课程负责人确认的源语言、目标语言（若适用）和数学领域资格。`iam.reviewer_qualification` 保存语言、领域、有效期和批准人，review_decision_item 保存签署时资格快照；不懂对应语言的审核员不能形式通过。
- 概念和别名支持多语言名称与符号，跨语言概念复用同一 concept 身份；课程节点可以保留不同语言教学口径。
- 默认用一个多语言 Embedding 空间检索所有已授权语言，不随语种增加随意拆库。查询可按语言过滤，也可跨语言召回。
- 查询翻译只作为低召回时的辅助检索通道，标记为机器生成、不得冒充原文证据。回答优先引用原文和已审核翻译。
- 每种新语言上线前执行独立语言包验收，不能由「模型支持 100+ 语言」推导「本平台的该语言数学教材已达标」。

#### 8.2.4 展示文本与向量输入分离

`display_text` 只保存可公开原文/审核翻译，不附加检索指令。`retrieval_text` 由确定性模板生成：

```text
课程 / 教材版次 / 章节路径 / 内容类型 / 编号
语言与别名（若有）
正文、公式规范化文本和必要父级上下文
```

不得让模型随意改写原文后再嵌入。AI 生成的摘要、解释和翻译若用于正式检索，必须先审核并作为独立内容来源记录。检索优先命中子切片，展示时按需要扩展父定理、完整条件或相邻证明步骤，避免只返回断句。

`retrieval_text` 只存在于 retrieval chunk，记录 `retrieval_template_version` 和输入切片哈希。检索模板、课程/章节展示路径、tokenizer、Embedding 模型及其参数不进入 content_slice 的 `content_hash`，因此这些投影变化只重建索引，不触发内容重新审核；规范化正文或正式解释本身变化才产生新内容版本。

### 8.3 自动质量门禁

发布前至少检查：

- release 中每个知识点、图谱节点、图谱关系、习题、答案、量规、知识正文、关联关系和公开内容切片，其最新终态人工决策均与 release_kind 匹配并通过，且当前版本、依赖哈希及 dependency_decision_manifest 全部有效；未审核、已撤销或失效项必须为 0。
- 标为「Lean 已验证」的习题必须有当前版本的 Lean `passed` 证据，命题哈希、证明 artifact、Lean 环境和验证 job 均可复查；任何题干、答案或形式化命题变化都会使旧验证记录不适用于新版本。
- 同一 release 中的原文与翻译必须指向同一 translation_of_slice_version_id 配对，不得把新原文与旧翻译组合；翻译/OCR 决策的语言与数学资格快照必须有效。
- content_role 与 origin_kind 必须清晰，AI/教师解释不能作为 source_original 或教材直接引用；父子切片必须固定 parent_slice_version_id 且依赖有效，批准后修改 symbol_binding/proof_step 等子对象会因父哈希变化而发布失败。
- 所有正式 chunk 有合法来源和页码；无来源内容不得标为教材原文。
- 概念和关系没有悬空外键、循环章节树和非法先修环。
- 习题题干、答案、量规和知识点关联符合题型 schema；权威答案必须通过其 answer_test，rubric 引用的答案/解析版本必须在同一 release，参数化实例和 Lean 标签必须一一对应。
- statement 的 formula_ast 通过作用域结构校验，symbol_binding/proof_step 等子对象哈希计入父版本；proof_step_dependency 无环且所有引用命题在 release 闭包内。
- 同一课程内按来源、题号和内容哈希识别重复题。
- 公式可解析，LaTeX 和渲染结果经过抽样校验；关键公式执行「原图 → OCR/LaTeX → 解析 AST → 再渲染」回环检查。双引擎结果不一致时进入人工队列，不能用多数投票自动决定数学符号。
- 新 release 的节点、题目、关系和 chunk 数量相对上一版没有无法解释的异常波动。
- 草稿与正式索引隔离测试通过。
- 所有 AI 生成项具有模型、prompt、run 和生成时间记录。

「一键发布」只是审核完成后的最后一个动作，不代表跳过这些检查。

## 9. 草稿、审核、发布与纠错

### 9.1 三个独立状态机

```text
change_set:  draft → in_review → changes_requested → draft
                  └→ review_completed

review_task: assigned → reviewing → approved / rejected / revoked

release:     building → ready → publishing → published
                 └→ failed       published → withdrawn
```

`review_decision_item` 保存终态审核事实，不与 `review_task` 的工作分配状态混用。Release 的内容 manifest 在创建后不可变，变化的是其构建、发布或撤回状态；撤回原因和状态变化另存事件。

- `change_set` 聚合一次建设或修改，记录 `owner_scope`、基线 release、目标、创建者、贡献者和来源。
- `review_task` 分配公共知识审核范围；`review_decision + review_decision_item` 保存逐项终态、决策类型、被审核版本、依赖哈希和授权快照，是所有人工标签的真源。班级任课教师可直接签署本班 `teaching_confirmation`，无需伪装成公共知识 review_task。
- `release` 是不可变内容快照，`release_kind` 明确学院、课程公共或班级教学类型；`release_item` 指向各领域具体版本及其闭包子对象，发布预检要求每一项具有与该类型匹配的最新终态人工决策。
- `college_catalog.current_release_id`、`course.current_release_id`、`course_offering.current_teaching_release_id` 分别指向学院公共目录、课程公共知识和班级教学材料的当前 release，并通过范围约束防止错绑。`agent_run`、assignment、submission 和可重放 `artifact_version` 均保存创建时的 `release_context_id` 与具体内容版本，避免新发布或索引重建改变历史教学实例。
- 当前课程浏览与本人历史/进行中教学实例使用不同读取路径。历史路径只能读取固定旧版本，严重撤回可以覆盖历史访问并关闭或替换受影响作业。

### 9.2 原子发布与一致读取

1. 锁定待发布 change set，固定 manifest、依赖哈希和 `expected_base_release_id`。
2. 在事务外构建完整 release、检索投影和新向量索引，并运行权限、检索和关键问答冒烟测试。
3. 切换时由 governance 发布应用服务开启唯一 Unit of Work，按 release 范围获取事务级 advisory lock；各领域校验器在同一连接中执行，不各自开事务。所有会改变最新终态审核/教师确认、撤销、紧急下线和 release 状态的写路径必须获取其 owner_scope 对应的同一把范围锁，否则不得提交。发布含学院/课程基线的 release 时，按「学院 → 课程 → 班级」固定顺序同时锁定自身与基线范围，避免跨范围死锁和终检竞态。
4. 在切换事务内重新检查 `expected_base_release_id`、完整 base 链、manifest 闭包、与 release_kind 匹配的最新终态决策、依赖决策 manifest、撤回状态、Lean 证据和必要许可，防止预检后发生并发变化；数据库触发器复核 release_item 与基线，不能只依赖应用预检。
5. 校验通过后只更新该 release_kind 对应范围的当前指针：学院目录、课程公共知识或班级 teaching release，禁止跨范围错绑。新 release 的 `active_index_version_id` 在构建期绑定并在切换事务中核验，历史 release_context 不变；不维护全局当前索引别名。外部模型调用不得放在该事务内。
6. 每个请求在开始时于同一数据库快照沿 base 链解析并持久化 release_context，后续内容、权限和检索只使用该不可变上下文；不得分别读取「当前 release」和「当前索引」造成撕裂。
7. 发布失败保留旧 release 和旧索引。回滚是一个引用旧 manifest 的新发布事件，不能篡改历史 release；对应索引在回滚窗口内不得删除。
8. 记录发布人、审核人、输入哈希、模型、锁、预检与终检报告，并测试双发布者、审核撤回与发布并发、发布期间持续读取等竞态。

### 9.3 已上线内容纠错

- 普通纠错创建新 change set，旧 release 在审核期间继续服务。
- 严重错误可由课程负责人执行紧急撤回，并填写原因；撤回不删除历史。
- 审核/教师确认撤销立即生效，不等待下一次发布。撤销决策、反向依赖展开和公开隔离集合在同一范围锁、同一数据库事务中写入，公开视图和检索受控函数同时排除隔离项及其失效依赖对象，确保旧索引残留也不能返回给模型或师生，再异步重建索引。
- 单项撤销影响进行中作业时，系统立即冻结该题、通知任课教师，并由教师选择换题或取消；学生已有作答保留为审计证据，但被撤销内容不再向学生展示。紧急撤回可关闭整份作业。并发测试覆盖撤销在发布终检前后提交及下线与发布并发。
- 新概念、改题、改关系会产生未审核的新版本，不能沿用原题、原节点或原边的审核标记。题干、答案、量规或形式化命题变化也会使旧 Lean 标签不适用于新版本，必须重新验证；旧正式版本及其历史标签保持不可变。
- 教师可以直接描述修改意图，但数据库变更仍通过结构化编辑器和 diff 完成。

### 9.4 审核工时与防橡皮图章

第 0 阶段必须用真实教材做审核工时试验，按知识点、关系、习题、证明、切片分别记录条目数、P50/P95 审核分钟、退回率和每周可用工时。排期使用「总条目 × 实测耗时 × 返工系数 ÷ 每周有效审核产能」，审核未完成不得以工程进度代替上线条件。

批量操作只适用于同一确定性转换、同一低风险来源且能展示完整逐项 diff 的对象；禁止跨类型全选通过。公共知识审核员只要修改任何进入 `content_hash` 的字段，就记为内容贡献者并触发另一名审核员复审，不存在可自行认定的「非实质语义修改」。评论、审核意见和不发布的协作标注走独立通道，不进入发布哈希。定义、定理条件、答案、rubric 和形式化对应关系保留逐项确认。相同内容哈希的重复对象引用同一已审核版本，不重复制造内容；检索投影重建不重复审核。

系统记录审核时长、修改和退回行为，对短时间异常大批量通过告警并安排质量抽审。抽审用于评估审核质量，不能替代用户要求的全项人工审核。

### 9.5 内容健康、矛盾与课程覆盖

`governance.quality_check / quality_finding` 持久化自动检查结果，记录对象、证据、严重级别、负责人、状态和解决的 change_set。检查包括重复概念、相似但条件不同的定理、悬空切片、未关联习题、失效翻译、符号冲突、教材约定冲突和异常发布差异。

相似或矛盾不能由模型自动合并：不同教材约定可以并存，通过 convention_version 和课程作用域解释；真正的内容错误进入审核队列。高严重级别发现阻止发布，不能通过标记 `accepted` 绕过，只有非阻断发现可以附理由接受，普通发现形成内容健康待办。

课程增加 `learning_outcome → concept → exercise/assessment` 覆盖矩阵，能够回答「哪些学习目标没有证据、哪些知识点没有练习、哪些作业重复覆盖」。目标使用 learning_outcome_version，映射作为 release 作用域内不可变子对象并计入父依赖哈希；该矩阵也进入 release 和审核，不以图谱节点数量代表课程建设质量。

## 10. 检索与可信问答

### 10.1 混合检索

1. 识别用户、角色、课程、release、任务类型和可用资源。
2. 在权限过滤后并行执行：概念/别名精确匹配、稀疏文本召回、向量召回。
3. 对定义、定理和证明题，可增加必要的一跳图谱扩展，如上下位、先修和对比概念。
4. 使用 Reciprocal Rank Fusion 合并候选，再由 rerank 模型排序。
5. 按定理完整性、来源权威性和上下文预算组装证据。
6. 生成答案后检查每个关键断言是否有证据支持，引用是否指向实际片段。
7. 无足够证据时明确说明，并给出可补充的资料或转交教师选项。

检索结果先封装为带 ID 和不可变版本的 EvidenceBundle，包含 release_context、切片版本、内容哈希、许可、原文跨度和图路径，模型不得用无来源自由文本替代。MathAnswer 将回答拆为 answer_claim，每个论断使用带类型校验的 source_ref union：

- `source_quote` 必须有 `content_slice_version_id + source_span + evidence_bundle_version_id`，且 content_role=source_original。
- `reviewed_translation` 必须有翻译 `content_slice_version_id + translation_of_slice_version_id + source_span + evidence_bundle_version_id`，原文与翻译均审核有效且配对，显示为「已审核翻译」而非教材原文。
- `teacher_guideline` 必须有 `policy_version_id` 或经审核的教师指导版本。
- `model_inference` 必须有 `agent_run_id + parent_claim_ids`，并明确标记为推断，不能伪装成教材原话。
- `computation` 必须有 `tool_call_id + input_hash + engine_version`。
- `formal_verification` 必须有 `formal_verification_id + attestation_hash`。

Artifact 保存 evidence_bundle_version 和 claim 集合，打开与回放时重新校验证据、审核、许可和 release_context 是否仍有效。教材原话、模型推断、CAS 结果和 Lean 结果必须可区分。

默认是严格课程证据模式。通用知识模式必须单独发起并显著标记，不能在无教材依据时悄悄混入。

### 10.2 教学响应策略矩阵

教学策略由服务端 Policy Decision Point 统一判定，不能仅靠 system prompt。判定输入包括用户、课程、教学班、活动场景、具体题目版本、截止时间、教师策略和学生作答记录；判定结果同时约束 Answer、通用知识、计算、绘图、代码、文件导出和其他工具，防止换一种问法或换工具绕过。

| 场景 | 定义/事实 | 例题与练习 | 作业/测验中的题目 | 完整解答 |
|---|---|---|---|---|
| 自由学习 | 直接回答并给来源 | 默认分级提示，可主动选择示例讲解 | 不适用 | 按课程策略和练习解锁规则 |
| 普通练习 | 若本身就是被考查答案则按题目处理 | 提示、步骤反馈、示例消退 | 不适用 | 当前题目版本存在非空且可解析的提交后，可按教师策略解锁 |
| 截止前作业 | 可讲相关概念，但不能直接泄露被考查项 | 只做教师允许的反馈 | 所有工具执行同一限制 | 不因空答、乱答或反复追问自动解锁 |
| 测验/考试模式 | 按教师策略限制或拒绝 | 不调用替代求解通道 | 默认拒绝并转教师 | 截止和教师开放前禁止 |
| 截止后复习 | 直接解释概念 | 对比错因与示例 | 按教师发布的解答范围 | 教师明确开放后提供 |

有效练习作答是防滥用的教学条件，不是安全边界：它必须绑定当前题目版本且非空、可解析；重复提交空白不累计解锁。高风险评价不依赖「先作答就解锁」，而由时间和教师发布状态强制控制。

- 教师限制是下限，学生、普通对话和模型不能放宽；无有效策略时默认拒绝高影响输出并转教师。
- 学生可在自由学习中退出苏格拉底式引导，获得清晰解释，避免过度追问造成挫败。
- 新手可使用完整示例，随后逐步撤去支架；定义直答与解题引导不混为一谈。
- 证明可检查条件和逻辑结构，但不能把语言流畅当作证明正确。
- 每个引用可打开原页，教材页与 PDF 页同时展示；模型不知道时不编造页码、定理编号、文献或题目来源。
- 必须测试改写提问、跨工具求解、空作答、截止时间和导出产物等绕过路径。

### 10.3 向量化与多语言检索实施

#### 10.3.1 模型基线与替换门槛

- **Embedding 基线：BAAI/BGE-M3。** 使用 dense 1,024 维向量，支持多语言和最长 8,192 tokens；首期不引入 ColBERT 多向量基础设施。BGE-M3 的 sparse/multi-vector 能力只作为后续评测方向。
- **对照模型：Qwen3-Embedding-0.6B。** 以 1,024 维输出参加同一多语言数学测试，不因参数量或榜单直接替换。切换必须同时满足：冻结隐藏集 Top-5 召回绝对提升至少 3 个百分点，paired bootstrap 95% 置信区间下界大于 0；每个关键语种召回下降不超过 2 个百分点且仍高于上线门禁；检索 P95 不超过基线的 1.2 倍并满足第 18.2 节 SLO；月度 Embedding 与 rerank 成本不超过基线的 1.2 倍且处于批准预算内。满足后才构建新 index_version，历史 release_context 不切换。
- **Rerank 基线：BAAI/bge-reranker-v2-m3。** Qwen3-Reranker-0.6B 可作为对照，按相同证据集和吞吐测试决定。
- 云端 API 与本地部署使用同一不可变 `embedding_model_version` 口径，记录模型 revision、tokenizer、预处理、指令模板、运行精度、归一化和距离函数。同名模型的不同 revision 或精度配置不得静默混用。
- 全量灌库前只用小规模基准集完成模型选择；选定后按内容哈希批量去重计算，避免模型反复更换造成全量费用。

#### 10.3.2 单次向量计算流程

```text
已审核 content_slice_version
→ 按 retrieval_template_version 生成检索投影 + tokenizer 计数
→ 按模型/语言/长度分桶和动态批处理
→ 固定模型 revision 计算 dense embedding
→ L2 normalize（数据库保存 FP32）
→ 校验维度、有限数值、输入哈希和数量
→ 写入该 index_version 的独立分区
→ 建立 HNSW vector_cosine_ops
→ 召回/权限/延迟验收后供新 release_context 使用
```

向量输入不包含 HTML、下载 URL、密钥、学生隐私或检索控制指令。数据库中的向量无语义人工审核标签，但其输入切片、模型、运行配置和构建清单必须可追溯。初始 HNSW 参数采用 `m=16`、`ef_construction=128`、`ef_search=100`，最终以真实召回与延迟测试调整，不能为速度牺牲已审核内容的召回。

#### 10.3.3 查询与重排参数

1. 固定 release_context 和许可范围，识别查询语言但不默认限制同语料。
2. 概念/别名精确召回 20 条、稀疏召回 60 条、dense 召回 60 条，必要的图谱扩展不超过 20 条。稀疏通道使用语言感知分词、别名和公式符号规范化，中文、日文等无空格语言不能套用单一英文 analyzer。
3. 使用 RRF（初始 `k=60`）合并，取前 30 条进入 multilingual reranker。
4. 重排后保留前 20 条候选，按原子完整性、来源权威性和去重规则选 5—8 个证据，初始上下文预算不超过 8,000 tokens。
5. 低召回时可增加已审核翻译或机器查询翻译通道；最终生成仍执行证据支持和数学正确性检查。

这些参数是首轮评测起点，全部记录于 index/query 配置版本，不能散落在代码中由不同模块各自修改。

#### 10.3.4 多语言质量门禁

每个新语言至少抽取 30—50 页真实教材，先用 50—100 个问题做筛查；正式上线前每个关键语言/文字系统至少有 100 个独立隐藏问题，样本不足不得给出通过率结论。中文和英文沿用完整金标准。对预先约定可接受清晰度的扫描件，通过门槛为：

- 文字 OCR 字符准确率不低于 98%，公式完全匹配率不低于 95%，原文块阅读顺序准确率不低于 98%，页码与坐标覆盖率为 100%；达不到输入质量要求的页面单独拒绝，不纳入通过平均值。
- 单语 Top-5 证据召回不低于 90%，跨语言 Top-5 召回不低于 85%；引用支持准确率均不低于 95%。
- 定义/定理/事实题最终数学正确率和关键条件完整率均不低于 95%，证明逻辑有效率不低于 90%；无依据正确拒答率不低于 95%，可答题过度拒答率不高于 5%。
- 所有指标按语言和文字系统分别报告样本量与 95% 置信区间。正向指标须以 CI 下界达到门槛，错误率指标须以 CI 上界不超过门槛；点估计达标但区间不达标时增加样本或判定不通过，不得用其他语言高分抵消。

RTL、竖排、多栏、混合语言和低置信度页面必须进入人工队列。上述指标是 OCR/检索进入人工审核与批量导入的门槛，正式发布仍执行全项人工审核，不能用审核后的人工修正分数冒充原始 OCR 准确率。

### 10.4 数学专用能力与 GraphRAG 边界

- **多解法与反例实验室**：借鉴 Wolfram|Alpha 的多方法步骤展示，solution_version 支持多种解法；反例工具必须验证「满足全部前提且否定结论」，CAS 无法确认时只作为候选，不能直接发布。
- **交互数学**：借鉴 GeoGebra 的参数—图形联动，InteractiveMath 保存公式、参数域、代码、数据和图形，学生可调整参数观察变化；高风险评价仍受统一教学策略约束。
- **证明结构分析**：优先定位假设使用、步骤依赖和第一处可疑错误，不用整段相似度给证明判对错。
- **GraphRAG 只用于探索**：自动实体/关系与社区摘要仅用于建设者/审核者的候选发现，或明确标记为 AI 草稿的私人空间；不得进入公共检索证据。师生的定义、定理、证明、评分和正式引用必须回到已审核原文与图边。自动社区摘要不作为数学真值，不在首期替代混合检索。

## 11. 智能体系统与功能地图

### 11.1 编排方式

采用一个持久化编排器和受控工具集。每次运行固定 `release_context_id`（含各范围 release 与 index_version）、`policy_version`、模型修订、prompt 模板、工具版本、检索参数、最大步骤、token/费用预算、超时和恢复点。教师可按教学班配置允许工具、资料范围、教学风格、提示策略和评价模式；策略修改生成新版本，运行中途不得静默切换。

教材解析、审核发布、作业批改和课件渲染采用「确定性工作流为主、模型节点为辅」。可借鉴 LangGraph 的 checkpoint、interrupt 和恢复模式实现持久化暂停/审批/重放，但领域工具、权限与 release 不依赖特定框架。模型只在需要理解、规划或生成的节点工作，不能自由选择数据库写入顺序或绕过工作流门禁。工具分为：

- 只读工具：检索、图谱查询、个人学习记录、公开资料。
- 草稿工具：生成练习、PPT、课程草稿、修改建议，可自动执行并留版本。
- 教学写入工具：布置作业、发布通知，需要用户确认。
- 高影响工具：学院/课程公共知识发布、班级 teaching release 发布、Artifact 可见性升级、正式成绩和紧急撤回，需要相应范围授权和显式确认。

模型不得直接执行 SQL、任意 shell 或任意网络请求。外部文档和网页内容均视为不可信数据，不能改变系统指令和工具权限。高影响确认绑定具体操作、参数和 payload 哈希；参数变化后必须重新确认，SSE 重连或 worker 重试不能重复执行副作用。前端只展示简短工具动作、进度和结果，不展示或依赖模型隐藏思维链。

### 11.2 学生能力

- 教材溯源问答：定义、定理、证明、例题和跨概念对比。
- AI 学伴：分级提示、错因诊断、针对性练习和复习计划。
- 个性化路径：根据先修关系和作答证据推荐下一步，而不是只按章节顺序。
- 作业学习：上传解答、查看识别与反馈、申请教师复核。
- 数学计算与科研绘图：符号计算、数值实验、交互图、代码和导出。
- 多解法与反例探索：比较不同证明/计算方法，调整条件并验证反例，不将未验证候选当结论。
- 论文式产物：公式、图表、数据和文字可组合为可编辑报告。
- 阅读助手：在原页上提问、解释公式和比较前后概念。
- 个人知识管理：收藏、笔记、错因卡和复习队列均可通过对话调用。

### 11.3 教师能力

- 备课：生成讲义、教案、PPT、例题和课堂提问。
- 作业与考试：按知识点、难度和覆盖蓝图组题，生成 rubric 和试卷。
- 批改：转写、答案匹配、步骤反馈、答案分组、组内量规复核、学生申诉和改分审计；借鉴 Gradescope 的人工确认分组，不让 AI 直接批量定分。
- 学情：直接用自然语言询问班级薄弱点，并回到匿名或授权证据。
- 课程纠错：修改知识点、关系、题目和引用，提交审核。
- 教学产物迭代：在原对话中修改同一 PPT、图或讲义，不重复上传和填表。

### 11.4 建设者与审核员能力

- 创建课程、批量上传教材、导入旧课程包。
- 自动解析知识点、关系、习题、页码和引用。
- 查看失败项、低置信度项、重复项和异常数量变化。
- 对照原文逐项或批量编辑，使用键盘完成高效审核。
- 查看图谱和题目 diff，退回具体修改项。
- 审核后一键发布，并能够回滚或紧急撤回。

### 11.5 后续可插拔能力

- 更多形式化验证器与 Lean 题库扩展；真实 Lean runner 已在阶段 6 单独验收。
- 交互式数学实验、Manim 动画和虚拟仿真。
- 文献检索、阅读和研究型学习。
- 编程题自动评测。
- 语音讲解和无障碍阅读。
- 校历、教务、统一身份和 LTI 集成。

这些能力通过新工具、新 worker 和新 Artifact 加入，不改变学生与教师的基本使用方式。

## 12. 数学计算、绘图与沙箱

绘图和代码执行使用独立沙箱，不在 API 或数据库服务器直接运行模型生成的代码。

- 真实不可信代码优先在独立 VM/沙箱节点运行；可复用同物理机的隔离 VM，但不得与 API 共用 Docker 权限。未具备该条件时只开放受控绘图模板，不开放任意代码执行。
- 禁止挂载 Docker socket；使用 rootless/非 root、只读 rootfs、`cap_drop=ALL`、no-new-privileges、seccomp/AppArmor、userns 和 cgroup。
- 默认网络命名空间无网络，依赖只来自校内锁定镜像和包缓存，记录镜像 digest。外部下载由独立出口代理执行，校验重定向、DNS rebinding、IPv6、内网与云 metadata 地址。
- 限制 CPU、内存、wall time、进程组、文件数/inode、输出大小和压缩包展开大小；沙箱与索引构建使用独立队列和资源配额，避免拖垮 PostgreSQL。
- 保存代码、依赖、参数、输入哈希、镜像 digest、运行日志和产物哈希。
- 主站绝不内联不可信 HTML/SVG。交互产物在无 Cookie 的独立渲染源运行，iframe 使用最小 sandbox 权限、严格 CSP 和受控 postMessage，禁止顶层导航、弹窗、远程资源和未批准下载。
- Markdown、KaTeX/MathJax、Plotly 配置、URL scheme、LaTeX 和 SVG 统一白名单清理。对象存储返回正确 MIME、`nosniff` 和短时授权 URL。
- PDF/PPTX 检查 JavaScript、宏、OLE、外部关系、压缩炸弹和恶意结构，不能只用「文件能打开」作为安全验证。

## 13. API、任务与审计

### 13.1 API 约定

- 统一前缀 `/api/v1`，OpenAPI 自动生成，Pydantic/JSON Schema 为契约。
- `POST /agent/runs` 创建任务，SSE 推送消息、工具状态、审批请求和产物事件。
- 领域资源使用明确 REST API；前端不能访问数据库。
- 长任务统一返回 `job_id`，支持查询进度、取消、重试和恢复。
- 写操作支持 Idempotency-Key，按「操作者 + 端点/工具 + key」唯一并绑定请求哈希；同键不同请求必须拒绝，保存首次结果和明确保留期。
- 领域写入与 outbox 在同一事务提交，relay 使用 `SKIP LOCKED`，消费者按唯一 event ID 去重。系统按 at-least-once 设计，不宣称 Celery/Redis 恰好一次。
- `ops.job` 是业务任务真源，保存 Celery task ID、lease、heartbeat、attempt、取消状态和恢复点；配置 late ack、合理 prefetch、丢失恢复和死信处理，Redis 丢失后可从数据库重建待办。
- 工具接口有名称、版本、输入输出 schema、所需权限、超时和副作用级别；schema 做向后兼容测试，工具输出按不可信数据校验大小与内容。

### 13.2 错误处理

- 模型失败不影响原始资料和已发布 release；任务可指数退避重试。
- 不可重试错误进入明确失败状态，保留上下文，不无限重试。
- 发布采用两阶段构建与切换，失败继续使用旧版本。
- 工具超时和模型预算耗尽时返回可恢复状态，不生成看似成功的空产物。
- 所有错误给用户提供可理解的下一步，内部堆栈只进入受控日志。

### 13.3 审计

审计记录追加写入，至少覆盖登录、授权变化、上传、AI 生成、人工修改、审核、发布、撤回、成绩、导出和模型出校调用。保存操作者、时间、范围、前后哈希、原因和关联 run；不记录不必要的密码、密钥和完整敏感文本。

## 14. 安全、隐私与版权

### 14.1 数据分类与出校矩阵

所有云端 chat、vision、OCR、Embedding、rerank、外部检索和遥测 SDK 都经过统一出口网关，不能只保护聊天接口。许可绑定具体文档版本和资产，默认 `cloud_allowed=false`；检索结果携带数据分类，网关再次校验。

服务端为每次调用构造 `context_envelope`，逐项列出当前消息、用户粘贴文本、附件、历史消息、检索结果和工具输出的所有者、数据分类、许可、出校依据和内容哈希，缺分类默认拒绝。受限教材或草稿所在 workspace 将整个会话标记为受限，用户不能通过复制粘贴或切换工具降级。自由文本和附件执行受限语料哈希/近似匹配，命中即阻断；检测只作纵深防御，不替代 workspace 级硬边界。

每轮只发送当前任务所需消息与最小历史窗口，历史优先在校内压缩为可审计摘要，不默认发送完整对话。私人指「对教师和其他用户不可见」，不等于「不会出校」：若使用云端模型，界面必须在发送前明确告知实际外发范围，并提供关闭云端或本地模型路径；未配置合规路径时功能明确不可用，不静默外发。

| 数据 | 默认处理 | 出校条件 |
|---|---|---|
| 公共教材切片 | 校内保存，检查具体授权 | 授权明确允许云端处理，只发送必要片段 |
| 受限教材/草稿 | 校内处理 | 硬阻断，不能由模型或管理员临时提示绕过 |
| 学生自由提问 | 去标识、剥离元数据、使用假名 ID | 学校制度与告知覆盖，字段白名单；假名化不宣称匿名化 |
| 作业原图/语音 | 本地预处理，清理姓名学号、EXIF、PDF 元数据 | 经学校审批和供应商协议后发送最小页面区域，否则不调用云端视觉 |
| 学习记录 | 默认不出校，聚合最小化 | 仅在明确合法依据、告知和必要范围内处理 |
| 私人对话与历史 | 校内保存，默认只取当前任务最小上下文 | 用户选择云端且制度允许时按 context_envelope 外发；否则使用本地模型或不发送 |
| 日志与 Trace | 默认只存脱敏摘要、哈希、token、费用 | 禁止把完整敏感 prompt、密钥或个人信息放入遥测 |

学校法务/网信流程确认供应商的数据角色、训练禁用、保留删除、子处理者、区域与跨境要求，并完成必要的个人信息保护影响评估。零保留或企业协议是上线条件之一，而不是可选优化。

### 14.2 不可信内容与出口闭环

- OCR 文本、PDF 隐藏文本、网页、LaTeX 链接、工具输出和对话历史都是不可信数据；提示词分隔和注入分类器只能辅助，不能作为权限边界。
- 每次工具调用在模型外重新鉴权，模型上下文不放密钥。外发域名、参数、字节数和用途采用白名单，模型不能自行增加 URL 或接收方。
- 不可信内容触发跨范围读取、出校或高影响动作时，必须由用户重新确认，确认框显示确定性生成的实际操作与参数。
- 红队测试覆盖间接注入后读取草稿再经工具外发、跨会话记忆污染、恶意引用和伪造审核确认。

### 14.3 生命周期与其他控制

1. 对接学校统一身份认证；本地账号只作为过渡和应急。
2. 全站 HTTPS、安全 Cookie、CSRF 防护、登录限流、账号/IP/任务三级限流。
3. 密钥只在服务端密钥存储中，支持轮换，前端和日志不得出现；模型 base URL 做 allowlist、重定向和 SSRF 校验。
4. 教材按授权控制全文下载；问答只返回必要片段，不提供整本 PDF 公共导出。
5. 定义对话、作业图片、模型日志、产物和备份的保留期限与清理任务。活跃数据删除后，备份按周期到期；恢复备份时必须先重放删除 tombstone，避免已删除数据复活。
6. 权限测试覆盖水平越权、课程越权、班级越权、草稿泄露、release/索引错配、对象存储和 SSE 越权。
7. 数据库、对象存储和密钥分别备份，备份加密且使用独立凭据，备份不等于公开共享。
8. 上传执行病毒扫描、大小/类型校验、路径与压缩炸弹检查；物理文件去重与每课程/所有者授权分离，防止跨课程存在性、下载和删除串权。

## 15. 部署与运维

### 15.1 工作负载与服务器基线

云端模型优先时，首期不要求 GPU。第 0 阶段先测量学生数与并发、教材页数、图片增长率、切片与向量条数、每日 token、OCR 页数、沙箱并发和审核工时，再确定配置。推荐档位仅用于采购讨论，不构成并发承诺：

| 用途 | 参考起点 | 适用假设 |
|---|---|---|
| 开发/演示 | 8 vCPU、32 GB、1 TB NVMe | 不处理真实不可信代码，不承载正式教学 |
| 小班试点 | 16 vCPU、64 GB、2 TB NVMe | 约 30—60 名学生、受限并发任务、单门课语料，经压测后启用 |
| 多课程扩展 | API/worker/数据库按指标拆分，必要时独立 GPU 节点 | 根据 DB IO、HNSW 内存、OCR 和沙箱队列决定，不提前微服务化 |

生产使用 Docker Compose 部署 Web、API、worker、PostgreSQL、Redis、MinIO 和监控；不可信代码使用独立 VM/沙箱节点。至少保留 30% 磁盘余量，为 PostgreSQL 保留 CPU/内存/IO 配额，索引构建和 OCR 限流。

成本看板至少包含每活跃学生/月、每次问答、每次批改、每次索引重建的模型成本，以及存储、备份和人工审核工时。需要本地 OCR 或本地模型时再增加 GPU，不在首期盲目采购。

### 15.1.1 必须先采集的服务器信息

在决定本地 OCR/Embedding 并发、批量导入窗口和是否增加 GPU 前，采集：

- CPU 型号、物理核/线程、指令集、NUMA；内存总量、可用量和 swap。
- GPU 型号、显存、驱动/CUDA；没有 GPU 也明确记录，而不是默认采购。
- NVMe/SATA/网络盘、可用容量、随机读写和 inode；备份位置与独立凭据。
- OS/内核、Docker/虚拟化支持、校园入站/出站带宽、模型域名连通和代理限制。
- 教材页数/语言/扫描比例、每日作业图片、并发问答、预计切片数、可接受批处理完成时间。

Linux 服务器可通过以下只读命令采集基础信息；无 NVIDIA GPU 时 `nvidia-smi` 不存在属于正常情况：

```bash
lscpu
free -h
nvidia-smi
df -hT
lsblk -o NAME,MODEL,SIZE,TYPE,FSTYPE,MOUNTPOINTS
uname -a
hostnamectl
docker info
```

这些信息只改变部署和运行参数，不改变 content_slice、release、审核和 Artifact 协议。第 0 阶段必须在目标服务器用真实 PDF 做 OCR 页/分钟、Embedding 向量/秒、rerank 对/秒、HNSW 构建时间和查询 P95 基准测试。

### 15.1.2 配置如何影响决策

| 实测条件 | 部署决策 |
|---|---|
| 无 GPU，CPU/内存足够 | 云端模型处理允许出校内容；受限内容使用本地 CPU 小队列，先测吞吐，不承诺整本 OCR 时间 |
| 有合适 GPU 与驱动 | 本地 PaddleOCR-VL、BGE-M3 和 reranker 批量运行；限制显存与并发，避免影响数据库 |
| 内存不足或索引构建影响业务 | 降低批量/HNSW 构建并发，将 worker 或检索拆到独立节点，不在生产主库高峰重建 |
| 磁盘慢或容量不足 | 优先保证 PostgreSQL NVMe；原页派生图、临时 OCR 和旧索引分层存储，但不删除仍被引用的证据 |
| 出网受限或教材禁止出校 | 自动切换本地 OCR/Embedding；没有合规聊天模型时只提供检索与原文，不伪造 AI 回答 |
| 需要不可信代码/Lean | 使用独立 VM/沙箱节点，不能因主服务器配置高就共用 Docker 权限 |

1,024 维 FP32 向量原始大小约为 `N × 4,096 bytes`。10 万条约 0.41 GB，100 万条约 4.10 GB，1,000 万条约 41.0 GB；这只包括向量值，不包括 HNSW 图、PostgreSQL 堆、索引分区、WAL、备份和余量。容量规划初期按原始向量的约 2—4 倍估算检索工作集，再以 pgvector 实测修正，不能把原始向量大小当作总内存需求。

OCR 页图像、PPT、作业和备份通常比向量更占空间。原图、派生裁剪、OCR 输出、向量、索引和 Artifact 分别统计，至少保留 30% 可用空间；GPU 是否值得购买由实测队列和年度云端费用决定，而不是由模型参数量单独决定。

### 15.1.3 R720 开发测试机基线（非生产，2026-11-04）

已获取学院 PowerEdge R720 的实际配置：双路 Xeon E5-2609 2.40 GHz，合计 8 核 8 线程、2 个 NUMA 节点，支持 AVX 但不支持 AVX2；约 32 GB 内存、8 GB swap；Ubuntu 26.04。根文件系统约 1.8 TB、可用约 1.7 TB。未提供 GPU/CUDA 信息，按无可用 GPU 规划。当前只看到一个 `sda` 逻辑盘，无法据此判断物理磁盘、SSD/HDD 或 RAID 冗余，存储性能与故障域必须另行实测。

| 项目 | 部署决定 |
|---|---|
| 核心服务 | 单机 Docker Compose 运行 nginx、Web、FastAPI、PostgreSQL/pgvector、Redis、MinIO 与轻量 worker，不引入 Kubernetes |
| 对话/视觉生成 | 默认使用经批准的云端模型；本地小模型不作为数学教学主模型 |
| Embedding | 本地 BGE-M3 为基线，1 个 worker，从单 NUMA 节点 4 线程起步，与 2/8 线程做对照；查询和索引使用同一运行配置 |
| Rerank | 本地小并发或合规云端，限制候选数与请求并发，不与批量 Embedding 争抢 CPU |
| 多语言 OCR | 本地仅后台排队，重任务总并发 1；整本教材安排维护窗口，允许出校内容才使用云端 OCR |
| HNSW 构建 | 避开教学高峰，限制 maintenance_work_mem 与并发，禁止与 OCR/大批量 Embedding 同时运行 |
| Lean/代码沙箱 | 使用受限 VM/沙箱配置，首期并发 1；资源不足时只开放受控绘图模板 |
| 数据库 | 从 shared_buffers 4—6 GB、effective_cache_size 16—20 GB、保守连接数起步，按实际查询调优，不按大内存服务器模板配置 |
| 存储与备份 | 根盘容量足够但性能/冗余未知；数据库与对象资产必须做异机/异地备份，不能把同盘备份当容灾 |
| 运行库 | 使用固定 Linux 容器和通用 x86_64/AVX 构建，禁止未验证的 AVX2/AVX512 专用二进制；Ubuntu 26.04 只作为宿主，ML 依赖在容器内锁定 |

建议资源预算采用「核心常驻 + 一个重任务」：核心数据库/API/对象存储等常驻，OCR、批量 Embedding、HNSW 重建、Lean 中同一时间只运行一类重任务。API 与数据库始终保留内存和 CPU，设置至少 4 GB 可用内存预警，禁止依赖长期 swap 维持服务。NUMA 绑定先以单节点 4 线程测试，不为使用全部 8 核而让模型跨节点访问内存。

R720 只用于开发、集成测试、小规模模型验证和 CI，不作为正式生产或并发容量依据，也不为其安排 GPU/内存升级。开发测试只使用合成或去标识数据，镜像、依赖和迁移流程与云生产保持一致；性能数字只用于判断开发可用性，云生产以独立 staging 压测重新确定容量。

开发阶段可在 R720 完成功能与小型基准：20—50 页多语言数学 PDF 的 OCR、5,000 个切片 Embedding、100 对重排和 10 万向量 HNSW 构建，用于发现兼容性与明显资源问题。生产并发、自动扩缩容和 SLO 必须在云 staging 用预期规模重新压测，不能引用 R720 结果作为生产承诺。

### 15.1.4 校内生产部署拓扑

正式生产部署在学校本地高配服务器，使用与 R720 开发测试相同的容器镜像、依赖锁定和数据库迁移，但按职责划分服务与资源：

- **接入层**：校内负载均衡/反向代理 + HTTPS，仅暴露 Web/API；无状态 API 可从 2 副本起步，按并发与 P95 扩容。
- **数据层**：独立 PostgreSQL/pgvector 数据目录与备份设备，优先 SSD/NVMe；对象文件采用 S3 兼容存储，Redis 独立运行，ops.job 仍为任务真源。
- **计算层**：OCR、Embedding、Rerank、PPT 渲染分队列运行，依据队列与耗时扩容 worker；重任务与数据库隔离 CPU、内存和 IO。
- **模型层**：对话/视觉默认走批准的云端模型；本地推理仅在校内模型节点运行，模型 revision 与索引配置保持一致。
- **隔离执行**：Lean 和不可信代码使用独立 VM/沙箱资源池，默认无网、无数据库凭据，不与 API 共享执行权限。
- **运维层**：开发、staging、生产使用不同数据库和密钥；集中日志/Trace、预算告警、跨设备备份和定期恢复演练。

校内生产优先使用 VM + Docker Compose 或学院现有容器平台；只有多节点调度、自动扩缩容和运维团队收益明确时才引入 Kubernetes。正式容量以学校高配服务器的 staging 压测确定，R720 只用于发现兼容性问题，不用于生产并发承诺。

教材与学生数据保存在学校批准的基础设施内；第三方模型调用仍执行 context_envelope、数据分类与出校门禁，校内服务器不能成为绕过云端数据政策的理由。

### 15.2 可靠性

- PostgreSQL 使用 pgBackRest 或等效方案执行全量、增量和 WAL 备份。
- 正式运行目标：RPO 不超过 15 分钟，RTO 不超过 4 小时。
- 对象存储启用版本和生命周期策略；数据库备份不替代文件备份。
- 每季度至少一次隔离环境恢复演练，记录实际恢复时间。
- 上线前检查迁移、磁盘、备份、HTTPS、权限、模型配额和 worker 心跳。
- 对模型调用设置用户、课程、单次 run 和全院预算，达到限额后降级而非失控消费。

### 15.3 可观测与可重放边界

统一 correlation/trace ID 贯穿 API、SSE、outbox、Celery、模型调用和发布任务。最小告警包括 outbox 最老未投递时间、队列延迟、job 卡死、发布失败、索引构建、DB 连接/磁盘/WAL、备份年龄、MinIO 容量、沙箱拒绝/超时和模型费用。Prometheus 不使用 user/conversation UUID 等高基数标签。

「可回放」分为审计重建、确定性工具重放和模型重新执行。云端模型有随机性，不能承诺逐字复现；run 必须固定完整 release_context、prompt、模型修订、工具版本和输入哈希，以便解释差异。审计表独立授权并定期导出或做哈希固化，但不宣称能防数据库超级用户篡改。

## 16. 旧系统迁移策略

### 16.1 保留与废弃

优先保留：课程包原文、题库、图片、教材、课件、来源信息、教师审核证据、公式转换器和经过测试的独立算法。

审慎复用：模型网关、导入器、worker 组件和部署脚本。复用前必须有独立接口和安全检查。

直接替换：重复导航、无明确所有者的数据表、自由文本状态、以静态包和数据库双真源驱动的逻辑、关键词 ILIKE 检索和旧接口契约。

### 16.2 迁移步骤

1. 对旧数据库和文件做只读快照，盘点线上课程与本地课程包差异。
2. 建立旧 ID 到新 UUID 的 `legacy_ref` 映射，保留旧来源和迁移批次。
3. 将旧内容先导入新系统的草稿区，不直接成为正式知识。
4. 按课程、来源、题号和内容哈希去重，生成数量与字段对账报告。
5. 历史上未实际逐题审核的「人工已审」标记不得直接迁移为新平台实审结论。
6. 建立新 release 和新索引，运行权限、检索、问答和课件回归。
7. 迁移分为只读影子验证、写入冻结/最终增量、正式切换、回滚窗口四段，每段只有一个唯一写入方，不做长期双写。
8. 新系统接收真实教学写入后，不能直接回滚旧系统导致新数据丢失。实施前选择并演练一种方案：短窗口冻结高价值写入、反向增量导回，或只切回流量但保留新系统数据并后续补偿。
9. 对账覆盖数量、内容哈希、外键、资产校验和、权限和审核证据，记录可接受差异及签字人。`legacy_ref` 以来源系统、实体类型和旧 ID 唯一，并映射到新 UUID。
10. 切换后旧系统保留只读快照和明确的回滚窗口；窗口结束后再按保留政策归档，不长期维护双真源。

## 17. 实施顺序与团队

### 17.1 阶段安排与上线门槛

| 阶段 | 参考周期 | 主要交付与门槛 |
|---|---:|---|
| 0. 基线与教学共创 | 2—3 周 | 服务器/数据/版权盘点、真实问题集、审核工时试验、教师工作流观察、原型测试 |
| 1. 平台内核 | 5—6 周 | 只实现垂直切片必要的身份/RLS、source/content/exercise 基本表、版本/审核/release、对象存储、outbox/job、CI；不在此阶段实现完整 math、proof_step、annotation/adjudication 或参数化题 |
| 2. 知识与检索 | 6—8 周 | 数学分析 I 解析、可审核切片、试点章节 convention/statement/symbol_binding、向量索引、图谱、混合检索、400 问双标金标准（含 annotation/adjudication）与权限测试 |
| 3. 智能体与审核工作台 | 4—6 周，与阶段 2 部分并行 | 纯对话 UI、溯源问答、结构化审核 diff、课程建设发布、受控绘图产物 |
| 4. 核心小班试点 | 3—4 周 | 教师 shadow mode 后开放 1—2 个班；先验证知识、问答和课程发布，再 go/no-go |
| 5. 教学增量 | 5—8 周 | 班级 policy、200 份作业双标集、作业复核、PPT、个性化复习；proof_step 与参数化实例库作为独立增量，每能力单独验收后开放 |
| 6. Lean 与安全加固 | 4—6 周，可并行 | 隔离 Lean runner、目标声明/公理检查、红队、恢复演练；未达标前只显示未验证 |

核心知识库、审核发布和溯源问答以 14—18 周形成可试点垂直切片为工程目标；一门课较完整教学闭环按 22—30 周估算。该估算以 3—4 名工程人员、教师持续参与以及 Lean/无障碍专家按需支持为假设，不是交付承诺，也不包含不可控的教材授权等待。400 个问答与 200 份作业的双标集分别纳入阶段 2 和阶段 5 的关键路径，实测人工审核工时不得压缩。所有百分比指标均为 go/no-go 目标，未达标就延后公开，不用功能数量换进度。

试点先让教师使用 shadow mode，再开放学生。试点建议 1—2 个班、30—60 名学生、持续至少 4 周，保留非 AI 等价路径、退出机制、事故响应和回滚条件。没有对照设计时不宣称因果学习提升。

### 17.2 最小团队

- 1 名技术负责人/后端工程师：架构、数据库、权限和发布。
- 1 名前端工程师：智能体交互、产物查看器和可访问性。
- 1 名 AI/数据工程师：解析、检索、模型评测和工具链。
- 1 名兼职测试/设计人员：场景测试、视觉一致性和试点反馈。
- Lean runner 阶段需要熟悉 Lean/mathlib 的教师或外部专家审查信任基础；无障碍验收需要具备 WCAG 与读屏测试经验的人员，不能完全由前端自评。
- 每门试点课至少 1 名任课教师和 1 名独立审核教师，另设备用审核员处理审核员修改后的复审，参与教材标注、题库审核和验收。

## 18. 测试与验收标准

### 18.1 工程验收

- 所有迁移可从空库重建，并通过 expand/contract 新旧版本升级测试；已部署迁移不原地修改。
- 所有发布、成绩、权限和状态转换必须有事务及并发测试，覆盖双发布者、审核撤回与发布并发、发布期间持续读取和不变内容跨 release 复用。
- 使用非 owner、非 superuser 账号完成 RLS 与权限矩阵测试，草稿、未审核、审核撤销/失效和跨班数据泄露为 0。
- 分别测试学院、课程公共和班级 teaching release：教师自确认只能发布到本人教学班，不能进入公共知识；班级 release 不错绑课程指针，class_shared Artifact 无法绕过 teaching release；release_context 合并检索不漏公共内容也不串班。
- 作业测试同时覆盖公共教材题和班级自建题的 source_release_id；新公共 release 发布后旧班级仍沿 base 链使用旧上下文，索引重建后历史作业仍使用固定 index_version，上级撤销使依赖上下文失效。
- 依赖失效测试覆盖：撤销节点后关联边消失但节点保留，撤销 rubric 后相关习题与 Lean 标签失效，撤销学院概念后采用它的课程/班级上下文隔离。
- 多语言测试覆盖：新原文与旧翻译不能进入同一新 release，无相应语言/数学资格者不能签署；历史 release 保留固定原文—翻译配对。
- 习题测试确认权威答案和解析分别只有 answer_version 与 solution_version，教材印刷答案只作来源证据；任一侧变更触发依赖复核。
- 数学语义测试覆盖不同教材约定、符号作用域和量词；structured/formal statement 缺少 convention_version 或符号绑定无效时不得发布，不能将不同约定误判为矛盾。公式 AST 必须区分 binder 作用域，proof DAG 禁止循环和引用同 release 之外的草稿命题。
- 参数化 assignment_item、submission_item 和 exercise_attempt 必须固定已审核 exercise_instance、template_version 与 seed；新建 release、重放作业或重新随机不得改变学生已收到的实例。
- 批准后尝试修改 symbol_binding、proof_step、proof_step_dependency 或父切片版本，必须因父哈希/依赖失效而发布失败；AI 解释不能作为 source_original 直接引用。
- answer_test 发布时实际验证权威答案通过自身测试，rubric 可接受解法必须引用同 release 的 answer/solution；参数化 Lean 标签必须绑定具体实例，不能由模板继承。
- 隐藏评测集执行 dataset_access_grant + RLS，冻结后 append-only，越权读取案例或 case_result/metric_result、写入普通 query_log、进入 Prompt/训练均被拒绝。
- Artifact 打开、渲染和下载重新执行 PDP，覆盖考前创建求解器、考中打开及包含 answer_test/解答的绕过尝试。
- MathAnswer 六类 source_ref 按类型强制校验必填 ID，缺 evidence_bundle、policy、tool_call 或 attestation 的论断不得发布或回放；已审核翻译必须带有效原文配对，未审核 AI 翻译不能作为证据。
- 所有终态决策、撤销、下线与发布共用范围锁；覆盖撤销在终检前/后提交、单项撤销冻结进行中作业和许可到期即时隔离。
- 云端测试覆盖用户粘贴受限教材、附件、完整历史、工具输出和受限 workspace 降级尝试，context_envelope 缺分类或命中禁止项时不得出校。
- 模型治理测试确认普通用户无法从 API、前端、日志、SSE、导出或错误信息读取密钥；自定义 Base URL、篡改 endpoint_id、选择下架/不兼容/超配额模型均被后端拒绝。受限内容即使用户手选云端模型也不能出校。
- 用户切换生成模型只影响后续 agent_run，历史模型与 release_context 不变；Embedding/Rerank 无用户切换入口，停用模型后新任务与排队任务重新校验。
- 自动构造未审核知识点、未审核图谱边和已修改但未复审的习题，验证发布均被阻止；仅凭 Lean 通过记录、仅存在历史 approved、或 release 缺少关联子对象同样被阻止。
- 所有「Lean 已验证」标签均可重放目标声明和依赖环境，检查无 `sorry/admit` 与策略外公理；篡改命题、rubric、对应关系或更换版本后 release_dependency_hash 变化并使标签失效。Lean 进程无 DB 凭据，伪造成功文件无法通过受信 wrapper 签名 attestation。
- 发布中断、Redis 丢失、worker 在副作用后 ack 前崩溃、模型超时和索引构建失败均不会破坏旧版本或重复高影响动作。
- 沙箱通过网络、资源、路径逃逸、恶意文件、iframe 权限和存储型 XSS 测试；Artifact/工具 schema 通过契约与向后兼容测试。
- 完成数据库与对象资产的真实恢复演练，并验证删除 tombstone 不会在恢复后复活。

### 18.2 可复现的知识与问答验收

以《数学分析 I》建立至少 400 个真实或教师确认的问题，其中开发集至少 200、冻结隐藏集至少 200，每个关键题型/语言隐藏分层至少 100。必须覆盖相似定理不同条件、量词遗漏、符号跨章冲突、教材约定差异、错误 OCR、跨语言证据、反例、替代证明和无依据问题。两位教师独立标注证据、答案和评分 rubric，分歧由第三人或课程负责人仲裁；最终报告给出预注册分母、样本量和 95% 置信区间，不能用调参集作为最终成绩。

- 正式公开内容切片来源定位覆盖率 100%。
- 正式知识点、图谱节点与关系、习题、答案量规、知识正文、关联对象和公开切片的人工审核有效率 100%。
- Top-5 金标准证据召回率不低于 90%。
- 引用与原文支持关系准确率不低于 95%，同时测量最终答案而非只测引用。
- 定义/定理/事实题最终数学正确率和关键条件完整率均不低于 95%；证明题逻辑有效率不低于 90%，由教师盲评。
- 无依据问题正确拒答率不低于 95%，可答题过度拒答率不高于 5%，防止靠一律拒答刷指标。
- 适用 CAS 的计算结果独立复核率 100%；虚构教材页码、定理号和题目来源为 0。
- 每种新增语言使用独立真实教材测试集，达到第 10.3.4 节门禁后才上线；分别报告 OCR、单语检索、跨语言检索和最终答案，不能用中文/英文总成绩覆盖小语种失败。
- Go/no-go 在运行前预注册：召回率、正确率等正向指标要求 95% CI 下界达到门槛，拒答不足、误判等错误率要求 CI 上界不超过门槛。零观察到错误同时报告二项分布置信上界，不能宣称真实错误率绝对为 0；样本不足必须扩充而非按点估计放行。该统计判定规则同样适用于作业、Lean 和智能体指标。
- 在固定课程规模、20 个并发直接问答、指定模型和正常校园网络下，普通问答完整响应 P95 目标不超过 8 秒，检索服务自身 P95 不超过 1.5 秒；长任务单独度量。

### 18.3 作业、Lean 与智能体验收

作业建立至少 200 份独立隐藏、覆盖清晰手写、印刷体、符号易错、空答、无法辨认、非标准正确解法和微妙错误证明的教师双标集，开发集另建；各关键题型在运行前预设最小隐藏样本量，不足时扩充：

- 在预先约定的可接受图像质量内，题目级转写完全正确率不低于 95%；不可辨认内容必须显式转人工，不能猜测。
- 可解析客观题和表达式题经确定领域/假设校验后，最终分数与教师完全一致率不低于 98%；SymPy 无法确定定义域、分支或假设时转人工。
- `answer_test` 单元与属性测试覆盖非法输入、定义域、分支、等价形式和容差；金标准危险错误答案误接受为 0，合法等价答案误拒绝率不高于 2%。参数化题目同种子重建一致率 100%，可见实例人工决策覆盖率 100%，assignment_item 固定 exercise_instance 与 seed。
- 证明题严重错误漏判率不高于 5%，有效替代解法被错误否定率不高于 10%。AI 只给建议，教师正式成绩责任确认率 100%，但禁止默认全选和批量接受。
- 核心智能体场景按版本化 rubric 评测，任务完成率不低于 85%，高影响动作越权执行为 0；包含跨工具绕过、SSE 重连和重复副作用测试。
- 生成 PPT 的文本、公式和图形可编辑，通过 PowerPoint 与 WPS 实机、字体和阅读顺序检查。

### 18.4 真实教学与无障碍验收

- 试点 1—2 个班、30—60 名学生、至少 4 周。核心任务无帮助完成率不低于 85%，记录耗时、求助次数、错误和退出情况，而不是只问满意度。
- 教师完成「上传—修改—审核—发布—撤回」，学生完成「提问—看证据—练习—反馈—复习」；高频任务同时测试对话与结构化入口。
- 教师总审核与教学工时不超过基线，且至少一个核心环节在质量不下降前提下节省 20% 以上；若显著增负则不扩课。
- 测量学习结果、错误逃逸、教师改分率、申诉和学生过度依赖。无对照设计时不宣称因果提升。
- 键盘、屏幕阅读器、公式朗读、图表替代、移动端相机上传、断线恢复和生成课件可访问性全部通过首期检查。

指标未达标时只修复试点链路，不用增加课程数量掩盖问题。

## 19. 主要风险与处理

| 风险 | 处理方式 |
|---|---|
| 数学 OCR 与公式错误 | 原文对照、低置信度队列、关键项全审、公式解析与渲染检查 |
| 向量检索看似智能但证据错误 | 精确概念召回、混合检索、重排、引用验证和真实问题集 |
| 图谱成为展示品 | 问答、先修诊断和推荐必须实际读取图谱；无证据关系不能发布 |
| 智能体权限过大 | 工具白名单、范围授权、预算、人工审批和全量审计 |
| 一键发布变成一键污染 | 双人审核、不可变 release、质量门禁、原子切换和回滚 |
| 云端模型成本失控 | 模型路由、缓存、上下文预算、配额、硬限额和成本看板 |
| 旧数据质量传入新平台 | 只迁入草稿、来源对账、重新审核，不继承虚假实审状态 |
| 功能持续增加导致再次臃肿 | 统一 Artifact 协议和工具插件，一级交互不随功能增加 |
| 单服务器故障 | 备份、恢复演练、服务健康检查；达到业务要求后再拆节点 |

## 20. 最终建设口径

首期完成的不是一个「有聊天框的课程网站」，而是以下最小闭环：

> 一本经授权的《数学分析 I》教材进入校内平台，被解析为可溯源原文、知识点、关系和习题；AI 生成内容始终保持草稿，建设者与审核者可以在同一智能体工作台中修改和发布；学生可以直接问定义、学习证明、练习和绘图；教师可以备课、生成 PPT、布置并复核作业；任何正式知识、索引和教学行为均可审计、可评测、可回滚。

这个闭环稳定后，再通过同一课程模板、工具协议和发布机制扩展到学院其他课程与科研训练场景。

## 21. 开发中的坑与强制注意事项

| 领域 | 常见错误 | 强制做法 |
|---|---|---|
| 版本 | 运行时读取某实体「最新版本」 | 所有公开读取按 release manifest 或历史教学实例固定版本 |
| 审核 | 只要历史上有 approved 就放行 | 最新终态决策为准，reject/revoke 立即使旧通过失效 |
| 哈希 | 只哈希父表，漏掉 rubric、证据和附件 | canonicalizer 版本化，计算完整依赖闭包和最终渲染输入 |
| 审核 | 课程级按钮一次批准所有新内容 | 逐项 review_decision_item；批量只是 UI 效率，不是覆盖豁免 |
| 自审 | 公共知识审核员修改后直接通过本人版本 | 任何进入 content_hash 的修改都记录贡献者，并由另一名授权审核员复审 |
| 图谱 | 节点通过就默认边通过，或把共题边当先修 | 节点、边、证据分别进入审核与 release 闭包 |
| 跨课程 | 编辑共享概念导致其他课静默变化 | 学院/课程 owner_scope 分离，课程显式采用具体版本 |
| 切片 | 每次重建向量都产生新内容审核义务 | content_slice_version 与 retrieval.chunk 分离 |
| 向量 | 混合不同模型/维度，或先 Top-K 再过滤权限 | 不可变模型修订和构建清单，按 release/权限分区召回 |
| 发布 | 事务外预检后直接切换，产生 TOCTOU | 范围锁 + expected base + 同事务终检 + 请求级 release 快照 |
| 作业 | 新 release 或索引重建使进行中作业和历史作答漂移 | agent_run/assignment/submission/artifact 固定完整 release_context 与题目版本 |
| 问答 | 有引用就当作数学正确 | 教师盲评最终答案、关键条件和证明逻辑，区分来源与模型推断 |
| 教学策略 | 只限制聊天，CAS/绘图可绕过高风险题目 | Policy Decision Point 统一约束所有工具和导出 |
| CAS | 无条件使用 `eval` 或忽略定义域和分支 | 安全解析、显式假设，不能确定时转人工 |
| 数学语义 | 把 LaTeX 当数学含义或跨教材统一符号/约定 | statement AST + symbol_binding + convention_version，派生语义独立审核 |
| 参数化题 | 审核模板后自动公开所有随机实例 | 每个具体实例都有人工决策和可复现 seed/参数/验证清单 |
| 评测 | 隐藏题进入 Prompt/检索或用模型自评当金标准 | 数据集权限隔离，教师双标/仲裁，记录分子分母和置信区间 |
| GraphRAG | 用自动社区摘要回答定义或证明 | 只做探索候选，正式回答回到审核原文和图边 |
| Lean | 以项目构建退出码显示已验证 | 固定目标声明、干净重建、检查公理和 sorry/admit，失败不等于命题为假 |
| 评测 | 用同一模型生成答案并给自己打分 | 确定性检查 + 教师双标/盲评 + 冻结隐藏集 |
| Agent | 模型直接 SQL/shell/URL，或确认后改变参数 | 版本化工具、模型外鉴权、确认绑定 payload 哈希 |
| 异步 | 把 Celery/Redis 当恰好一次队列 | DB job 真源、outbox、去重、late ack、幂等副作用和崩溃恢复测试 |
| SSE | 重连或重试重复发布、布置和改分 | 业务幂等键与请求哈希同事务消费，重放首次结果 |
| 事务 | 在数据库长事务中调用云端模型 | 外部调用在事务外，数据库事务只做终检与指针切换 |
| 前端 | 主站内联 AI HTML/SVG 或放宽 iframe | 独立无凭证渲染源、CSP、白名单清理、最小 sandbox |
| 图谱 UI | 一次渲染数万 SVG 节点导致页面卡死 | 服务端聚合、按层加载、列表等价视图和大数据虚拟化 |
| 无障碍 | 只靠颜色、tooltip 和 KaTeX 视觉字形 | 文本/图标双标签、MathML、键盘、数据表和焦点管理 |
| PDF/OCR | 假设单栏阅读顺序和固定页码偏移 | 逐版校准旋转、坐标、跨页块和书页/PDF 页映射 |
| 公式/课件 | 公式全部截图，依赖开发机字体 | LaTeX/MathML/OMML，服务器固化授权字体，PowerPoint/WPS 双端验收 |
| 上传 | 信任扩展名、压缩包和元数据 | 病毒/结构/大小检查，防 zip slip/bomb，剥离 EXIF 与主动内容 |
| 文件去重 | 内容相同就共享权限和删除生命周期 | 物理去重与逻辑授权、引用计数分离 |
| 云出口 | 只管控 chat，忘记 OCR/Embedding/rerank/trace | 所有外部处理统一经过数据分类与出口网关 |
| 模型密钥 | 浏览器保存 API Key，或允许用户自定义 endpoint | 仅管理员配置，密钥服务端写后不可读；用户只选已授权生成模型，后端复核策略 |
| 沙箱 | root 容器挂载 docker.sock 就当隔离 | 独立 VM/沙箱节点、默认无网、最小权限和资源限制 |
| 迁移 | 新系统接收正式写入后直接回滚旧系统 | 明确唯一写入方，演练反向增量或保留新数据的回滚方案 |
| 数据库迁移 | 一个版本内直接重命名/删除旧列 | expand/contract，先兼容再清理，生产规模预演 |
| 日志 | 记录完整敏感 prompt， metrics 使用用户 UUID 标签 | 脱敏摘要、低基数指标、保留期限和访问控制 |
| 备份 | 只检查 pg_dump 文件存在 | 数据库 + 对象 + 配置恢复演练，备份加密和独立凭据 |
| 成本 | 忽略 Embedding 重建、重排和失败重试费用 | 内容哈希去重、预算、缓存、按任务/用户统计单位成本 |
| 开发环境 | Windows 本机通过就直接上 Linux | 统一 Linux Dev Container/WSL2，固定字体、依赖和工具链 |

## 22. 架构决策记录与演进触发器

| 决策 | 当前结论 | 何时重新评估 |
|---|---|---|
| ADR-01 模块化单体 | 单 PostgreSQL、多 schema、应用服务边界 | 独立扩缩容或团队所有权收益明确高于运维成本时拆服务 |
| ADR-02 PostgreSQL 图谱 | 关系表 + 递归 CTE + 读取投影 | 真实图查询经基准证明无法满足，且 Neo4j 运维有明确收益 |
| ADR-03 pgvector | 按 release/模型分区的混合检索 | 召回、P95、构建时间或规模持续不达标时引入独立检索引擎 |
| ADR-04 内容与索引分离 | 可审核 content_slice，重建 retrieval chunk | 不随模型升级改变，这是长期不变量 |
| ADR-05 不可变 release | manifest 闭包、终态审核、事务切换 | 不通过「直接改正式表」优化性能 |
| ADR-06 分级审核 | 公共知识独立审核，班级材料教师确认 | 只调整责任流程，不取消公开前人工审核 |
| ADR-07 Lean 独立证据 | 与人工审核分离、绑定声明与环境 | 新验证器可并列增加，不能复用 Lean 标签 |
| ADR-08 单编排器 | 工具化、持久化 run、有限步骤和预算 | 只有明确收益才增加子智能体，禁止无边界自治 |
| ADR-09 云端出口 | 数据本地、默认禁止出校、统一网关 | 更换供应商不改变数据分类和权限不变量 |
| ADR-10 沙箱 | 不可信代码使用独立 VM/节点 | 安全条件不足时收缩为模板绘图，而不是降低隔离 |
| ADR-11 至少一次任务 | DB job + outbox + 幂等 | 不追求名义恰好一次，持续验证恢复语义 |
| ADR-12 开放标准 | QTI/LTI/MathML/OMML/开放导出 | 新对接先写映射和隐私评估，不让外部格式污染内部模型 |
| ADR-13 纯智能体工作台 | 对话主线 + 结构化 Artifact 面板 | 新功能优先新工具/产物，不恢复传统功能迷宫 |
| ADR-14 单写入方迁移 | 影子验证、冻结增量、切换、受控回滚 | 禁止无方案长期双写或直接丢弃新写入 |
| ADR-15 模型治理 | 管理员管密钥/目录/额度，用户选已授权生成模型 | 生成模型可切换不改变 Embedding/Rerank 索引；密钥永不下发浏览器 |
| ADR-16 渐进数学语义 | 原文为证据，约定/命题/符号/证明 AST 为独立审核派生层 | 不要求全量形式化；只有真实验证通过才使用 formal/Lean 标签 |
| ADR-17 评测即数据资产 | 数据集、标注、仲裁、运行和指标入库并版本化 | 隐藏集不得进入训练、Prompt 或检索，指标必须可复现 |
| ADR-18 GraphRAG 边界 | 只用于探索与候选导航 | 定义、证明、评分和正式引用必须回到审核原文，不采用自动社区摘要作真值 |

## 23. 多轮审查记录

最终版采用「独立产品调研 + 架构/数据审查 + 教学/安全审查 + 修订后终审」流程。审查结论只作为设计输入，所有修改均回到可执行规则、数据契约和验收测试。

| 轮次 | 重点 | 已吸收的关键结果 |
|---|---|---|
| 产品与教学调研 | 国内外 AI 学伴、教师工具、自适应学习、科研工作区 | 来源限定、交互图、周期再测、班级 policy、产物工作区；拒绝功能堆砌和默认监控 |
| 架构与数据审查 | release 闭包、审核有效性、并发、RLS、向量、迁移 | 内容/索引分离、最新终态审核、发布锁、单 release 上下文、任务幂等和历史版本固定 |
| 教学与安全审查 | 跨工具绕过、云端出校、审核工时、Lean、评价、无障碍 | 全工具策略矩阵、出校矩阵、Lean 信任基础、双标评测、审核产能与 WCAG 2.2 AA |
| 独立终审 | 跨范围发布、审核锁、Lean 闭包、出校和许可 | 发现班级 teaching release 阻断项及 5 个高优先级问题，已补决策类型、release_context、范围锁、受信 attestation、context_envelope 和运行时许可门禁 |
| 针对性复核与闭合确认 | 多级 release_context、依赖失效传播、作业来源 | 补齐三 release/三 index 固定上下文、dependency_decision_manifest 和 source_release_id；最终独立复核确认无剩余 P0/P1，可冻结设计基线 |
| 多语言切片与向量化复核 | 投影/审核边界、语言门禁、翻译资格、答案真源 | 修复检索模板误入内容哈希风险，量化 OCR/检索门槛，绑定原文—翻译与语言资格，拆分 answer_version/solution_version 唯一权威对象；闭合复核确认无新 P0/P1，v1.1 可冻结 |
| 模型治理补充 | 管理员密钥、用户切换、数据与索引一致性 | 密钥写后不可读且只存 secret_ref；用户仅选已授权生成模型；Embedding/Rerank/OCR 管理员统一评测；补越权、泄露、下架和历史回放负向测试 |
| 数学专项与智能体复核 | 公式作用域、证明 DAG、答案测试、实例不可变、隐藏集、统计判定与论断证据 | 修复 11 项 P1 及翻译引用/隐藏结果 2 项残项；新增 math/evaluation 数据层、typed MathAnswer、答案一致性门禁与 Artifact 打开权限；最终闭合复核无剩余 P0/P1 |

本轮完成的是设计与风险审查，不是代码实现验收。数据库约束、RLS、并发发布、沙箱隔离、模型质量和教学指标仍必须按第 18 章在实现后实测，不能把文档终审通过写成系统已经达标。

## 参考资料

### 产品与教学

- [ClawsGO Science](https://clawsgo.ai/)：科研工作区、代码、图形、运行与产物追溯。
- [ClawsGO Concepts](https://clawsgo.ai/docs/concepts)：知识库、文件系统、工作区、技能与流水线概念。
- [OpenAI：Introducing study mode in ChatGPT](https://openai.com/index/chatgpt-study-mode/)：分步引导、苏格拉底式提问和知识检查。
- [Khanmigo for Teachers](https://www.khanmigo.ai/teachers)：教师备课、题目、量规、活动和学情工具。
- [Khanmigo for Learners](https://www.khanmigo.ai/learners)：引导学生而非只给答案的学习导师。
- [Google：Gemini app features that support students](https://blog.google/products-and-platforms/products/education/gemini-features-for-students/)：Guided Learning、小测和交互图片。
- [Google：Study notebooks in the Gemini app](https://blog.google/innovation-and-ai/products/gemini-app/gemini-study-notebooks/)：诊断、短讲解、后续测验和个性化学习。
- [Google：NotebookLM student features](https://blog.google/technology/google-labs/notebooklm-student-features/)：来源引用、学习指南、闪卡、测验和报告。
- [ALEKS](https://www.aleks.com/k12)：Knowledge Space、初始诊断和个性化学习路径。
- [McGraw Hill：ALEKS FAQ](https://www.mheducation.com/prek-12/support/knowledge/aleks-frequently-asked-questions-faq-for-students.html)：周期性知识检查与保持情况再测。
- [Carnegie Learning：MATHia](https://www.carnegielearning.com/solutions/math/mathia)：技能级自适应反馈和教师报告。
- [Carnegie Learning：LiveLab](https://support.carnegielearning.com/help-center/math/livehint/educators/mathia/livelab/article/livelab-overview/)：教师课堂干预信息。
- [MagicSchool MagicStudent](https://www.magicschool.ai/magicstudent)：教师配置学生 AI 空间和工具。
- [SchoolAI Spaces](https://schoolai.com/products/spaces)：教师指导的学习空间、边界和班级洞察。
- [Squirrel AI：Adaptive Learning Technology](https://squirrelai.com/technology/)：知识图谱、知识追踪、诊断与推荐闭环。
- [雨课堂](https://www.yuketang.cn/)：PowerPoint 场景、课前课中课后和教学互动。
- [超星 AI 赋能智慧课程建设](https://v4.chaoxing.com/teacher/trainingActivity/detailPage/220059?showYxBm=0)：课程知识库、AI 助教、组题、批阅和学情。
- [LoreGraph for Education](https://loregraph.com/education)：上传资料、AI 初稿、教师审核与发布流程。
- [Carpenter、Pan、Butler：Spacing and Retrieval Practice](https://doi.org/10.1038/s44159-022-00089-1)：间隔与检索练习的学习科学综述。

### 教材解析与向量模型

- [Docling 官方文档](https://docling-project.github.io/docling/)：PDF 版面、阅读顺序、表格、公式和 OCR 转换。
- [PaddleOCR-VL 官方文档](https://www.paddleocr.ai/latest/version3.x/algorithm/PaddleOCR-VL/PaddleOCR-VL.html)：多语言文档、公式、表格和扫描件识别。
- [MinerU 官方文档](https://mineru.net/doc/docs/)：PDF 结构、公式和表格解析，用作对照评测。
- [BAAI/BGE-M3 模型卡](https://huggingface.co/BAAI/bge-m3)：多语言、1,024 维 dense 向量与归一化用法。
- [BGE-M3 官方文档](https://bge-model.com/bge/bge_m3.html)：多语言、长文本、dense/sparse/multi-vector 能力。
- [BGE-Reranker-v2 官方文档](https://bge-model.com/bge/bge_reranker_v2.html)：多语言重排模型。
- [Qwen3 Embedding 官方说明](https://qwenlm.github.io/zh/blog/qwen3-embedding/)：多语言、不同参数规模、上下文和可变维度。
- [pgvector 文档](https://github.com/pgvector/pgvector)：HNSW、cosine 距离与查询参数。

### 数学评价与智能体工程

- [STACK：Answer assessment](https://docs.stack-assessment.org/en/Students/Answer_assessment/)：输入校验与数学性质评价分离。
- [STACK：Answer tests](https://docs.stack-assessment.org/en/Topics/Linear_algebra/Answer_tests/)：基于 CAS 的代数等价、矩阵和数值答案测试。
- [WeBWorK：Problem Authoring](https://openwebwork.org/i-am-a-problem-author/)：数学题目、答案检查和可复现题目实例。
- [Gradescope：AI-assisted grading and answer groups](https://guides.gradescope.com/hc/en-us/articles/24838908062093-AI-assisted-grading-and-answer-groups)：答案分组、人工量规复核和改分流程。
- [OpenMath Standard](https://openmath.org/standard/om20-editors-draft/omstd20.html)：数学对象语义与 Content Dictionary。
- [Wolfram|Alpha：Step-by-Step Solutions](https://www.wolframalpha.com/examples/pro-features/step-by-step-solutions)：中间步骤、提示和多方法讲解。
- [GeoGebra](https://www.geogebra.org/)：交互图形、动态几何和数学活动。
- [LangGraph：Checkpointers](https://docs.langchain.com/oss/javascript/langgraph/checkpointers)：持久化、恢复与人工参与工作流。
- [LangGraph：Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)：暂停、审批和恢复执行。
- [Microsoft GraphRAG](https://microsoft.github.io/graphrag/)：实体关系、层级社区和全局/局部检索的适用边界。
- [好未来 MathGPT](https://www.100tal.com/zh-cn/news/detail/1770)：数学识图、解题、批改、讲解和答疑场景。
- [科大讯飞：数学错因与分步骤批改](https://edu.iflytek.com/about-us/news/company-news/2535)：错因标签、步骤诊断和针对性学习。

### 标准、安全与治理

- [1EdTech：QTI 3](https://www.imsglobal.org/spec/qti/v3p0/impl/)：题目、测验和结果交换。
- [1EdTech：LTI](https://standards.1edtech.org/lti/)：学习平台与外部工具互操作。
- [W3C：MathML 4](https://www.w3.org/TR/mathml4/)：数学语义和可访问表达。
- [W3C：WCAG 2.2](https://www.w3.org/TR/WCAG22/)：Web 可访问性要求。
- [Lean：Lake](https://lean-lang.org/doc/reference/latest/Build-Tools-and-Distribution/Lake/)：Lean 项目、依赖和构建工具。
- [NIST：Generative AI Profile](https://www.nist.gov/publications/artificial-intelligence-risk-management-framework-generative-artificial-intelligence)：生成式 AI 风险、隐私和间接注入。
- [OWASP：LLM Prompt Injection Prevention](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html)：不可信内容、工具权限和人工审批。
- [OWASP：AI Agent Security](https://cheatsheetseries.owasp.org/cheatsheets/AI_Agent_Security_Cheat_Sheet.html)：工具滥用、权限提升和数据外泄防护。
- [UNESCO：Guidance for generative AI in education and research](https://unesdoc.unesco.org/ark:/48223/pf0000386693)：人类能动性、隐私、机构验证与教师责任。
