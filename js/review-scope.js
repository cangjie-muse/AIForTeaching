/* 内容审核页的"可见范围 / 知识图谱 / 审核总表 / 按题号查"四块。
 *
 * 和 js/review.js 的分工：那边是队列与详情（"接下来审哪几道"），
 * 这边是口径与全量清单（"这门课 400 道题各自什么状态"）。
 * 拆开是因为 review.js 已经超过 eslint 的行数上限，再堆下去没人愿意读。
 * 共用工具（状态读取、等级徽标）由 A.reviewShared 提供，不在这里重复定义。
 */
(function () {
  const A = App, e = A.escape;
  const S = () => A.state.review;
  // 延迟取：两个文件的加载顺序不影响渲染。
  const shared = () => A.reviewShared || {};

  function verificationBlock(data) {
    if (!data) return '';
    if (data.error) {
      return `<div class="rv-verify rv-verify-error"><b>读不到审核记录</b><p class="muted small">${e(data.error)}</p></div>`;
    }
    const moment = value => (value
      ? e(String(value).slice(0, 19).replace('T', ' '))
      : '—');
    const human = data.human_verified_at
      ? `人工审核通过：${moment(data.human_verified_at)}${data.human_verified_by ? ` · ${e(data.human_verified_by)}` : ''}`
      : '尚未人工审核';
    const proofs = data.proofs || [];
    let lean;
    if (proofs.length) {
      lean = proofs.map(proof => {
        const state = proof.compiled ? '编译通过' : '编译未通过';
        const faithful = proof.faithful_status === 'teacher-reviewed'
          ? '忠实性已确认'
          : `忠实性：${e(proof.faithful_status || 'draft')}`;
        return `<li>#${proof.ord} · ${state} · ${faithful}${proof.toolchain ? ` · ${e(proof.toolchain)}` : ''}${proof.has_statement ? '' : ' · 缺形式化命题'}</li>`;
      }).join('');
      lean = `<ul class="rv-verify-lean">${lean}</ul>`;
    } else if (data.lean_available) {
      lean = '<span class="muted">没有 Lean 证明</span>';
    } else {
      lean = '<span class="muted">Lean 验证尚未接入（表与判定已就位）</span>';
    }
    const task = data.task
      ? `最近一次审核任务：${e(({ pending: '待审', 'in-review': '审核中', approved: '已通过', rejected: '已驳回', published: '已发布', 'changes-requested': '待修改' })[data.task.state] || data.task.state)}`
      : '还没有审核任务记录';
    return `<div class="rv-verify">
      <div class="rv-verify-head">
        ${shared().tierBadge(data.tier)}
        ${A.badge(`内容版本 v${data.content_version ?? 1}`, '')}
        ${data.edited_after_review ? `<span class="rv-warn">⚠ 审核通过后又被改过</span>` : ''}
      </div>
      <p class="rv-verify-line">${human}</p>
      <p class="rv-verify-line">Lean：${lean}</p>
      <p class="rv-verify-line muted small">${task} · 工作流状态 ${e(data.review_status || '—')} · 最后更新 ${moment(data.updated_at)}</p>
    </div>`;
  }

  function policyPanel() {
    const policy = S().visibility;
    if (!policy) return '';
    const draft = S().policyDraft || {
      exercise_tier: policy.effective.exercise_tier,
      qa_tier: policy.effective.qa_tier,
      graph_tier: policy.effective.graph_tier
    };
    const scope = S().policyScope || '';
    const options = current => (policy.options || ['human', 'verified', 'all']).map(key =>
      `<option value="${key}"${key === current ? ' selected' : ''}>${e(policy.labels[key] || key)} · 学生可见 ${policy.impact[key] ?? 0} 题</option>`).join('');
    const graphOptions = current => (policy.graph_options || ['all', 'human']).map(key =>
      `<option value="${key}"${key === current ? ' selected' : ''}>${e(policy.graph_labels[key] || key)} · 学生可见 ${policy.graph.impact[key] ?? 0} 节点</option>`).join('');
    const chips = (policy.tiers || []).map(row =>
      `<span class="rv-tier">${shared().tierBadge(row.tier)}<b>${row.total}</b> 题，已发布 ${row.published}</span>`).join('');
    const visible = policy.impact[draft.exercise_tier] ?? 0;
    const graphVisible = policy.graph.impact[draft.graph_tier] ?? 0;
    const warnings = [];
    if (visible === 0) {
      warnings.push(`这一档现在放不出任何已发布习题——还没有题达到这个验证等级。要么先去审核，要么换回更宽的档。`);
    }
    if (draft.graph_tier === 'human' && graphVisible === 0) {
      warnings.push('这一档下没有任何节点通过人工审核，学生端会看不到知识图谱。');
    }
    if (draft.exercise_tier === 'verified') {
      warnings.push('Lean 验证尚未接入：选这一档目前与「仅人工审核通过」效果相同。');
    }
    const warningHtml = warnings.map(text =>
      `<div class="notice amber">${A.icon('info')}<span>${e(text)}</span></div>`).join('');
    const scopeOptions = [{ id: '', title: '全局默认（所有课程）', has_override: false }]
      .concat(policy.courses || [])
      .map(course => `<option value="${e(course.id)}"${course.id === scope ? ' selected' : ''}>${e(course.title)}${course.has_override ? '（单独设置）' : ''}</option>`)
      .join('');
    return `<div class="panel rv-policy">
      <div class="rv-policy-head">
        <div><h2>学生可见范围</h2><p class="muted">审核不是二元的。这里决定学生端的习题、AI 问答、知识图谱从哪一档可信等级取内容；它与发布闸是「与」的关系：先发布，再按等级过滤。口径分两级，课程可以覆盖全局。</p></div>
        <div class="button-row">
          ${scope && policy.override ? `<button class="btn small" data-action="visibility-reset">取消本课设置</button>` : ''}
          ${A.button('保存设置', 'visibility-save', 'check', '', 'primary small')}
        </div>
      </div>
      <div class="rv-policy-scope">
        <label class="rv-select"><span>作用范围</span>
          <select data-input="visibility-scope">${scopeOptions}</select>
        </label>
        <p class="muted small">${shared().scopeStatus(policy)}</p>
      </div>
      <div class="rv-policy-row">
        <label class="rv-select"><span>习题与测验</span><select data-input="visibility-tier" data-kind="exercise">${options(draft.exercise_tier)}</select></label>
        <label class="rv-select"><span>AI 问答（完整解答）</span><select data-input="visibility-tier" data-kind="qa">${options(draft.qa_tier)}</select></label>
        <label class="rv-select"><span>知识图谱</span><select data-input="visibility-tier" data-kind="graph">${graphOptions(draft.graph_tier)}</select></label>
      </div>
      <div class="rv-policy-tiers">${chips}
        <span class="rv-tier">${'图谱'}<b>${policy.graph.verified}</b>/${policy.graph.total} 节点已人工审核</span>
      </div>
      ${warningHtml}
      <p class="muted small">当前设置下，学生端可见 <b>${visible}</b> 道已发布习题、<b>${graphVisible}</b> 个图谱节点；当前范围共 ${policy.total} 道题（已发布 ${policy.published_total}）、${policy.graph.total} 个节点。</p>
      <p class="muted small">${policy.updated_at ? `上次修改：${e(String(policy.updated_at).slice(0, 19).replace('T', ' '))}${policy.updated_by ? ` · ${e(policy.updated_by)}` : ''}` : '还没有改过，当前是默认设置（全部）。'}</p>
    </div>`;
  }

  function graphPanel() {
    const facts = S().graph;
    const scope = S().policyScope;
    if (!scope) {
      return `<div class="panel rv-graph"><h2>知识图谱审核</h2>
        <p class="muted">教材切片正文不审（原文转载）。需要人工确认的是图谱结构挂得对不对——在上面选一门课，就能按章通过。</p></div>`;
    }
    if (!facts) return '';
    const totals = facts.totals || { nodes: 0, verified: 0 };
    const rows = (facts.chapters || []).map(chapter => {
      const done = chapter.verified >= chapter.nodes;
      return `<tr>
        <td class="rv-main"><b>${e(chapter.title || chapter.chapter_id)}</b><small>${e(chapter.course_id)} · ${chapter.nodes} 个节点</small></td>
        <td>${done ? A.badge('已通过', 'green') : `<span class="muted">${chapter.verified}/${chapter.nodes} 已审</span>`}</td>
        <td class="rv-actions">
          <button class="btn small${done ? '' : ' primary'}" data-action="graph-verify" data-chapter="${e(chapter.chapter_id)}" data-verb="${done ? 'unverify' : 'verify'}">
            ${done ? '撤销本课通过' : '整章人工通过'}
          </button>
        </td>
      </tr>`;
    }).join('');
    return `<div class="panel rv-graph">
      <div class="rv-policy-head">
        <div><h2>知识图谱审核</h2>
          <p class="muted">教材切片正文不审（原文转载，审的是作者不是我们）。这里审「图谱挂得对不对」，只有人工一档——它不是定理，谈不上形式化验证。</p></div>
      </div>
      <p class="muted small">当前课程 ${totals.nodes} 个节点，已人工审核 <b>${totals.verified}</b> 个${totals.nodes && totals.verified === totals.nodes ? '（全部通过）' : ''}。</p>
      ${rows ? `<table class="rv-table"><thead><tr><th>章节</th><th>状态</th><th></th></tr></thead><tbody>${rows}</tbody></table>` : '<p class="muted">这门课还没有知识节点。</p>'}
    </div>`;
  }

  function ledgerPanel() {
    const course = S().filters.course_id;
    if (!course) {
      return `<div class="panel rv-ledger"><h2>课程审核总表</h2>
        <p class="muted">先在上面的课程筛选里选一门课，这里会列出<b>这门课的全部题目</b>及各自的审核状态（含已发布、含没人审过的）。</p></div>`;
    }
    const data = S().ledger;
    if (!data) {
      return `<div class="panel rv-ledger"><h2>课程审核总表</h2>
        <p class="muted">正在读取这门课的题目…（若一直停在这里，点上面的「刷新」）</p></div>`;
    }
    const counts = (data.counts || []).reduce((sum, row) => sum + row.n, 0);
    const chips = [{ tier: '', label: '全部' }].concat((data.counts || []).map(row => ({
      tier: row.tier, label: `${(shared().TIER_LABELS[row.tier] || [row.tier])[0]} ${row.n}`,
      n: row.n
    }))).map(chip => `<button class="chip ${S().ledgerTier === chip.tier ? 'active' : ''}"
        data-action="ledger-tier" data-tier="${e(chip.tier)}">${e(chip.label)}</button>`).join('');
    const rows = (data.items || []).map(row => `<tr>
      <td class="rv-main"><button class="rv-title" data-action="ledger-open" data-id="${e(row.id)}">${e(row.id)}</button>
        <small>${e((row.prompt || '').replace(/\s+/g, ' ').slice(0, 60))}</small></td>
      <td>${shared().tierBadge(row.tier)}</td>
      <td>${row.human_verified_at
        ? `${e(String(row.human_verified_at).slice(0, 16).replace('T', ' '))}${row.human_verified_by ? ` · ${e(row.human_verified_by)}` : ''}`
        : '<span class="muted">—</span>'}
        ${row.edited_after_review ? '<br><span class="rv-warn">审核后改过</span>' : ''}</td>
      <td>${row.lean_confirmed ? A.badge('Lean 已确认', 'green') : '<span class="muted">—</span>'}</td>
      <td>${e(shared().STATUS_SHORT[row.review_status] || row.review_status)}</td>
      <td>${e(row.task_state ? (shared().STATE_LABELS[row.task_state] || row.task_state) : '无任务')}</td>
    </tr>`).join('');
    const shown = (data.items || []).length;
    const more = data.total > shown
      ? `<p class="muted small">显示前 ${shown} / ${data.total} 道。用上面的等级筛选或搜索缩小范围。</p>`
      : `<p class="muted small">共 ${data.total} 道。</p>`;
    return `<div class="panel rv-ledger">
      <div class="rv-policy-head">
        <div><h2>课程审核总表<span class="muted small"> · ${e(course)}</span></h2>
          <p class="muted">这门课的全部题目（含已发布、含没人审过的），按可信等级排：未审核在最前。</p></div>
        <div class="button-row">
          <label class="rv-select"><span>题号或题干</span>
            <input data-input="ledger-query" value="${e(S().ledgerQuery || '')}" placeholder="如 ex-1 或 极限" autocomplete="off">
          </label>
          <button class="btn small" data-action="ledger-search">查询</button>
        </div>
      </div>
      <div class="rv-chips">${chips}</div>
      ${rows ? `<div class="rv-table-wrap"><table class="rv-table"><thead><tr><th>题目</th><th>可信等级</th><th>人工审核</th><th>Lean</th><th>工作流</th><th>审核任务</th></tr></thead><tbody>${rows}</tbody></table></div>` : `<p class="muted">这门课没有符合条件的题目（共 ${counts} 道）。</p>`}
      ${more}
    </div>`;
  }

  function lookupBar() {
    const course = S().filters.course_id;
    return `<div class="rv-lookup">
      <label class="rv-select"><span>按题号查审核记录</span>
        <input data-input="verify-id" value="${e(S().lookupId || '')}" placeholder="例如 pub-a" autocomplete="off">
      </label>
      <button class="btn small" data-action="review-verify-lookup" ${course ? '' : 'disabled'}>查询</button>
      <span class="muted small">${course ? `在「${e(course)}」里查` : '先在上面选一门课'}</span>
    </div>${S().verification ? verificationBlock(S().verification) : ''}`;
  }

  A.reviewScope = {
    verificationBlock,
    policyPanel,
    graphPanel,
    ledgerPanel,
    lookupBar
  };

  A.inputs['ledger-query'] = function (input) {
    S().ledgerQuery = input.value.trim();
  };

  A.actions['ledger-search'] = function () {
    shared().reload();
  };

  A.actions['ledger-tier'] = function (button) {
    S().ledgerTier = button.dataset.tier || '';
    shared().reload();
  };

  // 总表里点题号 = 打开这道题的审核记录（不再需要手输题号）
  A.actions['ledger-open'] = async function (button) {
    const course = S().filters.course_id;
    if (!course) return;
    S().lookupId = button.dataset.id;
    try {
      S().verification = await A.api.exerciseVerification(course, button.dataset.id);
    } catch (error) {
      S().verification = { error: error.message };
    }
    A.render();
  };

  A.inputs['verify-id'] = function (input) {
    S().lookupId = input.value.trim();
  };

  A.actions['review-verify-lookup'] = async function (button) {
    const course = S().filters.course_id;
    const exerciseId = S().lookupId;
    if (!course) {
      A.toast('先在上面选一门课，再问题号。');
      return;
    }
    if (!exerciseId) {
      A.toast('填一个题号，比如 pub-a。');
      return;
    }
    button.disabled = true;
    try {
      S().verification = await A.api.exerciseVerification(course, exerciseId);
    } catch (error) {
      S().verification = { error: error.message };
    }
    button.disabled = false;
    A.render();
  };

  A.inputs['visibility-scope'] = function (select) {
    // 换作用域 = 换一份口径，草案必须丢掉，否则会把全局的值带到某门课上
    S().policyScope = select.value;
    S().policyDraft = null;
    S().graph = null;
    shared().reload();
  };

  A.inputs['visibility-tier'] = function (select) {
    const policy = S().visibility;
    if (!policy) return;
    const draft = S().policyDraft || {
      exercise_tier: policy.effective.exercise_tier,
      qa_tier: policy.effective.qa_tier,
      graph_tier: policy.effective.graph_tier
    };
    const key = select.dataset.kind === 'qa' ? 'qa_tier'
      : select.dataset.kind === 'graph' ? 'graph_tier' : 'exercise_tier';
    draft[key] = select.value;
    S().policyDraft = draft;
    // 立刻重渲染，把"这一档会放出多少题"算给操作者看——保存前就知道后果
    A.render();
  };

  A.actions['visibility-save'] = async function (button) {
    const policy = S().visibility;
    if (!policy) return;
    const draft = S().policyDraft || {
      exercise_tier: policy.effective.exercise_tier,
      qa_tier: policy.effective.qa_tier,
      graph_tier: policy.effective.graph_tier
    };
    const scope = S().policyScope || '';
    button.disabled = true;
    try {
      await A.api.saveVisibility({
        exercise_tier: draft.exercise_tier,
        qa_tier: draft.qa_tier,
        graph_tier: draft.graph_tier
      }, scope || undefined);
      S().policyDraft = null;
      A.toast(scope ? '已保存：这门课单独设置生效（其余课程仍用全局口径）。' : '已保存：全局口径生效，没有单独设置的课程都按它来。');
      S().status = 'idle';
      shared().reload();
    } catch (error) {
      A.toast(`保存失败：${error.message}`);
    }
    button.disabled = false;
    A.render();
  };

  A.actions['visibility-reset'] = async function (button) {
    const scope = S().policyScope;
    if (!scope || !window.confirm('取消这门课的单独设置，让它回到跟随全局？')) return;
    button.disabled = true;
    try {
      await A.api.resetVisibility(scope);
      S().policyDraft = null;
      A.toast('已取消：这门课回到跟随全局。');
      S().status = 'idle';
      shared().reload();
    } catch (error) {
      A.toast(`取消失败：${error.message}`);
    }
    button.disabled = false;
    A.render();
  };

  A.actions['graph-verify'] = async function (button) {
    const scope = S().policyScope;
    if (!scope) return;
    const verifying = button.dataset.verb !== 'unverify';
    if (!verifying && !window.confirm('撤销这一章的人工审核？学生端会重新看不到它（若图谱口径为仅人工审核）。')) return;
    button.disabled = true;
    try {
      const result = await A.api.verifyGraph({
        course_id: scope, chapter_id: button.dataset.chapter,
        action: verifying ? 'verify' : 'unverify'
      });
      A.toast(verifying ? `已通过 ${result.changed} 个节点。` : `已撤销 ${result.changed} 个节点。`);
      S().status = 'idle';
      shared().reload();
    } catch (error) {
      A.toast(`操作失败：${error.message}`);
    }
    button.disabled = false;
    A.render();
  };

})();
