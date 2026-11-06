# 固定卷组卷、在线练习与作答完整设计

> 版本：v1.0  
> 日期：2026-11-04  
> 状态：完整方案，用户审查后冻结  
> 上游文档：`2026-11-03-ai-education-agent-platform-design.md`  
> 配套蓝图：`方案/数学知识库技术蓝图.html` v1.4.0  
> 范围：教师命题蓝图、手工与约束辅助组卷、固定试卷版本、A/B 卷、打印、在线固定卷作答、在线练习、评分反馈、安全和验收。

## 1. 执行摘要与最终决策

本平台采用「**命题蓝图 → 已审核候选实例 → 约束组卷 → 教师整卷审核 → 不可变固定卷 → 打印或在线作答**」链路。固定卷是首期主线；在线练习复用同一试卷、题目、答案、量规和审核链，只使用不同的作答与反馈策略。

| 决策点 | 最终决定 |
|---|---|
| 首期组卷形态 | 固定卷优先，支持整班同卷和预先审核的 A/B 固定卷 |
| 在线练习 | 使用教师批准的固定练习卷或题集，不在学生点击「开始」时临时随机抽题 |
| 组卷方式 | 教师手工可完成全流程；OR-Tools CP-SAT 提供约束候选方案，不是发布按钮 |
| 候选题来源 | 只来自固定 Release Context 中审核有效的题目版本和具体实例；AI 草稿不得进入 |
| 试题修改 | 已审核试卷不可原位修改；改题、调分、调序、改策略均产生新版本或新作业版本 |
| 整卷责任 | 单题审核不替代整卷审核；总分、覆盖、题目互相泄题、版式和反馈策略须整卷确认 |
| 在线作答 | 服务端自动保存、断线重连续答、幂等交卷、服务端截止时间；不等于离线考试 |
| 练习反馈 | 支持即时输入校验、按策略开放正误/提示/解析；练习结果不进入正式成绩 |
| 正式成绩 | 客观题可确定性预评，CAS 无法判定和证明题转人工；教师确认后才发布正式成绩 |
| 试卷输出 | 学生卷、答案卷、量规卷分开渲染、分开授权、分开审计 |
| 数据交换 | QTI 3 作为后续导入/导出边界，不作为内部业务真源；导入内容一律先进入草稿 |
| 首期排除 | 在线监考、锁屏/摄像、自适应 IRT、运行时随机卷、AI 自动发布试卷或成绩 |

核心组卷和作答不依赖 LLM。模型网关不可用时，教师仍能手工组卷、打印，学生仍能保存和提交答案，确定性判题仍能运行。

## 2. 目标与非目标

### 2.1 必须达成的业务目标

1. 教师能按课程、学习目标、章节、概念、题型、难度标称、预计时长、总分和题量建立命题蓝图。
2. 教师能从零手工组卷，也能让约束求解器生成最多 3 个候选方案，再替换、调序、调分。
3. 每一个进入固定卷的题目都可追溯到 Release、题目版本、具体实例、答案、量规和有效人工决策。
4. 教师能预览并分别导出学生卷、答案卷和量规卷，确认页码、公式、图表和分页。
5. 学生能在线完成固定作业卷或练习卷，看到明确保存状态，在断线重连后继续。
6. 教师能配置次数、限时、迟交、提示、正误、解析和成绩开放策略，系统在所有工具与 Artifact 入口一致执行。
7. 系统能防止练习解析提前泄露保密正式卷，防止学生 API、页面、PDF 或日志携带答案。
8. 所有发布、撤回、替换、交卷、评分、改分和导出都可审计、可回放。

### 2.2 明确不做

- 不在首期实现学生开始时临时抽题、无限随机卷或按能力自适应选题。
- 不使用未校准的小样本统计宣称 A/B 卷等值，不把教师难度标签伪装成通过概率。
- 不通过摄像、屏幕监控或锁屏浏览器提供「在线监考」。
- 不让 LLM 自由选题后直接发布，不让 LLM 评分直接成为正式成绩。
- 不承诺 QTI 对其他平台的 CAS、Lean、rubric、策略和数学语义做无损往返。
- 不把断线续答描述为完全离线作答；长期无网络时需要打印卷或学校批准的专门离线方案。

## 3. 外部资料与本地化取舍

| 外部做法 | 可验证能力 | 本平台采用 | 不照搬 |
|---|---|---|---|
| 1EdTech QTI 3 | Test/Section/Item 结构，呈现前 selection 与 ordering，结果交换 | 固定卷结构、导入/导出边界、可访问性 | 不把 QTI XML 当内部审核、版本或成绩真源 |
| Moodle Quiz | 题库选题、随机槽位、自动保存、恢复 attempt、到期处理 | 固定槽位、自动保存、服务端到期策略 | 首期不启用运行时随机槽位 |
| STACK | Deployed Variants、Deferred Feedback、Adaptive Mode、输入校验后再数学评价 | 实例预审、固定卷延迟反馈、练习模式、CAS `fail` 转人工 | 不用单次模板审核代替具体实例审核 |
| Numbas | Question Groups、排序、随机子集、变量覆盖 | 分节与槽位模型、确定性显示配置 | 首期不引入其运行时随机策略 |
| 自动测试组装研究 | 以数学优化满足测试规格并优化目标 | 硬/软约束分离、求解结果解释 | 数据不足时不采用 IRT 信息量目标 |
| OR-Tools CP-SAT | 整数约束与目标求解、明确求解状态 | 候选题选择、约束验证、超时返回可行解 | 不把 `FEASIBLE` 宣称为数学最优 |

外部平台能力只支持设计取舍，不证明本平台已经达到相同可靠性。正式采用前须对锁定版本再次核对官方文档。

## 4. 角色、试卷类型与策略

### 4.1 角色责任

| 角色 | 允许动作 | 明确禁止 |
|---|---|---|
| 任课教师 | 建蓝图、手工/自动组卷、编辑草稿、整卷确认、布置、开放练习、发布成绩 | 使用未审核题、修改已审核卷、查看无权班级保密卷 |
| 指定复核教师 | 对整卷内容、答案、量规、覆盖、版式和策略独立复核 | 复核自己刚修改的内容而不留下贡献者记录 |
| 课程负责人 | 配置正式卷复核人数、撤回试卷、处理重大纠错 | 删除历史试卷、作答或改分证据 |
| 学生 | 在授权时间开始分配给自己的试卷、保存、提交、按策略查看反馈和申诉 | 读取答案卷、量规卷、他人作答、未开放试卷 |
| 建设/审核人员 | 维护题目及内容审核 | 以内容审核身份代替任课教师发布班级试卷 |
| 系统 worker | 执行求解、渲染、到期交卷和确定性预评 | 签署人工审核、修改固定题目、直接发布正式成绩 |

### 4.2 三类使用模式

