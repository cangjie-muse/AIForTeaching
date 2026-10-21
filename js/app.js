(function () {
  const A = App, e = A.escape;
  const labels = { catalog: '课程中心', overview: '课程概览', outline: '课程大纲', graph: '知识图谱', exercises: '习题与测验', ai: 'AI 学习助手', resources: '课程资源', teaching: '教学工作台', courseware: '课件生成与修改', analytics: '建设与学习分析', study: '我的学习', content: '课程内容', import: '课程内容', generate: '课程内容', settings: '平台设置', construction: '建设进度汇总', 'api-config': '模型接口配置', review: '内容审核' };
  function navItem(page, title, icon, courseId, tag = '') {
    return `<a class="nav-item ${A.route.page === page ? 'active' : ''}" href="${A.url(page, courseId)}" ${A.route.page === page ? 'aria-current="page"' : ''}>${A.icon(icon)}${title}${tag ? `<em>${tag}</em>` : ''}</a>`;
  }
  function sidebar() {
    const id = A.course?.id || A.featuredCourse()?.id;
    return `<aside class="sidebar"><a href="#/catalog" class="brand"><img src="assets/mark.svg" alt=""><span><b>智慧武大</b><small>MATHEMATICS LEARNING</small></span></a><nav aria-label="主导航"><div class="nav-group-label">课程与学习</div>${navItem('catalog', '课程中心', 'grid', null)}${A.course ? navItem('overview', '课程概览', 'book', id) + navItem('outline', '课程大纲', 'layers', id) : ''}${navItem('graph', '知识图谱', 'graph', id)}${navItem('exercises', '习题与测验', 'pen', id)}${navItem('ai', 'AI 学习助手', 'ai', id, 'AI')}${navItem('qa', 'AI 问答（已接入模型）', 'ai', id, '新')}<div class="nav-group-label">教学与建设</div>${navItem('teaching', '教学工作台', 'activity', A.course?.id)}${navItem('courseware', '课件生成与修改', 'layers', A.course?.id || A.featuredCourse()?.id)}${navItem('resources', '课程资源', 'folder', A.course?.id)}${navItem('analytics', '建设与学习分析', 'chart', A.course?.id)}<div class="nav-group-label">工作空间</div>${navItem('study', '我的学习', 'bookmark', null)}${navItem('content', '课程内容', 'upload', null)}${navItem('settings', '平台设置', 'settings', null)}<div class="nav-group-label">系统管理</div>${navItem('review', '内容审核', 'check', null)}${navItem('api-config', '模型接口配置', 'settings', null, '管理')}<div class="nav-group-label construction-group-label">信息汇总(仅建设过程用)</div>${navItem('construction', '建设进度汇总', 'chart', null)}</nav><div class="sidebar-bottom"><div class="sidebar-note">${A.icon('graph')}<b>以数学知识，连接教与学</b><p>课程 · 图谱 · 习题 · 智能助教</p></div><div class="sidebar-foot"><span class="tiny-dot"></span>本地展示版 <span>v${A.version}</span></div></div></aside>`;
  }
  function topbar() {
    return `<header class="topbar"><div class="breadcrumb"><button class="icon-btn mobile-menu" data-action="toggle-menu" aria-label="打开导航">${A.icon('menu')}</button><a href="#/catalog">课程平台</a>${A.icon('chevron')}${A.course ? `<a href="${A.url('overview', A.course.id)}">${e(A.course.title)}</a>${A.icon('chevron')}` : ''}<span>${labels[A.route.page] || '页面'}</span></div><div class="topbar-right"><button class="top-search" data-action="global-search">${A.icon('search')}搜索知识<kbd>⌘ K</kbd></button><span class="display-status"><span class="tiny-dot"></span>建设展示</span><button class="avatar" data-action="help" aria-label="查看使用指南">数</button></div></header>`;
  }
  function parseRoute() {
    const [path, query] = (location.hash.slice(1) || '/catalog').split('?');
    const segments = path.split('/').filter(Boolean).map(decodeURIComponent);
    const isCourse = segments[0] === 'course';
    A.course = isCourse ? A.courseData(segments[1]) : null;
    A.route = { page: isCourse ? segments[2] || 'overview' : segments[0] || 'catalog', params: new URLSearchParams(query || '') };
    return !isCourse || !!A.course;
  }
  // 旧入口重定向：老师收藏过 #/import 或 #/generate 的话，不能给他一个空白页。
  const LEGACY_TABS = { import: 'import', generate: 'generate' };
  A.render = function () {
    const exists = parseRoute();
    const legacyTab = LEGACY_TABS[A.route.page];
    if (legacyTab) {
      A.state.content = A.state.content || {};
      A.state.content.tab = legacyTab;
      A.route.page = 'content';
    }
    document.getElementById('dialog').close(); document.body.classList.remove('menu-open');
    const view = A.views[A.route.page];
    let body;
    if (!exists || !view) body = A.empty('book', '没有找到这个页面', '可返回课程中心继续浏览。', A.link('返回课程中心', 'catalog', null, 'arrow', 'primary'));
    else {
      try { body = view(); }
      catch (error) { console.error('[page/render]', error); body = A.empty('info', '页面暂时无法显示', e(error.message), A.link('返回课程中心', 'catalog', null)); }
    }
    document.getElementById('app').innerHTML = `${sidebar()}<div class="main-shell">${topbar()}<main id="main" tabindex="-1">${body}</main></div>`;
    document.title = `${A.course?.title || labels[A.route.page] || '智慧武大'} · 数学课程平台`;
    if (exists && view) A.after[A.route.page]?.();
    A.log('navigate', { page: A.route.page, courseId: A.course?.id });
  };
  document.addEventListener('click', async event => {
    const button = event.target.closest('[data-action]');
    if (!button || button.disabled) return;
    const action = A.actions[button.dataset.action]; if (!action) return;
    event.preventDefault();
    try { await action(button); }
    catch (error) { console.error('[action]', button.dataset.action, error); A.toast(`操作未完成：${error.message}`); }
  });
  document.addEventListener('input', event => {
    const input = event.target.closest('[data-input]'); if (input) A.inputs[input.dataset.input]?.(input);
  });
  document.addEventListener('submit', event => {
    if (event.target.matches('[data-form="chat"]')) { event.preventDefault(); A.actions['send-chat'](); }
  });
  document.addEventListener('keydown', event => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); A.actions['global-search'](); }
  });
  A.actions['global-search'] = () => {
    A.openDialog('搜索课程与知识', `<div class="search-input global-search-field">${A.icon('search')}<input id="global-query" data-input="global-query" placeholder="输入课程、知识点名称…" aria-label="全局搜索" autofocus></div><div id="global-results"><p class="muted">在已接入课程中搜索知识点，支持中英文关键词。</p></div>`);
    document.getElementById('global-query').focus();
  };
  A.inputs['global-query'] = input => {
    const q = input.value.trim().toLowerCase(), results = document.getElementById('global-results');
    if (!q) { results.innerHTML = '<p class="muted">输入一个关键词开始搜索。</p>'; return; }
    const courses = A.allCourses().filter(c => c.title.toLowerCase().includes(q));
    const nodes = A.courses.flatMap(c => c.nodes.filter(n => n.kind === 'knowledge' && n.title.toLowerCase().includes(q)).map(n => ({ c, n })));
    results.innerHTML = `${courses.map(c => `<a class="search-hit" href="${A.url('overview', c.id)}">${A.icon('book')}<span><b>${e(c.title)}</b><small>课程</small></span>${A.icon('arrow')}</a>`).join('')}${nodes.slice(0, 30).map(({ c, n }) => `<a class="search-hit" href="${A.url('outline', c.id, { node: n.id })}">${A.icon('graph')}<span><b>${e(n.title)}</b><small>${e(c.title)} · ${e(n.path[1])}</small></span>${A.icon('arrow')}</a>`).join('')}` || '<p class="muted">未找到匹配结果。</p>';
  };
  window.addEventListener('hashchange', () => { A.render(); window.scrollTo(0, 0); });
  A.render();
  CourseStore.load().then(records => {
    records.forEach(record => A.applyImported(record));
    if (records.length) A.render();
  }).catch(error => { console.warn('[course/restore]', error); });

  // 已发布的课程来自后端：库里有已发布内容的课覆盖静态包，其余继续用静态包。
  // 只覆盖"有已发布习题"的课——否则刚发布一个空壳会把演示课程顶掉。
  // 后端不可用时静默退回静态包，页面照常能看。
  A.api.catalog().then(items => {
    if (!items.length) return;
    return Promise.all(items.map(item => A.api.catalogCourse(item.id).catch(() => null)))
      .then(courses => {
        if (A.mergePublishedCourses(courses)) A.render();
      });
  }).catch(error => { console.warn('[catalog/publish]', error); });
})();
