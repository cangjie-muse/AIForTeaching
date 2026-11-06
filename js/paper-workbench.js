/* 组卷工作台：把「这门课每份卷到哪一步」画成一张表。
 *
 * 数据来自 GET /api/v1/paper-workbench（一次只读聚合）。这里只做渲染，不发请求：
 * 取数由页面在进入工作台时做一次，避免每次重渲染都打接口——工作台的表格会随
 * 输入变化频繁重画。
 *
 * 为什么把 next_action 显示成中文句子而不是状态图标：教师需要的是「下一步干
 * 什么」，状态名（in_review）不告诉他该等还是该催。渲染排在发布之前是硬依赖
 * （提交审核要求三类卷都 ready），这里直接把「先导出三类卷」写出来。
 */
(function () {
  const A = App;

  const ACTION_LABEL = {
    fill_slots: '选题填空位',
    validate: '跑整卷校验',
    submit_review: '提交审核',
    await_review: '等待第二位复核人',
    render: '先导出学生卷/答案卷/量规卷',
    publish_assignment: '发布成打印作业',
    withdrawn: '已撤回，需重新建卷',
    none: '已完成'
  };

  const STATUS_LABEL = {
    draft: '草稿', in_review: '审核中', approved: '已批准', withdrawn: '已撤回'
  };

  function escape(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  A.paperWorkbenchHtml = function (payload) {
    if (!payload || !Array.isArray(payload.papers)) {
      return `<div class="workbench empty"><p>还没有读取到工作台数据。</p></div>`;
    }
    const counts = payload.counts || {};
    const summary = ['draft', 'in_review', 'approved', 'withdrawn']
      .map(status => `${STATUS_LABEL[status]} ${Number(counts[status]) || 0}`)
      .join(' · ');

    const head = `<div class="workbench-head">
      <h3>${escape(payload.course_ref || '')} · 组卷工作台</h3>
      <p class="muted">${summary}</p></div>`;

    if (!payload.papers.length) {
      return `<div class="workbench">${head}
        <p class="empty">这门课还没有卷子。先冻结一个 Release，再建手工卷。</p></div>`;
    }

    const rows = payload.papers.map(paper => {
      const renders = Number(paper.renders_ready) || 0;
      const published = Number(paper.assignments_published) || 0;
      const action = ACTION_LABEL[paper.next_action] || paper.next_action || '';
      return `<tr data-status="${escape(paper.status)}">
        <td>v${escape(paper.version_no)}</td>
        <td>${escape(paper.paper_kind)}</td>
        <td>${escape(STATUS_LABEL[paper.status] || paper.status)}</td>
        <td>${escape(paper.total_score)}</td>
        <td>${renders}/3</td>
        <td>${published}</td>
        <td class="next">${escape(action)}</td>
      </tr>`;
    }).join('');

    return `<div class="workbench">${head}
      <table class="workbench-table">
        <thead><tr>
          <th>版本</th><th>类型</th><th>状态</th><th>总分</th>
          <th>渲染</th><th>已发布</th><th>下一步</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table></div>`;
  };

  // ---------------------------------------------------------------------------
  // 页面入口
  // ---------------------------------------------------------------------------

  function S() {
    return A.state.workbench = A.state.workbench || {
      courseId: null, data: null, loading: false, error: null
    };
  }

  /* 进入工作台时取一次数；课程切换或点刷新时重取。
   *
   * 不做轮询：工作台的数据只在教师做动作（校验/提交/渲染/发布）之后才变，
   * 那些动作自己会触发刷新。定时轮询只会让"没变化"也变成一次请求。 */
  A.loadPaperWorkbench = function (courseId, { force = false } = {}) {
    const state = S();
    if (!courseId) return;
    const changed = state.courseId !== courseId;
    if (!force && !changed && (state.data || state.loading)) return;
    state.courseId = courseId;
    state.loading = true;
    state.error = null;
    A.api.paperWorkbench(courseId)
      .then(payload => { state.data = payload; state.loading = false; A.render(); })
      .catch(error => {
        state.loading = false;
        state.error = error?.message || '读取工作台失败';
        A.render();
      });
  };

  A.views.workbench = function () {
    const state = S();
    const course = A.course;
    const heading = A.sectionTitle(
      'PAPER WORKBENCH',
      '组卷工作台',
      '冻结 Release → 建卷 → 整卷校验 → 双人复核 → 导出三类卷 → 发布打印作业。每一步都要留证据。',
      `<div class="button-row">${A.button('刷新', 'workbench-refresh', 'refresh')}</div>`
    );
    if (!course) {
      return heading + `<div class="panel"><p class="muted">先选择一门课程。</p></div>`;
    }
    if (state.loading && !state.data) {
      return heading + `<div class="panel"><p class="muted">正在读取工作台…</p></div>`;
    }
    if (state.error && !state.data) {
      return heading + `<div class="panel"><h2>读不到工作台</h2>
        <p class="muted">${escape(state.error)}</p>
        <div class="button-row">${A.button('重试', 'workbench-retry', 'refresh')}</div></div>`;
    }
    return heading + `<div class="panel">${A.paperWorkbenchHtml(state.data)}</div>`;
  };

  A.after.workbench = function () { A.loadPaperWorkbench(A.course?.id); };

  A.actions['workbench-refresh'] = function () {
    A.loadPaperWorkbench(A.course?.id, { force: true });
  };
  A.actions['workbench-retry'] = function () {
    A.loadPaperWorkbench(A.course?.id, { force: true });
  };
})();