| 模式 | 试卷内容 | 默认次数 | 默认反馈 | 成绩性质 |
|---|---|---:|---|---|
| 正式固定卷 | 保密、逐卷审核、可打印或在线 | 1 | 教师发布前不给正误、答案或解析 | 教师确认后进入正式成绩 |
| 固定作业卷 | 固定题目、固定版本 | 1，可由教师调整 | 默认提交后或截止后 | 按课程规则进入成绩 |
| 固定练习卷 | 教师公开的练习卷或题集 | 多次或不限 | 即时输入校验；正误、提示、解析按策略 | 非正式学习证据，不写正式成绩 |

同一 `paper_version` 能否同时用于练习与正式评价，受曝光策略限制。已经公开答案的题目或其明确同族变体，不得再进入保密正式卷，除非课程负责人将其重新标为公开题并重新完成整卷风险评估。

## 5. 总体架构与端到端流程

### 5.1 逻辑架构

```text
教师工作台
  ├─ Blueprint Editor：目标、分节、槽位、硬约束、软目标
  ├─ Candidate Explorer：只看固定 Release Context 中的已审核候选
  ├─ Paper Editor：替换、调序、调分、冲突检查、版本 diff
  ├─ Print Preview：学生卷 / 答案卷 / 量规卷
  └─ Review & Publish：整卷审核、作业发布、策略与时间安排
                         │
                         ▼
assessment 应用服务 ─────── 复用 governance 审核/撤回/Release 门禁
  ├─ Assembly Service     → CP-SAT worker（可缺席，手工链路仍可用）
  ├─ Paper Service        → 不可变 paper_version / slot / manifest
  ├─ Attempt Service      → 自动保存、续答、幂等交卷、服务端计时
  ├─ Evaluation Service   → answer_test / CAS / 人工复核 / grade
  └─ Render Service       → 分离渲染、访问控制、导出审计
                         │
                         ▼
学生作答台
  ├─ 固定卷在线作答：延迟反馈、保存、提交、回执
  └─ 固定卷在线练习：按策略检查、提示、解析、再次尝试
```

### 5.2 教师组卷主流程

1. 选择课程/教学班和固定 `release_context_id`。
2. 创建蓝图：设置用途、总分、时长、分节、槽位、硬约束和软目标。
3. 系统生成候选清单及每项内容/审核/许可哈希，先报告候选不足。
4. 教师选择手工挑题，或启动 `assembly_run` 获取最多 3 个候选方案。
5. 教师在草稿卷中替换、调序、调分；每次修改后同步运行整卷校验。
6. 系统生成学生卷、答案卷、量规卷预览，检查公式、图表、分页和敏感字段。
7. 教师提交整卷审核；正式保密卷默认需要任课教师与另一名授权复核教师确认，课程可提高但不能降低到学校门禁以下。
8. 审核通过后生成不可变 `paper_version`。此时仍不代表学生可见。
9. 教师创建 assignment，固定 paper_version、attempt_policy、开放/截止时间和目标学生范围。
10. assignment 发布时再次检查题目审核、许可、撤回、曝光冲突和打印/在线准备状态。
11. 学生按分配开始作答；系统保存、收卷、预评、教师复核和发布成绩。
12. 练习卷的每次提交可投影为学习证据；正式成绩只走 `assessment.grade`。

### 5.3 学生在线作答主流程

```text
开始 attempt
  → 服务端固定 paper_version、assignment_item 和 policy_version
  → 下载仅含题干的学生投影
  → 作答并自动保存 response_revision
  → 断线重连后读取服务端最新版本并处理本地未同步冲突
  → 提交前汇总未答、无效输入和未同步项
  → 幂等提交，生成不可变 submission / submission_item
  → 返回提交回执
  → 按策略预评、人工复核、反馈与成绩开放
```

## 6. 命题蓝图详细规格

### 6.1 蓝图结构

`paper_blueprint_version` 包含以下逻辑部分：

```json
{
  "schema_version": "paper-blueprint/v1",
  "purpose": "formal",
  "course_id": "uuid",
  "offering_id": "uuid",
  "release_context_id": "uuid",
  "total_score": 100.00,
  "duration_seconds": 7200,
  "sections": [
    {
      "code": "A",
      "title": "基础概念",
      "instruction": "本大题共 4 小题。",
      "slots": [
        {
          "code": "A1",
          "score": 5.00,
          "exercise_kinds": ["expression"],
          "required_concept_ids": ["uuid"],
          "learning_outcome_ids": ["uuid"],
          "difficulty_label": "medium",
          "estimated_seconds": 480,
          "mandatory_exercise_version_ids": [],
          "excluded_exercise_version_ids": []
        }
      ]
    }
  ],
  "hard_constraints": {},
  "soft_objectives": {},
  "review_policy": "formal_dual_review"
}
```

实际存储使用分版本 JSON Schema；分节和槽位在生成试卷后规范化为 `paper_section`、`paper_slot`，不依赖 JSON 数组顺序承担业务身份。

### 6.2 候选资格是前置硬门槛

候选集合生成时不进入求解器的对象包括：

- 不在固定 Release Context 或其有效 base 链中的题目；
- 题干、答案、解析、量规、具体实例或必要来源缺少有效人工决策；
- 最新决策为 rejected/revoked，许可失效，或出现在当前撤回集合；
- 参数化题没有固定且已审核的 `exercise_instance_id`；
- 题目所需答案测试、渲染资产、数学约定或语言版本不完整；
- 属于隐藏评测集、学生私人草稿、教师未公开个人草稿；
- 与蓝图 purpose 冲突，例如公开解析题进入保密正式卷；
- 需要云端处理但教材/学生数据许可不允许的题目资产。

候选清单必须保存 `candidate_manifest_hash`，内容覆盖稳定题目身份、题目/实例版本、来源 Release、答案/量规依赖哈希、审核 decision_seq、难度、时长、概念、题型和曝光状态。求解不能读取「当前最新题」。

### 6.3 硬约束

| 类别 | 规则 |
|---|---|
| 槽位完整性 | 每个槽位恰好选择一个合格候选；手工草稿未填满时不能提交审核 |
| 总分与分节分 | 分值统一缩放为整数「分/100」计算；各节及全卷必须精确等于目标 |
| 题量与题型 | 满足每节题数、必考题型和禁止题型 |
| 学习目标与概念覆盖 | 按 `learning_outcome`、`exercise_concept` 角色和权重核验，不只按题目标题 |
| 必选题与排除题 | 必选版本必须出现，排除版本及其指定同族题不得出现 |
| 去重 | 同一稳定题目不得重复；同题干、同族变体、共享刺激材料按冲突组处理 |
| 相互泄题 | 若 A 的题干/解析直接给出 B 的答案或关键步骤，A/B 不得同卷；冲突关系必须可审核 |
| 时长 | 有总时长硬上限时，预计总时长不得超过；缺失时长的题不能按 0 参与 |
| 语言与版本 | 原文/审核翻译必须配对固定；不得以未审核 AI 翻译补齐 |
| 安全与曝光 | 保密题与已公开解析、不同保密窗口、不同班级授权不得冲突 |
| 实例有效性 | 参数化实例的 seed、参数、验证清单和审核在组卷与发布终检均有效 |
| 版式必要条件 | 图表、公式和必需附件必须能渲染；打印作业要求下学生卷渲染失败则阻断 |

