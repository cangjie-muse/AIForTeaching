(function () {
  const A = App, e = A.escape;
  function courseCard(course) {
    const ready = course.status === 'ready';
    return `<a class="course-card" data-course-id="${e(course.id)}" href="${A.url('overview', course.id)}">${A.visual.courseCover(course)}<div class="course-card-body"><div class="card-category">${e(course.direction || course.categoryLabel || course.subject)}<span>${ready ? '已接入' : '待建设'}</span></div><h3>${e(course.title)}</h3><p>${e(course.description)}</p><div class="course-card-foot"><span>${ready ? `${course.stats.knowledge} 个知识点 <i>·</i> ${course.stats.exercises} 道习题` : '知识图谱 · 习题 · AI 助教'}</span><span class="circle-arrow">${A.icon('arrow')}</span></div></div></a>`;
  }
  function catalogGroup(group, all) {
    const courses = all.filter(course => course.groupId === group.id);
    return `<section class="catalog-group" data-group="${group.id}"><div class="catalog-group-heading"><span class="phase-number">${group.number}</span><div><h3>${group.label}</h3><p class="group-count">${courses.length} 门课程 · ${courses.filter(c => c.status === 'ready').length} 门已接入</p></div></div><div class="course-grid">${courses.map(courseCard).join('')}</div></section>`;
  }
  function studySequence() {
    const steps = [['01', '从定义出发', '查阅条件、公式与出处'], ['02', '沿结构展开', '在章节中定位知识点'], ['03', '用习题检验', '记录推导，回看简解']];
    return `<div class="whu-study-sequence" aria-label="数学研习路径">${steps.map(([number, title, detail]) => `<div><span class="whu-step-index">${number}</span><span><b>${title}</b><small>${detail}</small></span></div>`).join('')}</div>`;
  }
  // 库里存在但没发布的课：学生看不到，教师要知道"它在，只是还没放出去"。
  // 做成卡片而不是藏起来——藏起来的话，新导入的课会像凭空消失。
  /* 「建设中」卡片点进来：直接打开这门课的「修改与导出」。
     发布面板在那儿；不指定课程的话，内容页会落到列表第一门课。 */
  A.actions['course-open-content'] = function (target) {
    const state = A.state.content || (A.state.content = {});
    if (target.dataset.id) { state.courseId = target.dataset.id; state.content = null; }
    state.tab = 'edit';
    window.location.hash = '#/content';
  };

  function draftCard(row) {
    // 带 action：不指定课程 id 的话，内容页会落到列表第一门课，
    // 老师点了《交换代数》却看到《机器学习》，只会以为点错了。
    return `<a class="course-card draft-card" data-course-id="${e(row.id)}"
      data-action="course-open-content" data-id="${e(row.id)}" href="#/content">
      <div class="draft-cover">${A.icon('upload')}<span>建设中</span></div>
      <div class="course-card-body">
        <div class="card-category">未发布<span>仅教师可见</span></div>
        <h3>${e(row.title)}</h3>
        <p>${row.nodes} 个知识点 · ${row.exercises} 道习题，尚未发布给学生。去「课程内容」补齐并发布。</p>
        <div class="course-card-foot"><span>${A.icon('pen')} 打开课程内容</span><span class="circle-arrow">${A.icon('arrow')}</span></div>
      </div></a>`;
  }

  function draftSection() {
    const rows = (A.dataSource === 'db' && A.draftCourses) ? A.draftCourses : [];
    if (!rows.length || !A.auth || !A.auth.is('teacher', 'admin')) return '';
    return `<section class="catalog-group draft-group" data-group="draft">
      <div class="catalog-group-heading"><span class="phase-number">·</span><div>
        <h3>建设中（未发布）</h3><p class="group-count">${rows.length} 门课 · 库里已有内容，学生暂时看不到</p>
      </div></div>
      <div class="course-grid">${rows.map(draftCard).join('')}</div>
    </section>`;
  }

  A.views.catalog = function () {
    A.state.catalogCategory = 'all';
    const all = A.allCourses(), ready = all.filter(course => course.status === 'ready').length;
    const filters = [{ id: 'all', label: '全部课程', count: all.length }, ...CourseCatalog.groups.map(group => ({ ...group, count: all.filter(c => c.groupId === group.id).length })), { id: 'ready', label: '已接入', count: ready }];
    const imported = all.filter(course => course.groupId === 'imported');
    const displayGroups = imported.length ? [...CourseCatalog.groups, { id: 'imported', label: '新导入课程（待归类）', number: '03' }] : CourseCatalog.groups;
    return `<div class="catalog-hero"><div class="hero-copy"><div class="eyebrow">WUHAN UNIVERSITY · MATHEMATICS</div><h1>数学课程中心</h1><p>沿着定义、定理与习题，梳理数学的来龙去脉。</p><div class="hero-actions">${A.link('探索示范课程', 'overview', A.featuredCourse()?.id, 'arrow', 'primary')}${A.link('查看建设概览', 'construction', null, '', 'light')}</div><div class="hero-caption"><span class="tiny-dot"></span>数学与统计学院 · 智慧课程建设</div></div><div class="hero-photo" role="img" aria-label="武汉大学老图书馆"></div><div class="hero-corner">珞珈 · 求知</div></div>
      <div class="catalog-stats"><div><strong>${CourseCatalog.entries.length}</strong><span>门课程</span></div><div><strong>${CourseCatalog.groups.length}</strong><span>个学科方向</span></div><div><strong>${String(ready).padStart(2, '0')}</strong><span>门已接入内容</span></div><div><strong>${String(CourseCatalog.entries.length - ready).padStart(2, '0')}</strong><span>门待接入</span></div></div>
      <section class="catalog-section"><div class="section-heading"><div><h2>发现课程</h2><p>按学科方向组织；新导入、还没归类的课程单列在末尾。</p></div><div class="search-input">${A.icon('search')}<input id="catalog-search" data-input="filter-catalog" aria-label="搜索课程" placeholder="搜索课程名称…"></div></div><div class="catalog-filters" role="group" aria-label="课程分类">${filters.map((filter, i) => `<button class="filter-chip ${i === 0 ? 'active' : ''}" data-action="catalog-filter" data-filter="${filter.id}">${filter.label}<span>${filter.count}</span></button>`).join('')}</div><div id="course-groups">${displayGroups.map(group => catalogGroup(group, all)).join('')}${draftSection()}</div><div id="catalog-empty" hidden>${A.empty('search', '没有找到课程', '试试其他关键词，或查看全部课程。')}</div></section>${studySequence()}<footer class="page-footer"><span>智慧武大 · 数学课程平台</span><button data-action="credits">代码与资料来源 ${A.icon('link')}</button></footer>`;
  };
  function filterCatalog() {
    const query = CourseCatalog.normalize(document.getElementById('catalog-search').value).toLowerCase();
    const category = A.state.catalogCategory || 'all';
    const courses = new Map(A.allCourses().map(course => [course.id, course]));
    (A.draftCourses || []).forEach(row => courses.set(row.id, { ...row, description: '' }));
    let count = 0;
    document.querySelectorAll('.course-card').forEach(card => {
      const course = courses.get(card.dataset.courseId);
      const searchable = CourseCatalog.normalize(course.title + course.description + (course.aliases || []).join(' ')).toLowerCase();
      const matches = searchable.includes(query) && (category === 'all' || (category === 'ready' ? course.status === 'ready' : course.groupId === category));
      card.hidden = !matches; if (matches) count += 1;
    });
    document.querySelectorAll('.catalog-group').forEach(group => {
      const cards = [...group.querySelectorAll('.course-card')], visible = cards.filter(card => !card.hidden);
      group.hidden = !visible.length;
      group.querySelector('.group-count').textContent = `显示 ${visible.length} / ${cards.length} 门课程`;
    });
    document.getElementById('catalog-empty').hidden = count > 0;
  }
  A.inputs['filter-catalog'] = filterCatalog;
  A.actions['catalog-filter'] = button => {
    A.state.catalogCategory = button.dataset.filter;
    document.querySelectorAll('.filter-chip').forEach(el => el.classList.toggle('active', el === button));
    filterCatalog();
  };
})();
