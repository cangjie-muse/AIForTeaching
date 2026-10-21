'use strict';
/* 内容审核工作台。
 *
 * 设计取舍（与 docs/审核工作台.md 一致）：
 *   1. 队列不是人工建的——从数据异常自动生成。所以页面第一件事是"建队"，
 *      而不是"新建任务"。
 *   2. 每条决定都落审计。页面不提供"直接改数据库"的入口。
 *   3. 题目正确性永远由人判定：机器只标"缺挂载""无溯源""页码不精确"这类可判定事实。
 *
 * 权限：审核员可判、可改；学生在服务端就无法读取队列（前端不重复实现这套逻辑，
 * 只根据 whoami 隐藏按钮，真正的拦截在后端）。
 */
(function () {
  const A = App, e = A.escape;

  const CATEGORY_LABELS = {
    'missing-node': '未挂知识点',
    'source-missing': '溯源缺失',
    'page-imprecise': '页码不精确',
    'page-missing': '无书页',
    'content-error': '内容待核',
    'answer-error': '答案存疑',
    'duplicate': '重复题',
    'format-error': '格式错误'
  };

  const TIER_LABELS = {
    none: ['未核', ''],
    lean: ['Lean 校验', 'green'],
    human: ['人工通过', 'green'],
    strongest: ['双重通过', 'green']
  };

  const STATE_LABELS = {
    pending: '待审',
    'in-review': '审核中',
    approved: '已通过',
    rejected: '已驳回',
    published: '已发布',
    'changes-requested': '待修改'
  };

  A.state.review = A.state.review || {
    status: 'idle',
    error: '',
    summary: null,
    items: [],
    filters: { course_id: '', category: '', priority: '' },
    checked: {},
    busy: false
  };

  const S = () => A.state.review;

  function categoryBadge(category) {
    const label = CATEGORY_LABELS[category] || category;
    const variant = category === 'content-error' || category === 'answer-error' ? 'amber' : '';
    return A.badge(label, variant);
  }

  function tierBadge(tier) {
    const [label, variant] = TIER_LABELS[tier] || TIER_LABELS.none;
    return A.badge(label, variant);
  }

  function priorityMark(priority) {
    const stars = '●'.repeat(Math.max(1, 6 - priority));
    const level = priority <= 2 ? 'high' : priority === 3 ? 'mid' : 'low';
    return `<span class="rv-priority ${level}" title="优先级 ${priority}（1 最高）">${stars}</span>`;
  }

  function issueList(issues) {
    if (!issues || !issues.length) return '<span class="muted">无问题标记</span>';
    return `<ul class="rv-issues">${issues.map(issue => `
      <li>${categoryBadge(issue.category)}<span>${e(issue.detail || '')}</span></li>`).join('')}</ul>`;
  }

  // ---------------------------------------------------------------------------
  // 数据
  // ---------------------------------------------------------------------------

  function query() {
    const f = S().filters;
    const params = new URLSearchParams({ state: 'pending', limit: '50' });
    if (f.course_id) params.set('course_id', f.course_id);
    if (f.category) params.set('category', f.category);
    return params.toString();
  }

  async function load() {
    const state = S();
    if (state.status === 'loading') return;
    state.status = 'loading';
    state.error = '';
    A.render();
    try {
      const [summary, queue] = await Promise.all([
        A.api.get('/api/v1/review/summary'),
        A.api.get(`/api/v1/review/queue?${query()}`)
      ]);
      state.summary = summary;
      state.items = queue.items || [];
      state.checked = {};
      state.status = 'ready';
    } catch (error) {
      state.error = error.message;
      state.status = error.status === 403 ? 'denied' : 'error';
    }
    A.render();
  }

  function reload() {
    S().status = 'idle';
    load();
  }

  // ---------------------------------------------------------------------------
  // 渲染
  // ---------------------------------------------------------------------------

  function statsPanel() {
    const summary = S().summary;
    if (!summary) return '';
    const states = summary.task_states || [];
    const pending = states.find(row => row.state === 'pending')?.n || 0;
    const approved = states.find(row => row.state === 'approved')?.n || 0;
    const published = states.find(row => row.state === 'published')?.n || 0;
    const issues = (summary.open_issues || []).reduce((sum, row) => sum + row.n, 0);
    return `<div class="rv-stats">
      <div class="rv-stat"><b>${pending}</b><span>待审</span></div>
      <div class="rv-stat"><b>${approved}</b><span>已通过</span></div>
      <div class="rv-stat"><b>${published}</b><span>已发布</span></div>
      <div class="rv-stat"><b>${issues}</b><span>未解决问题</span></div>
    </div>`;
  }

  function issueSummary() {
    const rows = S().summary?.open_issues || [];
    if (!rows.length) return '';
    return `<div class="rv-chips">${rows.map(row => `
      <button class="chip ${S().filters.category === row.category ? 'active' : ''}"
              data-action="review-filter" data-category="${e(row.category)}">
        ${e(CATEGORY_LABELS[row.category] || row.category)} <b>${row.n}</b>
      </button>`).join('')}
      ${S().filters.category ? `<button class="chip" data-action="review-filter" data-category="">全部</button>` : ''}
    </div>`;
  }

  function row(item) {
    const checked = S().checked[item.task_id] ? 'checked' : '';
    const nodeText = (item.node_ids || []).length ? `${item.node_ids.length} 个知识点` : '未挂载';
    return `<tr class="${S().checked[item.task_id] ? 'rv-selected' : ''}">
      <td class="rv-check"><input type="checkbox" data-action="review-check" data-task="${item.task_id}" ${checked} aria-label="选择第 ${item.task_id} 条"></td>
      <td class="rv-pri">${priorityMark(item.priority)}</td>
      <td class="rv-main">
        <button class="rv-title" data-action="review-open" data-task="${item.task_id}">${e((item.prompt_md || '').slice(0, 70))}</button>
        <small>${e(item.course_id)} · ${e(item.exercise_id)} · ${nodeText}${item.source_page ? ` · 第 ${item.source_page} 页` : ''}</small>
      </td>
      <td>${tierBadge(item.tier)}</td>
      <td class="rv-issue-cell">${issueList(item.issues).slice(0, 160)}</td>
      <td class="rv-actions">
        <button class="btn small" data-action="review-open" data-task="${item.task_id}">审核</button>
      </td>
    </tr>`;
  }

  function listPanel() {
    const state = S();
    if (state.status === 'loading' && !state.items.length) {
      return `<div class="panel"><p class="muted">正在读取审核队列…</p></div>`;
    }
    if (state.status === 'denied') {
      return `<div class="panel">${A.empty('user', '没有审核权限', '当前身份无法查看审核队列。审核需要 reviewer / teacher / admin 角色。')}</div>`;
    }
    if (!state.items.length) {
      return `<div class="panel">${A.empty('check', '队列是空的', '如果数据还没建队，点右上角「从数据生成队列」。机器只会挑出缺挂载、无溯源、页码不精确这类可判定问题；内容对错永远留给人判断。')}</div>`;
    }
    const checkedCount = Object.keys(state.checked).filter(key => state.checked[key]).length;
    return `<section class="panel rv-list-panel">
      <div class="panel-heading">
        <h2>待审队列 <span class="muted">${state.items.length} 条</span></h2>
        <div class="button-row">
          ${checkedCount ? `<span class="muted">已选 ${checkedCount} 条</span>
            <button class="btn small" data-action="review-batch-approve">批量通过</button>` : ''}
        </div>
      </div>
      <div class="rv-table-wrap">
        <table class="rv-table">
          <thead><tr><th></th><th>优先级</th><th>题目</th><th>可信度</th><th>问题</th><th></th></tr></thead>
          <tbody>${state.items.map(row).join('')}</tbody>
        </table>
      </div>
    </section>`;
  }

  function detailPanel(item) {
    const nodes = (item.node_ids || []).length
      ? item.node_ids.map(id => `<code>${e(id)}</code>`).join(' ')
      : '<span class="rv-warn">未挂任何知识点</span>';
    return `<section class="panel rv-detail">
      <div class="panel-heading">
        <h2>${e(item.exercise_id)}</h2>
        <div class="button-row">
          <button class="btn small text-btn" data-action="review-close">关闭</button>
        </div>
      </div>
      <div class="rv-detail-meta">
        <span>${e(item.course_id)}</span>${tierBadge(item.tier)}
        <span>${e(item.provenance || '溯源未知')}</span>
        <span>${item.source_page ? `书页 ${item.source_page}` : '无书页'}</span>
        <span>难度 ${item.difficulty ?? '—'}</span>
      </div>
      <h3>题干</h3>
      <div class="md">${A.markdown(item.prompt_md || '')}</div>
      <h3>挂载知识点</h3>
      <p>${nodes}</p>
      <h3>问题清单</h3>
      ${issueList(item.issues)}
      <div class="rv-detail-actions">
        ${A.button('领取', 'review-decide', 'check', `data-task="${item.task_id}" data-verb="claim"`, 'small')}
        ${A.button('通过', 'review-decide', 'check', `data-task="${item.task_id}" data-verb="approve"`, 'small primary')}
        ${A.button('退回修改', 'review-decide', 'close', `data-task="${item.task_id}" data-verb="request-changes"`, 'small')}
        ${A.button('修改题目', 'review-edit', 'pen', `data-task="${item.task_id}"`, 'small')}
      </div>
      <p class="muted small">决定会写入审计日志，并记录到题目的人工校验字段。通过不等于发布——发布是单独一步。</p>
    </section>`;
  }

  A.views.review = function () {
    const state = S();
    const heading = A.sectionTitle(
      'REVIEW WORKBENCH',
      '内容审核',
      '机器标出可疑之处，人做判断。通过、退回、发布都是独立动作，全部留痕。',
      `<div class="button-row">
        ${A.button('从数据生成队列', 'review-build', 'refresh')}
        ${A.button('刷新', 'review-refresh', 'refresh')}
      </div>`
    );

    if (state.status === 'error' && !state.items.length) {
      return heading + `<div class="panel">
        <h2>读不到审核队列</h2>
        <p class="muted">${e(state.error)}</p>
        <div class="api-base-row">
          <label for="api-base-input">后端地址</label>
          <input id="api-base-input" data-input="api-base" value="${e(A.api.base())}" placeholder="http://127.0.0.1:8000" autocomplete="off">
          <button class="btn primary" data-action="review-refresh">重试</button>
        </div>
      </div>`;
    }

    const openId = state.open;
    const detail = openId ? state.items.find(item => String(item.task_id) === String(openId)) : null;

    return heading + statsPanel() + issueSummary() + `
      <div class="rv-layout ${detail ? 'has-detail' : ''}">
        ${listPanel()}
        ${detail ? detailPanel(detail) : ''}
      </div>`;
  };

  A.after.review = function () {
    if (S().status === 'idle') load();
  };

  // ---------------------------------------------------------------------------
  // 动作
  // ---------------------------------------------------------------------------

  A.actions['review-refresh'] = function () { reload(); };

  A.actions['review-filter'] = function (button) {
    S().filters.category = button.dataset.category || '';
    reload();
  };

  A.actions['review-check'] = function (input) {
    S().checked[input.dataset.task] = input.checked;
    A.render();
  };

  A.actions['review-open'] = function (button) {
    S().open = button.dataset.task;
    A.render();
  };

  A.actions['review-close'] = function () {
    S().open = null;
    A.render();
  };

  A.actions['review-build'] = async function (button) {
    button.disabled = true;
    try {
      const result = await A.api.post('/api/v1/review/queue/build');
      A.toast(`新建 ${result.tasks_created} 条待审、${result.issues_created} 条问题`);
      reload();
    } catch (error) {
      A.toast(error.message);
      button.disabled = false;
    }
  };

  A.actions['review-decide'] = async function (button) {
    const verb = button.dataset.verb;
    const taskId = button.dataset.task;
    const label = verb === 'claim' ? '领取这条任务' : verb === 'approve' ? '通过这道题' : '退回要求修改';
    if (verb !== 'claim' && !window.confirm(`${label}？此操作会写入审计日志。`)) return;
    try {
      await A.api.post(`/api/v1/review/tasks/${taskId}/decision`, { action: verb });
      A.toast(`${label}：已完成`);
      S().open = null;
      reload();
    } catch (error) {
      A.toast(error.message);
    }
  };

  A.actions['review-batch-approve'] = async function () {
    const ids = Object.keys(S().checked).filter(key => S().checked[key]);
    if (!ids.length) return;
    if (!window.confirm(`批量通过 ${ids.length} 道题？\n\n注意：批量通过只解决"机器标记已处理"，不代替逐题阅读。建议只对缺页码这类机械问题使用。`)) return;
    let ok = 0;
    for (const id of ids) {
      try {
        await A.api.post(`/api/v1/review/tasks/${id}/decision`, { action: 'approve', note: '批量通过' });
        ok += 1;
      } catch (error) {
        A.toast(`第 ${id} 条失败：${error.message}`);
        break;
      }
    }
    A.toast(`已通过 ${ok} / ${ids.length} 条`);
    reload();
  };

  A.actions['review-edit'] = function (button) {
    const item = S().items.find(row => String(row.task_id) === String(button.dataset.task));
    if (!item) return;
    S().editing = item.task_id;
    A.openDialog(`修改 ${e(item.exercise_id)}`, `
      <div class="api-form">
        <label class="api-form-wide">题干（Markdown + $LaTeX$）<textarea id="rv-prompt" rows="6">${e(item.prompt_md || '')}</textarea></label>
        <label class="api-form-wide">答案<textarea id="rv-answer" rows="4"></textarea></label>
        <label>难度 1–5<input id="rv-difficulty" type="number" min="1" max="5" value="${e(item.difficulty ?? '')}"></label>
        <label>书页<input id="rv-page" type="number" value="${e(item.source_page ?? '')}"></label>
        <label class="api-form-wide">修改说明（会写进修订历史）<input id="rv-note" value="审核时修订"></label>
      </div>
      <p class="muted small">保存会自动生成一条修订快照，可以回溯改了什么。</p>
      <div class="button-row api-form-actions">
        <button class="btn primary" data-action="review-save" data-course="${e(item.course_id)}" data-exercise="${e(item.exercise_id)}">保存</button>
        <button class="btn text-btn" data-action="close-dialog">取消</button>
      </div>`);
  };

  A.actions['review-save'] = async function (button) {
    const read = id => document.getElementById(id);
    const payload = {
      prompt_md: read('rv-prompt').value,
      change_note: read('rv-note').value.trim() || '审核时修订'
    };
    const answer = read('rv-answer').value.trim();
    if (answer) payload.answer_md = answer;
    const difficulty = read('rv-difficulty').value.trim();
    if (difficulty) payload.difficulty = Number(difficulty);
    const page = read('rv-page').value.trim();
    if (page) payload.source_page = Number(page);
    button.disabled = true;
    try {
      await A.api.patch(
        `/api/v1/review/exercises/${encodeURIComponent(button.dataset.course)}/${encodeURIComponent(button.dataset.exercise)}`,
        payload
      );
      document.getElementById('dialog').close();
      A.toast('已保存，修订已留痕');
      reload();
    } catch (error) {
      A.toast(error.message);
      button.disabled = false;
    }
  };

})();
