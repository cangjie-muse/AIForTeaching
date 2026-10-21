/* 学生可见范围（教师 / 管理员）：决定学生端能看到哪一档内容。
 *
 * 从「内容审核」搬出来单独成页。审核页回答"这条内容对不对"，这一页回答
 * "学生现在能看到多少"——两件事的操作节奏完全不同：审核是逐条判断，
 * 口径是偶尔调一次但不能调错（调错学生端直接空白）。放一起会让人误以为
 * 改口径也是审核动作的一部分。
 *
 * 口径两级：全局默认 + 课程覆盖。选中某一档时先把"这一档会放出多少题/节点"
 * 算出来再保存，而不是等保存完发现学生端空了再回头查。
 */
(function () {
  const A = App, e = A.escape;

  A.state.visibility = A.state.visibility || {
    status: 'idle', error: '',
    policy: null,
    scope: '',      // '' = 全局；课程号 = 这门课的覆盖
    draft: null,    // 未保存的草案
    saving: false
  };
  const S = () => A.state.visibility;

  function currentDraft() {
    const policy = S().policy;
    return S().draft || {
      exercise_tier: policy.effective.exercise_tier,
      qa_tier: policy.effective.qa_tier,
      graph_tier: policy.effective.graph_tier
    };
  }

  async function load() {
    S().status = 'loading';
    A.render();
    try {
      S().policy = await A.api.visibility(S().scope || undefined);
      S().status = 'ready';
      S().error = '';
    } catch (error) {
      S().status = error.status === 403 ? 'denied' : 'error';
      S().error = error.message;
    }
    A.render();
  }

  A.after.visibility = function () {
    if (S().status === 'idle') load();
  };

  function scopeOptions(policy) {
    return [{ id: '', title: '全局默认（所有课程）', has_override: false }]
      .concat(policy.courses || [])
      .map(course => `<option value="${e(course.id)}"${course.id === S().scope ? ' selected' : ''}>${e(course.title)}${course.has_override ? '（单独设置）' : ''}</option>`)
      .join('');
  }

  function scopeStatus(policy) {
    const label = value => (policy.labels || {})[value] || value;
    const graphLabel = value => (policy.graph_labels || {})[value] || value;
    if (policy.scope === 'global') {
      return `全局默认：习题 ${e(label(policy.effective.exercise_tier))} · 问答 ${e(label(policy.effective.qa_tier))} · 图谱 ${e(graphLabel(policy.effective.graph_tier))}`;
    }
    const tail = `全局是 习题 ${e(label(policy.global.exercise_tier))} · 问答 ${e(label(policy.global.qa_tier))} · 图谱 ${e(graphLabel(policy.global.graph_tier))}`;
    if (policy.override) {
      return `<b>这门课单独设置</b>：习题 ${e(label(policy.effective.exercise_tier))} · 问答 ${e(label(policy.effective.qa_tier))} · 图谱 ${e(graphLabel(policy.effective.graph_tier))}（${tail}）`;
    }
    return `这门课跟随全局（${tail}）；保存后才会为它单独设置`;
  }

  function panel(policy) {
    const draft = currentDraft();
    const options = current => (policy.options || ['human', 'verified', 'all']).map(key =>
      `<option value="${key}"${key === current ? ' selected' : ''}>${e(policy.labels[key] || key)} · 学生可见 ${policy.impact[key] ?? 0} 题</option>`).join('');
    const graphOptions = current => (policy.graph_options || ['all', 'human']).map(key =>
      `<option value="${key}"${key === current ? ' selected' : ''}>${e(policy.graph_labels[key] || key)} · 学生可见 ${policy.graph.impact[key] ?? 0} 节点</option>`).join('');
    const chips = (policy.tiers || []).map(row =>
      `<span class="rv-tier"><b>${row.total}</b> 题 · ${e(policy.labels[row.tier] || row.tier)}，已发布 ${row.published}</span>`).join('');
    const visible = policy.impact[draft.exercise_tier] ?? 0;
    const graphVisible = policy.graph.impact[draft.graph_tier] ?? 0;
    const warnings = [];
    if (visible === 0) {
      warnings.push('这一档现在放不出任何已发布习题——还没有题达到这个验证等级。要么先去审核，要么换回更宽的档。');
    }
    if (draft.graph_tier === 'human' && graphVisible === 0) {
      warnings.push('这一档下没有任何节点通过人工审核，学生端会看不到知识图谱。');
    }
    if (draft.exercise_tier === 'verified') {
      warnings.push('Lean 验证尚未接入：选这一档目前与「仅人工审核通过」效果相同。');
    }
    const warningHtml = warnings.map(text =>
      `<div class="notice amber">${A.icon('info')}<span>${e(text)}</span></div>`).join('');
    const dirty = S().draft && (
      draft.exercise_tier !== policy.effective.exercise_tier
      || draft.qa_tier !== policy.effective.qa_tier
      || draft.graph_tier !== policy.effective.graph_tier);
    return `<div class="panel rv-policy">
      <div class="rv-policy-head">
        <div><h2>学生可见范围</h2>
          <p class="muted">审核不是二元的。这里决定学生端的习题、AI 问答、知识图谱从哪一档可信等级取内容；
            它与发布闸是「与」的关系：先发布，再按等级过滤。口径分两级，课程可以覆盖全局。</p></div>
        <div class="button-row">
          ${S().scope && policy.override ? `<button class="btn small" data-action="visibility-reset">取消本课设置</button>` : ''}
          ${A.button('保存设置', 'visibility-save', 'check', '', 'primary small')}
        </div>
      </div>
      <div class="rv-policy-scope">
        <label class="rv-select"><span>作用范围</span>
          <select data-input="visibility-scope">${scopeOptions(policy)}</select>
        </label>
        <p class="muted small">${scopeStatus(policy)}</p>
      </div>
      <div class="rv-policy-row">
        <label class="rv-select"><span>习题与测验</span><select data-input="visibility-tier" data-kind="exercise">${options(draft.exercise_tier)}</select></label>
        <label class="rv-select"><span>AI 问答（完整解答）</span><select data-input="visibility-tier" data-kind="qa">${options(draft.qa_tier)}</select></label>
        <label class="rv-select"><span>知识图谱</span><select data-input="visibility-tier" data-kind="graph">${graphOptions(draft.graph_tier)}</select></label>
      </div>
      <div class="rv-policy-tiers">${chips}
        <span class="rv-tier">图谱 <b>${policy.graph.verified}</b>/${policy.graph.total} 节点已人工审核</span>
      </div>
      ${warningHtml}
      <p class="muted small">当前设置下，学生端可见 <b>${visible}</b> 道已发布习题、
        <b>${graphVisible}</b> 个图谱节点；当前范围共 ${policy.total} 道题（已发布 ${policy.published_total}）、
        ${policy.graph.total} 个节点。</p>
      <p class="muted small">${policy.updated_at
        ? `上次修改：${e(String(policy.updated_at).slice(0, 19).replace('T', ' '))}${policy.updated_by ? ` · ${e(policy.updated_by)}` : ''}`
        : '还没有改过，当前是默认设置（全部）。'}</p>
      ${dirty ? `<p class="rv-dirty">改动还没保存：改完点右上角「保存设置」才生效。</p>` : ''}
    </div>`;
  }

  A.views.visibility = function () {
    const heading = A.sectionTitle(
      'SYSTEM SETTINGS',
      '学生可见范围',
      '决定学生端能看到哪一档内容。调低会立刻减少学生可见的题与图谱节点。',
      `<div class="button-row">${A.button('刷新', 'visibility-reload', 'refresh')}</div>`
    );
    if (S().status === 'denied') {
      return heading + `<div class="panel">${A.empty('user', '没有这个权限', '学生可见范围只有教师与管理员能改。')}</div>`;
    }
    if (S().status === 'error') {
      return heading + `<div class="panel"><h2>读不到当前设置</h2>
        <p class="muted">${e(S().error)}</p>${A.button('重试', 'visibility-reload', 'refresh')}</div>`;
    }
    if (!S().policy) {
      return heading + `<div class="panel"><p class="muted">正在读取设置…</p></div>`;
    }
    return heading + panel(S().policy);
  };

  A.actions['visibility-reload'] = function () {
    S().status = 'idle';
    load();
  };

  A.inputs['visibility-scope'] = function (select) {
    // 换作用域 = 换一份口径，草案必须丢掉，否则会把全局的值带到某门课上
    S().scope = select.value;
    S().draft = null;
    load();
  };

  A.inputs['visibility-tier'] = function (select) {
    const policy = S().policy;
    if (!policy) return;
    const draft = currentDraft();
    const key = select.dataset.kind === 'qa' ? 'qa_tier'
      : select.dataset.kind === 'graph' ? 'graph_tier' : 'exercise_tier';
    draft[key] = select.value;
    S().draft = draft;
    // 立刻重渲染，把"这一档会放出多少题"算给操作者看——保存前就知道后果
    A.render();
  };

  A.actions['visibility-save'] = async function (button) {
    const policy = S().policy;
    if (!policy) return;
    const draft = currentDraft();
    const scope = S().scope || '';
    button.disabled = true;
    S().saving = true;
    try {
      await A.api.saveVisibility({
        exercise_tier: draft.exercise_tier,
        qa_tier: draft.qa_tier,
        graph_tier: draft.graph_tier
      }, scope || undefined);
      S().draft = null;
      S().saving = false;
      A.toast(scope ? '已保存：这门课单独设置生效（其余课程仍用全局口径）。'
        : '已保存：全局口径生效，没有单独设置的课程都按它来。');
      S().status = 'idle';
      load();
    } catch (error) {
      S().saving = false;
      button.disabled = false;
      A.toast(`保存失败：${error.message}`);
    }
  };

  A.actions['visibility-reset'] = async function (button) {
    const scope = S().scope;
    if (!scope || !window.confirm('取消这门课的单独设置，让它回到跟随全局？')) return;
    button.disabled = true;
    try {
      await A.api.resetVisibility(scope);
      S().draft = null;
      A.toast('已取消：这门课回到跟随全局。');
      S().status = 'idle';
      load();
    } catch (error) {
      button.disabled = false;
      A.toast(`取消失败：${error.message}`);
    }
  };
})();
