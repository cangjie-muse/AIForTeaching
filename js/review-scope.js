/* 内容审核页的两块辅助面板：可见范围已搬去「学生可见范围」页（js/visibility.js），
 * 这里只剩「知识图谱审核」的章节树，以及「课程审核总表」「按题号查」这两块全量清单。
 *
 * 图谱为什么做成树：原来是一张按章的长表格，老师只看到「第一章 32 个节点」，
 * 看不到挂了哪些知识点，也就判断不了"挂得对不对"。现在每章展开就是它的子节点，
 * 逐条通过/撤销——和图谱页看到的形状一致，不用在两套表达之间换算。
 *
 * 这里**只审不改**（2026-10-22 定）：改标题、挂题、移动、删除都在知识图谱页做。
 * 理由是那些动作会让题退回未核，和"审"放在同一屏，留痕会分不清是审出来的还是改出来的。
 * 行上给一个「去图谱里改」的深链，带 node 参数，到那儿编辑面板是打开的。
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

  // ---------------- 知识图谱审核：章节树 ----------------

  // 按章归拢节点，再在章内按 parent_id 连成树。
  // 章节容器节点自己也算一章的根（它可能没有 chapter_id，用 id 顶替）。
  function groupByChapter(nodes) {
    const byChapter = new Map();
    nodes.forEach(node => {
      const key = node.chapter_id || node.id;
      if (!byChapter.has(key)) byChapter.set(key, []);
      byChapter.get(key).push(node);
    });
    return byChapter;
  }

  // 章内的根 = 父节点不在本章里的那些（父节点是章节容器、是章外节点，或者没有父节点）。
  // 只看 parent_id === chapter_id 会漏掉"父节点为空"的老数据。
  function rootsOf(chapterNodes) {
    const ids = new Set(chapterNodes.map(node => node.id));
    return chapterNodes.filter(node => !node.parent_id || !ids.has(node.parent_id));
  }

  function childrenOf(chapterNodes) {
    const byParent = new Map();
    chapterNodes.forEach(node => {
      const key = node.parent_id || '';
      if (!byParent.has(key)) byParent.set(key, []);
      byParent.get(key).push(node);
    });
    return byParent;
  }

  // 一行 = 一个知识点。列固定：名称（带缩进与导引线）· 类型 · 挂题数 · 状态 · 动作。
  // 状态和挂题数直接摆在行上，是为了"扫一遍就知道哪几行不对"，不用逐个点开。
  function nodeRow(node, byParent, depth, courseId) {
    const children = byParent.get(node.id) || [];
    const hasChildren = children.length > 0;
    const fold = S().graphOpen[`node:${node.id}`] !== false;   // 子节点默认展开
    const verified = node.verified
      ? A.badge('已通过', 'green')
      : '<span class="rv-node-state">未审</span>';
    const kind = node.kind === 'container' ? (node.level <= 2 ? '章' : '节') : '知识点';
    const mounted = Number(node.exercise_count || 0);
    return `<li class="rv-node" data-node="${e(node.id)}" data-depth="${depth}">
      <div class="rv-node-row${S().nodePanel === node.id ? ' open' : ''}">
        ${hasChildren ? `<button class="rv-fold" data-action="graph-fold" data-node="${e(node.id)}"
          aria-label="${fold ? '收起子节点' : '展开子节点'}">${fold ? '▾' : '▸'}</button>` : '<span class="rv-fold-space"></span>'}
        <span class="rv-node-title">${e(node.title)}</span>
        <span class="rv-node-kind">${kind}</span>
        <span class="rv-node-count${mounted ? '' : ' empty'}" title="直接挂在这个知识点上的题">${mounted ? `${mounted} 题` : '未挂题'}</span>
        ${verified}
        <span class="rv-node-actions">
          <button class="btn small${node.verified ? ' text-btn' : ''}" data-action="graph-node-verify"
                  data-node="${e(node.id)}" data-course="${e(courseId)}" data-verb="${node.verified ? 'unverify' : 'verify'}">
            ${node.verified ? '撤销' : '通过'}
          </button>
          <a class="btn small text-btn" href="${A.url('graph', courseId, { node: node.id })}"
             title="改标题、挂题、移动、删除都在知识图谱页做">去图谱里改</a>
        </span>
      </div>
      ${hasChildren && fold ? `<ul class="rv-tree">${children
        .map(child => nodeRow(child, byParent, depth + 1, courseId)).join('')}</ul>` : ''}
    </li>`;
  }

  function chapterCard(chapter, chapterNodes, courseId) {
    const done = chapter.verified >= chapter.nodes;
    const open = S().graphOpen[chapter.chapter_id] === true;
    const byParent = childrenOf(chapterNodes);
    // 章节容器节点（id === chapter_id）本身不算"子节点"——它的标题已经在章头上。
    // 有子节点时把子节点提上来当根，避免章头下面又出现一行同名节点。
    const children = byParent.get(chapter.chapter_id) || [];
    const roots = children.length
      ? chapterNodes.filter(node => node.parent_id === chapter.chapter_id)
      : rootsOf(chapterNodes);
    const body = roots.length
      ? `<ul class="rv-tree">${roots.map(node => nodeRow(node, byParent, 0, courseId)).join('')}</ul>`
      : '<p class="muted small">这一章没有挂知识点。</p>';
    return `<section class="rv-chapter ${done ? 'done' : ''}">
      <div class="rv-chapter-head">
        <button class="rv-chapter-toggle" data-action="graph-chapter-toggle" data-chapter="${e(chapter.chapter_id)}">
          <span class="rv-chapter-caret">${open ? '▾' : '▸'}</span>
          <b>${e(chapter.title || chapter.chapter_id)}</b>
          <span class="muted small">${chapter.nodes} 个知识点 · 已审 ${chapter.verified}</span>
        </button>
        <span class="rv-chapter-actions">
          ${done ? A.badge('本章已通过', 'green') : ''}
          ${open ? `<button class="btn small text-btn" data-action="graph-chapter-expand"
                    data-chapter="${e(chapter.chapter_id)}">展开全部</button>` : ''}
          <button class="btn small${done ? ' text-btn' : ' primary'}"
                  data-action="graph-verify" data-chapter="${e(chapter.chapter_id)}"
                  data-course="${e(courseId)}" data-verb="${done ? 'unverify' : 'verify'}">
            ${done ? '撤销本章通过' : '整章通过'}
          </button>
        </span>
      </div>
      ${open ? body : ''}
    </section>`;
  }

  function graphPanel() {
    const course = S().filters.course_id;
    const facts = S().graph;
    const heading = `<div class="rv-policy-head">
      <div><h2>知识图谱审核</h2>
        <p class="muted">教材切片正文不审（原文转载，审的是作者不是我们）。这里审「树挂得对不对」：
          每章展开就是它的知识点，行上直接写着挂了几道题，逐条通过/撤销。
          <b>这一页只审不改</b>：改标题、挂题/解绑、移动、删除都在「知识图谱」页做——
          那些动作会让受影响的题退回未核，和"审"放在一起，留痕就分不清是审出来的还是改出来的。
          行上的「去图谱里改」会带着这个节点跳过去，编辑面板是打开的。</p></div>
    </div>`;
    if (!course) {
      return `<div class="panel rv-graph">${heading}
        <p class="muted">先在上面选一门课。图谱审核按课程进行——一次只审一棵树，才看得清结构。</p>
        ${coursePicker()}</div>`;
    }
    if (!facts) {
      return `<div class="panel rv-graph">${heading}
        ${coursePicker()}<p class="muted">正在读取这门课的图谱…</p></div>`;
    }
    const totals = facts.totals || { nodes: 0, verified: 0 };
    const byChapter = groupByChapter(facts.nodes || []);
    const chapters = (facts.chapters || []).map(chapter =>
      chapterCard(chapter, byChapter.get(chapter.chapter_id) || [], course));
    const allDone = totals.nodes > 0 && totals.verified === totals.nodes;
    const root = facts.root;
    return `<div class="panel rv-graph">
      ${heading}
      ${coursePicker()}
      <div class="rv-graph-summary">
        <span>共 <b>${totals.nodes}</b> 个节点（含课程根节点），已人工审核 <b>${totals.verified}</b> 个</span>
        ${root ? `<span class="rv-root">
          ${root.verified ? A.badge('课程根节点已通过', 'green') : '<span class="muted small">课程根节点未审，学生端的树长不出来</span>'}
          <button class="btn small${root.verified ? ' text-btn' : ''}" data-action="graph-root-verify"
                  data-node="${e(root.id)}" data-course="${e(course)}" data-verb="${root.verified ? 'unverify' : 'verify'}">
            ${root.verified ? '撤销整门课' : '整门课通过'}
          </button>
        </span>` : ''}
        ${allDone ? '' : `<span class="muted small">点章节展开，逐条看；没问题也可以整章通过。</span>`}
      </div>
      ${chapters.length ? chapters.join('') : '<p class="muted">这门课还没有知识节点。</p>'}
    </div>`;
  }

  // 图谱这块自己带一个课程选择器：审核时换课比换筛选条件更频繁，
  // 藏在别的区域的筛选里容易被忽略。
  function coursePicker() {
    // 和习题那半边用同一个组合框：两处各自一个下拉，长得不一样，
    // 还得各记一次"哪门课选过"。
    return A.reviewCoursePicker({ emptyLabel: '选一门课' });
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

  // 节点管理面板在 js/review-node.js；延后取，避免脚本顺序耦合。
  A.reviewNodeOf = () => A.reviewNode || { panelFor: () => '' };

  A.reviewScope = {
    verificationBlock,
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

  // ---------------- 图谱树交互 ----------------

  A.actions['graph-chapter-toggle'] = function (button) {
    const key = button.dataset.chapter;
    S().graphOpen[key] = S().graphOpen[key] !== true;
    A.render();
  };

  A.actions['graph-fold'] = function (button) {
    const key = button.dataset.node;
    S().graphOpen[key] = S().graphOpen[key] === false;
    A.render();
  };

  // 「展开全部」：一章十几个节点时逐个点开太慢，一次铺开更好扫。
  A.actions['graph-chapter-expand'] = function (button) {
    const chapter = button.dataset.chapter;
    const chapterNodes = (S().graph?.nodes || []).filter(node => node.chapter_id === chapter);
    S().graphOpen[chapter] = true;
    chapterNodes.forEach(node => { S().graphOpen[`node:${node.id}`] = true; });
    A.render();
  };

  async function verifyNodes(payload, message) {
    try {
      const result = await A.api.verifyGraph(payload);
      A.toast(`${message}（${result.changed} 个节点）`);
      S().status = 'idle';
      shared().reload();
    } catch (error) {
      A.toast(`操作失败：${error.message}`);
    }
  }

  A.actions['graph-node-verify'] = async function (button) {
    const verifying = button.dataset.verb !== 'unverify';
    button.disabled = true;
    await verifyNodes({
      course_id: button.dataset.course,
      node_ids: [button.dataset.node],
      action: verifying ? 'verify' : 'unverify'
    }, verifying ? '已通过这个节点' : '已撤销这个节点');
    button.disabled = false;
  };

  // 整门课 = 课程根节点 + 整棵子树。学生端的图谱从根开始渲染，根没通过时
  // 只通过各章会得到一棵"有枝无干"的树。
  A.actions['graph-root-verify'] = async function (button) {
    const verifying = button.dataset.verb !== 'unverify';
    if (!verifying && !window.confirm('撤销整门课的人工审核？学生端会重新看不到整棵树（若图谱口径为仅人工审核）。')) return;
    button.disabled = true;
    await verifyNodes({
      course_id: button.dataset.course,
      node_ids: [button.dataset.node],
      action: verifying ? 'verify' : 'unverify'
    }, verifying ? '整门课已通过' : '已撤销整门课');
    button.disabled = false;
  };

  A.actions['graph-verify'] = async function (button) {
    const verifying = button.dataset.verb !== 'unverify';
    if (!verifying && !window.confirm('撤销这一章的人工审核？学生端会重新看不到它（若图谱口径为仅人工审核）。')) return;
    button.disabled = true;
    await verifyNodes({
      course_id: button.dataset.course,
      chapter_id: button.dataset.chapter,
      action: verifying ? 'verify' : 'unverify'
    }, verifying ? '整章已通过' : '已撤销本章');
    button.disabled = false;
  };

})();