硬约束不能由教师点击「仍然发布」绕过。教师必须修改蓝图、补充候选或调整分值后重新组卷。

### 6.4 软目标

| 目标 | 计算方式与边界 |
|---|---|
| 难度分布 | 以教师标称难度的有序等级计算偏差；不是学生答对率 |
| 时长接近目标 | 对预计总时长与目标的偏差计罚；显示估计来源 |
| 认知层次分布 | 按受控 taxonomy 标签与目标分布偏差计罚 |
| 近期使用惩罚 | 根据同一课程/教学班历史使用次数和时间衰减计罚 |
| 曝光均衡 | 在非保密练习中减少少数题反复出现；正式保密卷服从安全约束 |
| 教师偏好 | 对收藏、优先题库或指定来源加权，但不能突破审核与安全门槛 |
| A/B 结构接近 | 比较分节、题型、概念、标称难度和预计时长，不宣称统计等值 |

所有权重使用版本化整数，不使用未固定的浮点隐式权重。求解结果展示每个目标的原始值、偏差和罚分，教师可以理解「为什么选这套题」。

### 6.5 缺失数据处理

- 缺少概念、题型或内容哈希：不能进入正式候选。
- 缺少难度：可以手工组卷，但不能参与「难度分布已满足」的结论；界面标为未知。
- 缺少预计时长：若蓝图启用时长约束则阻断，否则明确显示未知。
- 缺少历史使用量：按新题处理，不虚构为 0 次以外的精确统计。
- 缺少统计难度/区分度：首期不启用 IRT 目标，也不影响固定卷手工组卷。

## 7. 约束组卷算法

### 7.1 计算模型

对每个槽位 `j` 和候选 `i` 定义二元变量 `x[j,i]`。只有预过滤合格的组合才创建变量。

- 每槽位恰选一题：`sum_i x[j,i] = 1`。
- 同题不重复：对每个稳定题目身份，`sum_j x[j,i] <= 1`。
- 分值、概念、题型、时长和覆盖按对应整数系数建立线性约束。
- 互斥题组按组建立 `sum(selected) <= 1`；必选题建立等式。
- 软目标用非负偏差变量表示，目标函数为各偏差乘整数权重之和。

分值缩放到 0.01 分，时间使用秒，避免求解器中直接比较浮点分值。预计时长与难度都是元数据，不是数学真值。

### 7.2 求解过程

1. 固定蓝图、候选清单、求解器版本、配置和随机种子。
2. 执行确定性预检，先给出逐槽位候选数、空槽、总分不可能、覆盖不足和互斥冲突。
3. 使用锁定版本的 OR-Tools CP-SAT 求解；保存 worker、线程、时间上限和随机种子。
4. 需要多个方案时，通过排除已返回解或不同固定种子生成最多 3 个不同 manifest。
5. 每个方案再次由独立整卷校验器检查，不能只相信求解器内部约束。
6. 保存 `OPTIMAL / FEASIBLE / INFEASIBLE / TIMEOUT / FAILED` 状态、目标分解、最优界限/gap（若提供）和诊断。
7. `FEASIBLE` 或超时后得到的可行解必须标注「可行，未证明最优」；没有可行解时不得生成空试卷冒充成功。
8. 教师选中某方案后创建草稿 `paper_version`，之后的手工修改不再继承「求解器已验证」标签，必须重新校验。

### 7.3 不可行与诊断

- 预检报告优先给出可行动原因，例如「A3 要求证明题＋概念 C＋10 分，只有 1 个实例，但需 2 个且禁止重复」。
- `INFEASIBLE` 不自动放松任何约束。系统可在**诊断副本**中逐组临时移除软/硬约束以定位冲突，但报告必须明确这是诊断，不改变原蓝图。
- 建议仅包括「增加候选」「调整该槽位概念范围」「修改题数/分值」「移除互斥要求」等，由教师主动选择后生成新蓝图版本。
- 求解回放以保存的候选清单、配置和结果 manifest 为准；跨求解器版本或硬件不承诺重新求出同一最优解。

### 7.4 A/B 固定卷

1. A/B 卷共享一个蓝图版本和候选清单，但各自生成独立 `paper_version`。
2. 可配置少量共同锚题；锚题必须显式列出，不能靠求解器暗中重复。
3. 非锚题默认禁止稳定题目及同族题重复，避免另一卷直接泄露答案路径。
4. 对分节、概念、题型、标称难度、预计时长和总分做结构对照报告。
5. 每卷分别渲染、分别整卷审核、分别分配；开考前不能由系统临时替换。
6. 学生数据足够后才可做统计等值分析；样本不足只称「结构匹配的 A/B 卷」。

## 8. 教师工作台与交互

### 8.1 蓝图编辑器

- 顶部：课程、班级、Release Context、用途、总分、时长和版本状态。
- 左侧：学习目标、概念覆盖、分节和约束摘要。
- 中部：分节/槽位列表，支持键盘与按钮调序，拖拽不是唯一操作方式。
- 右侧：合格候选、候选不足原因、替换预览和题目证据。
- 硬约束与软目标分栏显示；「未满足硬约束」和「软目标更接近」不得混成一个绿色分数。

### 8.2 试卷编辑器

每次替换显示：

- 新旧题目、答案、量规、概念、难度、时长和来源 diff；
- 替换后总分、覆盖、互斥、曝光和时长变化；
- 是否存在同族题、共享材料或相互泄题；
- 当前候选是否来自同一 Release 和有效审核决策。

提供「撤销本次草稿编辑」，但不提供「撤销审核」按钮。审核撤回走治理流程并留下原因。

### 8.3 整卷审核视图

审核人必须看到完整学生卷，而非只看元数据清单。检查项包括：

1. 题意、答案、解析和量规；
2. 分值、总分、时长、题号和说明；
3. 学习目标/概念覆盖和重复；
4. 题目之间是否泄题，A/B 卷是否符合分配策略；
5. 公式、图表、分页、字体和可访问性；
6. 学生卷是否含答案，答案/量规是否只在授权版本；
7. 在线策略是否正确，包括次数、时间、提示、解析和成绩开放；
8. 所有依赖审核、许可和撤回状态。

批量「全选通过」不适用于整卷审核。审核意见与内容修改分离，任何进入 manifest 的修改都使原审核失效。

## 9. 版本、审核、发布与纠错

### 9.1 状态机

```text
paper_blueprint_version:
  draft → validated → used
  修改已使用蓝图 = 新建版本

assembly_run:
  queued → running → optimal / feasible / infeasible / timeout / failed / cancelled

paper_version:
  draft → in_review → approved → withdrawn
             └→ changes_requested → draft（新草稿修订，不覆盖审核事实）

assignment:
  draft → scheduled → published → closed / withdrawn

paper_attempt:
  in_progress → finalizing → submitted → grading → graded → returned
                    └→ closed_unsubmitted
  任意未终态阶段可因严重撤回进入 withdrawn，并保留原因
```

