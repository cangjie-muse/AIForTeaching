(function () {
  const A = App, e = A.escape;

  /* 谁能改这门课的归类：管理员可以改全部，老师只能改自己导入的。
     老课程 created_by 是空的（这字段出现之前就在库里），服务端把空值当成
     "任何老师都能改"，这里跟着放行，两边判断保持一致。 */
  function canTag(course) {
    if (A.dataSource !== 'db' || !A.auth || !A.auth.is('teacher', 'admin')) return false;
    const row = (A.courseRows || []).find(item => item.id === course.id);
    // 静态目录里有、库里没有的课改不了：PATCH 打过去只会 404。
    if (!row) return false;
    if (A.auth.role() === 'admin') return true;
    const owner = row.created_by;
    const actor = A.auth.actor() || {};
    return !owner || owner === actor.id;
  }

  /* 「待归类」是死文字，人看到它只会问「在哪儿改」。把入口放在它旁边：
     新导入的课落在目录末尾，要归位不该先点进课程、再翻到「修改与导出」。
     点开用弹窗而不是卡片内浮层——.course-card 是 overflow:hidden，浮层会被裁掉。 */
  function tagButton(course) {
    if (!canTag(course)) return '';
    return `<button type="button" class="card-tag" data-action="catalog-tag" data-id="${e(course.id)}"
      title="改这门课的学科归类">归类</button>`;
  }

  function groups() {
    return (window.CourseCatalog && window.CourseCatalog.groups) || [];
  }

  function courseCard(course) {
    const ready = course.status === 'ready';
    const label = e(course.direction || course.categoryLabel || course.subject);
    return `<a class="course-card" data-course-id="${e(course.id)}" href="${A.url('overview', course.id)}">${A.visual.courseCover(course)}<div class="course-card-body"><div class="card-category">${label}<span class="card-cat-right">${ready ? '已接入' : '待建设'}${tagButton(course)}</span></div><h3>${e(course.title)}</h3><p>${e(course.description)}</p><div class="course-card-foot"><span>${ready ? `${course.stats.knowledge} 个知识点 <i>·</i> ${course.stats.exercises} 道习题` : '知识图谱 · 习题 · AI 助教'}</span><span class="circle-arrow">${A.icon('arrow')}</span></div></div></a>`;
  }
  function catalogGroup(group, all) {
    const courses = all.filter(course => course.groupId === group.id);
    return `<section class="catalog-group" data-group="${group.id}"><div class="catalog-group-heading"><span class="phase-number">${group.number}</span><div><h3>${group.label}</h3><p class="group-count">${courses.length} 门课程 · ${courses.filter(c => c.status === 'ready').length} 门已接入</p></div></div><div class="course-grid">${courses.map(courseCard).join('')}</div></section>`;
  }
  function studySequence() {
    const steps = [['01', '从定义出发', '查阅条件、公式与出处'], ['02', '沿结构展开', '在章节中定位知识点'], ['03', '用习题检验', '记录推导，回看简解']];
    return `<div class="whu-study-sequence" aria-label="数学研习路径">${steps.map(([number, title, detail]) => `<div><span class="whu-step-index">${number}</span><span><b>${title}</b><small>${detail}</small></span></div>`).join('')}</div>`;
  }

  /* 卡片上的「归类」：弹出一个方向列表，点哪个就归到哪组。
     保存后重拉目录——分组是前端按 direction 算的，不重拉就还显示在旧组里。 */
  A.actions['catalog-tag'] = function (button) {
    const course = (A.allCourses() || []).find(item => item.id === button.dataset.id) || {};
    const current = String(course.direction || '');
    const choices = [{ id: '', label: '待归类' }, ...groups()];
    const rows = choices.map(choice => {
      const active = choice.id ? (choice.id === current || choice.label === current) : !current;
      return `<button class="btn${active ? ' primary' : ''}" data-action="catalog-tag-pick"
        data-id="${e(course.id)}" data-direction="${e(choice.id)}" data-label="${e(choice.label)}">${e(choice.label)}</button>`;
    }).join('');
    A.openDialog(`归类：${e(course.title || '课程')}`, `<p class="muted small">
      现在归在「${e(course.direction || '待归类')}」。选一个学科方向，保存后它立刻出现在课程中心对应分组。</p>
      <div class="button-row">${rows}</div>`);
  };

  A.actions['catalog-tag-pick'] = async function (button) {    const courseId = button.dataset.id;
    const direction = button.dataset.direction || '';
    const label = button.dataset.label || '待归类';
    button.disabled = true;
    try {
      const result = await A.api.updateCourse(courseId, { direction });
      const dialog = document.getElementById('dialog');
      if (dialog && dialog.open) dialog.close();
      A.toast(result && result.changed && result.changed.includes('direction')
        ? `已归入「${label}」` : '归类没有变化');
      if (typeof A.loadCatalog === 'function') await A.loadCatalog();
      A.render();
    } catch (error) {
      button.disabled = false;
      A.toast(`没保存成功：${error.message}`);
    }
  };

  /* 新建学科分类：只让管理员做。分类是全校共用的分类法，
     老师各建各的会把目录拆成互不相认的一堆小格子。 */
  A.actions['catalog-add-direction'] = function () {
    A.state.newDirection = { label: '' };
    const existing = groups().map(group => `<button class="btn" data-action="catalog-del-direction"
      data-id="${e(group.id)}" data-label="${e(group.label)}">${e(group.label)} ✕</button>`).join('');
    A.openDialog('新建 / 管理学科分类', `<div class="field">
      <label for="direction-label">分类名称</label>
      <input id="direction-label" data-input="direction-label" maxlength="20" placeholder="例如：数理逻辑">
      <small>新分类会出现在筛选条与「归类」列表里；已有课程不会自动搬过去。</small></div>
      <div class="button-row"><button class="btn primary" data-action="catalog-direction-save">创建分类</button></div>
      <div class="field"><label>已有分类</label>
      <div class="button-row">${existing}</div>
      <small>点分类右边的 ✕ 删除。归在里面的课会回到「待归类」，不会丢。</small></div>`);
    const input = document.getElementById('direction-label');
    if (input && input.focus) input.focus();
  };

  /* 删除分类：还有课归在它名下时，服务端先回 409（带课数），
     问一句「这些课会回到待归类」再带着 force 删——不让人以为只是删了个名字。 */
  A.actions['catalog-del-direction'] = async function (button) {
    const id = button.dataset.id;
    const label = button.dataset.label || '';
    if (!window.confirm(`删除分类「${label}」？归在里面的课会回到「待归类」。`)) return;
    button.disabled = true;
    let moved = 0;
    try {
      const result = await A.api.deleteDirection(id);
      moved = result.courses_moved || 0;
    } catch (error) {
      if (error.status !== 409) {
        button.disabled = false;
        A.toast(`没删掉：${error.message}`);
        return;
      }
      const count = (error.detail && error.detail.courses) || 0;
      if (!window.confirm(`还有 ${count} 门课归在「${label}」下。删除后它们回到「待归类」，继续？`)) {
        button.disabled = false;
        return;
      }
      try {
        const forced = await A.api.deleteDirection(id, true);
        moved = forced.courses_moved || 0;
      } catch (second) {
        button.disabled = false;
        A.toast(`没删掉：${second.message}`);
        return;
      }
    }
    const dialog = document.getElementById('dialog');
    if (dialog && dialog.open) dialog.close();
    A.toast(moved ? `已删除「${label}」，${moved} 门课回到待归类` : `已删除分类「${label}」`);
    await A.loadDirections();
    await A.loadCatalog();
  };

  A.inputs['direction-label'] = function (input) {
    A.state.newDirection = Object.assign(A.state.newDirection || {}, { label: input.value });
  };

  A.actions['catalog-direction-save'] = async function (button) {
    const label = String((A.state.newDirection || {}).label || '').trim();
    if (!label) { A.toast('先给分类起个名字'); return; }
    button.disabled = true;
    try {
      await A.api.createDirection(label);
      const dialog = document.getElementById('dialog');
      if (dialog && dialog.open) dialog.close();
      A.toast(`已新建分类「${label}」`);
      await A.loadDirections();
      await A.loadCatalog();
    } catch (error) {
      button.disabled = false;
      A.toast(`没建成：${error.message}`);
    }
  };
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
    // 建设中：库里有、还没发布给学生的课。单独一档，老师不用滚到页面最底下找。
    const drafts = (A.dataSource === 'db' && A.draftCourses) ? A.draftCourses : [];
    const faculty = A.auth && A.auth.is('teacher', 'admin');
    // 没归类的课也要有自己的一档：它们在最底下单独一组，筛选条上却没有入口，
    // 人只看得到「全部 39」里差了几门，不知道差在哪。
    const imported = all.filter(course => course.groupId === 'imported');
    const filters = [
      { id: 'all', label: '全部课程', count: all.length },
      ...CourseCatalog.groups.map(group => ({ ...group, count: all.filter(c => c.groupId === group.id).length })),
      ...(imported.length ? [{ id: 'imported', label: '待归类', count: imported.length }] : []),
      { id: 'ready', label: '已接入', count: ready },
      ...(drafts.length && faculty ? [{ id: 'draft', label: '正在建设', count: drafts.length }] : [])
    ];
    const displayGroups = imported.length
      ? [...CourseCatalog.groups, { id: 'imported', label: '新导入课程（待归类）', number: String(CourseCatalog.groups.length + 1).padStart(2, '0') }]
      : CourseCatalog.groups;
    // 分类是运营数据，管理员在界面上加：改代码 + 部署才多一档，等于这个决定只属于程序员。
    const addChip = A.auth && A.auth.is('admin')
      ? `<button class="filter-chip chip-add" data-action="catalog-add-direction">＋ 新建分类</button>` : '';
    return `<div class="catalog-hero"><div class="hero-copy"><div class="eyebrow">WUHAN UNIVERSITY · MATHEMATICS</div><h1>数学课程中心</h1><p>沿着定义、定理与习题，梳理数学的来龙去脉。</p><div class="hero-actions">${A.link('探索示范课程', 'overview', A.featuredCourse()?.id, 'arrow', 'primary')}${A.link('查看建设概览', 'construction', null, '', 'light')}</div><div class="hero-caption"><span class="tiny-dot"></span>数学与统计学院 · 智慧课程建设</div></div><div class="hero-photo" role="img" aria-label="武汉大学老图书馆"></div><div class="hero-corner">珞珈 · 求知</div></div>
      <div class="catalog-stats"><div><strong>${CourseCatalog.entries.length}</strong><span>门课程</span></div><div><strong>${CourseCatalog.groups.length}</strong><span>个学科方向</span></div><div><strong>${String(ready).padStart(2, '0')}</strong><span>门已接入内容</span></div><div><strong>${String(CourseCatalog.entries.length - ready).padStart(2, '0')}</strong><span>门待接入</span></div></div>
      <section class="catalog-section"><div class="section-heading"><div><h2>发现课程</h2><p>按学科方向组织；新导入、还没归类的课程单列在末尾。</p></div></div>
      <div class="catalog-toolbar"><div class="catalog-filters" role="group" aria-label="课程分类">${filters.map((filter, i) => `<button class="filter-chip ${i === 0 ? 'active' : ''}" data-action="catalog-filter" data-filter="${filter.id}">${filter.label}<span>${filter.count}</span></button>`).join('')}${addChip}</div><div class="search-input">${A.icon('search')}<input id="catalog-search" data-input="filter-catalog" aria-label="搜索课程" placeholder="搜索课程名称…"></div></div>
      <div id="course-groups">${displayGroups.map(group => catalogGroup(group, all)).join('')}${draftSection()}</div><div id="catalog-empty" hidden>${A.empty('search', '没有找到课程', '试试其他关键词，或查看全部课程。')}</div></section>${studySequence()}<footer class="page-footer"><span>智慧武大 · 数学课程平台</span><button data-action="credits">代码与资料来源 ${A.icon('link')}</button></footer>`;
  };
  function filterCatalog() {
    const query = CourseCatalog.normalize(document.getElementById('catalog-search').value).toLowerCase();
    const category = A.state.catalogCategory || 'all';
    const courses = new Map(A.allCourses().map(course => [course.id, course]));
    // 建设中的课单独归到 draft 档：它们不在 allCourses 里，得自己标注分组
    (A.draftCourses || []).forEach(row => courses.set(row.id, {
      ...row, description: '', groupId: 'draft', status: 'draft'
    }));
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
    scrollToCategory(A.state.catalogCategory);
  };

  /* 点分类就跳到那一组：让人自己往下滚，滚着滚着就不知道在看哪一档了。
     「全部」「已接入」这种跨组的档回到列表开头。 */
  function scrollToCategory(category) {
    const group = ['all', 'ready'].includes(category)
      ? document.getElementById('course-groups')
      : document.querySelector(`.catalog-group[data-group="${category}"]`);
    if (!group || group.hidden) return;
    group.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
})();
