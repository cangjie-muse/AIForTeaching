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

  const STATUS_SHORT = {
    draft: '草稿',
    'machine-checked': '已机检',
    'teacher-reviewed': '教师已确认',
    published: '已发布',
    retired: '已停用'
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
    courses: [],
    items: [],
    filters: { course_id: '', category: '', priority: '' },
    checked: {},
    diffs: {},       // 按「课程/题目」缓存修订历史，避免每次重渲染都请求
    busy: false,
    // 两块工作分开看：习题逐题判断、图谱按章看树。混在一页里，审题时被几百个
    // 节点挤走视线，审图时又被题队列打断。
    tab: 'exercises',   // exercises | graph
    graph: null,        // 当前课程的知识图谱审核状态（含每个节点）
    graphOpen: {},      // 展开了哪几章（默认全收起）
    editingNode: '',    // 正在就地改标题的节点
    editingTitle: '',   // 改到一半的标题
    ledger: null,     // 当前课程的全部题目审核总表
    ledgerTier: '',   // 总表的等级筛选
    ledgerQuery: '',  // 总表的题号/题干搜索
    verification: null,      // 按题号查到的审核记录
    lookupId: '',            // 查询框里填的题号
    detailVerification: null // 当前打开那条任务的完整审核记录
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

  // 一道题"标注过什么"。徽标只说等级，这里说清楚证据：
  // 谁在什么时候审的、Lean 走到哪一步、审完之后有没有被改过。

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
      const course = state.filters.course_id;
      const [summary, queue, courses, graph, ledger] = await Promise.all([
        A.api.get('/api/v1/review/summary'),
        A.api.get(`/api/v1/review/queue?${query()}`),
        A.api.get('/api/v1/courses'),
        // 图谱审核按课程走：没选课就是没有树可审，别把 14 门课的节点全拉下来
        course ? A.api.reviewGraph(course) : Promise.resolve(null),
        course
          ? A.api.reviewLedger({
            course_id: course, tier: state.ledgerTier,
            question: state.ledgerQuery, limit: 100
          })
          : Promise.resolve(null)
      ]);
      state.summary = summary;
      state.items = queue.items || [];
      state.courses = courses.items || [];
      state.graph = graph;
      state.ledger = ledger;
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

  // 学生可见范围：把"审核到什么程度才给学生看"变成一句可执行的口径。
  // 两级：全局默认 + 课程覆盖。选中某一档时先把"这一档会放出多少题/多少节点"算出来，
  // 而不是等保存之后发现学生端空了再回头查。
  function scopeStatus(policy) {
    const names = (policy.labels && {});
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


  // 知识图谱审核：按章通过。几百个节点逐个点不现实，老师看图本来也是按章看。

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

  function courseFilter() {
    const courses = S().courses || [];
    if (!courses.length) return '';
    const current = S().filters.course_id;
    return `<label class="rv-select">
      <span>课程</span>
      <select data-input="review-course">
        <option value="">全部课程（${courses.length}）</option>
        ${courses.map(course => `<option value="${e(course.id)}"${course.id === current ? ' selected' : ''}>${e(course.title)}（${course.exercise_count ?? 0} 题）</option>`).join('')}
      </select>
    </label>`;
  }

  // 一门课全部题目的审核总表。队列回答"接下来审哪几道"，总表回答
  // "这门课每道题各自什么状态"——后者才是"哪些还没人审"能被发现的地方。

  // 审核队列只装"待审"的题。已发布的题不在队列里，但老师最常要查的偏偏是它们，
  // 所以给一个按题号直接查的口子。

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

  function diffKey(item) {
    return `${item.course_id}/${item.exercise_id}`;
  }

  async function loadDiff(item) {
    const key = diffKey(item);
    S().diffs = S().diffs || {};
    const cached = S().diffs[key];
    if (cached && cached.status === 'ready') return;
    S().diffs[key] = { status: 'loading' };
    A.render();
    try {
      const course = encodeURIComponent(item.course_id);
      const exercise = encodeURIComponent(item.exercise_id);
      const data = await A.api.get(`/api/v1/review/exercises/${course}/${exercise}/revisions`);
      S().diffs[key] = { status: 'ready', current: data.current || {}, revisions: data.revisions || [] };
    } catch (error) {
      S().diffs[key] = { status: 'error', error: error.message };
    }
    A.render();
  }

  // 差异按"最近一次修改前 → 现在"呈现。这里显示的是**原文**而不是渲染后的公式：
  // 渲染会把 \ 与空格吃掉，而审核员要看的往往正是这些。
  function diffHunk(before, after) {
    const parts = A.diffText(before || '', after || '');
    if (!parts.length) return '<span class="muted">无变化</span>';
    const html = parts.map(part => {
      if (part.type === 'same') return e(part.text);
      const tag = part.type === 'add' ? 'ins' : 'del';
      return `<${tag} class="df-${part.type}">${e(part.text)}</${tag}>`;
    }).join('');
    return `<div class="df-text">${html}</div>`;
  }

  function scalarHunk(before, after) {
    const diff = A.diffField(before, after);
    if (!diff.changed) return '<span class="muted">无变化</span>';
    return `<span class="df-scalar"><del>${e(before ?? '—')}</del> → <ins>${e(after ?? '—')}</ins></span>`;
  }

  function setHunk(before, after) {
    const diff = A.diffSet(before, after);
    if (!diff.added.length && !diff.removed.length) return '<span class="muted">无变化</span>';
    return [
      ...diff.added.map(id => `<ins class="df-add">+ ${e(id)}</ins>`),
      ...diff.removed.map(id => `<del class="df-del">- ${e(id)}</del>`)
    ].join(' ');
  }

  function revisionSection(item) {
    const cached = (S().diffs || {})[diffKey(item)] || { status: 'idle' };
    if (cached.status === 'loading' || cached.status === 'idle') {
      return '<p class="muted small">正在读取修订历史…</p>';
    }
    if (cached.status === 'error') {
      return `<p class="muted small">读不到修订历史：${e(cached.error)}</p>`;
    }
    const revisions = cached.revisions || [];
    if (!revisions.length) {
      return '<p class="muted small">这道题还没有修订记录——当前内容就是导入时的样子。</p>';
    }
    const latest = revisions[0];
    const current = cached.current || {};
    const when = latest.changed_at ? String(latest.changed_at).replace('T', ' ').slice(0, 16) : '时间未知';
    return `<details class="rv-diff" open>
      <summary>${A.icon('layers')} 修订差异：第 ${e(latest.version)} 次修改前 → 现在
        <span class="muted small">${e(latest.changed_by || '改动者未知')} · ${e(when)}${latest.change_note ? ` · ${e(latest.change_note)}` : ''}</span>
      </summary>
      <dl class="df-list">
        <dt>题干</dt><dd>${diffHunk(latest.prompt_md, current.prompt_md)}</dd>
        <dt>答案</dt><dd>${diffHunk(latest.answer_md, current.answer_md)}</dd>
        <dt>难度</dt><dd>${scalarHunk(latest.difficulty, current.difficulty)}</dd>
        <dt>书页</dt><dd>${scalarHunk(latest.source_page, current.source_page)}</dd>
        <dt>挂载</dt><dd>${setHunk(latest.node_ids, current.node_ids)}</dd>
      </dl>
      ${revisions.length > 1 ? `<p class="muted small">此前还有 ${revisions.length - 1} 次修改记录。</p>` : ''}
      <p class="muted small">显示的是 Markdown 原文，不是渲染后的公式——空格与反斜杠常常正是被改坏的地方。</p>
    </details>`;
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
      <h3>审核记录</h3>
      ${S().detailVerification && S().detailVerification.exercise_id === item.exercise_id
        ? A.reviewScopeOf().verificationBlock(S().detailVerification)
        : `<p class="muted small">正在读取…（若一直停在这里，点上面的「刷新」）</p>`}
      <h3>题干</h3>
      <div class="md">${A.markdown(item.prompt_md || '')}</div>
      <h3>挂载知识点</h3>
      <p>${nodes}</p>
      <h3>修订差异</h3>
      ${revisionSection(item)}
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

  // 面板拆到了 js/review-scope.js（文件太大，eslint 有行数上限）。
  // 这里只留一个取用点，两侧加载顺序无关：面板渲染时才去取。
  A.reviewScopeOf = () => A.reviewScope;

  // 老会话里可能没有 tab 字段：统一按"习题审核"兜底，别让两个标签都不高亮。
  function currentTab() {
    return S().tab === 'graph' ? 'graph' : 'exercises';
  }

  function tabs() {
    // 两块工作各自什么状态，直接写在入口上——不用点进去才知道有没有活要干。
    const state = S();
    const tab = currentTab();
    const graphScope = state.filters.course_id;
    const graphCount = state.graph && graphScope
      ? (state.graph.totals || {}).nodes : 0;
    const pending = (state.summary?.task_states || [])
      .find(row => row.state === 'pending')?.n || 0;
    const item = (key, label, badge) => `<button class="rv-tab ${tab === key ? 'active' : ''}"
      data-action="review-tab" data-tab="${key}" role="tab" aria-selected="${tab === key}">
      ${label}${badge ? `<em>${badge}</em>` : ''}</button>`;
    return `<div class="rv-tabs" role="tablist">
      ${item('exercises', '习题审核', pending ? `${pending} 待审` : '')}
      ${item('graph', '知识图谱审核', graphCount ? `${graphCount} 个节点` : '')}
    </div>`;
  }

  A.views.review = function () {
    const state = S();
    const heading = A.sectionTitle(
      'REVIEW WORKBENCH',
      '内容审核',
      '机器标出可疑之处，人做判断。通过、退回、发布都是独立动作，全部留痕。',
      `<div class="button-row">
        ${currentTab() === 'exercises' ? A.button('从数据生成队列', 'review-build', 'refresh') : ''}
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

    if (currentTab() === 'graph') {
      return heading + tabs() + A.reviewScopeOf().graphPanel();
    }

    const openId = state.open;
    const detail = openId ? state.items.find(item => String(item.task_id) === String(openId)) : null;

    return heading + tabs() + statsPanel()
      + `<div class="rv-filters">${courseFilter()}${issueSummary()}</div>`
      + A.reviewScopeOf().lookupBar() + A.reviewScopeOf().ledgerPanel() + `
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

  A.actions['review-tab'] = function (button) {
    S().tab = button.dataset.tab === 'graph' ? 'graph' : 'exercises';
    A.render();
  };

  A.inputs['review-course'] = function (select) {
    S().filters.course_id = select.value;
    S().graph = null;
    S().editingNode = '';
    reload();
  };

  A.actions['review-filter'] = function (button) {
    S().filters.category = button.dataset.category || '';
    reload();
  };

  A.actions['review-check'] = function (input) {
    S().checked[input.dataset.task] = input.checked;
    A.render();
  };

  A.actions['review-open'] = async function (button) {
    const taskId = button.dataset.task;
    S().open = taskId;
    S().detailVerification = null;
    A.render();
    const item = S().items.find(row => String(row.task_id) === String(taskId));
    if (item) {
      loadDiff(item);  // 修订差异与审核记录互不依赖，并行取
      try {
        S().detailVerification = await A.api.exerciseVerification(item.course_id, item.exercise_id);
      } catch (error) {
        S().detailVerification = { error: error.message };
      }
      A.render();
    }
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

  // 一次决定动的是两层：任务状态（队列）+ 题目自身的可信等级（总表、内容页、可见范围）。
  // 不同步作废缓存的话，就会出现"通过了但其他面板一动不动"。
  function invalidateAfterDecision(exercise) {
    if (A.state.content) A.state.content.content = null;   // 内容页缓存了旧等级
    S().detailVerification = null;
    if (S().verification && exercise && S().verification.exercise_id === exercise.exercise_id) {
      S().verification = null;   // 按题号查的审核记录也过期了
    }
  }

  function decisionMessage(verb, exercise) {
    const tier = (TIER_LABELS[exercise.tier] || TIER_LABELS.none)[0];
    const id = exercise.exercise_id || '这道题';
    if (verb === 'approve') {
      return `已通过：${id} 标为人工已审（${tier}）。学生仍看不到——要进「课程内容」发布后才放出。`;
    }
    if (verb === 'request-changes') {
      return `已退回：${id} 的人工审核标记已撤销，学生端不会放出。`;
    }
    if (verb === 'publish') {
      return `已发布：${id} 现在对符合可见范围的学生可见。`;
    }
    return `${id} 已更新。`;
  }

  A.actions['review-decide'] = async function (button) {
    const verb = button.dataset.verb;
    const taskId = button.dataset.task;
    const label = verb === 'claim' ? '领取这条任务' : verb === 'approve' ? '通过这道题（标为人工已审）' : '退回要求修改（撤销人工审核）';
    if (verb !== 'claim' && !window.confirm(`${label}？此操作会写入审计日志。`)) return;
    try {
      const result = await A.api.post(`/api/v1/review/tasks/${taskId}/decision`, { action: verb });
      const exercise = result.exercise || {};
      invalidateAfterDecision(exercise);
      A.toast(decisionMessage(verb, exercise));
      S().open = null;
      reload();
    } catch (error) {
      A.toast(error.message);
    }
  };

  A.actions['review-batch-approve'] = async function () {
    const ids = Object.keys(S().checked).filter(key => S().checked[key]);
    if (!ids.length) return;
    if (!window.confirm(`把这 ${ids.length} 道题标为「人工已审」？\n\n注意：这个动作等于你逐题看过了。只批量处理你确实翻过的题；没看的别勾。`)) return;
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
    if (A.state.content) A.state.content.content = null;
    S().verification = null;
    A.toast(`已把 ${ok} / ${ids.length} 道题标为人工已审；学生仍看不到，发布后才放出。`);
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

  // 供 js/review-scope.js 用：状态读取与徽标渲染只在这里定义一次，
  // 避免两个文件各写一套、等级名字对不上。
  A.reviewShared = {
    S,
    scopeStatus,
    tierBadge,
    TIER_LABELS,
    STATE_LABELS,
    STATUS_SHORT,
    reload
  };
})();