`approved` 表示可分配给教师创建作业，不表示学生已经可见。学生可见性只由 assignment 发布时间、范围、当前撤回状态和策略决定。

### 9.2 不可变边界

- `paper_version` 的 manifest 覆盖蓝图、分节、槽位、题目/实例、来源 Release、答案/量规依赖、整卷策略摘要和审核清单版本。
- 已 approved 的 paper_version 不允许 UPDATE 题目、分值、顺序或内容哈希。
- assignment 固定 paper_version 和 attempt_policy_version；开始后不能改换纸张或策略。
- `assignment_item` 在 assignment 发布事务中从 paper_slot 生成并固定 `(release_context_id, source_release_id, exercise_version_id, exercise_instance_id)`。
- 学生 attempt 固定 assignment_item 集合；之后重新组卷、重新随机或发布新 Release 均不影响它。
- 打印渲染有独立 `paper_render`，但学生卷、答案卷和量规卷必须引用同一 paper_version。

### 9.3 普通纠错

- 尚未开始的 assignment：教师创建新 paper_version，重新审核后显式切换 assignment 草稿；已发布未开始的切换须通知并留下变更事件。
- 已有 attempt 的 assignment：不得换题或调分。教师可关闭后续新 attempt，并为后续学生创建新 assignment。
- 需要重评时，使用新答案/量规版本创建 regrade run，逐题展示差异，教师确认后生成新 grade revision。

### 9.4 严重撤回

- 立即阻止新 attempt、学生卷下载和答案/Artifact 访问。
- 进行中的 attempt 不再显示被撤销题；保存已有学生证据并通知教师。
- 教师选择取消该题或关闭整份作业；任何分数处理必须有明确理由和学生可见说明。
- 不允许系统静默删除该题后按原总分计算，也不允许用另一题无提示替换。
- 撤回事件与历史 manifest 保留，特权审计回放只读并标记「历史版本」。

## 10. 数据模型

以下均为逻辑契约，正式实施以 Alembic migration 为准。内部 ID 使用 UUIDv7，时间使用 TIMESTAMPTZ，哈希使用 canonical JSON + SHA-256。

### 10.1 `assessment.paper_blueprint_version`

| 字段 | 约束与含义 |
|---|---|
| `id / blueprint_id / version_no` | PK、稳定蓝图身份、版本号唯一 |
| `course_id / offering_id` | 课程必填；正式班级卷要求 offering |
| `release_context_id` | 固定内容、索引与检索配置上下文 |
| `purpose` | `formal / coursework / practice` |
| `assembly_mode` | `manual / constraint_assisted` |
| `total_score / duration_seconds` | 精确总分与目标时长 |
| `blueprint_ast` | 分节、槽位、硬约束、软目标、复核策略，受 JSON Schema 约束 |
| `base_version_id` | 同蓝图的可空编辑基线 |
| `content_hash / change_set_id` | 完整蓝图哈希与建设批次 |

### 10.2 `assessment.assembly_run`

| 字段 | 约束与含义 |
|---|---|
| `id / blueprint_version_id` | 一次组卷运行及其固定输入 |
| `candidate_manifest / candidate_manifest_hash` | 候选与审核/许可/版本快照 |
| `solver_name / solver_version / solver_config` | 求解器、版本、种子、线程和时限 |
| `status` | `queued / running / optimal / feasible / infeasible / timeout / failed / cancelled` |
| `solution_manifest` | 最多 3 个方案，每方案含槽位选择与目标分解 |
| `objective_breakdown` | 各软目标原值、偏差和罚分 |
| `diagnostics` | 候选不足、不可行原因和诊断副本结果 |
| `best_bound / optimality_gap` | 求解器提供时保存，否则为空并说明 |
| `job_id / started_at / completed_at` | 可靠异步执行与时间 |

同一输入可以按幂等键复用结果；需要不同方案时使用新种子或明确排除已有解，不能覆盖旧运行。

### 10.3 `assessment.paper_version`

| 字段 | 约束与含义 |
|---|---|
| `id / paper_id / version_no` | 固定试卷版本身份 |
| `blueprint_version_id` | 必填；手工卷也有手工蓝图 |
| `assembly_run_id / selected_solution_index` | 约束组卷时必填，手工卷为空 |
| `base_paper_version_id` | 克隆或纠错基线 |
| `course_id / offering_id / release_context_id` | 与蓝图一致并校验范围 |
| `paper_kind / variant_group_id / variant_label` | 正式/作业/练习及 A/B 标识 |
| `total_score / duration_seconds` | 与槽位汇总一致 |
| `status` | `draft / in_review / approved / withdrawn` |
| `manifest_hash` | 全卷不可变清单 |
| `review_manifest_hash` | 整卷人工决策清单哈希；正式双审包含两名授权复核者的有效 decision_seq，发布终检重新验证最新状态 |
| `created_by / created_at / approved_at` | 责任与时间 |

### 10.4 `assessment.paper_section`

字段：`id`、`paper_version_id`、`code`、`title`、`instruction`、`sort_order`、`section_score`、`display_config`、`content_hash`。`(paper_version_id, code)` 与 `(paper_version_id, sort_order)` 唯一，分节分等于槽位分之和。

### 10.5 `assessment.paper_slot`

| 字段 | 约束与含义 |
|---|---|
| `id / section_id / slot_no` | 固定槽位及顺序 |
| `exercise_version_id` | 固定题干版本 |
| `exercise_instance_id` | 参数化题必填，静态题为空 |
| `source_release_id` | 必须属于 paper release context 的有效来源 |
| `score / estimated_seconds` | 槽位权威分值与预计时间 |
| `display_config` | 语言配对、选项顺序、图表和打印设置；不得含随机运行时规则 |
| `dependency_manifest_hash` | 答案、解析、量规、测试、约定和资产依赖 |
| `content_hash` | 槽位与完整依赖哈希 |

同一 paper_version 中稳定题目身份不得重复；确需多个子问共享公共材料时，应建模为一道含子问的题，而不是复制同一题绕过约束。

### 10.6 `assessment.attempt_policy_version`

| 字段组 | 内容 |
|---|---|
| 身份 | `id / policy_id / version_no / policy_kind / content_hash` |
| 次数 | `max_attempts`，NULL 仅练习可表示不限 |
| 行为 | `deferred / adaptive_practice` |
| 时间 | 是否限时、服务端宽限策略、迟交处理；具体开放/截止时间由 assignment 固定 |
| 校验 | 是否展示语法错误、数学解释和输入预览 |
| 反馈 | 正误、分数、提示、解析、答案分别在何时开放 |
| 练习 | 是否允许逐题检查、提示级别、再次尝试和练习罚分 |
| 正式评价 | 是否允许标记、回看、上传附件；不包含监考承诺 |
| 保存 | 自动保存 debounce、最大保存间隔和冲突提示策略 |

