/* 知识图谱页：学生看结构，教师/管理员在这里改结构。
 *
 * 分工（2026-10-22 定）：审核页只审不改，改标题/挂题/解绑/删节点/换父节点都在这一页做。
 * 理由：这五个动作都会让题退回未核，"审核"和"边审边改"放在同一屏，留痕就说不清了。
 *
 * 「未审」标记来自 /review/graph，与审核页同一个数据源：同一棵树不该有两个说法。
 */
(function () {
  const A = App, e = A.escape;
  const ALL = '__all__';
  // 只有能进审核页的角色才看得到编辑入口（服务端还会再拦一次）。
  const canEdit = () => Boolean(A.auth && A.auth.can('review'));
  const G = A.network = { current: null, selected: null, subtree: null, focusOnly: false, showShared: true, display: 'graph', fitScale: 1, camera: { x: 0, y: 0, scale: 1 }, drag: null };
  function options() {
    return `<option value="${ALL}">全课程知识点（${A.course.stats.knowledge}）</option>` + A.course.nodes.filter(node => node.children.length).map(node => `<option value="${e(node.id)}">${node.level === 1 ? '全课程概览（章节）' : e(node.path.slice(1).join(' / '))}</option>`).join('');
  }
  A.views.graph = function () {
    const c = A.course;
    if (!c.nodes) return A.sectionTitle('KNOWLEDGE MAP', '知识图谱', e(c.title)) + A.empty('graph', '等待知识图谱', '导入标准资源包后，即可探索节点与它们之间的关系。', A.link('导入课程资源', 'import', null, 'upload', 'primary'));
    const root = c.nodes.find(node => node.level === 1);
    // 换课再换回来，接着上次的层级看：记忆按课程分开存（App.courseMemory），
    // 所以从"第三章"换去另一门课、再换回来，不会掉回全课程视图。
    const memory = A.courseMemory(c.id);
    G.display = memory.graphDisplay === 'table' ? 'table' : 'graph';
    G.showShared = memory.graphShowShared !== false;
    const remembered = memory.graphFocus && (memory.graphFocus === ALL || c.nodes.some(node => node.id === memory.graphFocus)) ? memory.graphFocus : ALL;
    const focus = A.route.params.get('chapter') || remembered;
    memory.graphFocus = focus; A.state.networkCourse = c.id; A.state.networkFocus = focus;
    if (focus !== ALL && !c.nodes.some(node => node.id === focus && node.children.length)) {
      memory.graphFocus = A.state.networkFocus = root.id;
    }
    return A.sectionTitle('KNOWLEDGE NETWORK', '知识图谱', '全课程先看图谱；需要逐条阅读时切换表格。打开任一层级，画布都会自动适配。', A.link('课程大纲', 'outline', c.id, 'book')) + `<div class="network-toolbar"><div class="field-inline"><label for="network-scope">浏览范围</label><select id="network-scope" data-input="network-scope">${options()}</select></div><div class="search-input">${A.icon('search')}<input id="network-query" data-input="network-query" placeholder="搜索知识点并定位…" aria-label="搜索知识点"></div><button class="btn small" data-action="network-home">${A.icon('graph')}全课程知识点</button>${canEdit() ? '<span class="network-review-summary" id="network-review-summary"></span>' : ''}</div><div class="network-layout"><section class="network-board" id="network-board"><div class="network-board-head"><div><b id="network-title"></b><span id="network-count"></span></div><div class="network-head-actions"><div class="network-view-switch" id="network-view-switch" role="group" aria-label="全课程显示方式"><button data-action="network-view" data-view="graph">图谱</button><button data-action="network-view" data-view="table">表格</button></div><button class="btn small" data-action="network-parent" id="network-parent">返回上级</button></div></div><div id="network-search-results" class="network-search-results" hidden></div><div class="network-canvas" id="network-canvas"></div><div class="network-controls"><button data-action="network-zoom" data-direction="-1" aria-label="缩小图谱">−</button><input id="network-zoom-range" data-input="network-zoom-range" type="range" min="100" max="1200" step="25" value="100" aria-label="图谱缩放比例"><span id="network-scale">100%</span><button data-action="network-zoom" data-direction="1" aria-label="放大图谱">＋</button><button data-action="network-reset">适应画布</button><button data-action="network-focus" id="network-focus" disabled>仅看关联</button></div><div class="network-key"><span><i class="edge-sample"></i>章节归属</span><label><input type="checkbox" id="network-shared" data-input="network-shared" ${G.showShared ? 'checked' : ''}><i class="edge-sample shared"></i>共用习题</label><small>共用习题不表示先修关系；节点位置仅为浏览排布</small></div></section><aside class="panel network-detail" id="network-detail" hidden></aside></div>${canEdit() ? '<section class="network-edit" id="network-edit" hidden></section>' : ''}<section class="network-subtree" id="network-subtree"></section>`;
  };
  function renderSubtree(focusId) {
    const summary = G.subtree = GraphModel.subtree(A.course, focusId);
    const focus = A.node(summary.focusId);
    document.getElementById('network-subtree').innerHTML = `<div class="network-subtree-head"><div><div class="eyebrow">CURRENT SCOPE</div><h2 id="network-subtree-title">${e(focus.title)}的下属内容</h2><p>末端知识点只计叶节点；习题含中间节点直连题，按源题 ID 去重。</p></div><div class="network-subtree-stats"><span><strong>${summary.leaves.length}</strong> 个末端知识点</span><span><strong>${summary.exerciseIds.length}</strong> 道关联习题</span></div></div><button class="network-subtree-toggle" data-action="network-subtree-toggle" aria-expanded="false" aria-controls="network-subtree-panel">展开知识点与习题清单</button><div id="network-subtree-panel" role="region" aria-labelledby="network-subtree-title" hidden></div>`;
  }
  function exercisePreview(source) {
    const template = document.createElement('template');
    template.innerHTML = A.markdown(source);
    template.content.querySelectorAll('a').forEach(link => link.replaceWith(...link.childNodes));
    return template.innerHTML;
  }
  function renderSubtreeLists() {
    const scope = G.subtree, allowed = new Set(scope.exerciseIds);
    const questions = A.course.exercises.filter(exercise => allowed.has(exercise.id));
    const points = scope.leaves.map(node => `<li><button data-action="read-node" data-id="${e(node.id)}"><span>${e(node.title)}<small>${e(node.path.slice(1, -1).join(' / '))}</small></span><em>${node.exerciseIds.length} 题</em></button></li>`).join('');
    const exercises = questions.map((exercise, index) => `<li class="network-subtree-question"><a href="${A.url('exercises', A.course.id, { exercise: exercise.id })}" aria-label="打开习题 ${index + 1} 的作答页面"><h4>习题 ${index + 1}</h4>${exercisePreview((exercise.variants || [exercise])[0].prompt)}<span class="network-subtree-open">进入作答与查看解析 →</span></a></li>`).join('');
    document.getElementById('network-subtree-panel').innerHTML = `<div class="network-subtree-columns"><section><h3>末端知识点 <small>${scope.leaves.length}</small></h3><ol class="network-subtree-list network-subtree-points">${points || '<li class="network-subtree-empty">暂无末端知识点</li>'}</ol></section><section><h3>关联习题 <small>${questions.length} 道去重</small></h3><ol class="network-subtree-list network-subtree-exercises ${questions.length > 60 ? 'is-large' : ''}">${exercises || '<li class="network-subtree-empty">暂无关联习题</li>'}</ol></section></div>`;
  }
  function lines(title, capacity) {
    const chunks = [], characters = [...title]; let current = '', width = 0;
    characters.forEach(character => {
      const size = /[\x00-\x7f]/.test(character) ? .64 : 1.15;
      if (current && width + size > capacity) { chunks.push(current); current = ''; width = 0; }
      current += character; width += size;
    });
    if (current) chunks.push(current);
    return chunks;
  }
  function nodeMarkup(node) {
    const classes = ['net-node', node.isRoot ? 'net-root' : '', node.kind === 'container' ? 'net-container' : 'net-knowledge', node.appendix ? 'net-appendix' : ''].join(' ');
    const radius = node.isRoot ? 72 : node.kind === 'container' ? 40 : G.current.scope === 'all' ? 17 : 22;
    const wrapped = lines(node.title, node.isRoot ? 10 : node.kind === 'container' ? 15 : 18);
    const label = wrapped.map((part, index) => `<tspan x="0" dy="${index ? 18 : 0}">${e(part)}</tspan>`).join('');
    const labelY = node.isRoot ? 5 - (wrapped.length - 1) * 9 : radius + 24;
    const shape = `<circle class="net-hit" r="${radius + 17}"/><circle class="net-halo" r="${radius + 9}"/><circle class="net-orbit" r="${radius + 5}"/><circle class="net-dot" r="${radius}"/>${node.isRoot ? '' : `<circle class="net-spark" cy="${radius + 11}" r="3"/>`}<text class="net-label" text-anchor="middle" y="${labelY}">${label}</text>`;
    return `<g class="${classes}" transform="translate(${node.x} ${node.y})" data-node-id="${e(node.id)}" tabindex="0" role="button" aria-label="${e(node.title)}"><title>${e(node.title)}${node.kind === 'container' ? ' · 点击展开' : ' · 点击查看，双击阅读'}</title>${shape}</g>`;
  }
  function edgeMarkup(edge, index) {
    return `<g class="net-link ${edge.type === 'shared_exercise' ? 'net-shared' : 'net-contains'}" data-edge-index="${index}"><path class="net-edge" fill="none"/><path class="net-edge-hit" fill="none"/><title>${edge.type === 'shared_exercise' ? `共用 ${edge.exerciseIds.length} 道习题：${edge.exerciseIds.join('、')}` : '章节归属'}</title></g>`;
  }
  function openTable() {
    A.state.networkFocus = ALL;
    A.courseMemory().graphFocus = ALL; G.current = null; G.selected = null; G.focusOnly = false;
    document.getElementById('network-scope').value = ALL;
    document.getElementById('network-title').textContent = '全课程知识点 · 表格';
    document.getElementById('network-count').textContent = `${A.course.stats.knowledge} 个知识点 · ${A.course.stats.sections} 个章节与附录`;
    document.getElementById('network-parent').disabled = true;
    document.getElementById('network-board').classList.add('is-atlas');
    document.getElementById('network-board').classList.remove('is-global');
    document.querySelector('.network-layout').classList.add('is-atlas-layout');
    document.getElementById('network-canvas').innerHTML = GraphAtlas.render(A.course, e);
    document.getElementById('network-detail').hidden = true;
    viewSwitch(true);
    renderSubtree(A.course.nodes.find(node => node.level === 1).id);
  }
  function viewSwitch(visible) {
    document.getElementById('network-view-switch').hidden = !visible;
    document.querySelectorAll('#network-view-switch button').forEach(button => {
      const active = button.dataset.view === G.display;
      button.classList.toggle('active', active); button.setAttribute('aria-pressed', active);
    });
  }
  function guideMarkup(graph) {
    const center = graph.width / 2;
    return `<g class="net-guides" aria-hidden="true">${graph.rings.map((radius, index) => `<circle class="${index ? '' : 'major'}" cx="${center}" cy="${center}" r="${radius}"/>`).join('')}</g>`;
  }
  G.open = function (focusId) {
    if (focusId === ALL && G.display === 'table') { openTable(); return; }
    A.state.networkFocus = focusId;
    A.courseMemory().graphFocus = focusId;
    G.current = GraphModel.layout(focusId === ALL ? GraphModel.buildAll(A.course) : GraphModel.build(A.course, focusId));
    G.selected = null; G.focusOnly = false;
    const focus = A.node(G.current.focusId);
    document.getElementById('network-board').classList.remove('is-atlas');
    document.getElementById('network-board').classList.toggle('is-global', focusId === ALL);
    document.querySelector('.network-layout').classList.remove('is-atlas-layout');
    document.getElementById('network-scope').value = focusId;
    document.getElementById('network-title').textContent = focusId === ALL ? '全课程知识图谱' : focus.level === 1 ? '全课程概览 · 章节' : focus.title;
    document.getElementById('network-count').textContent = `${G.current.nodes.length - 1} 个下级节点 · ${G.current.sharedCount} 条共用习题关系`;
    document.getElementById('network-count').textContent = focusId === ALL ? `${A.course.stats.knowledge} 个知识点 · ${A.course.stats.sections} 个章节与附录 · ${G.current.sharedCount} 条共题关系` : document.getElementById('network-count').textContent;
    document.getElementById('network-parent').disabled = focusId === ALL;
    const hint = focusId === ALL ? '全部知识点已入框 · 放大至 170% 显示知识点名称 · 表格可逐条阅读' : '初始已适配全部节点 · 放大后探索局部 · 点击节点查看内容';
    document.getElementById('network-canvas').innerHTML = `<svg id="network-svg" class="${focusId === ALL ? 'net-global' : ''}" viewBox="0 0 ${G.current.width} ${G.current.height}" aria-label="${e(focus.title)}交互知识网络"><defs><radialGradient id="net-hub-gradient"><stop stop-color="#6489c7"/><stop offset="1" stop-color="#294e8d"/></radialGradient><radialGradient id="net-chapter-gradient"><stop stop-color="#a2bce4"/><stop offset="1" stop-color="#4d76b5"/></radialGradient><radialGradient id="net-point-gradient"><stop stop-color="#b8e5da"/><stop offset="1" stop-color="#52ada3"/></radialGradient></defs><g id="network-scene">${guideMarkup(G.current)}<g class="net-links">${G.current.edges.map(edgeMarkup).join('')}</g><g class="net-nodes">${G.current.nodes.map(nodeMarkup).join('')}</g></g></svg><div class="network-hint">${hint}</div>`;
    viewSwitch(focusId === ALL);
    document.getElementById('network-detail').hidden = true;
    renderSubtree(focus.id);
    G.updateGeometry(); G.bindInteractions(); G.fit(); G.highlight(null); showOverview();
    document.getElementById('network-shared').checked = G.showShared;
  };
  G.updateGeometry = function () {
    const lookup = new Map(G.current.nodes.map(node => [node.id, node]));
    document.querySelectorAll('.net-link').forEach(link => {
      const edge = G.current.edges[Number(link.dataset.edgeIndex)], a = lookup.get(edge.source), b = lookup.get(edge.target);
      link.querySelectorAll('path').forEach(path => path.setAttribute('d', `M${a.x} ${a.y}L${b.x} ${b.y}`));
    });
    document.querySelectorAll('.net-node').forEach(el => { const node = lookup.get(el.dataset.nodeId); el.setAttribute('transform', `translate(${node.x} ${node.y})`); });
  };
  G.applyCamera = function () {
    document.getElementById('network-scene').setAttribute('transform', `translate(${G.camera.x} ${G.camera.y}) scale(${G.camera.scale})`);
    const percent = Math.round(G.camera.scale / G.fitScale * 100);
    document.getElementById('network-scale').textContent = `${percent}%`;
    document.getElementById('network-zoom-range').value = Math.max(100, Math.min(1200, percent));
    document.getElementById('network-svg').classList.toggle('zoomed-labels', percent >= 170);
  };
  G.fit = function () {
    const bounds = document.querySelector('.net-nodes').getBBox();
    const padding = G.current.scope === 'all' ? 120 : 70;
    const scale = Math.min((G.current.width - 2 * padding) / Math.max(1, bounds.width), (G.current.height - 2 * padding) / Math.max(1, bounds.height));
    G.fitScale = scale;
    G.camera = { x: G.current.width / 2 - scale * (bounds.x + bounds.width / 2), y: G.current.height / 2 - scale * (bounds.y + bounds.height / 2), scale };
    G.applyCamera();
  };
  G.neighbors = function (id) {
    const visible = G.current.edges.filter(edge => G.showShared || edge.type === 'contains');
    return new Set([id, ...visible.filter(edge => edge.source === id || edge.target === id).flatMap(edge => [edge.source, edge.target])]);
  };
  G.highlight = function (hovered) {
    const selected = hovered || G.selected, nearby = selected ? G.neighbors(selected) : null;
    const focused = G.focusOnly && G.selected ? G.neighbors(G.selected) : null;
    document.querySelectorAll('.net-node').forEach(el => {
      const id = el.dataset.nodeId;
      el.classList.toggle('is-selected', id === G.selected);
      el.classList.toggle('is-near', !!nearby && nearby.has(id));
      el.classList.toggle('is-dimmed', !!nearby && !nearby.has(id));
      el.classList.toggle('is-filtered', !!focused && !focused.has(id));
    });
    document.querySelectorAll('.net-link').forEach(el => {
      const edge = G.current.edges[Number(el.dataset.edgeIndex)], incident = selected && (edge.source === selected || edge.target === selected);
      el.classList.toggle('is-emphasized', !!incident);
      el.classList.toggle('is-dimmed', !!selected && !incident);
      el.classList.toggle('is-filtered', (edge.type === 'shared_exercise' && !G.showShared) || (!!focused && (!focused.has(edge.source) || !focused.has(edge.target))));
    });
    const focusButton = document.getElementById('network-focus');
    focusButton.disabled = !G.selected; focusButton.textContent = G.focusOnly ? '退出聚焦' : '仅看关联';
  };
  // ---------------- 审核状态（与审核页同一个接口） ----------------

  function reviewRow(id) {
    const cache = A.state.networkReview;
    return cache && cache.byId ? cache.byId.get(id) : null;
  }

  function statusBadge(id) {
    if (!canEdit()) return '';
    const row = reviewRow(id);
    if (!row) return '';
    return row.verified
      ? '<span class="net-state ok">已人工审核</span>'
      : '<span class="net-state todo">未审 · 学生端看不到</span>';
  }

  function summaryChip() {
    const host = document.getElementById('network-review-summary');
    const cache = A.state.networkReview;
    if (!host || !cache || !cache.totals) return;
    const total = cache.totals.nodes || 0;
    const verified = cache.totals.verified || 0;
    host.innerHTML = verified >= total && total
      ? `<span class="net-state ok">整门课已审（${total} 个节点）</span>`
      : `<span class="net-state todo">未审 ${total - verified} / ${total} 个节点</span>`;
  }

  async function loadReviewStatus() {
    if (!canEdit()) return;
    const courseId = A.course.id;
    const stale = A.state.networkReviewStale;
    A.state.networkReviewStale = false;
    if (!stale && A.state.networkReview && A.state.networkReview.courseId === courseId) {
      summaryChip();
      return;
    }
    try {
      const data = await A.api.reviewGraph(courseId);
      const byId = new Map();
      (data.nodes || []).forEach(row => byId.set(row.id, row));
      A.state.networkReview = { courseId, byId, totals: data.totals || {} };
    } catch (error) {
      // 读不到状态不该把图谱页弄坏：徽标不显示，画布照常。
      A.state.networkReview = null;
    }
    summaryChip();
    // 只重画右侧那一块，不动画布——否则每次取状态都会把用户的缩放位置清掉。
    if (G.selected) {
      const detail = document.getElementById('network-detail');
      if (detail && A.node(G.selected)) detail.innerHTML = knowledgeDetail(A.node(G.selected));
    }
  }

  function showOverview() {
    const focus = A.node(G.current.focusId);
    document.getElementById('network-detail').innerHTML = `<div class="eyebrow">EXPLORE THE CONNECTIONS</div><span class="preview-node-icon">${A.icon('graph')}</span><h2>${e(focus.title)}</h2><p>节点直接显示名称。点击章节进入局部网络，点击知识点查看内容。</p><div class="network-detail-stats"><div><b>${G.subtree.leaves.length}</b><small>末端知识点</small></div><div><b>${G.subtree.exerciseIds.length}</b><small>去重习题</small></div></div><div class="divider"></div><h3>每条连线都有含义</h3><p>浅蓝实线表示章节归属，青色虚线表示两个知识点关联了同一道习题。</p><p class="muted">先修、推导等关系等待教师提供与确认。节点位置仅用于排布。</p>`;
  }
  function knowledgeDetail(node) {
    const id = node.id;
    const related = G.current.edges.filter(edge => edge.type === 'shared_exercise' && (edge.source === id || edge.target === id));
    const edit = canEdit()
      ? `<div class="network-edit-entry"><button class="btn small" data-action="network-edit" data-id="${e(id)}">${A.icon('pen')}编辑这个节点（改标题 / 挂题 / 移动 / 删除）</button></div>`
      : '';
    return `<button class="network-detail-close" data-action="network-close-detail" aria-label="关闭知识点预览">×</button><div class="eyebrow">KNOWLEDGE DETAIL</div><h2>${e(node.title)}</h2>${statusBadge(id)}<div class="preview-source">${e(node.source)}</div><div class="preview-text">${A.markdown(node.content.split(/\n\s*\n/)[0])}</div><div class="divider"></div><p>${node.exerciseIds.length} 道关联习题 · ${related.length} 个共题知识点</p><div class="network-detail-actions">${A.button('阅读知识内容', 'read-node', 'book', `data-id="${e(id)}"`, 'primary')}${A.link('查看关联习题', 'exercises', A.course.id, 'pen', '', { node: id })}${A.link('带着知识点提问', 'ai', A.course.id, 'ai', 'text-btn', { node: id })}</div>${edit}${related.length ? `<h3>共用习题的知识点</h3><div class="network-related">${related.map(edge => { const other = A.node(edge.source === id ? edge.target : edge.source); return `<button data-action="network-related" data-id="${e(other.id)}">${e(other.title)}<small>${edge.exerciseIds.length} 道共用题</small></button>`; }).join('')}</div>` : ''}`;
  }
  G.select = function (id, openOnSmall = true) {
    const node = A.node(id); if (!node) return;
    if (node.children.length) { G.open(node.id === G.current.focusId ? node.parentId || node.id : node.id); return; }
    G.selected = id; G.highlight(null);
    document.getElementById('network-detail').innerHTML = knowledgeDetail(node);
    document.getElementById('network-detail').hidden = false;
    if (openOnSmall && window.innerWidth <= 960) A.actions['read-node']({ dataset: { id } });
  };
  G.inspectEdge = function (index) {
    const edge = G.current.edges[index], source = A.node(edge.source), target = A.node(edge.target);
    if (edge.type === 'contains') { G.select(target.id); return; }
    const content = `<div class="notice">${e(source.title)} ↔ ${e(target.title)}<br>两者关联同一道源题；不同知识点下的题目表述均保留。这不表示先修或推导依赖。</div>${edge.exerciseIds.map(id => {
      const exercise = A.course.exercises.find(item => item.id === id);
      return `<section class="network-evidence"><h3>${e(id)}</h3>${(exercise.variants || [exercise]).map((variant, i) => `<div class="network-evidence-variant">${exercise.variants ? `<small>表述 ${i + 1} · ${e(variant.nodeIds.map(nodeId => A.node(nodeId)?.title).join('、'))}</small>` : ''}${A.markdown(variant.prompt)}<details><summary>参考简解与来源</summary>${A.markdown(variant.answer)}${A.markdown(variant.source)}</details></div>`).join('')}</section>`;
    }).join('')}`;
    A.openDialog('共用习题：连线依据', content, 'reader');
  };
  function syncEditPanel() {
    const host = document.getElementById('network-edit');
    if (!host) return;
    const id = A.reviewNode && A.state.review ? A.state.review.nodePanel : '';
    const body = id ? A.reviewNode.panelFor(id) : '';
    host.hidden = !body;
    host.innerHTML = body
      ? `<div class="network-edit-head">${A.icon('pen')}修改结构：改完要重新点一次「通过」，审核页才知道你看过了。</div>${body}`
      : '';
    if (body) document.getElementById('network-detail').hidden = true;
  }

  A.actions['network-edit'] = function (button) {
    if (!canEdit() || !A.reviewNode) return;
    if (A.state.review.nodePanel === button.dataset.id) {
      A.actions['node-close']();
      return;
    }
    A.reviewNode.loadDetail(button.dataset.id, A.course.id);
  };

  A.after.graph = () => {
    if (!A.course.nodes) return;
    G.open(A.state.networkFocus);
    // 深链 #/graph?node=xxx：先定位到它，再把编辑面板打开（审核页的「去图谱里改」用它）。
    const wanted = A.route.params.get('node');
    if (wanted && A.node(wanted)) {
      G.select(wanted, false);
      const editing = A.state.review && A.state.review.nodePanel;
      if (canEdit() && A.reviewNode && editing !== wanted) {
        A.reviewNode.loadDetail(wanted, A.course.id);
      }
    }
    syncEditPanel();
    loadReviewStatus();
  };
  A.inputs['network-scope'] = input => G.open(input.value);
  A.inputs['network-shared'] = input => { G.showShared = input.checked; A.courseMemory().graphShowShared = input.checked; G.highlight(null); };
  A.inputs['network-zoom-range'] = input => G.zoom(Number(input.value) / 100 * G.fitScale / G.camera.scale, { x: G.current.width / 2, y: G.current.height / 2 });
  A.actions['network-view'] = button => { G.display = button.dataset.view; A.courseMemory().graphDisplay = G.display; G.open(ALL); };
  A.actions['network-subtree-toggle'] = button => {
    const panel = document.getElementById('network-subtree-panel'), expanding = panel.hidden;
    if (expanding && !panel.dataset.rendered) { renderSubtreeLists(); panel.dataset.rendered = 'true'; }
    panel.hidden = !expanding; button.setAttribute('aria-expanded', String(expanding));
    button.textContent = expanding ? '收起知识点与习题清单' : '展开知识点与习题清单';
  };
  A.actions['network-close-detail'] = () => { G.selected = null; G.highlight(null); document.getElementById('network-detail').hidden = true; };
  A.actions['atlas-chapter'] = button => G.open(button.dataset.id);
  A.actions['atlas-jump'] = button => document.getElementById(`atlas-${button.dataset.id}`)?.scrollIntoView({ block: 'start', behavior: 'auto' });
  A.actions['network-related'] = button => G.select(button.dataset.id);
  A.actions['network-home'] = () => G.open(ALL);
  A.actions['network-parent'] = () => G.open(G.current.parentId || ALL);
  A.actions['network-reset'] = () => { G.focusOnly = false; G.highlight(null); G.fit(); };
  A.actions['network-focus'] = () => { G.focusOnly = !G.focusOnly; G.highlight(null); };
  A.actions['network-zoom'] = button => G.zoom(Number(button.dataset.direction) > 0 ? 1.25 : .8, { x: G.current.width / 2, y: G.current.height / 2 });
  A.inputs['network-query'] = input => {
    const container = document.getElementById('network-search-results'), query = input.value.trim().toLowerCase();
    container.hidden = !query; if (!query) return;
    const matches = A.course.nodes.filter(node => node.title.toLowerCase().includes(query));
    container.innerHTML = matches.length ? matches.map(node => `<button data-action="network-find" data-id="${e(node.id)}">${A.icon('search')}<span>${e(node.title)}<small>${e(node.path.slice(0, -1).join(' / '))}</small></span></button>`).join('') : '<p>没有匹配的知识点。</p>';
  };
  A.actions['network-find'] = button => {
    const node = A.node(button.dataset.id); G.open(node.children.length ? node.id : node.parentId);
    if (!node.children.length) G.select(node.id);
    document.getElementById('network-query').value = ''; document.getElementById('network-search-results').hidden = true;
  };
})();
