/* 教师复核台与学生成绩/申诉页（阶段 D 任务 9—10）。
 *
 * 两页都只做一件事：把"分数从哪来、谁能看见"讲清楚。
 *
 * 教师复核台（grading-review）：
 *   - 逐项填分（分项合计必须等于项分，否则服务端会拒），提交 = confirm（只确认，不发布）；
 *   - 发布会单独一个按钮，且必须带幂等键；撤回必须写原因；
 *   - 申诉受理后可以重评，但重评只是产生草稿，还要再确认+发布。
 *
 * 学生成绩页（student-feedback）：
 *   - 只显示**已发布**的版本（服务端已保证），页面不缓存草稿；
 *   - 申诉一个版本只能提交一次；提交后显示"已提交，等待教师受理"。
 *
 * 设计上的取舍：页面不缓存成绩副本（学生刷新必回服务端）；所有写操作都显示
 * 服务端的错误码，而不是把错误翻译成"操作失败"——教师需要知道是幂等键缺失、
 * 分数越界，还是发布缺项。
 */
(function () {
  const A = App;

  function escape(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function S(page) {
    A.state[page] = A.state[page] || {
      submissionId: null, itemScores: {}, loading: false, loaded: false,
      error: null, notice: null, busy: null, result: null, grades: [],
      appeals: {}, baseRevisionNo: 0
    };
    return A.state[page];
  }

  // ---------------------------------------------------------------------------
  // 纯函数（视图测试直接断言）
  // ---------------------------------------------------------------------------

  /** 把逐项输入换算成 confirm 请求体；`itemMaxScores` 由页面从题目分值带入。 */
  A.gradingConfirmBody = function (input) {
    const decisions = Object.entries(input.itemScores || {}).map(([itemId, score]) => ({
      submission_item_id: itemId,
      score: String(score),
      criterion_scores: []
    }));
    return {
      decisions,
      base_revision_no: Number(input.baseRevisionNo || 0),
      item_max_scores: input.itemMaxScores || {}
    };
  };

  /** 服务端错误码 → 教师能看懂的话（不吞掉原始码）。 */
  const ERROR_TEXT = {
    idempotency_key_required: '发布/撤回必须带幂等键（页面会自动生成，重试请用同一个）',
    review_incomplete: '还有提交项没有教师确认的分数，不能发布',
    rubric_score_mismatch: '分项合计与项分不一致，请先改分项',
    grade_already_published: '这一版已经发布过，改分请先撤回',
    grade_already_withdrawn: '没有可撤回的已发布成绩',
    grading_not_configured: '评分服务未装配（后端未注入仓储）',
    grading_error: '服务端拒绝了这个操作'
  };

  A.gradingErrorText = function (error) {
    const detail = (error && error.detail) || {};
    const code = detail.code || (error && error.code) || '';
    const base = ERROR_TEXT[code];
    if (base) return `${base}（${code}）`;
    return detail.message || (error && error.message) || '请求失败';
  };

  /** 逐项合计：教师填的分累加，用来和服务端返回的分值对照。 */
  A.gradingTotal = function (itemScores) {
    return Object.values(itemScores || {})
      .map(value => Number(value))
      .filter(value => Number.isFinite(value))
      .reduce((sum, value) => sum + value, 0);
  };

  A.gradingAppealState = function (state, revisionId) {
    const entry = (state.appeals || {})[revisionId];
    if (!entry) return '可申诉';
    return entry.status === 'open' ? '已提交，等待教师受理' : `申诉状态：${entry.status}`;
  };

  // ---------------------------------------------------------------------------
  // 教师复核台
  // ---------------------------------------------------------------------------
  A.views['grading-review'] = function () {
    const state = S('gradingReview');
    const heading = A.sectionTitle(
      'GRADING',
      '教师复核台',
      '确认分数（追加版本、不发布）→ 发布（学生可见）→ 撤回（留原因）。'
      + 'AI 的分组建议只能进建议，不能进成绩。',
      ''
    );
    return heading + `<div class="panel">${exportReview(state)}</div>`;
  };

  function exportReview(state) {
    if (state.error) {
      return `<div class="alert" role="alert">${escape(state.error)}</div>
        <div class="panel-body">${reviewForm(state)}</div>`;
    }
    return `<div class="panel-body">
      ${state.notice ? `<div class="notice" role="status">${escape(state.notice)}</div>` : ''}
      ${reviewForm(state)}
      ${state.result ? `<pre class="result" data-grading-result>${escape(JSON.stringify(state.result, null, 2))}</pre>` : ''}
    </div>`;
  }

  function reviewForm(state) {
    return `
      <div class="field">
        <label for="grading-submission">提交标识</label>
        <input id="grading-submission" data-input="grading-submission"
               value="${escape(state.submissionId || '')}"
               placeholder="提交 id（来自学生交卷回执）">
      </div>
      <div class="field">
        <label>逐项得分</label>
        <p class="muted small">填「提交项 id = 分数」，一行一项。分项合计必须等于项分。</p>
        <textarea data-input="grading-items" rows="4"
          placeholder="dd000000-…0001 = 5.00">${escape(itemLines(state))}</textarea>
      </div>
      <div class="button-row">
        ${A.button('确认分数（不发布）', 'grading-confirm', 'check', '', 'primary')}
        ${A.button('发布成绩', 'grading-publish', 'upload')}
        ${A.button('撤回成绩', 'grading-withdraw', 'undo')}
      </div>
      <p class="muted small">
        确认后状态是 <code>confirmed</code>；发布后学生才可见；撤回会新增一条
        <code>withdrawn</code> 版本并保留历史。
      </p>`;
  }

  function itemLines(state) {
    return Object.entries(state.itemScores || {})
      .map(([itemId, score]) => `${itemId} = ${score}`)
      .join('\n');
  }

  function parseItems(text) {
    const scores = {};
    String(text || '').split('\n').forEach(line => {
      const [left, right] = line.split('=');
      if (!left || right == null) return;
      const itemId = left.trim();
      const score = right.trim();
      if (itemId && score) scores[itemId] = score;
    });
    return scores;
  }

  A.inputs['grading-submission'] = function (input) {
    S('gradingReview').submissionId = input.value.trim();
    return undefined;
  };

  A.inputs['grading-items'] = function (input) {
    S('gradingReview').itemScores = parseItems(input.value);
    return undefined;
  };

  function runReview(action, task) {
    const state = S('gradingReview');
    if (state.busy) return Promise.resolve();
    state.busy = action;
    state.error = null;
    state.notice = null;
    A.render();
    return Promise.resolve().then(task).then(result => {
      state.busy = null;
      state.result = result;
      state.notice = '已提交到服务端';
      A.render();
    }).catch(error => {
      state.busy = null;
      state.error = A.gradingErrorText(error);
      A.render();
    });
  }

  function needSubmission(state) {
    if (!state.submissionId) {
      state.error = '先填提交标识';
      A.render();
      throw new Error('missing submission');
    }
    return state.submissionId;
  }

  A.actions['grading-confirm'] = function () {
    const state = S('gradingReview');
    return runReview('confirm', () => {
      const submissionId = needSubmission(state);
      const body = A.gradingConfirmBody({
        itemScores: state.itemScores,
        itemMaxScores: state.itemMaxScores,
        baseRevisionNo: state.baseRevisionNo
      });
      if (!body.decisions.length) throw new Error('没有可确认的项');
      return A.api.gradingConfirm(submissionId, body);
    });
  };

  A.actions['grading-publish'] = function () {
    const state = S('gradingReview');
    return runReview('publish', () => {
      const submissionId = needSubmission(state);
      const items = Object.keys(state.itemScores || {});
      return A.api.gradingPublish(submissionId, items, nextKey('publish'));
    });
  };

  A.actions['grading-withdraw'] = function () {
    const state = S('gradingReview');
    return runReview('withdraw', () => {
      const submissionId = needSubmission(state);
      const reason = A.prompt ? A.prompt('撤回原因（学生可见）') : '教师撤回';
      if (!reason) throw new Error('缺少撤回原因');
      return A.api.gradingWithdraw(submissionId, reason, nextKey('withdraw'));
    });
  };

  function nextKey(prefix) {
    return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  }

  A.after['grading-review'] = function () {
    /* 页面无需自动取数：一切以教师显式操作为准，避免"打开就扣分"。 */
  };

  // ---------------------------------------------------------------------------
  // 学生成绩与申诉
  // ---------------------------------------------------------------------------
  A.views['student-feedback'] = function () {
    const state = S('studentFeedback');
    const heading = A.sectionTitle(
      'MY GRADES',
      '我的成绩',
      '只显示已发布的成绩版本；重评草稿不会提前出现。对某一条成绩有疑问可以申诉。',
      `<div class="button-row">${A.button('刷新', 'feedback-refresh', 'refresh')}</div>`
    );
    return heading + `<div class="panel">${studentBody(state)}</div>`;
  };

  function studentBody(state) {
    if (state.error) {
      return `<div class="alert" role="alert">${escape(state.error)}</div>`;
    }
    if (state.loading) return '<p class="muted">正在读取成绩…</p>';
    if (!state.grades || !state.grades.length) {
      return '<p class="muted">还没有已发布的成绩。确认不等于发布，教师发布后这里才会出现。</p>';
    }
    return state.grades.map(grade => `
      <div class="grade-card" data-grade="${escape(grade.revision_no)}">
        <h5>第 ${escape(grade.revision_no)} 版 · 总分 ${escape(grade.total_score)}</h5>
        <p class="muted small">发布：${escape(grade.published_at || '')}</p>
        <ul>${(grade.items || []).map(item => `<li>${escape(item.submission_item_id)}：
          ${escape(item.score)}</li>`).join('')}</ul>
        <div class="button-row">
          ${A.button('申诉这一版', 'feedback-appeal', 'message',
            `data-revision="${escape(grade.revision_no)}"`, 'small')}
        </div>
        <p class="muted small">${escape(A.gradingAppealState(state, grade.revision_no))}</p>
      </div>`).join('');
  }

  A.after['student-feedback'] = function () {
    const state = S('studentFeedback');
    if (state.loaded) return;
    state.loaded = true;
    state.loading = true;
    A.render();
    A.api.myGrades().then(grades => {
      state.grades = grades || [];
      state.loading = false;
      A.render();
    }).catch(error => {
      state.loading = false;
      state.error = A.gradingErrorText(error);
      A.render();
    });
  };

  A.actions['feedback-refresh'] = function () {
    const state = S('studentFeedback');
    state.loaded = false;
    state.error = null;
    return A.after['student-feedback']();
  };

  A.actions['feedback-appeal'] = function (button) {
    const state = S('studentFeedback');
    const revisionNo = Number(button.dataset.revision);
    const grade = (state.grades || []).find(item => item.revision_no === revisionNo);
    if (!grade) return undefined;
    const reason = A.prompt ? A.prompt('申诉理由（教师会看到）') : '我对这一版成绩有疑问';
    if (!reason) return undefined;
    A.api.gradingAppeal({
      submission_id: grade.submission_id,
      grade_revision_id: grade.revision_id || grade.submission_id,
      reason
    }).then(result => {
      state.appeals[revisionNo] = { status: result.status };
      A.render();
    }).catch(error => {
      state.error = A.gradingErrorText(error);
      A.render();
    });
    return undefined;
  };
})();