### 10.7 `assessment.paper_attempt`

字段：`id`、`assignment_id`、`paper_version_id`、`student_id`、`attempt_no`、`attempt_policy_version_id`、`mode`、`status`、`started_at`、`server_deadline_at`、`finalizing_at`、`submitted_at`、`last_server_revision`、`response_manifest_hash`、`close_reason`、`created_at`。

`(assignment_id, student_id, attempt_no)` 唯一。开始、重连和提交均重新检查学生分配、当前撤回和 attempt 状态。

### 10.8 `assessment.response_revision`

| 字段 | 约束与含义 |
|---|---|
| `id / attempt_id / paper_slot_id` | 某槽位的一次传输修订 |
| `client_revision_id` | 客户端生成的 UUID，与 attempt 联合唯一，用于幂等 |
| `base_revision_no / revision_no` | 乐观并发与服务端单调序号；`(attempt_id, paper_slot_id, revision_no)` 唯一 |
| `raw_response` | 学生原始输入，按题型 JSON Schema |
| `normalized_response / parse_status` | 受控规范化及 `valid / invalid / unsupported` |
| `client_sent_at / server_received_at` | 客户端时间仅展示，截止判断使用服务端时间 |
| `response_hash` | 原始与规范化响应哈希 |

自动保存修订不是最终提交，也不直接成为学习掌握度证据。最终 `submission_item` 固定具体 revision；练习逐题检查则投影为 `learning.exercise_attempt`。

### 10.9 对现有 assignment/submission/grade 的扩展

- `assignment` 增加 `paper_version_id`、`attempt_policy_version_id`、`visible_at`、`due_at`、`hard_close_at`、`distribution_kind`；发布校验 `visible_at <= due_at <= hard_close_at`，且 paper、policy、目标学生与 course/offering 范围一致。
- `assignment_item` 增加 `paper_slot_id`，保留既有 Release、来源和实例字段；发布时从 paper_slot 事务性生成。
- `submission` 增加 `attempt_id`、`submission_reason`、`response_manifest_hash`、`finalized_at`；`attempt_id` 唯一，重复交卷返回同一 submission。
- `submission_item` 固定 `assignment_item_id`、`response_revision_id`、`rubric_version_id`、`answer_test_run_id`、`evaluation_status`、`suggested_score` 和 `requires_human`。教师正式分仍只在 `grade`。
- `grade` 采用追加 revision，记录前次成绩、原因、操作者和学生申诉关联，不覆盖旧正式分。
- `learning.exercise_attempt` 增加来源 `response_revision_id` 与 `paper_attempt_id`；它是学习分析投影，不是交卷或正式成绩真源。

### 10.10 `assessment.paper_render`

字段：`id`、`paper_version_id`、`render_kind`、`template_version`、`language_code`、`artifact_version_id`、`renderer_version`、`font_manifest_hash`、`security_manifest_hash`、`output_hash`、`status`、`created_by / created_at`。

`render_kind = student / answer / rubric`。学生渲染的安全 manifest 只能引用题干和公开资产；答案、解析、量规、answer_test 和隐藏批注不得进入其输入。

### 10.11 `assessment.item_exposure_event`

字段：`id`、`exercise_version_id`、`exercise_instance_id`、`exercise_family_id`、`paper_version_id`、`assignment_id`、`scope_offering_id`、`event_kind`、`visible_from`、`occurred_at`、`metadata_hash`。

事件至少包括 `secure_assigned / student_opened / practice_answer_released / formal_answer_released / withdrawn`。发布练习解析和正式卷时必须在同一事务写入相应事件，用于阻断「练习已公开答案，正式卷仍保密使用」的冲突。

### 10.12 复用而非新建的对象

- 单题内容审核：`governance.review_decision_item`。
- 整卷审核：同一审核模型，`entity_type=paper_version`，依赖 manifest 包含所有槽位。
- 紧急撤回：`governance.revocation_event`。
- 异步任务：`ops.job / job_attempt / outbox_event / idempotency_key`。
- PDF 等文件：`agent.artifact_version` 负责存储与访问策略，`paper_render` 保存组卷领域关系。
- 题目、答案、量规和实例：复用 `exercise` 领域，不复制第二套内容真源。

## 11. 在线保存、续答与交卷

### 11.1 开始与重连

开始 attempt 的数据库事务必须：

1. 校验 assignment 已发布、学生属于分配范围、开放时间有效、无当前撤回。
2. 校验 paper_version 已 approved，所有 assignment_item 和依赖仍有效。
3. 校验次数并生成 attempt_no、policy_version 和服务端 deadline。
4. 固定题目集合；响应只包含学生投影，不序列化答案、解析、量规或 answer_test。

同一 assignment＋student 已有 `in_progress/finalizing` attempt 时，开始接口返回该进行中的 attempt；即使客户端换了幂等键，也不能因此消耗新次数。只有旧 attempt 已终态且剩余次数允许时才创建新 attempt。

重连返回服务端各槽位最新 revision 和状态。客户端有未同步内容时按槽位显示冲突，不允许整卷静默覆盖。

### 11.2 自动保存

- 输入变化后使用短 debounce；持续输入时也有最大保存间隔，不能直到交卷才发送。
- 保存请求携带 `attempt_id + paper_slot_id + client_revision_id + base_revision_no`。
- 相同 client_revision_id 重试返回原结果，不生成重复 revision。
- base_revision 过期时返回 409 和服务端版本；学生可选择载入最新或复制本人未同步内容后重新保存。
- 页面明确显示「正在保存 / 已保存至服务器 / 离线未同步 / 冲突 / 已关闭」。
- `beforeunload` 只作提醒，不能作为保存可靠性的唯一保障。
- 断线期间仅承诺有限重试与重连续答；共享设备上的本地缓存默认关闭，若课程启用须提交后清除。

### 11.3 最终提交事务

1. 对 attempt 行 `SELECT ... FOR UPDATE`。
2. 若已有 submission，直接返回相同回执，保证幂等。
3. 若 withdrawn/closed，按策略返回现有状态，不再接受新答案。
4. 依据服务端接收时间、deadline 和 late policy 选择每槽位最后一个合格 revision。
5. 固定所有 `submission_item`、原始响应、parse 状态和 response manifest。
6. 创建 submission，更新 attempt 状态，写入 outbox 预评任务和审计事件。
7. 提交后到达的保存请求标记为迟到并拒绝写入快照，不能改变成绩输入。
8. 返回 submission ID、服务端提交时间、题目数量、未答/无效输入数量和回执哈希。

### 11.4 到期与竞态

- 客户端倒计时只作展示，服务端 deadline 是唯一权威。
- 支持三种教师策略：到期自动提交、只读宽限后自动提交、逾期拒收；正式卷默认自动提交。
- 到期 worker 与手动提交使用同一 finalize 应用服务和幂等键。
- 以 `server_received_at <= effective_deadline` 为可进入快照的闭区间边界；之后的 revision 不进入，宽限期是否接受新输入由策略明确控制。
- 服务短暂不可用后 worker 重试，不得生成重复 submission。
- 自动评阅不在提交事务内等待；提交成功不等于评阅完成。

