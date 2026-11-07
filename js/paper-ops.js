/* 整卷审核与发布：校验 → 提交审核 → 双人复核 → 三类打印产物 → 发布打印作业 → 撤回。
 *
 * 为什么单独一页而不是塞进卷面编辑器：编辑是"改内容"，这里是"改状态"。两件事的
 * 失败方式完全不同——编辑器里 409 stale_version 表示别人改过卷面；这里 409
 * renders_not_ready 表示流程顺序没走完。混在一页，教师分不清"我改坏了"还是
 * "我还差一步"。
 *
 * 三条硬规则：
 *   1. 每步能不能点，由服务端已经返回的事实决定（状态、复核人数、产物种类数），
 *      前端不猜"应该可以了吧"。
 *   2. 不能点的按钮旁边**写清楚为什么**；只把按钮变灰等于让人去猜。
 *   3. 复核签名必须是"我认可这一份卷面 + 这一份校验报告"——manifest_hash 与
 *      report_hash 都从服务端读，前端不自己拼也不自己算。
 */
(function () {
  const A = App;

  const STATUS_LABEL = {
    draft: '草稿', in_review: '审核中', approved: '已批准', withdrawn: '已撤回'
  };
  const RENDER_LABEL = { student: '学生卷', answer: '答案卷', rubric: '量规卷' };
  const RENDER_KINDS = ['student', 'answer', 'rubric'];
  const ASSIGNMENT_LABEL = {
    draft: '草稿', scheduled: '已排期', published: '已发布',
    closed: '已结束', withdrawn: '已撤回'
  };

  function escape(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // ---------------------------------------------------------------------------
  // 纯推导：把服务端事实压成"每步能不能做"
  // ---------------------------------------------------------------------------

  A.paperOpsStatus = function (state) {
    const paper = (state && state.paper) || {};
    const review = (state && state.review) || {};
    const renders = (state && state.renders && state.renders.renders) || [];
    const assignments = (state && state.assignments) || [];
    const readyKinds = new Set(renders.map(item => item.render_kind));
    const missing = RENDER_KINDS.filter(kind => !readyKinds.has(kind));
    const draft = assignments.find(item => item.status === 'draft') || null;
    const live = assignments.filter(item => item.status === 'published'
      || item.status === 'scheduled');
    const required = Number(review.required_approvals || 0);
    const approvals = Number(review.approval_count || 0);
    const status = paper.status || 'draft';
    const withdrawn = status === 'withdrawn';
    const rendersComplete = missing.length === 0;
    return {
      status, withdrawn, required, approvals, missing,
      rendersReady: RENDER_KINDS.length - missing.length,
      rendersComplete,
      reportHash: review.report_hash || null,
      draft, live,
      canValidate: !withdrawn,
      canRender: !withdrawn,
      canSubmit: status === 'draft' && rendersComplete,
      canApprove: status === 'in_review' && !!review.report_hash,
      canPublish: status === 'approved' && rendersComplete && !!draft,
      canWithdraw: status === 'approved'
    };
  };

  /* 复核进度用文字说，不用"2/2"这种只有内部人才懂的写法：
   * 教师需要知道的是"还差一个人"还是"已经批完"。 */
  function approvalText(flags) {
    if (flags.status === 'in_review') {
      const left = Math.max(0, flags.required - flags.approvals);
      return left === 0 ? '整卷双复核完成' : `还需 ${left} 名复核教师`;
    }
    if (flags.status === 'approved') return `复核完成（${flags.required} 人）`;
    if (flags.status === 'withdrawn') return '已撤回，历史版本只读';
    return '还没提交审核';
  }

  function reviewSignature(flags, state) {
    const review = (state && state.review) || {};
    if (!flags.canApprove) return '提交审核后，本轮校验报告哈希才会生成；签名要绑它。';
    if (flags.approvals >= flags.required) return '本轮复核人数已够，不需要再签。';
    const mine = (review.decisions || []).some(decision =>
      decision.decision === 'approved' && decision.decision_seq > 0);
    return mine
      ? `你已签过本轮（${flags.approvals}/${flags.required}），等另一位教师。`
      : `你签的是当前卷面 ${String(review.manifest_hash || '').slice(0, 8)}… 与校验报告。`;
  }

  // ---------------------------------------------------------------------------
  // 渲染（纯函数：只吃 state，不发请求）
  // ---------------------------------------------------------------------------

  function stepRow(id, title, hint, enabled, blocker, label, busy) {
    // 有动作在飞的时候全部禁用：连点两下"提交审核"会拿到第二次的 409，
    // 而教师并不知道第一下其实成功了。
    const disabled = enabled && !busy ? '' : 'disabled';
    return `<li class="ops-step" data-step="${id}">
      <div class="ops-step-main">
        <b>${escape(title)}</b>
        <button type="button" class="btn" data-action="${id}" ${disabled}>${escape(label)}</button>
      </div>
      <p class="muted small">${escape(enabled ? hint : blocker)}</p>
    </li>`;
  }

  function notice(state) {
    if (state.error) return `<div class="notice bad" role="alert">${escape(state.error)}</div>`;
    if (state.message) return `<div class="notice" role="status" id="ops-message">${escape(state.message)}</div>`;
    return '';
  }

  function statusLine(flags, state) {
    const paper = state.paper || {};
    const published = flags.live.length;
    const text = `${STATUS_LABEL[flags.status] || flags.status} · ${approvalText(flags)}`
      + ` · 打印产物 ${flags.rendersReady}/3 · 已发布 ${published} 份作业`;
    return `<div class="ops-head">
      <h3>第 ${escape(paper.version_no || 1)} 版 · ${escape(paper.paper_kind || '')}
        · ${escape(paper.total_score || '')} 分</h3>
      <p role="status" class="ops-status">${escape(text)}</p></div>`;
  }

  function steps(flags, state) {
    const submitBlocker = flags.rendersComplete
      ? '已提交审核的卷子不能再改内容，要改先克隆新卷。'
      : `先导出三类卷（当前 ${flags.rendersReady}/3，还差 ${flags.missing
        .map(kind => RENDER_LABEL[kind]).join('、')}）。`;
    return `<ol class="ops-steps">
      ${stepRow('ops-validate', '整卷校验', '只读、确定性；提交审核时会再跑一次。',
        flags.canValidate, '已撤回的卷子不能再校验。', '运行整卷校验', state.busy)}
      ${stepRow('ops-render', '导出三类打印产物', '入队后由 worker 渲染；完成后才能提交审核。',
        flags.canRender, '已撤回的卷子不能再导出。', '导出学生卷/答案卷/量规卷', state.busy)}
      ${stepRow('ops-submit', '提交整卷审核', '冻结当前卷面与校验报告，进入双人复核。',
        flags.canSubmit, submitBlocker, '提交整卷审核', state.busy)}
      ${stepRow('ops-approve', '本人复核签名', reviewSignature(flags, state),
        flags.canApprove, flags.status === 'withdrawn'
          ? '已撤回。' : '只有审核中的卷子可以复核。', '确认本人复核', state.busy)}
      ${stepRow('ops-publish', '发布打印作业', '发布后 assignment 固定题目与分值，不可原位修改。',
        flags.canPublish, publishBlocker(flags), '发布打印作业', state.busy)}
      ${stepRow('ops-withdraw-open', '撤回试卷', '撤回保留历史与已有读数，但不能再导出或发布。',
        flags.canWithdraw, '只有已批准的卷子可以撤回。', '撤回试卷', state.busy)}
    </ol>`;
  }

  function publishBlocker(flags) {
    if (flags.status !== 'approved') return '试卷还没通过双人复核，不能发布。';
    if (!flags.rendersComplete) {
      return `还差 ${flags.missing.map(kind => RENDER_LABEL[kind]).join('、')}：没导出的卷子打印不出来。`;
    }
    return '先在本页新建一份作业草稿（选班级与三个时间点）。';
  }

  /* 下载地址走 api-client（它知道后端地址前缀）；单独渲染时没有 A.api，
   * 退回同源路径——视图测试只加载 core.js，不该因为缺 api-client 就崩。 */
  function renderUrl(renderId) {
    if (A.api && A.api.paperRenderUrl) return A.api.paperRenderUrl(renderId);
    return `/api/v1/paper-renders/${encodeURIComponent(renderId)}/download`;
  }

  /* 预览是另一条路由（inline 对 attachment）：复核人要"看到的就是要打印的那一版"，
   * 让人先下载再看，常常就变成"看的是上一次下载的文件"。 */
  function previewUrl(renderId) {
    if (A.api && A.api.paperRenderPreviewUrl) return A.api.paperRenderPreviewUrl(renderId);
    return `/api/v1/paper-renders/${encodeURIComponent(renderId)}/preview`;
  }

  function rendersBlock(state) {
    const renders = (state.renders && state.renders.renders) || [];
    const byKind = {};
    renders.forEach(item => { byKind[item.render_kind] = item; });
    const rows = RENDER_KINDS.map(kind => {
      const item = byKind[kind];
      const link = item
        ? `<a href="${escape(renderUrl(item.render_id))}" download>下载${RENDER_LABEL[kind]}</a>`
        : '<span class="muted">未生成</span>';
      const preview = item
        ? `<a href="${escape(previewUrl(item.render_id))}" target="_blank" rel="noopener">预览</a>`
        : '<span class="muted">—</span>';
      return `<tr data-kind="${kind}"><td>${RENDER_LABEL[kind]}</td><td>${preview}</td><td>${link}</td></tr>`;
    }).join('');
    // 本仓库的渲染产物是可打印 HTML，不是 PDF。与其让人下载后才发现，不如先写出来。
    const noticeText = state.renders && state.renders.pdf_implemented === false
      ? `<p class="muted small">产物是可打印 HTML（当前环境没有 PDF 渲染依赖）：用浏览器按 A4、100% 打印。</p>`
      : '';
    return `<section class="panel ops-renders" aria-label="打印产物">
      <h4>打印产物</h4>
      <table class="ops-table"><thead><tr><th>卷种</th><th>预览</th><th>下载</th></tr></thead>
        <tbody>${rows}</tbody></table>${noticeText}</section>`;
  }

  function assignmentsBlock(state, flags) {
    const rows = (state.assignments || []).map(item => {
      const times = `${escape(String(item.visible_at || '').slice(0, 16).replace('T', ' '))}`
        + ` → ${escape(String(item.due_at || '').slice(0, 16).replace('T', ' '))}`;
      return `<tr data-assignment="${escape(item.id)}">
        <td>${escape(item.offering_ref || '')}</td>
        <td>${escape(ASSIGNMENT_LABEL[item.status] || item.status)}</td>
        <td>${times}</td></tr>`;
    }).join('');
    const table = rows
      ? `<table class="ops-table"><thead><tr><th>班级</th><th>状态</th><th>可见 → 截止</th></tr>
          </thead><tbody>${rows}</tbody></table>`
      : '<p class="muted">这份卷还没有登记过作业。</p>';
    return `<section class="panel ops-assignments" aria-label="打印作业">
      <h4>打印作业</h4>${table}${assignmentForm(flags)}</section>`;
  }

  /* 新建草稿表单只在"已批准且产物齐"时给出来：其它状态下建了也发不出去，
   * 只会留下一条永远过不了终检的草稿。 */
  function assignmentForm(flags) {
    if (!flags.canWithdraw) {
      return '<p class="muted small">整卷通过复核后才能新建打印作业。</p>';
    }
    return `<div class="ops-form">
      <label for="ops-offering">班级</label>
      <select id="ops-offering" data-input="ops-offering">
        <option value="">（选择班级）</option>
      </select>
      <label for="ops-visible">可见时间</label>
      <input id="ops-visible" type="datetime-local" data-input="ops-visible">
      <label for="ops-due">截止时间</label>
      <input id="ops-due" type="datetime-local" data-input="ops-due">
      <label for="ops-close">硬关闭时间</label>
      <input id="ops-close" type="datetime-local" data-input="ops-close">
      <label for="ops-distribution">分发方式</label>
      <select id="ops-distribution" data-input="ops-distribution">
        <option value="print">打印卷</option>
        <option value="online">在线作答</option>
        <option value="both">打印 + 在线</option>
      </select>
      <div class="button-row">
        <button type="button" class="btn" data-action="ops-assignment-create">新建作业草稿</button>
      </div>
      <p class="muted small">三个时间点由服务端按 UTC 保存；可见 &lt; 截止 ≤ 硬关闭。
        建好草稿后用上面的「发布打印作业」发布。</p>
    </div>`;
  }

  function withdrawBlock(state, flags) {
    if (!state.withdrawOpen) return '';
    if (!flags.canWithdraw) {
      return `<div class="notice bad" role="alert">只有已批准的卷子可以撤回。</div>`;
    }
    return `<section class="panel ops-withdraw" aria-label="撤回试卷">
      <label for="withdraw-reason">撤回原因</label>
      <textarea id="withdraw-reason" rows="2" data-input="withdraw-reason"></textarea>
      <div class="button-row">
        <button type="button" class="btn" data-action="ops-withdraw-confirm">确认撤回</button>
      </div>
      <p class="muted small">撤回后历史版本与已有提交保留，但不能再导出、发布或作答。</p>
    </section>`;
  }

  A.paperOpsHtml = function (state) {
    if (!state || !state.paper) return '<div class="ops empty"><p>还没有读取到卷子。</p></div>';
    const flags = A.paperOpsStatus(state);
    return `<div class="ops">
      ${statusLine(flags, state)}
      ${notice(state)}
      ${flags.status === 'withdrawn' ? '<div class="notice bad" role="alert">已撤回，历史版本只读</div>' : ''}
      ${steps(flags, state)}
      ${rendersBlock(state)}
      ${assignmentsBlock(state, flags)}
      ${withdrawBlock(state, flags)}
    </div>`;
  };

  // ---------------------------------------------------------------------------
  // 表单 → 请求体（纯函数，便于单独验证）
  // ---------------------------------------------------------------------------

  A.paperOpsAssignmentPayload = function (form) {
    const offering = String((form && form.offering) || '').trim();
    if (!offering) throw new Error('先选一个班级');
    const parse = name => {
      const value = new Date(String((form && form[name]) || ''));
      if (Number.isNaN(value.getTime())) throw new Error('三个时间点都要填');
      return value;
    };
    const visible = parse('visible');
    const due = parse('due');
    const close = parse('close');
    if (!(visible < due)) throw new Error('可见时间必须早于截止时间');
    if (!(due <= close)) throw new Error('截止时间不能晚于硬关闭时间');
    const distribution = String((form && form.distribution) || 'print');
    if (!['print', 'online', 'both'].includes(distribution)) {
      throw new Error('分发方式只允许 print / online / both');
    }
    return {
      offering_ref: offering,
      visible_at: visible.toISOString(),
      due_at: due.toISOString(),
      hard_close_at: close.toISOString(),
      distribution_kind: distribution
    };
  };

  // ---------------------------------------------------------------------------
  // 页面状态与动作
  // ---------------------------------------------------------------------------

  function S() {
    return A.state.paperOps = A.state.paperOps || {
      versionId: null, paper: null, review: null, renders: null, assignments: [],
      classes: [], loading: false, error: null, message: null, busy: null,
      withdrawOpen: false, form: {}
    };
  }

  A.paperOpsError = function (error) {
    const code = (error && error.code) || (error && error.detail && error.detail.code);
    const messages = {
      renders_not_ready: '三类打印产物还没齐：先导出再提交审核。',
      paper_not_draft: '已提交审核的卷子不能直接修改，请克隆新卷。',
      review_manifest_changed: '卷面在你复核期间变过，这次签名不算数；刷新后重新审。',
      duplicate_reviewer: '同一个人不能在同一轮里复核两次，需要另一位教师。',
      contributor_cannot_review: '命题人不能复核自己出的卷子。',
      human_reviewer_required: '复核必须由真人教师签署。',
      paper_invalid: '整卷校验未通过：先按校验结论改卷。',
      stale_version: '别人刚刚改过这份卷，刷新后再操作。',
      review_not_open: '本轮审核还没开始：先提交整卷审核。',
      assignment_not_approved: '卷子还没通过审核，不能发布作业。',
      course_scope_denied: '你不在这一批班级的授权范围内。',
      paper_withdrawn: '已撤回的卷子不能再操作。',
      render_scope_denied: '你没有这个卷种的导出权限。'
    };
    return messages[code] || (error && error.message) || '操作未完成';
  };

  A.loadPaperOps = function (versionId, { force = false } = {}) {
    const state = S();
    if (!versionId) return;
    if (!force && state.versionId === versionId && (state.loading || state.paper)) return;
    state.versionId = versionId;
    state.loading = true;
    A.render();
    const courseId = A.course && A.course.id;
    Promise.all([
      A.api.paperVersion(versionId),
      A.api.paperReview(versionId).catch(() => null),
      A.api.paperRenders(versionId).catch(() => null),
      A.api.paperAssignments(versionId).catch(() => []),
      courseId ? A.api.classes({ course_id: courseId }).catch(() => null) : null
    ]).then(([paper, review, renders, assignments, classes]) => {
      state.paper = paper;
      state.review = review;
      state.renders = renders;
      state.assignments = Array.isArray(assignments) ? assignments : [];
      state.classes = (classes && classes.items) || [];
      state.loading = false;
      A.render();
      fillOfferingSelect(state);
    }).catch(error => {
      state.loading = false;
      state.error = A.paperOpsError(error);
      A.render();
    });
  };

  /* 班级下拉框在服务端数据到齐后才填：先画一个空 select 再补 options，
   * 比"禁用 + 转圈"更不容易让人以为没选上。 */
  function fillOfferingSelect(state) {
    if (typeof document === 'undefined' || !document.getElementById) return;
    const select = document.getElementById('ops-offering');
    if (!select) return;
    select.innerHTML = '<option value="">（选择班级）</option>' + state.classes
      .map(item => `<option value="${escape(item.id)}">${escape(item.name)}</option>`)
      .join('');
    if (state.form.offering) select.value = state.form.offering;
  }

  function run(action, task) {
    const state = S();
    if (state.busy) return Promise.resolve();
    state.busy = action;
    state.error = null;
    state.message = null;
    A.render();
    return task().then(message => {
      state.busy = null;
      state.message = message || '已完成。';
      state.withdrawOpen = false;
      A.loadPaperOps(state.versionId, { force: true });
    }).catch(error => {
      state.busy = null;
      state.error = A.paperOpsError(error);
      A.render();
    });
  }

  function newKey(action) {
    return `paper-ops-${action}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  }

  A.views['paper-ops'] = function () {
    const state = S();
    const versionId = A.route.params.get('version');
    const heading = A.sectionTitle(
      'PAPER OPERATIONS',
      '整卷审核与发布',
      '校验 → 提交审核 → 两位教师分别复核 → 导出三类卷 → 发布打印作业。每一步都以服务端事实为准。',
      `<div class="button-row">${A.button('重新读取', 'ops-refresh', 'refresh')}</div>`
    );
    if (!versionId) {
      return heading + `<div class="panel"><p class="muted">缺少卷版本号：从组卷工作台进入。</p></div>`;
    }
    if (state.loading && !state.paper) {
      return heading + `<div class="panel"><p class="muted">正在读取卷子与审核状态…</p></div>`;
    }
    if (state.error && !state.paper) {
      return heading + `<div class="panel"><h2>读不到这份卷</h2>
        <p class="muted">${escape(state.error)}</p></div>`;
    }
    return heading + `<div class="panel">${A.paperOpsHtml(state)}</div>`;
  };

  A.after['paper-ops'] = function () {
    A.loadPaperOps(A.route.params.get('version'));
  };

  A.inputs['ops-offering'] = input => { S().form.offering = input.value; };
  A.inputs['ops-visible'] = input => { S().form.visible = input.value; };
  A.inputs['ops-due'] = input => { S().form.due = input.value; };
  A.inputs['ops-close'] = input => { S().form.close = input.value; };
  A.inputs['withdraw-reason'] = input => { S().form.reason = input.value; };

  A.actions['ops-refresh'] = function () {
    A.loadPaperOps(A.route.params.get('version'), { force: true });
  };

  A.actions['ops-validate'] = function () {
    const state = S();
    return run('validate', () => A.api.paperValidate(state.versionId).then(result => {
      const issues = (result && result.issues) || [];
      const errors = issues.filter(issue => issue.severity !== 'warning');
      if (!errors.length) {
        return issues.length
          ? `硬约束全部通过，另有 ${issues.length} 条提示。`
          : '硬约束全部通过。';
      }
      return `整卷校验未通过（${errors.length} 项）：${errors
        .map(issue => issue.message).join('；')}`;
    }));
  };

  A.actions['ops-render'] = function () {
    const state = S();
    return run('render', () => A.api.paperRequestRenders(
      state.versionId, RENDER_KINDS, newKey('render')).then(result => {
      const queued = (result && result.jobs) || [];
      return queued.length
        ? `已排队 ${queued.length} 类产物，worker 完成后刷新即可下载。`
        : '三类产物都已就绪。';
    }));
  };

  A.actions['ops-submit'] = function () {
    const state = S();
    return run('submit', () => A.api.paperSubmitReview(
      state.versionId, newKey('submit')).then(result => {
      const required = Number((result && result.required_approvals) || 2);
      return `已提交审核，还需要 ${required} 位教师复核。`;
    }));
  };

  A.actions['ops-approve'] = function () {
    const state = S();
    const review = state.review || {};
    const flags = A.paperOpsStatus(state);
    if (!flags.canApprove) {
      state.error = '这一轮还不能复核：先提交审核，等校验报告哈希生成。';
      A.render();
      return Promise.resolve();
    }
    return run('approve', () => A.api.paperApprove(state.versionId, {
      // 签名绑的是"这份卷面 + 这份校验报告"，两个哈希都取服务端当前值。
      manifest_hash: review.manifest_hash,
      evidence_hash: review.report_hash
    }, newKey('approve')).then(result => {
      const approvals = Number((result && result.approval_count) || 0);
      const required = Number((result && result.required_approvals) || 2);
      return approvals >= required
        ? '整卷双复核完成，可以发布打印作业。'
        : `还需 ${Math.max(0, required - approvals)} 名复核教师。`;
    }));
  };

  A.actions['ops-assignment-create'] = function () {
    const state = S();
    let body;
    try {
      body = A.paperOpsAssignmentPayload(state.form);
    } catch (error) {
      state.error = error.message;
      A.render();
      return Promise.resolve();
    }
    return run('assignment', () => A.api.createAssignment({
      ...body, paper_version_id: state.versionId
    }, newKey('assignment')).then(result => {
      const row = (result && result.assignment) || {};
      return `已建作业草稿（${row.offering_ref || body.offering_ref}），下一步是发布。`;
    }));
  };

  A.actions['ops-publish'] = function () {
    const state = S();
    const flags = A.paperOpsStatus(state);
    if (!flags.draft) {
      state.error = '还没有可发布的作业草稿：先在上面新建一份。';
      A.render();
      return Promise.resolve();
    }
    return run('publish', () => A.api.publishAssignment(
      flags.draft.id, newKey('publish')).then(() => 'assignment 已发布'));
  };

  A.actions['ops-withdraw-open'] = function () {
    S().withdrawOpen = true;
    S().error = null;
    A.render();
  };

  A.actions['ops-withdraw-confirm'] = function () {
    const state = S();
    const reason = String(state.form.reason || '').trim();
    if (!reason) {
      state.error = '撤回必须写原因：半年后要能回答"为什么撤了"。';
      A.render();
      return Promise.resolve();
    }
    return run('withdraw', () => A.api.paperWithdraw(
      state.versionId, reason, newKey('withdraw')).then(() => '已撤回，历史版本只读'));
  };
})();
