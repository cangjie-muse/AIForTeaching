/* 知识图谱页：学生看结构，教师/管理员在这里**在画布上**改结构。
 *
 * 分工（2026-10-22 定）：审核页只审不改，改标题/挂题/解绑/删节点/换父节点都在这一页做。
 * 理由：这五个动作都会让题退回未核，"审核"和"边审边改"放在同一屏，留痕就说不清了。
 *
 * 编辑模式（右上角「编辑结构」开关，只有教师/管理员看得到）：
 *   双击知识点 → 就地改名（浮在节点上的输入框）
 *   拖知识点到章/节 → 改归属（虚线预览，松手记下）
 *   点节点 → 右侧打开编辑表单（挂题/解绑/移动下拉/删除，各自即时提交）
 *   改名和换归属先在画布上攒着（节点标黄虚线），点「提交改动」按顺序逐条落库。
 *
 * 为什么改名/移动要攒着而挂题/删除即时提交：改名与移动是"先摆一摆、看一眼整体"的动作，
 * 一次性提交才符合人的判断顺序；挂题要搜索、删除要报连带范围，本来就是一次成型的动作。
 * 提交是逐条的（后端每个动作一个事务一条审计），某一条失败就停在那儿、其余留在待提交里。
 *
 * 「未审」标记来自 /review/graph，与审核页同一个数据源：同一棵树不该有两个说法。
 */