## 12. 练习、评分与反馈

### 12.1 策略矩阵

| 能力 | 正式固定卷 | 固定作业卷 | 固定练习卷 |
|---|---|---|---|
| 输入语法校验 | 可按策略显示，不透露正误 | 默认显示 | 默认显示 |
| 即时正误 | 默认关闭 | 默认关闭或提交后 | 可开启 |
| 分步提示 | 默认关闭 | 教师限定 | 可分级开放 |
| 完整解析 | 教师发布后 | 截止/教师发布后 | 按策略，可在有效作答后 |
| 多次尝试 | 默认不允许 | 教师限定 | 允许 |
| 自动预评 | 可用，教师确认前不公开 | 可用 | 可用 |
| 正式成绩 | 教师确认发布 | 教师确认发布 | 不生成 |

「有效作答后显示解析」只是教学策略，不是安全边界。保密正式卷不依赖该条件，而由时间、曝光和教师发布状态强制控制。

### 12.2 练习逐题检查

- 检查请求绑定具体 response_revision，避免学生已改答案后看到旧输入的反馈却误以为属于新输入。
- 先校验输入，再运行 answer_test；无效输入不能当成数学错误。
- `true / false / fail` 分开；`fail`、超时、定义域不确定和引擎异常均转「暂不能判定」。
- 每次明确检查生成不可变 `learning.exercise_attempt`，保存题目/实例、response revision、引擎版本和反馈策略。
- 自动保存本身不触发正误检查，不消耗练习次数。
- 练习提示和解析的 Artifact 同样在打开时重新判定，不可通过旧链接绕过。

### 12.3 正式评分

1. 客观题、数值题和表达式题运行固定 answer_test/CAS。
2. 所有自动结果保存输入哈希、引擎版本、假设、分支和状态。
3. 证明、开放题、CAS fail 和低置信度 OCR 进入教师复核。
4. AI 可建议分组或错因，但不能直接写正式 grade。
5. 教师使用量规逐题或按已确认组评分；改分创建新 revision。
6. 成绩发布是独立动作，重新检查复核完整性和当前策略。
7. 学生看到成绩时同时看到题目版本、量规和申诉入口；申诉不修改原提交。

## 13. 打印、渲染与 QTI

### 13.1 三类渲染

| 渲染类型 | 内容 | 权限 |
|---|---|---|
| 学生卷 | 题干、公共材料、作答区域、必要说明、页码和版本短码 | 仅授权教师可考前导出；学生在线开放不等于可下载 |
| 答案卷 | 学生卷内容＋权威答案＋解析 | 独立保密权限，导出审计 |
| 量规卷 | 题目、答案、步骤分、可接受解法和扣分规则 | 教师/复核教师权限 |

- 学生卷渲染输入在数据层即排除答案字段，不能生成后再用 CSS 隐藏。
- PDF 文本层、注释、附件、元数据和隐藏图层均不得含答案。
- 公式保留可缩放渲染，图表有替代文本；检查跨页、裁切、空白页、孤立题号和长证明分页。
- 页眉显示课程、版本短码和页码；姓名/学号字段只在教师选择时出现。
- 导出记录操作者、时间、render_kind、output_hash 和下载范围；保密卷可加水印与副本编号。
- 打印版式或字体变化产生新 render，不必重审数学内容；但若人工预览发现题干被截断，必须阻断该渲染发布。

### 13.2 QTI 边界

- 后续导入支持 Test/Section/Item 基本结构和可识别题型。
- 导入后全部成为新草稿，外部「已审核」「成绩」「发布」状态不继承。
- 不能识别的 response processing、CAS、rubric、扩展和数学语义必须进入导入差异报告，不能静默丢弃。
- 导出以固定 paper_version 为输入，保持槽位与顺序；本平台特有策略放入明确命名的扩展或旁路 manifest。
- QTI 导入/导出不是绕过 Release、许可、人工审核和曝光检查的通道。

## 14. 安全、隐私与防泄露

### 14.1 学生投影白名单

学生 API 使用独立 DTO，只包含：槽位 ID、题号、题干、公开资产、允许输入类型、当前学生响应、保存状态和策略允许的反馈。

禁止通过以下路径泄露答案：ORM 全对象序列化、GraphQL 任意字段、页面预取、Service Worker 缓存、SSE、PDF 隐藏层、浏览器源码、错误堆栈、分析事件、普通日志和可预测 Artifact URL。

### 14.2 保密卷与曝光

- 保密 paper_version、学生卷和答案卷启用 RLS/FORCE RLS，普通学生数据库角色无权读取。
- 练习答案发布、正式卷分配和答案开放写入 item_exposure_event。
- 候选预过滤与 assignment 发布终检都执行曝光冲突检查。
- 同族变体是否构成泄题由授权教师确认；模型相似度只能提示，不能自动判定安全。
- 不同保密窗口的班级不得因为「同一课程」自动共享题目。

### 14.3 内容与输入安全

- 题干、LaTeX、MathML、图片说明和上传附件均按不可信内容处理，防止存储型 XSS 和渲染注入。
- 求解器只读取规范化元数据，不执行题干中的代码或指令。
- CAS/代码题继续使用隔离沙箱；组卷 worker 无答案发布权限。
- 响应 revision 属于学生评价数据，教师只能访问其授权班级；普通学习分析使用最小必要投影。
- 中间自动保存修订按学校保留政策限期保存；最终提交和成绩按正式记录政策保存，二者不能混为无限期保留。

### 14.4 高影响动作审计

审计覆盖：蓝图版本、求解输入输出、整卷审核、assignment 发布、学生卷/答案卷导出、曝光事件、开始/交卷、自动收卷、预评、人工评分、成绩发布、改分、撤回和申诉。

审计日志不记录完整答案正文或密钥；需要内容证据时引用受权限保护的版本和哈希。

## 15. API 契约

所有写接口要求身份、范围授权和 Idempotency-Key；草稿编辑另要求 base_version。以下路径表达资源边界，实际路由可沿用项目前缀。

### 15.1 蓝图与组卷

| 方法与路径 | 作用 |
|---|---|
| `POST /paper-blueprints/{id}/versions` | 创建新蓝图版本 |
| `POST /paper-blueprint-versions/{id}/validate` | 候选预检和约束校验 |
| `POST /assembly-runs` | 固定蓝图和候选清单，启动求解 |
| `GET /assembly-runs/{id}` | 查询状态、方案和诊断 |
| `POST /paper-versions` | 从手工蓝图或选中方案创建草稿卷 |
| `PATCH /paper-versions/{id}/slots` | 草稿替换、调序、调分，带 base_version |
| `POST /paper-versions/{id}/validate` | 独立整卷校验 |
| `POST /paper-versions/{id}/submit-review` | 提交整卷审核 |
| `POST /paper-versions/{id}/approve` | 授权人类确认，worker 不可调用 |
| `POST /paper-versions/{id}/renders` | 创建学生/答案/量规渲染 |

