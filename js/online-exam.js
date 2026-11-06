/* 学生在线作答台（阶段 C 任务 8）：开始 → 逐题自动保存 → 交卷 → 回执。
 *
 * 这一页有三条不能妥协的规则：
 *   1. **不信任本地时钟**：倒计时只是显示；能不能保存由服务端决定。页面从不因为
 *      "本地时间还没到"就放行，也从不因为"本地时间到了"就禁止保存——它交给服务端。
 *   2. **冲突不静默覆盖**：保存收到 409 response_conflict 时，必须把服务端最新本人
 *      响应摆出来，让用户选择"用服务端的"还是"确认覆盖"。悄悄覆盖是最坏的实现。
 *   3. **已交卷只读**：拿到回执后不再发送保存请求，页面显示回执哈希（申诉要靠它）。
 */
(function () {
  const A = App;

  const STATUS_LABEL = {
    in_progress: '作答中',
    finalizing: '正在收卷',
    submitted: '已交卷',
    closed: '已结束',
    published: '已发布',
    scheduled: '已排期',
    withdrawn: '已撤回'
  };

  const PARSE_LABEL = {
    parsed: '已保存',
    invalid: '已保存（内容无法解析，将人工处理）',
    unparsed: '已保存（待解析）',
    empty: '未作答'
  };

  function escape(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function S() {
    return A.state.onlineExam = A.state.onlineExam || {
      assignmentId: null, attemptId: null, detail: null, loading: false,
      loaded: false, error: null, message: null, busy: null,
      receipts: {}, conflicts: {}, clientIds: {}, receipt: null
    };
  }

  function nowKey(prefix) {
    return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  }

  // ---------------------------------------------------------------------------
  // 纯函数（视图测试直接断言）
  // ---------------------------------------------------------------------------

  /** 剩余秒数：只用来看，不参与任何判定。负数按 0 处理（显示"已截止"）。 */
  A.onlineExamRemaining = function (detail, nowMs) {
    if (!detail || !detail.server_deadline_at) return null;
    const deadline = Date.parse(detail.server_deadline_at);
    if (Number.isNaN(deadline)) return null;
    const base = nowMs == null ? Date.now() : nowMs;
    return Math.max(0, Math.floor((deadline - base) / 1000));
  };

  A.onlineExamCountdown = function (detail, nowMs) {
    const remaining = A.onlineExamRemaining(detail, nowMs);
    if (remaining == null) return '';
    const minutes = Math.floor(remaining / 60);
    const seconds = remaining % 60;
    return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
  };

  /** 现在还能不能录入：已交卷/已截止/作业撤回都算不能。 */
  A.onlineExamEditable = function (detail) {
    if (!detail) return false;
    const status = String(detail.status || '');
    if (!['in_progress', 'finalizing'].includes(status)) return false;
    if (String(detail.assignment_status || '') === 'withdrawn') return false;
    return A.onlineExamRemaining(detail, Date.now()) !== 0;
  };

  /** 保存载荷：客户端修订号 + 基准版本 + 原文。禁止夹带额外字段（服务端 extra=forbid）。 */
  A.onlineExamSaveBody = function (slot) {
    return {
      client_revision_id: String(slot.clientRevisionId || ''),
      base_revision_no: Number(slot.revision_no || 0),
      raw_response: { text: String(slot.draftText || '') }
    };
  };

  /** 冲突提示：必须同时给出"我的版本"与"服务端的版本"，让用户自己选。 */
  A.onlineExamConflictText = function (conflict) {
    if (!conflict) return '';
    const server = conflict.server_result || {};
    const revision = Number(server.revision_no || 0);
    if (!revision) return '另一处已保存过这道题，服务端还没有可回退的版本。';
    return `另一处已保存过这道题（服务端第 ${revision} 版）。`
      + '选择「用服务端版本」会丢弃你当前输入。';
  };

  // ---------------------------------------------------------------------------
  // 渲染
  // ---------------------------------------------------------------------------
  function slotRow(state, slot) {
    const editable = A.onlineExamEditable(state.detail);
    const conflict = state.conflicts[slot.slot_id];
    const status = state.receipts[slot.slot_id] || PARSE_LABEL[slot.parse_status]
      || slot.parse_status;
    const value = slot.draftText == null ? textOf(slot.raw_response) : slot.draftText;
    return `<div class="exam-slot" data-slot="${escape(slot.slot_id)}">
      <div class="exam-slot-head"><b>第 ${escape(slot.slot_no)} 题</b>
        <span class="muted">${escape(status)}</span></div>
      <div class="exam-stem">${escape(slot.stem)}</div>
      <textarea data-input="exam-answer" data-slot="${escape(slot.slot_id)}"
        ${editable ? '' : 'readonly aria-readonly="true"'} rows="4"
        placeholder="在这里作答">${escape(value)}</textarea>
      ${conflict ? `<div class="exam-conflict" role="alert">
        <p>${escape(A.onlineExamConflictText(conflict))}</p>
        <div class="button-row">
          ${A.button('用服务端版本', 'exam-use-server', 'refresh',
            `data-slot="${escape(slot.slot_id)}"`, 'small')}
          ${A.button('保留我的输入并覆盖', 'exam-keep-mine', 'check',
            `data-slot="${escape(slot.slot_id)}"`, 'small')}
        </div></div>` : ''}
    </div>`;
  }

  function textOf(raw) {
    return raw && typeof raw.text === 'string' ? raw.text : '';
  }

  function receiptBlock(state) {
    const receipt = state.receipt;
    if (!receipt) return '';
    return `<div class="exam-receipt" role="status">
      <h5>已交卷</h5>
      <p>题数 ${escape(receipt.item_count)} · 未答 ${escape(receipt.unanswered_count)}
        · 无法解析 ${escape(receipt.invalid_count)}</p>
      <p>回执哈希 <code>${escape(String(receipt.receipt_hash || '').slice(0, 16))}…</code>
        （申诉时出示这个）</p></div>`;
  }

  A.onlineExamHtml = function (state) {
    const current = state || S();
    if (!current.attemptId && !current.detail) {
      if (current.loading) return '<p class="muted">正在读取作答状态…</p>';
      if (current.error) return `<div class="alert" role="alert">${escape(current.error)}</div>`;
      return `<div class="panel"><p>还没有开始这份卷。</p>
        <small class="muted">开始后计时由服务端判定，本地时钟仅供参考。</small>
        <div class="button-row">${A.button('开始答题', 'exam-start', 'pen', '', 'primary')}</div>
      </div>`;
    }
    const detail = current.detail;
    if (!detail) {
      return `<p class="muted">正在打开卷面…</p>`;
    }
    const editable = A.onlineExamEditable(detail);
    const header = `<div class="exam-head">
      <span class="badge">${escape(STATUS_LABEL[detail.status] || detail.status)}</span>
      <span class="muted">第 ${escape(detail.attempt_no)} 次作答 · 剩余
        <b data-exam-clock>${escape(A.onlineExamCountdown(detail))}</b></span>
      ${editable ? '' : '<span class="muted">（已停止录入）</span>'}
    </div>`;
    const slots = (detail.slots || []).map(slot => slotRow(current, slot)).join('');
    const submit = editable
      ? `<div class="button-row">${A.button('交卷', 'exam-finalize', 'check',
          current.busy ? 'disabled' : '', 'primary')}</div>`
      : '';
    return `<div class="online-exam">${header}
      ${current.error ? `<div class="alert" role="alert">${escape(current.error)}</div>` : ''}
      ${current.message ? `<div class="notice" role="status">${escape(current.message)}</div>` : ''}
      <div class="exam-slots">${slots}</div>
      ${submit}${receiptBlock(current)}</div>`;
  };

  A.views['online-exam'] = function () {
    const state = S();
    const heading = A.sectionTitle(
      'ONLINE EXAM',
      '在线作答',
      '逐题自动保存，交卷后只读。截止时间由服务端判定，本地时钟仅用于显示倒计时。',
      `<div class="button-row">${A.button('刷新', 'exam-refresh', 'refresh')}</div>`
    );
    const course = A.course;
    if (state.courseId !== (course && course.id)) {
      state.courseId = course && course.id;
      state.detail = null;
      state.conflict = null;
      state.receipt = null;
      state.loaded = false;
    }
    return heading + `<div class="panel">${A.onlineExamHtml(state)}</div>`;
  };

  A.after['online-exam'] = function () {
    A.loadOnlineExam();
  };

  // ---------------------------------------------------------------------------
  // 取数
  // ---------------------------------------------------------------------------
  A.loadOnlineExam = function ({ force = false } = {}) {
    const state = S();
    const assignmentId = A.route && A.route.params && A.route.params.assignment;
    const attemptId = A.route && A.route.params && A.route.params.attempt;
    if (!assignmentId && !attemptId) return;
    if (!force && state.loaded) return;
    state.loaded = true;
    state.loading = true;
    A.render();
    const load = attemptId
      ? A.api.attemptDetail(attemptId).then(detail => { state.detail = withDrafts(detail); })
      : A.api.assignmentAttempt(assignmentId).then(found => {
        if (!found || !found.attempt_id) {
          state.detail = null;
          return null;
        }
        return A.api.attemptDetail(found.attempt_id)
          .then(detail => { state.detail = withDrafts(detail); });
      });
    return load.then(() => {
      state.loading = false;
      if (state.detail) state.attemptId = state.detail.attempt_id;
      A.render();
    }).catch(error => {
      state.loading = false;
      state.error = A.onlineExamError(error);
      A.render();
    });
  };

  function withDrafts(detail) {
    const slots = (detail.slots || []).map(slot => ({
      ...slot,
      draftText: textOf(slot.raw_response),
      clientRevisionId: null
    }));
    return { ...detail, slots };
  }

  A.onlineExamError = function (error) {
    const detail = (error && error.detail) || {};
    const code = detail.code || (error && error.code);
    const map = {
      attempt_not_found: '找不到这次作答（可能已结束）。',
      attempt_finalized: '作答已经结束，不能继续修改。',
      assignment_withdrawn: '这份作业已撤回。',
      response_conflict: '另一处已保存过这道题。',
      idempotency_conflict: '同一个保存标识被用于不同内容，请刷新页面。',
      assignment_not_open: '还没到开始时间，或作业尚未发布。',
      distribution_not_online: '这份作业是打印卷，没有在线作答。',
      attempt_limit_exceeded: '作答次数已用完。',
      idempotency_key_required: '缺少 Idempotency-Key。'
    };
    if (code && map[code]) return map[code];
    if (detail.message) return detail.message;
    return (error && error.message) || '请求失败，请稍后重试。';
  };

  function run(action, task) {
    const state = S();
    if (state.busy) return Promise.resolve();
    state.busy = action;
    state.error = null;
    state.message = null;
    A.render();
    return Promise.resolve().then(task).then(message => {
      state.busy = null;
      if (message) state.message = message;
      A.render();
    }).catch(error => {
      state.busy = null;
      state.error = A.onlineExamError(error);
      A.render();
    });
  }

  // ---------------------------------------------------------------------------
  // 动作
  // ---------------------------------------------------------------------------
  A.actions['exam-refresh'] = function () {
    S().loaded = false;
    return A.loadOnlineExam({ force: true });
  };

  A.actions['exam-start'] = function () {
    const state = S();
    const assignmentId = A.route && A.route.params && A.route.params.assignment;
    if (!assignmentId) {
      state.error = 'URL 里缺少作业标识（?assignment=…）';
      A.render();
      return Promise.resolve();
    }
    return run('start', () => A.api.startAttempt(assignmentId, nowKey('attempt'))
      .then(started => A.api.attemptDetail(started.attempt_id))
      .then(detail => {
        state.detail = withDrafts(detail);
        state.attemptId = detail.attempt_id;
        return '已开始作答。答案会自动保存。';
      }));
  };

  A.actions['exam-finalize'] = function () {
    const state = S();
    if (!state.attemptId) return Promise.resolve();
    return run('finalize', () => A.api.finalizeAttempt(state.attemptId,
      nowKey('finalize')).then(receipt => {
        state.receipt = receipt;
        if (state.detail) state.detail.status = 'submitted';
        return '已交卷。下方回执哈希在申诉时需要。';
      }));
  };

  A.actions['exam-use-server'] = function (button) {
    const state = S();
    const slotId = button.dataset.slot;
    const conflict = state.conflicts[slotId];
    if (!conflict) return undefined;
    const server = conflict.server_result || {};
    applyServer(state, slotId, server);
    delete state.conflicts[slotId];
    A.render();
    return undefined;
  };

  function applyServer(state, slotId, server) {
    const slot = (state.detail.slots || []).find(item => item.slot_id === slotId);
    if (!slot) return;
    slot.raw_response = server.raw_response || null;
    slot.parse_status = server.parse_status || 'empty';
    slot.revision_no = Number(server.revision_no || 0);
    slot.draftText = textOf(slot.raw_response);
    slot.clientRevisionId = null;
  }

  A.actions['exam-keep-mine'] = function (button) {
    const state = S();
    const slotId = button.dataset.slot;
    const conflict = state.conflicts[slotId];
    if (!conflict) return undefined;
    // 覆盖前先把基准对齐到服务端最新版本，否则下一次保存必然还是冲突。
    const slot = (state.detail.slots || []).find(item => item.slot_id === slotId);
    if (slot) slot.revision_no = Number((conflict.server_result || {}).revision_no || 0);
    delete state.conflicts[slotId];
    return saveSlot(slotId);
  };

  // ---------------------------------------------------------------------------
  // 输入与保存
  // ---------------------------------------------------------------------------
  A.inputs['exam-answer'] = function (input) {
    const state = S();
    const slotId = input.dataset.slot;
    const slot = (state.detail.slots || []).find(item => item.slot_id === slotId);
    if (!slot || !A.onlineExamEditable(state.detail)) return undefined;
    slot.draftText = input.value;
    if (!slot.clientRevisionId) slot.clientRevisionId = nowKey('rev');
    clearTimeout(state.timer);
    state.timer = setTimeout(() => saveSlot(slotId), state.detail.save_debounce_ms || 800);
    return undefined;
  };

  function saveSlot(slotId) {
    const state = S();
    if (!state.detail || !state.attemptId) return Promise.resolve();
    if (!A.onlineExamEditable(state.detail)) return Promise.resolve();
    const slot = (state.detail.slots || []).find(item => item.slot_id === slotId);
    if (!slot) return Promise.resolve();
    const body = A.onlineExamSaveBody(slot);
    if (!body.client_revision_id) return Promise.resolve();
    return A.api.saveAttemptSlot(state.attemptId, slotId, body)
      .then(result => {
        slot.revision_no = result.revision_no;
        slot.parse_status = result.parse_status;
        slot.clientRevisionId = null;
        state.receipts[slotId] = PARSE_LABEL[result.parse_status] || '已保存';
        delete state.conflicts[slotId];
        A.render();
      })
      .catch(error => {
        const detail = (error && error.detail) || {};
        if (detail.code === 'response_conflict') {
          state.conflicts[slotId] = { server_result: detail.server_result };
          A.render();
          return;
        }
        state.error = A.onlineExamError(error);
        A.render();
      });
  }
})();
