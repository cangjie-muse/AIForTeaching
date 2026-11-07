/* QTI 导入导出页（阶段 E 任务 4）。
 *
 * 教师端的核心诉求只有一个：**这份卷子进来以后少了什么**。
 * 所以页面把差异按严重度分组（error 在前），并明确告诉教师：
 *   - 导入成功也只是草稿（draft_ready），系统不会替他"审核通过"；
 *   - 导出被拒时把服务端的 reasons 原样列出，不说"导出失败"就完事。
 *
 * 页面不做的事：不在浏览器里解析 QTI（服务端解析才能保证同一套差异规则）、
 * 不缓存导入结果（每次都以服务端返回为准）。
 */
(function () {
  const A = App;

  const SEVERITY_LABEL = { error: '必须处理', warning: '需要注意', info: '提示' };
  const CODE_TEXT = {
    missing_prompt: '缺少题干',
    invalid_score: '分值不是正数',
    unsupported_interaction: '题型不受支持，已降级为文本作答',
    foreign_governance_dropped: '外部审核/许可字段已丢弃（导入不继承外部结论）',
    item_not_object: '条目格式不对',
    qti_parse_failed: '文件无法解析',
    paper_status_draft: '卷子还没批准',
    paper_has_no_slots: '卷子里没有题目',
    slot_missing_exercise: '槽位缺题目',
    slot_missing_release: '槽位缺来源 Release',
    slot_missing_prompt: '槽位缺题干'
  };

  function escape(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function S() {
    A.state.qti = A.state.qti || {
      courseRef: null, document: '', sourceName: '', loading: false,
      error: null, notice: null, run: null, export: null
    };
    return A.state.qti;
  }

  // ---------------------------------------------------------------------------
  // 纯函数
  // ---------------------------------------------------------------------------

  A.qtiCodeText = function (code) {
    const base = CODE_TEXT[String(code || '')];
    if (base) return base;
    // slot_A1_missing_release 这类带槽位号的码：把后缀翻成人话，保留槽位号
    const match = String(code || '').match(/(?:slot_[^_]+_)(missing_.*)/);
    if (match) {
      const tail = { missing_exercise: '缺题目', missing_release: '缺来源 Release',
        missing_prompt: '缺题干' }[match[1]];
      if (tail) return `${code}：${tail}`;
    }
    return String(code || '未识别的差异');
  };

  A.qtiStatusText = function (run) {
    if (!run) return '';
    if (run.status === 'draft_ready') {
      return `导入成功：${run.item_count} 道题，已是草稿（需要你在建卷页建蓝图）。`;
    }
    if (run.status === 'rejected') {
      return `导入被拒绝：${run.error_count} 项错误需要处理后才能使用。`;
    }
    return `导入状态：${run.status}`;
  };

  /** 差异按严重度分组；组内按 code 排序，保证同一份文件每次顺序一致。 */
  A.qtiGroupDifferences = function (differences) {
    const groups = { error: [], warning: [], info: [] };
    (differences || []).forEach(item => {
      const severity = groups[item.severity] ? item.severity : 'info';
      groups[severity].push(item);
    });
    Object.keys(groups).forEach(key => {
      groups[key].sort((left, right) => String(left.code).localeCompare(String(right.code)));
    });
    return groups;
  };

  A.qtiExportText = function (exported) {
    if (!exported) return '';
    if (exported.status === 'blocked') {
      return `不能导出：${(exported.reasons || []).length} 个原因需要先处理。`;
    }
    return `已导出 ${exported.item_count} 道题（产物哈希 ${String(exported.payload_sha256 || '').slice(0, 12)}…）。`;
  };

  // ---------------------------------------------------------------------------
  // 渲染
  // ---------------------------------------------------------------------------
  function differenceList(differences) {
    const groups = A.qtiGroupDifferences(differences);
    return Object.keys(groups).map(severity => {
      if (!groups[severity].length) return '';
      return `<section class="qti-diff qti-${severity}" data-severity="${severity}">
        <h5>${escape(SEVERITY_LABEL[severity])}（${groups[severity].length}）</h5>
        <ul>${groups[severity].map(item => `<li>
          <b>${escape(A.qtiCodeText(item.code))}</b>
          <span class="muted small">${escape(item.message)}</span>
          ${item.item_ref ? `<code>${escape(item.item_ref)}</code>` : ''}
        </li>`).join('')}</ul></section>`;
    }).join('');
  }

  A.qtiHtml = function (state) {
    const current = state || S();
    const run = current.run;
    return `<div class="qti-page">
      ${current.error ? `<div class="alert" role="alert">${escape(current.error)}</div>` : ''}
      <div class="field">
        <label for="qti-course">课程号</label>
        <input id="qti-course" data-input="qti-course"
               value="${escape(current.courseRef || '')}" placeholder="analysis">
      </div>
      <div class="field">
        <label for="qti-document">QTI 文件内容（JSON）</label>
        <textarea id="qti-document" data-input="qti-document" rows="10"
          placeholder='{"items":[{"identifier":"q-1","itemBody":{"prompt":"…"},"maxScore":"10.00"}]}'>${escape(current.document || '')}</textarea>
      </div>
      <div class="button-row">
        ${A.button('导入为草稿', 'qti-import', 'upload', '', 'primary')}
        ${A.button('导出当前固定卷', 'qti-export', 'download')}
      </div>
      <p class="muted small">导入不会继承外部的「已审核」结论，一律是草稿；
        导出需要卷子已批准、每个槽位都有题目与来源。</p>
      ${run ? `<div class="qti-run" role="status" data-qti-run>
        <h5>${escape(A.qtiStatusText(run))}</h5>
        ${differenceList(run.differences)}</div>` : ''}
      ${current.export ? `<div class="qti-export" role="status" data-qti-export>
        <h5>${escape(A.qtiExportText(current.export))}</h5>
        ${(current.export.reasons || []).length
    ? `<ul>${current.export.reasons.map(reason => `<li>${escape(A.qtiCodeText(reason))}</li>`).join('')}</ul>`
    : ''}</div>` : ''}
    </div>`;
  };

  A.views['qti-transfer'] = function () {
    const state = S();
    const heading = A.sectionTitle(
      'QTI',
      '导入导出',
      '把外部 QTI 卷子导入为草稿，或把本平台已批准的固定卷导成交换包。',
      ''
    );
    return heading + `<div class="panel">${A.qtiHtml(state)}</div>`;
  };

  A.after['qti-transfer'] = function () {
    /* 不自动取数：导入/导出都由教师显式触发。 */
  };

  // ---------------------------------------------------------------------------
  // 动作
  // ---------------------------------------------------------------------------
  function run(action, task) {
    const state = S();
    if (state.loading) return Promise.resolve();
    state.loading = true;
    state.error = null;
    state.notice = null;
    A.render();
    return Promise.resolve().then(task).then(() => {
      state.loading = false;
      A.render();
    }).catch(error => {
      state.loading = false;
      state.error = A.gradingErrorText
        ? A.gradingErrorText(error)
        : ((error && error.message) || '请求失败');
      A.render();
    });
  }

  A.actions['qti-import'] = function () {
    const state = S();
    return run('import', () => {
      if (!state.courseRef) throw new Error('先填课程号');
      let payload;
      try {
        payload = JSON.parse(state.document || '{}');
      } catch (error) {
        throw new Error('文件不是合法 JSON：' + error.message);
      }
      return A.api.qtiImport({
        course_ref: state.courseRef,
        source_name: state.sourceName || null,
        document: payload
      }).then(result => { state.run = result; });
    });
  };

  A.actions['qti-export'] = function () {
    const state = S();
    return run('export', () => {
      const paperId = (A.route && A.route.params && A.route.params.paper) || '';
      if (!paperId) throw new Error('URL 里缺少卷子标识（?paper=…）');
      return A.api.qtiExport({ paper_version_id: paperId })
        .then(result => { state.export = result; })
        .catch(error => {
          const detail = (error && error.detail) || {};
          if (detail.code === 'qti_export_blocked') {
            state.export = { status: 'blocked', reasons: detail.reasons || [] };
            return;
          }
          throw error;
        });
    });
  };

  A.inputs['qti-course'] = function (input) {
    S().courseRef = input.value.trim();
    return undefined;
  };

  A.inputs['qti-document'] = function (input) {
    S().document = input.value;
    return undefined;
  };
})();