### 15.2 作业与作答

| 方法与路径 | 作用 |
|---|---|
| `POST /assignments` | 固定 paper、policy、范围和时间 |
| `POST /assignments/{id}/publish` | 发布终检并生成 assignment_item |
| `POST /assignments/{id}/attempts` | 开始或按幂等键恢复原请求 |
| `GET /attempts/{id}` | 返回学生安全投影和服务端状态 |
| `PUT /attempts/{id}/responses/{slotId}` | 幂等自动保存，带 base_revision |
| `POST /attempts/{id}/checks/{slotId}` | 练习逐题检查，绑定 response_revision |
| `POST /attempts/{id}/submit` | 幂等最终提交 |
| `GET /submissions/{id}` | 按反馈策略返回提交与结果 |
| `POST /submissions/{id}/regrade-requests` | 学生申诉 |
| `POST /submissions/{id}/grades` | 教师确认成绩或新 revision |

### 15.3 统一错误语义

- `403 policy_denied`：无范围权限、时间未开放或反馈策略拒绝。
- `404 resource_not_found`：避免向无权用户泄露保密资源是否存在。
- `409 stale_version / response_conflict / attempt_finalized`：编辑或响应冲突。
- `410 withdrawn`：内容已撤回，按策略只保留历史回执。
- `422 blueprint_invalid / response_invalid / hard_constraint_violation`：结构或硬约束失败。
- `503 evaluation_unavailable`：预评暂不可用；提交本身已成功时不得返回「提交失败」。

## 16. 异常与边界情况

| 场景 | 系统行为 |
|---|---|
| 某槽位无候选 | 组卷前显示缺口，不启动无意义求解 |
| 约束不可行 | 返回诊断，不自动降低硬约束 |
| 求解超时但有可行解 | 可保存候选，标记未证明最优 |
| 求解超时且无可行解 | 不生成 paper，保留诊断和重试入口 |
| 教师手工改坏总分 | 草稿可保存，提交审核被阻断 |
| 审核后题目内容变化 | 依赖哈希不匹配，整卷审核失效 |
| 发布与题目撤销并发 | 使用同一范围锁和发布终检，撤销优先隔离 |
| A/B 卷候选重叠 | 除明确锚题外阻断 |
| 学生重复点击开始 | 相同幂等键返回同一 attempt，不消耗额外次数 |
| 多标签页同时编辑同题 | base revision 冲突，禁止静默最后写入覆盖 |
| 保存与自动交卷竞态 | attempt 行锁＋服务端时间，快照只取合格 revision |
| 重复提交或 worker 重试 | 返回同一 submission，不重复预评副作用 |
| CAS 不可用 | 提交保留，预评排队并转人工，不打 0 分 |
| 练习检查旧响应 | 结果标明对应 revision，不覆盖新输入 |
| 学生空答或无效输入 | 保留原样并计入提交摘要；不能静默删除 |
| 模型网关故障 | 不影响固定卷、保存、交卷和确定性评价 |
| 渲染失败 | 在线模式可保留草稿；要求打印的 assignment 不得发布 |
| 练习解析与正式卷冲突 | 发布终检阻断，记录曝光冲突 |
| 正式卷中途严重撤回 | 禁止新 attempt，冻结相关题，教师决定取消/关闭并通知 |
| 重评期间学生查看旧成绩 | 标明成绩状态和版本；新成绩经教师确认后另发 revision |

## 17. 可观测性与运维

### 17.1 核心指标

- 组卷：候选数、预检失败原因、求解状态、耗时、目标偏差、手工替换率、整卷退回率。
- 渲染：学生/答案/量规成功率、分页问题、敏感信息扫描结果。
- 作答：保存成功率、保存延迟、冲突率、未同步响应、重连恢复、自动收卷成功率。
- 评阅：CAS fail、转人工量、教师改分率、成绩发布延迟、申诉量。
- 安全：越权拒绝、曝光冲突、答案字段扫描、保密导出和撤回阻断。
- 学习：只使用允许的学习投影，不通过读取全部中间按键做隐蔽监控。

### 17.2 告警与降级

- 到期未 finalized 的 attempt 超过阈值立即告警。
- outbox 预评积压不影响交卷回执，但需通知教师评阅延迟。
- 自动保存连续失败时在学生端显著提示，并允许按策略导出本人原始文本作为补救；正式保密卷不得导出整卷题干。
- 求解器故障自动降级为手工组卷，不重试成重复 paper。
- 渲染故障不影响已保存作答；答案发布前必须恢复必要渲染与权限检查。

### 17.3 初始压测负载

以下是验收负载，不是当前性能成绩：

- 单蓝图 1,000 个候选、50 个槽位、包含互斥和覆盖约束；求解允许受控超时并返回明确状态。
- 单班 100 名并发学生同时开始、保存和交卷；到期 worker 与学生手动提交同时发生。
- 每槽位多次 revision、重复请求、乱序重试和多标签页冲突。
- 学生卷含长公式、跨页证明、图表和中英混排；检查渲染完整性与敏感字段。

具体 P95/SLO 在校内生产 staging 以目标硬件冻结，不能沿用 R720 开发机结果作生产承诺。

## 18. 验收测试

### 18.1 组卷正确性

1. 手工建立 100 分固定卷，槽位、分节与总分精确一致。
2. 自动方案与手工修改经过同一个独立整卷校验器。
3. 对所有测试 fixture，已发布卷硬约束违反数为 0。
4. 未审核、审核撤销、许可失效、隐藏评测和私人草稿题均不能进入候选。
5. 参数化题固定 exercise_instance、seed、题干和验证清单。
6. 相同输入回放读取相同结果 manifest；重跑求解器跨版本不被错误宣称为同一结果。
7. `INFEASIBLE`、`TIMEOUT` 和 `FEASIBLE` 的界面及 API 语义正确。
8. A/B 卷除锚题外无禁止重叠，每卷有独立审核与 manifest。
9. 难度/时长缺失时显示未知，不按 0 或精确统计值处理。

### 18.2 版本与治理

10. approved paper 的题目、分值、顺序、策略摘要和依赖均不可原位修改。
11. 单题审核通过但整卷未审核时，学生不可见。
12. assignment 发布后生成固定 assignment_item；新 Release 不改变进行中 attempt。
13. 改题产生新版本，历史 attempt、submission、grade 和 render 保持可回放。
14. 严重撤回立即阻断新 attempt 和新下载，历史证据仍可限权审计。
15. 整卷审核人修改内容后，必须按贡献者规则重新复核。

### 18.3 在线作答