(function () {
  const A = App, e = A.escape;
  const ALL = '__all__';
  // 只有能进审核页的角色才看得到编辑入口（服务端还会再拦一次）。
  const canEdit = () => Boolean(A.auth && A.auth.can('review'));
  const G = A.network = { current: null, selected: null, subtree: null, focusOnly: false, showShared: true, display: 'graph', fitScale: 1, camera: { x: 0, y: 0, scale: 1 }, drag: null };
  function editState() {
    if (!A.state.networkEdit) A.state.networkEdit = { on: false, drafts: [], busy: false, error: '' };
    return A.state.networkEdit;
  }
  function draftOf(kind, id) {
    return editState().drafts.find(draft => draft.kind === kind && draft.id === id) || null;
  }
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
    return A.sectionTitle('KNOWLEDGE NETWORK', '知识图谱', '全课程先看图谱；需要逐条阅读时切换表格。打开任一层级，画布都会自动适配。', A.link('课程大纲', 'outline', c.id, 'book')) + `<div class="network-toolbar"><div class="field-inline"><label for="network-scope">浏览范围</label><select id="network-scope" data-input="network-scope">${options()}</select></div><div class="search-input">${A.icon('search')}<input id="network-query" data-input="network-query" placeholder="搜索知识点并定位…" aria-label="搜索知识点"></div><button class="btn small" data-action="network-home">${A.icon('graph')}全课程知识点</button>${canEdit() ? '<span class="network-review-summary" id="network-review-summary"></span>' : ''}</div><div class="network-layout"><section class="network-board" id="network-board"><div class="network-board-head"><div><b id="network-title"></b><span id="network-count"></span></div><div class="network-head-actions">${editToggle()}<div class="network-view-switch" id="network-view-switch" role="group" aria-label="全课程显示方式"><button data-action="network-view" data-view="graph">图谱</button><button data-action="network-view" data-view="table">表格</button></div><button class="btn small" data-action="network-parent" id="network-parent">返回上级</button></div></div><div id="network-search-results" class="network-search-results" hidden></div>${editBar()}<div class="network-canvas" id="network-canvas"></div><div class="network-controls"><button data-action="network-zoom" data-direction="-1" aria-label="缩小图谱">−</button><input id="network-zoom-range" data-input="network-zoom-range" type="range" min="100" max="1200" step="25" value="100" aria-label="图谱缩放比例"><span id="network-scale">100%</span><button data-action="network-zoom" data-direction="1" aria-label="放大图谱">＋</button><button data-action="network-reset">适应画布</button><button data-action="network-focus" id="network-focus" disabled>仅看关联</button></div><div class="network-key"><span><i class="edge-sample"></i>章节归属</span><label><input type="checkbox" id="network-shared" data-input="network-shared" ${G.showShared ? 'checked' : ''}><i class="edge-sample shared"></i>共用习题</label><small>共用习题不表示先修关系；节点位置仅为浏览排布</small></div></section><aside class="panel network-detail" id="network-detail" hidden></aside></div><section class="network-subtree" id="network-subtree"></section>`;
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
    const action = editState().on && canEdit() ? 'network-edit-point' : 'read-node';
    const points = scope.leaves.map(node => `<li><button data-action="${action}" data-id="${e(node.id)}"><span>${e(node.title)}<small>${e(node.path.slice(1, -1).join(' / '))}</small></span><em>${node.exerciseIds.length} 题</em></button></li>`).join('');
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
  function nodeMarkup(node, titleOverride) {
    const classes = ['net-node', node.isRoot ? 'net-root' : '', node.kind === 'container' ? 'net-container' : 'net-knowledge', node.appendix ? 'net-appendix' : ''].join(' ');
    const radius = node.isRoot ? 72 : node.kind === 'container' ? 40 : G.current.scope === 'all' ? 17 : 22;
    const title = titleOverride || node.title;
    const wrapped = lines(title, node.isRoot ? 10 : node.kind === 'container' ? 15 : 18);
    const label = wrapped.map((part, index) => `<tspan x="0" dy="${index ? 18 : 0}">${e(part)}</tspan>`).join('');
    const labelY = node.isRoot ? 5 - (wrapped.length - 1) * 9 : radius + 24;
    const shape = `<circle class="net-hit" r="${radius + 17}"/><circle class="net-halo" r="${radius + 9}"/><circle class="net-orbit" r="${radius + 5}"/><circle class="net-dot" r="${radius}"/>${node.isRoot ? '' : `<circle class="net-spark" cy="${radius + 11}" r="3"/>`}<text class="net-label" text-anchor="middle" y="${labelY}">${label}</text>`;
    return `<g class="${classes}" transform="translate(${node.x} ${node.y})" data-node-id="${e(node.id)}" tabindex="0" role="button" aria-label="${e(title)}"><title>${e(title)}${node.kind === 'container' ? ' · 点击展开' : ' · 点击查看，双击阅读'}</title>${shape}</g>`;
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
    document.getElementById('network-canvas').innerHTML = GraphAtlas.render(A.course, e, { edit: editState().on && canEdit() });
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
    document.getElementById('network-canvas').innerHTML = `<svg id="network-svg" class="${focusId === ALL ? 'net-global' : ''}" viewBox="0 0 ${G.current.width} ${G.current.height}" aria-label="${e(focus.title)}交互知识网络"><defs><radialGradient id="net-hub-gradient"><stop stop-color="#6489c7"/><stop offset="1" stop-color="#294e8d"/></radialGradient><radialGradient id="net-chapter-gradient"><stop stop-color="#a2bce4"/><stop offset="1" stop-color="#4d76b5"/></radialGradient><radialGradient id="net-point-gradient"><stop stop-color="#b8e5da"/><stop offset="1" stop-color="#52ada3"/></radialGradient></defs><g id="network-scene">${guideMarkup(G.current)}<g class="net-links">${G.current.edges.map(edgeMarkup).join('')}</g><g class="net-nodes">${G.current.nodes.map(nodeMarkup).join('')}</g><g class="net-drafts" id="net-drafts" aria-hidden="true"></g></g></svg><div class="network-hint">${hint}</div>`;
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
    const edit = canEdit() && !editState().on
      ? `<div class="network-edit-entry"><button class="btn small" data-action="network-edit" data-id="${e(id)}">${A.icon('pen')}编辑结构（改标题 / 挂题 / 移动 / 删除）</button></div>`
      : '';
    return `<button class="network-detail-close" data-action="network-close-detail" aria-label="关闭知识点预览">×</button><div class="eyebrow">KNOWLEDGE DETAIL</div><h2>${e(node.title)}</h2>${statusBadge(id)}<div class="preview-source">${e(node.source)}</div><div class="preview-text">${A.markdown(node.content.split(/\n\s*\n/)[0])}</div><div class="divider"></div><p>${node.exerciseIds.length} 道关联习题 · ${related.length} 个共题知识点</p><div class="network-detail-actions">${A.button('阅读知识内容', 'read-node', 'book', `data-id="${e(id)}"`, 'primary')}${A.link('查看关联习题', 'exercises', A.course.id, 'pen', '', { node: id })}${A.link('带着知识点提问', 'ai', A.course.id, 'ai', 'text-btn', { node: id })}</div>${edit}${related.length ? `<h3>共用习题的知识点</h3><div class="network-related">${related.map(edge => { const other = A.node(edge.source === id ? edge.target : edge.source); return `<button data-action="network-related" data-id="${e(other.id)}">${e(other.title)}<small>${edge.exerciseIds.length} 道共用题</small></button>`; }).join('')}</div>` : ''}`;
  }
  // 编辑模式下双击章/节要能改名，可单击章/节本来是"进下一层"。
  // 所以单击先压 260ms：这期间来了第二击就当双击（改名），否则照旧进入。
  let enterTimer = null;
  function enterLater(node) {
    if (enterTimer) return;
    enterTimer = setTimeout(() => { enterTimer = null; G.open(node.id); }, 260);
  }
  function cancelEnter() {
    if (enterTimer) { clearTimeout(enterTimer); enterTimer = null; }
  }

  G.select = function (id, openOnSmall = true) {
    const node = A.node(id); if (!node) return;
    if (node.children.length) {
      if (editState().on && canEdit() && !node.isRoot) { enterLater(node); return; }
      G.open(node.id === G.current.focusId ? node.parentId || node.id : node.id);
      return;
    }
    G.selected = id; G.highlight(null);
    // 编辑模式下点节点 = 打开右侧的编辑表单（不再额外给一个"点按钮才编辑"的中间步骤）。
    if (editState().on && canEdit() && A.reviewNode) return openEditCard(id);
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
  // ---------------- 编辑模式：在画布上改结构 ----------------

  function editToggle() {
    if (!canEdit()) return '';
    const on = editState().on;
    return `<button class="btn small net-edit-toggle${on ? ' active' : ''}" data-action="network-edit-toggle"
      aria-pressed="${on}" title="打开后可以直接在画布上改结构">${A.icon('pen')}${on ? '退出编辑' : '编辑结构'}</button>`;
  }

  function editBar() {
    const state = editState();
    if (!canEdit() || !state.on) return '';
    return `<div class="net-edit-bar" id="net-edit-bar" role="region" aria-label="结构编辑">
      <span class="net-edit-hint">编辑中：双击章/节/知识点就地改名，拖知识点到章/节上换归属；点节点在右侧改挂题与删除。</span>
      <span class="net-edit-count" id="net-edit-count"></span>
      <button class="btn small primary" data-action="network-commit" ${state.busy ? 'disabled' : ''}>提交改动</button>
      <button class="btn small" data-action="network-discard" ${state.busy ? 'disabled' : ''}>放弃</button>
      ${state.error ? `<span class="net-edit-error">${e(state.error)}</span>` : ''}</div>`;
  }

  // 草稿画在画布上：改名的节点换成新标题、偏移的节点标虚、移动画一条虚线预览。
  // 只重画节点层，不动 camera——否则每敲一次回车视图都会跳一下。
  function paintDrafts() {
    const state = editState();
    if (!state.on || !G.current) return;
    const host = document.querySelector('.net-nodes');
    if (host) host.innerHTML = G.current.nodes.map(node => nodeMarkup(node, (draftOf('rename', node.id) || {}).title)).join('');
    const layer = document.getElementById('net-drafts');
    if (layer) {
      const lookup = new Map(G.current.nodes.map(node => [node.id, node]));
      layer.innerHTML = state.drafts.filter(draft => draft.kind === 'move').map(draft => {
        const from = lookup.get(draft.id), to = lookup.get(draft.parentId);
        return from && to ? `<path class="net-draft-edge" d="M${to.x} ${to.y}L${from.x} ${from.y}"/>` : '';
      }).join('');
    }
    const count = document.getElementById('net-edit-count');
    if (count) count.textContent = state.drafts.length ? `待提交 ${state.drafts.length} 处` : '还没有改动';
    const to = new Set(state.drafts.filter(draft => draft.kind === 'move').map(draft => draft.parentId));
    document.querySelectorAll('.net-node').forEach(el => {
      const id = el.dataset.nodeId;
      el.classList.toggle('is-draft', state.drafts.some(draft => draft.id === id));
      el.classList.toggle('is-drop-hint', to.has(id));
    });
    G.highlight(null);
  }

  function pushDraft(draft) {
    const state = editState();
    state.drafts = state.drafts.filter(item => !(item.kind === draft.kind && item.id === draft.id));
    state.drafts.push(draft);
    state.error = '';
  }

  function closeRename() {
    const host = document.getElementById('net-rename');
    if (host) host.remove();
  }

  function commitRename(id, value) {
    closeRename();
    const node = A.node(id), title = (value || '').trim();
    if (!node || !title || title === node.title) return;
    pushDraft({ kind: 'rename', id, title });
    paintDrafts();
  }

  function openRename(id) {
    const node = A.node(id), canvas = document.getElementById('network-canvas');
    const svg = document.getElementById('network-svg');
    if (!node || !canvas || !svg || !G.current) return;
    closeRename();
    // 输入框浮在节点上：用 SVG 的屏幕矩形换算成画布容器里的像素位置。
    const svgBox = svg.getBoundingClientRect(), canvasBox = canvas.getBoundingClientRect();
    const factor = G.current.width ? svgBox.width / G.current.width : 1;
    const left = svgBox.left - canvasBox.left + (G.camera.x + G.camera.scale * node.x) * factor;
    const top = svgBox.top - canvasBox.top + (G.camera.y + G.camera.scale * node.y) * factor;
    const host = document.createElement('div');
    host.className = 'net-rename';
    host.id = 'net-rename';
    host.style.left = `${Math.round(left)}px`;
    host.style.top = `${Math.round(top)}px`;
    host.innerHTML = '<input class="net-rename-input" aria-label="改标题" autocomplete="off"><span class="net-rename-hint">回车收下、Esc 取消｜攒够了一起提交</span>';
    canvas.appendChild(host);
    const input = host.querySelector('input');
    input.value = (draftOf('rename', id) || node).title;
    input.focus(); input.select();
    let done = false;
    input.addEventListener('keydown', event => {
      if (event.key === 'Enter') { event.preventDefault(); done = true; commitRename(id, input.value); }
      if (event.key === 'Escape') { event.preventDefault(); done = true; closeRename(); }
    });
    input.addEventListener('blur', () => { if (!done) { done = true; commitRename(id, input.value); } });
  }

  // ---- 提交：逐条落库，失败就停在那一条，其余留在待提交里 ----
  function applyLocal(draft) {
    if (!A.course || !A.course.nodes) return;
    const node = A.node(draft.id);
    if (draft.kind === 'rename' && node) retitleSubtree(node, draft.title);
    if (draft.kind === 'move' && node) moveLocally(node, draft.parentId);
  }

  async function submitDrafts() {
    const state = editState();
    if (!state.drafts.length || state.busy) return;
    state.busy = true; state.error = '';
    const queue = [...state.drafts.filter(item => item.kind === 'rename'), ...state.drafts.filter(item => item.kind === 'move')];
    let done = 0;
    while (queue.length) {
      const draft = queue[0];
      try {
        await A.api.patchNode(A.course.id, draft.id,
          draft.kind === 'rename' ? { title: draft.title } : { parent_id: draft.parentId });
      } catch (error) {
        // 停在这一条：已成功的落到内存模型里，剩下的原样留在待提交，人可以改完再点一次。
        state.busy = false;
        state.error = `「${(A.node(draft.id) || {}).title || draft.id}」没提交成功：${error.message}（已生效 ${done} 处，其余还在待提交里）`;
        state.drafts = [...queue];
        A.render();
        return;
      }
      queue.shift();
      done += 1;
      state.drafts = [...queue];
      applyLocal(draft);
    }
    state.busy = false;
    state.drafts = [];
    // 换过归属就要重排：交给 after.graph 里的 networkRefit 标记，别在提交前抢着画。
    A.state.networkRefit = true;
    A.render();
    A.toast(`已提交 ${done} 处结构改动`);
  }

  function discardDrafts() {
    const state = editState();
    state.drafts = [];
    state.error = '';
    paintDrafts();
    A.toast('已放弃未提交的改动');
  }

  // ---- 本地改动：提交成功后不重新拉整门课，直接改内存里的模型再重排 ----
  function rewalk(node, parentPath, parentLevel) {
    node.level = parentLevel + 1;
    node.path = [...parentPath, node.title];
    A.course.nodes.filter(child => child.parentId === node.id).forEach(child => rewalk(child, node.path, node.level));
  }

  function retitleSubtree(node, title) {
    const parentPath = (node.path || []).slice(0, -1), parentLevel = (node.level || 1) - 1;
    node.title = title;
    rewalk(node, parentPath, parentLevel);
  }

  function moveLocally(node, parentId) {
    const parent = A.node(parentId);
    if (!node || !parent) return;
    const oldParent = A.node(node.parentId);
    if (oldParent && Array.isArray(oldParent.children)) oldParent.children = oldParent.children.filter(child => child.id !== node.id);
    if (Array.isArray(parent.children) && !parent.children.some(child => child.id === node.id)) parent.children.push(node);
    node.parentId = parentId;
    (A.course.relationships || []).forEach(edge => {
      if (edge.type === 'contains' && edge.target === node.id) edge.source = parentId;
    });
    rewalk(node, parent.path || [], parent.level);
  }

  function subtreeIds(id) {
    const ids = new Set([id]), queue = [id];
    while (queue.length) {
      const current = queue.pop();
      A.course.nodes.filter(node => node.parentId === current).forEach(node => {
        if (!ids.has(node.id)) { ids.add(node.id); queue.push(node.id); }
      });
    }
    return ids;
  }

  // 右侧表单里挂题/解绑/删除/改标题之后，图谱页的内存模型要跟着走，不然画布上还留着旧结构。
  A.networkLocalChange = (kind, payload) => {
    if (!A.course || !A.course.nodes) return;
    if (kind === 'rename' && payload.id) { const node = A.node(payload.id); if (node) retitleSubtree(node, payload.title); }
    if (kind === 'move' && payload.id) { const node = A.node(payload.id); if (node) moveLocally(node, payload.parentId); }
    if (kind === 'delete' && payload.id) {
      const doomed = subtreeIds(payload.id);
      A.course.nodes = A.course.nodes.filter(node => !doomed.has(node.id));
      A.course.relationships = (A.course.relationships || []).filter(edge => !doomed.has(edge.source) && !doomed.has(edge.target));
      A.course.nodes.forEach(node => {
        if (Array.isArray(node.children)) node.children = node.children.filter(child => !doomed.has(child.id));
      });
      if (G.selected && doomed.has(G.selected)) G.selected = null;
    }
  };

  // ---- 右侧：既有的详情/表单都画在这一栏里 ----
  function renderAside() {
    const host = document.getElementById('network-detail');
    if (!host) return;
    const panel = A.state.review && A.state.review.nodePanel;
    if (panel && A.reviewNode) {
      const body = A.reviewNode.panelFor(panel);
      if (body) {
        host.innerHTML = `<button class="network-detail-close" data-action="network-close-detail" aria-label="关闭">×</button><div class="network-edit-head">${A.icon('pen')}改完要重新点一次「通过」，审核页才知道你看过了。</div>${body}`;
        host.hidden = false;
        return;
      }
    }
    if (G.current && G.selected && A.node(G.selected)) {
      host.innerHTML = knowledgeDetail(A.node(G.selected));
      host.hidden = false;
      return;
    }
    if (G.current) showOverview();
  }

  function openEditCard(id) {
    if (!canEdit() || !A.reviewNode) return undefined;
    G.selected = id; G.highlight(null);
    // 交给 review-node 去取：面板只有一份，取数逻辑不该在两边各写一遍。
    const pending = A.reviewNode.loadDetail(id, A.course.id);
    // 取回来再画一次右栏：面板通常靠整页重画刷新，这里补一次，免得取数失败时右栏停在空白。
    return Promise.resolve(pending).then(renderAside, renderAside);
  }

  A.actions['network-edit-toggle'] = () => {
    const state = editState();
    if (state.on && state.drafts.length && typeof window !== 'undefined' && window.confirm
      && !window.confirm(`还有 ${state.drafts.length} 处改动没提交，退出编辑会丢掉。继续？`)) return;
    state.on = !state.on;
    state.drafts = [];
    state.error = '';
    closeRename();
    if (A.state.review) A.state.review.nodePanel = '';
    A.render();
  };
  A.actions['network-edit'] = function (button) {
    if (!canEdit() || !A.reviewNode) return;
    editState().on = true;
    A.state.networkPendingEdit = button.dataset.id;
    A.render();
  };
  A.actions['network-commit'] = () => submitDrafts();
  A.actions['network-discard'] = () => discardDrafts();
  A.actions['network-rename'] = button => openRename(button.dataset.id);
  // 拖动改归属由 js/graph-interaction.js 调这两个钩子。
  G.draftMove = function (id, parentId) {
    const node = A.node(id);
    if (!node || !parentId || parentId === node.parentId) return;
    pushDraft({ kind: 'move', id, parentId });
    paintDrafts();
  };
  G.cancelEnter = () => cancelEnter();
  G.showDropTarget = function (targetId) {
    document.querySelectorAll('.net-node').forEach(el => el.classList.toggle('is-drop-target', el.dataset.nodeId === targetId));
  };
  G.scenePoint = function (svg, event) {
    const point = new DOMPoint(event.clientX, event.clientY).matrixTransform(svg.getScreenCTM().inverse());
    return { x: (point.x - G.camera.x) / G.camera.scale, y: (point.y - G.camera.y) / G.camera.scale };
  };
  G.containerAt = function (point) {
    if (!G.current) return null;
    const hit = G.current.nodes.filter(node => node.kind === 'container').map(node => {
      const radius = node.isRoot ? 72 : 40;
      return { node, distance: Math.hypot(node.x - point.x, node.y - point.y), radius };
    }).filter(item => item.distance <= item.radius + 18).sort((a, b) => a.distance - b.distance);
    return hit.length ? hit[0].node : null;
  };

  // 换课了：待提交的草稿是上一门课的节点 id，留着提交只会 404，直接丢掉。
  function syncCourseState() {
    const state = editState();
    if (state.course === A.course.id) return;
    state.course = A.course.id;
    state.drafts = [];
    state.error = '';
    if (A.state.review) A.state.review.nodePanel = '';
  }

  // 整页重画之后把"右侧该显示什么"接回来：待打开的节点 > 深链 > 刚才选中的 > 概览。
  function restoreAside(saved) {
    const pending = A.state.networkPendingEdit;
    A.state.networkPendingEdit = '';
    if (pending && A.node(pending)) { openEditCard(pending); return; }
    const wanted = A.route.params.get('node');
    if (wanted && A.node(wanted)) {
      // 从审核页「去图谱里改」过来的深链：人是要来改的，直接把编辑模式打开，
      // 别让人再找一次开关（下一帧整页重画时浮条与开关状态会跟上）。
      if (canEdit() && A.reviewNode) editState().on = true;
      if (editState().on && canEdit() && A.reviewNode) {
        G.selected = wanted; G.highlight(null);
        if (A.state.review && A.state.review.nodePanel === wanted) renderAside();
        else openEditCard(wanted);
      } else if (G.current) {
        G.select(wanted, false);
      }
      if (!G.current) renderAside();
      return;
    }
    if (saved && saved.selected && A.node(saved.selected) && G.current) {
      G.selected = saved.selected; G.highlight(null); renderAside();
      return;
    }
    renderAside();
  }

  A.after.graph = () => {
    if (!A.course.nodes) return;
    cancelEnter();
    syncCourseState();
    // 面板里的写动作会触发整页重画；这里把 camera 与选中节点接回来，
    // 否则每改一次东西画布就跳回"适应画布"，正在看的局部就丢了。
    const keep = Boolean(G.current) && A.state.networkFocus === A.state.networkLastFocus;
    const saved = keep ? { camera: { ...G.camera }, fitScale: G.fitScale, selected: G.selected } : null;
    G.open(A.state.networkFocus);
    A.state.networkLastFocus = A.state.networkFocus;
    if (A.state.networkRefit) {
      A.state.networkRefit = false;
      if (G.current) G.fit();
    } else if (saved && G.current && document.getElementById('network-svg')) {
      G.camera = saved.camera; G.fitScale = saved.fitScale; G.applyCamera();
    }
    paintDrafts();
    restoreAside(saved);
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
  A.actions['network-close-detail'] = () => {
    G.selected = null; G.highlight(null); closeRename();
    if (A.state.review) A.state.review.nodePanel = '';
    document.getElementById('network-detail').hidden = true;
  };
  A.actions['atlas-chapter'] = button => G.open(button.dataset.id);
  A.actions['atlas-jump'] = button => document.getElementById(`atlas-${button.dataset.id}`)?.scrollIntoView({ block: 'start', behavior: 'auto' });
  A.actions['network-related'] = button => G.select(button.dataset.id);
  A.actions['network-edit-point'] = button => G.select(button.dataset.id);
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
