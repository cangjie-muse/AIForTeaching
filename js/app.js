(function () {
  const A = App, e = A.escape;
  const labels = { catalog: '课程中心', overview: '课程概览', outline: '课程大纲', graph: '知识图谱', exercises: '习题与测验', ai: 'AI 学习助手', resources: '课程资源', teaching: '教学工作台', courseware: '课件生成与修改', analytics: '建设与学习分析', study: '我的学习', content: '课程内容', import: '课程内容', generate: '课程内容', settings: '平台设置', construction: '建设进度汇总', 'api-config': '模型接口配置', review: '内容审核', classes: '班级与选课', visibility: '学生可见范围', accounts: '账号管理', login: '登录', pick: '选择课程' };
  function navItem(page, title, icon, courseId, tag = '') {
    // 没权限的入口直接不渲染：菜单里留一堆 403 入口比不显示更糟。
    if (A.auth && !A.auth.can(page)) return '';
    const active = A.route.page === page || (A.route.page === 'pick' && A.route.want === page);
    return `<a class="nav-item ${active ? 'active' : ''}" href="${A.url(page, courseId)}" ${active ? 'aria-current="page"' : ''}>${A.icon(icon)}${title}${tag ? `<em>${tag}</em>` : ''}</a>`;
  }
  function navGroup(label, items, className = '') {
    const rendered = items.filter(Boolean).join('');
    if (!rendered) return '';
    return `<div class="nav-group-label ${className}">${label}</div>${rendered}`;
  }

  function sidebar() {
    // 没有打开中的课程时，课程相关入口不预设课程：交给 #/pick/<page> 让老师自己选。
    // 菜单形状固定（课程概览/大纲一直存在），选课前后位置不变——菜单跳来跳去
    // 会让人以为是两套系统。
    const id = A.course?.id || null;
    return `<aside class="sidebar"><a href="#/catalog" class="brand"><img src="assets/mark.svg" alt=""><span><b>智慧武大</b><small>MATHEMATICS LEARNING</small></span></a><nav aria-label="主导航">${navGroup('课程与学习', [navItem('catalog', '课程中心', 'grid', null), navItem('overview', '课程概览', 'book', id), navItem('outline', '课程大纲', 'layers', id), navItem('graph', '知识图谱', 'graph', id), navItem('exercises', '习题与测验', 'pen', id), navItem('ai', 'AI 学习助手', 'ai', id, 'AI'), navItem('qa', 'AI 问答（已接入模型）', 'ai', id, '新')])}${navGroup('教学与建设', [navItem('teaching', '教学工作台', 'activity', id), navItem('classes', '班级与选课', 'user', null), navItem('courseware', '课件生成与修改', 'layers', id), navItem('resources', '课程资源', 'folder', id), navItem('review', '内容审核', 'check', null), navItem('analytics', '建设与学习分析', 'chart', id)])}${navGroup('工作空间', [navItem('study', '我的学习', 'bookmark', null), navItem('content', '课程内容', 'upload', null), navItem('settings', '平台设置', 'settings', null)])}${navGroup('系统管理', [navItem('visibility', '学生可见范围', 'eye', null, '管理'), navItem('accounts', '账号管理', 'user', null, '管理'), navItem('api-config', '模型接口配置', 'settings', null, '管理')])}${navGroup('信息汇总(仅建设过程用)', [navItem('construction', '建设进度汇总', 'chart', null)], 'construction-group-label')}</nav><div class="sidebar-bottom"><div class="sidebar-note">${A.icon('graph')}<b>以数学知识，连接教与学</b><p>课程 · 图谱 · 习题 · 智能助教</p></div><div class="sidebar-foot"><span class="tiny-dot"></span>本地展示版 <span>v${A.version}</span></div></div></aside>`;
  }
  function authArea() {
    if (!A.auth) return '';
    if (A.auth.anonymous()) {
      return `<button class="btn small primary login-btn" data-action="auth-login-open">${A.icon('user')}登录</button>`;
    }
    const actor = A.auth.actor();
    const role = A.auth.roleLabel();
    const initial = (actor.display_name || actor.id || '?').slice(0, 1);
    return `<div class="who-block">
      <span class="avatar" aria-hidden="true">${e(initial)}</span>
      <span class="who-meta"><b>${e(actor.display_name || actor.id)}</b><small>${e(role)}</small></span>
      <button class="btn small text-btn" data-action="auth-password-open" title="修改密码">改密</button>
      <button class="btn small" data-action="auth-logout">退出</button>
    </div>`;
  }

  function sourceChip() {
    if (A.dataSource !== 'offline') return '';
    return `<span class="source-chip" title="后端连不上，这一页用的是打包在页面里的旧内容；审核与可见范围此时不生效">离线演示数据</span>`;
  }

  function topbar() {
    // 选课页的面包屑：课程平台 > 知识图谱 > 选择课程。
    // 顺序别反——先把"要去哪个功能"说清楚，再说"还没选课程"。
    const trail = A.route.page === 'pick'
      ? `<a href="#/pick/${A.route.want || 'graph'}">${labels[A.route.want] || '课程功能'}</a>${A.icon('chevron')}<span>选择课程</span>`
      : `<span>${labels[A.route.page] || '页面'}</span>`;
    return `<header class="topbar"><div class="breadcrumb"><button class="icon-btn mobile-menu" data-action="toggle-menu" aria-label="打开导航">${A.icon('menu')}</button><a href="#/catalog">课程平台</a>${A.icon('chevron')}${A.course ? `<a href="${A.url('overview', A.course.id)}">${e(A.course.title)}</a>${A.icon('chevron')}` : ''}${trail}</div><div class="topbar-right">${sourceChip()}<button class="top-search" data-action="global-search">${A.icon('search')}搜索知识<kbd>⌘ K</kbd></button>${authArea()}</div></header>`;
  }
  function parseRoute() {
    const route = App.parseHash(location.hash);
    A.course = route.isCourse ? A.courseData(route.courseId) : null;
    if (route.isCourse) App.rememberCourse(A.course?.id);
    A.route = App.normalizeRoute(route);
    return !route.isCourse || !!A.course;
  }
  A.parseRoute = parseRoute;
  // 旧入口重定向：老师收藏过 #/import 或 #/generate 的话，不能给他一个空白页。
  const LEGACY_TABS = { import: 'import', generate: 'generate' };
  // 侧栏菜单是整块重建的（每次导航都换一遍 innerHTML），浏览器会把新节点的
  // scrollTop 归零。菜单比一屏长的时候，滚到底点一项就被弹回顶部，人得重新
  // 找刚才点的位置。这里把滚动位置接住再放回去。
  const NAV_SCROLLER = '.sidebar nav';
  function navScroll() {
    const nav = document.querySelector(NAV_SCROLLER);
    return nav ? nav.scrollTop : 0;
  }
  function restoreNavScroll(top) {
    const nav = document.querySelector(NAV_SCROLLER);
    if (nav && top) nav.scrollTop = top;
  }

  A.render = function () {
    const exists = parseRoute();
    const navTop = navScroll();
    const legacyTab = LEGACY_TABS[A.route.page];
    if (legacyTab) {
      A.state.content = A.state.content || {};
      A.state.content.tab = legacyTab;
      A.route.page = 'content';
    }
    document.getElementById('dialog').close(); document.body.classList.remove('menu-open');
    const view = A.views[A.route.page];
    let body;
    const allowed = !A.auth || A.auth.can(A.route.page);
    if (!allowed) {
      const need = (A.auth.rolesFor(A.route.page) || []).map(role => ({ student: '学生', teacher: '教师', admin: '管理员' })[role] || role).join(' / ');
      body = A.auth.anonymous()
        ? A.empty(
          'lock',
          '这个页面需要登录',
          `「${labels[A.route.page] || A.route.page}」只对${need}开放。登录后再回来。`,
          A.button('去登录', 'auth-login-open', 'user', '', 'primary')
        )
        : A.empty(
          'lock',
          '当前身份没有这个页面的权限',
          `「${labels[A.route.page] || A.route.page}」只对${need}开放，你当前是${A.auth.roleLabel() || '未登录'}。`,
          `<div class="button-row">${A.link('返回课程中心', 'catalog', null, '', 'primary')}</div>`
        );
    } else if (!exists || !view) body = A.empty('book', '没有找到这个页面', '可返回课程中心继续浏览。', A.link('返回课程中心', 'catalog', null, 'arrow', 'primary'));
    else {
      try { body = view(); }
      catch (error) { console.error('[page/render]', error); body = A.empty('info', '页面暂时无法显示', e(error.message), A.link('返回课程中心', 'catalog', null)); }
    }
    document.getElementById('app').innerHTML = `${sidebar()}<div class="main-shell">${topbar()}<main id="main" tabindex="-1">${A.mustChangeBanner ? A.mustChangeBanner() : ''}${A.courseBar()}${body}</main></div>`;
    restoreNavScroll(navTop);
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
    if (event.target.matches('[data-form="login"]')) { event.preventDefault(); A.actions['auth-submit'](document.querySelector('[data-action="auth-submit"]') || {}); }
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

  // 先问一次"我是谁"，再渲染首屏：否则已登录的人会先看到"登录"按钮闪一下。
  // 后端不可达时也照常渲染——匿名浏览课程本来就允许。
  if (A.auth) {
    A.auth.load().then(() => { if (A.route.page === 'login') A.render(); });
  }
  A.render();
  CourseStore.load().then(records => {
    records.forEach(record => A.applyImported(record));
    if (records.length) A.render();
  }).catch(error => { console.warn('[course/restore]', error); });

  // 课程目录以数据库为准（见 core.js 的 App.applyDbCatalog）。
  // 先问"库里有哪些课"（含未发布），再只取已发布课程的内容：未发布的课
  // 对学生不该存在，教师端在目录里单列成"建设中"。
  // 后端连不上时退回打包在页面里的静态包，并在顶栏标明是离线数据。
  //
  // 抽成 A.loadCatalog 是为了"发布完就地刷新"：发布课程后如果目录不跟着变，
  // 人只会以为发布没成功，然后去点第二次。
  async function loadCatalog() {
    try {
      const rows = await A.api.courses();
      const published = rows.filter(row => row.review_status === 'published');
      const courses = await Promise.all(
        published.map(item => A.api.catalogCourse(item.id).catch(() => null))
      );
      A.applyDbCatalog(rows, courses);
    } catch (error) {
      console.warn('[catalog]', error);
      A.dataSource = 'offline';
    }
    A.render();
  }
  A.loadCatalog = loadCatalog;
  loadCatalog();
})();