16. 自动保存重试只生成一个 revision，服务端保存状态可见。
17. 断线重连后恢复服务器响应；本地未同步冲突不被静默覆盖。
18. 多标签页 base revision 冲突返回 409，并有可解决界面。
19. 手动交卷与到期 worker 并发只产生一个 submission。
20. 截止前后边界、只读宽限、拒收策略和空卷提交均按政策执行。
21. 提交后保存请求不能修改 submission_item 或成绩输入。
22. 无效数学输入保留原文和 parse_status，不被当作空答静默丢弃。
23. 练习逐题检查绑定具体 revision，自动保存不消耗检查次数。
24. 练习结果不写正式 grade；正式成绩教师确认发布率 100%。

### 18.4 安全与防泄露

25. 学生 API、HTML、JS 预取、SSE、PDF 文本层、元数据和日志均无答案/量规隐藏副本。
26. 学生篡改 paper、assignment、attempt、slot 和 artifact ID 均不能越权。
27. 练习答案发布后，冲突题进入保密正式卷被阻断。
28. 签名 URL 续期、旧练习 Artifact 和重新打开页面均重新执行反馈策略。
29. 求解器不执行题干中的指令，公式/附件渲染通过 XSS 与恶意文件测试。
30. 非 owner、非 superuser 数据库角色执行 RLS 负向测试，不能读取保密卷。

### 18.5 可用性与可访问性

31. 教师不用拖拽也能完成替换、调序、调分和审核。
32. 学生可全程键盘作答，公式有可读预览，屏幕阅读器能识别题号、状态和错误。
33. 保存失败、响应冲突、未答和无效输入使用明确文本，不只靠颜色。
34. 学生卷、答案卷和量规卷在目标浏览器及打印 PDF 中无公式裁切、乱码和错误分页。
35. 试点教师能独立完成「蓝图—组卷—审核—发布—撤回」，学生能完成「开始—断线—续答—提交—查看回执」。

## 19. 分期交付

### 阶段 A：手工固定卷与打印

- 蓝图、候选资格、paper_version/section/slot、手工编辑、整卷审核。
- assignment 固定映射和学生/答案/量规分离渲染。
- 验收：一门课完成真实 100 分固定卷，版本、审核、打印和撤回全部通过。

### 阶段 B：约束辅助组卷与 A/B 卷

- 候选清单、CP-SAT、诊断、目标解释、方案比较、曝光事件。
- 验收：约束与不可行测试通过，A/B 固定卷独立审核，无运行时抽题。

### 阶段 C：固定卷在线作答

- attempt policy、自动保存、重连、冲突、幂等交卷、到期 worker、提交回执。
- 验收：100 并发压测负载、截止竞态、撤回和故障恢复通过。

### 阶段 D：在线练习与评分闭环

- 逐题检查、分级提示、反馈策略、学习投影、人工评分和成绩发布。
- 验收：正式/作业/练习策略矩阵、CAS fail、改分、申诉和防泄露通过。

### 阶段 E：互操作与统计增强

- QTI 导入/导出、跨课程受控复用、真实使用数据积累。
- 只有在样本量、测量质量和课程责任明确后，才另行设计 IRT、随机平行卷或自适应组卷。

各阶段都可独立停止，不要求用后续随机功能证明前期固定卷价值。

## 20. 未来演进触发器

| 新能力 | 必须满足的触发条件 |
|---|---|
| 运行时随机练习卷 | 有足够已审核实例、曝光策略、组合约束和故障恢复；只先用于非正式练习 |
| 随机正式平行卷 | 所有实际变体可预生成、可审核、结构匹配，且学校接受相应考试治理 |
| IRT/统计等值 | 有足够的独立作答数据、样本量与偏差报告、心理测量专家参与 |
| 在线监考 | 单独完成合法性、必要性、学生告知、数据保护、可访问性和供应商评估 |
| 跨平台完整 QTI | 明确支持的题型与扩展清单，导入差异和往返测试通过 |

这些能力不是当前方案的隐含承诺。

## 21. 与上游设计的同步要求

冻结本方案后，需要同步更新：

1. `方案/数学知识库技术蓝图.html`：新增「组卷与在线作答」页面及核心表。
2. `2026-11-03-ai-education-agent-platform-design.md`：补充 paper/attempt 状态机、曝光事件和 response revision。
3. 数据表注册：`paper_version` 纳入受控 `entity_type`，支持整卷审核与撤回。
4. Artifact 协议：新增 `Paper`、`PaperBlueprint`、`AttemptReceipt`，练习反馈继续复用 ExerciseSet/HomeworkReview。
5. 权限字典：增加保密卷读取、整卷审核、答案卷导出、成绩发布和撤回权限。
6. 实施计划：按阶段 A—D 编写任务，不先实现后续 IRT 或在线监考。

## 22. 参考资料

### 标准与平台

- [1EdTech QTI 规范中心](https://www.imsglobal.org/question/index.html)
- [1EdTech QTI 3 实施指南](https://www.imsglobal.org/spec/qti/v3p0/impl)
- [Moodle Building Quiz](https://docs.moodle.org/500/en/mod/quiz/edit)
- [Moodle Quiz Preview 与自动保存](https://docs.moodle.org/500/en/Quiz_preview)
- [Moodle Quiz Timing](https://docs.moodle.org/310/en/mod/quiz/timing)
- [STACK Authoring Quick Start：Quiz Behaviour](https://docs.stack-assessment.org/en/AbInitio/Authoring_quick_start_9/)
- [STACK Randomisation 与 Deployed Variants](https://docs.stack-assessment.org/en/AbInitio/Authoring_quick_start_4/)
- [STACK Answer Tests](https://docs.stack-assessment.org/en/Authoring/Answer_Tests/)
- [Numbas Exams](https://docs.numbas.org.uk/en/latest/exam/reference.html)

### 组卷与求解

- [OR-Tools CP-SAT Solver](https://developers.google.cn/optimization/cp/cp_solver?hl=en)
- [OR-Tools Constraint Optimization](https://developers.google.cn/optimization/cp?hl=en)
- [Robust Automated Test Assembly for Testlet-Based Tests（2017）](https://www.frontiersin.org/articles/10.3389/feduc.2017.00063/full)

## 23. 设计自检结论

- **范围一致：** 固定卷优先，同时包含学生在线练习和作答；未扩张为随机正式考试或在线监考。
- **版本一致：** 蓝图、题目、实例、整卷、assignment、attempt、submission、grade 各有明确版本边界。
- **数据唯一：** 题目/答案复用 exercise，正式成绩复用 grade，学习记录是投影，没有第二业务真源。
- **审核一致：** 单题审核与整卷审核分离，作业发布与成绩发布分离，AI/worker 无人工签署权。
- **异常闭合：** 不可行、超时、冲突、断线、到期竞态、CAS 失败、撤回和重评均有确定行为。
- **安全闭合：** 练习到正式卷的曝光冲突、学生投影、渲染隐藏答案、Artifact 续期和 RLS 均有负向验收。
- **实施可行：** 手工固定卷可先于求解器交付，核心链路不依赖 LLM，后续能力有明确进入条件。
