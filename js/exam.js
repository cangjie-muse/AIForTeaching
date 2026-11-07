/* 考试工作台（新的命题与考试入口，按设计文档 §4.2 / §5.2 的流程组织）。
 *
 * 为什么单开一页：旧界面把"组卷"塞在课程菜单里，教师看不到完整链条——
 * 蓝图 → 候选 → 拼卷 → 审核 → 作业 → 作答 → 评分 → 申诉。
 * 这一页按**流程顺序**列出七步，每步给出当前状态与入口；没有可用数据时
 * 给出"下一步该做什么"，而不是空白页。
 *
 * 这一步先把**信息架构**换成新设计的样子；各页细节仍沿用既有实现
 * （它们都已按设计做过约束与校验）。
 */
(function () {
  const A = App;

  const STEPS = [
    { key: 'blueprint', title: '① 蓝图', page: 'paper-build', icon: 'layers',
      what: '定义分节、槽位、分值与硬约束；候选必须来自冻结的 Release。' },
    { key: 'assemble', title: '② 拼卷', page: 'paper-assembly', icon: 'graph',
      what: '人手挑题或让求解器挑题（CP-SAT）；不可行时给"去掉哪组约束就能解"。' },
    { key: 'edit', title: '③ 卷面编辑', page: 'paper-editor', icon: 'pen',
      what: '调整顺序与措辞；每次编辑留证据，锁定后不再改动。' },
    { key: 'review', title: '④ 审核与发布', page: 'paper-ops', icon: 'check',
      what: '整卷校验 → 双人复核 → 建作业（打印 / 在线 / 两者）→ 发布。' },
    { key: 'attempt', title: '⑤ 在线作答', page: 'online-exam', icon: 'clipboard',
      what: '逐题自动保存；冲突不覆盖；服务端判定截止；交卷给回执哈希。' },
    { key: 'grade', title: '⑥ 评分与申诉', page: 'grading-review', icon: 'chart',
      what: '机器只给建议（判不出来转人工）→ 教师确认追加版本 → 独立发布 → 申诉重评。' },
    { key: 'qti', title: '⑦ 导入导出', page: 'qti-transfer', icon: 'download',
      what: '外部 QTI 只进草稿，不继承别人的审核结论；导出需已批准且来源完整。' }
  ];

  function escape(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function S() {
    A.state.exam = A.state.exam || { courseId: null, loaded: false };
    return A.state.exam;
  }

  A.examSteps = function () {
    return STEPS.map(step => ({ ...step }));
  };

  /** 入口链接：有当前课程就直连那门课，没有就先去选课（不替老师默认选课）。 */
  A.examStepHref = function (step, courseId) {
    if (!step || !step.page) throw new Error('步骤缺 page');
    return courseId ? A.url(step.page, courseId) : `#/pick/${step.page}`;
  };

  A.views.exam = function () {
    const state = S();
    const course = A.course;
    const courseId = course && course.id;
    const cards = A.examSteps().map(step => `
      <a class="exam-step" href="${A.examStepHref(step, courseId)}"
         data-step="${escape(step.key)}">
        <span class="exam-step-num">${escape(step.title.slice(0, 1))}</span>
        <span class="exam-step-body">
          <b>${escape(step.title)}</b>
          <small>${escape(step.what)}</small>
        </span>
      </a>`).join('');
    return `<div class="exam-home">
      <header class="panel exam-hero">
        <div>
          <p class="eyebrow">EXAM WORKBENCH</p>
          <h2>命题与考试工作台</h2>
          <p class="muted">按设计文档的流程：蓝图 → 拼卷 → 编辑 → 审核发布 → 作答 → 评分 → 申诉。
            每一步都能单独验收，失败时给的是原因，不是"操作失败"。</p>
        </div>
        <div class="button-row">
          ${A.button('刷新', 'exam-refresh', 'refresh')}
        </div>
      </header>
      <div class="panel">
        <h3>${courseId ? '当前课程：' + escape((course && (course.title || course.id)) || courseId) : '还没有选择课程'}</h3>
        <p class="muted small">${courseId
    ? '下面每一步都会带你进入这门课的对应页面。'
    : '先选一门课，再进入具体步骤；也可以直接点任意一步去选课页。'}</p>
        <div class="exam-steps">${cards}</div>
      </div>
      <div class="panel exam-rules">
        <h3>三条不可违反的规则</h3>
        <ul>
          <li><b>候选只能来自冻结 Release</b>：未经审核的题目不会进卷。</li>
          <li><b>判不出来 ≠ 判错了</b>：机器建议为 NULL 并转人工，绝不给 0 分。</li>
          <li><b>成绩只能追加</b>：改分是新增版本，历史版本保留（申诉与审计要用）。</li>
        </ul>
      </div>
    </div>`;
  };

  A.after.exam = function () {
    /* 纯入口页：不自动取数，避免"打开页面就触发网络请求与权限检查"。 */
  };

  A.actions['exam-refresh'] = function () {
    A.render();
    return undefined;
  };
})();
