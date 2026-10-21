(function () {
  const A = App, e = A.escape;

  // 每个入口自己的说法：同一个选课动作，进入后要做的事不一样，
  // 标题和副标题跟着入口变，用户才知道"我选完会看到什么"。
  const ENTRIES = {
    graph: {
      title: '选择课程 · 知识图谱',
      detail: '先选一门课程，再进入它的知识图谱：先看全课程结构，再逐章展开，最后落到单个知识点。',
      icon: 'graph',
      action: '查看图谱'
    },
    exercises: {
      title: '选择课程 · 习题与测验',
      detail: '先选一门课程，再按知识点练习。作答后可展开完整解答，错题会记进「我的学习」。',
      icon: 'pen',
      action: '开始练习'
    },
    outline: {
      title: '选择课程 · 课程大纲',
      detail: '先选一门课程，再按章节浏览知识点与关联习题。',
      icon: 'layers',
      action: '打开大纲'
    },
    overview: {
      title: '选择课程 · 课程概览',
      detail: '一门课的结构、规模与建设状态。',
      icon: 'book',
      action: '打开课程'
    },
    ai: {
      title: '选择课程 · AI 学习助手',
      detail: '助手按课程范围作答，切换到哪门课就只答那门课的内容。',
      icon: 'ai',
      action: '进入助手'
    },
    qa: {
      title: '选择课程 · AI 问答',
      detail: '先选课程，再基于这门课的教材内容提问。',
      icon: 'ai',
      action: '进入问答'
    },
    resources: {
      title: '选择课程 · 课程资源',
      detail: '教材、讲义、课件与插图按课程归档。',
      icon: 'folder',
      action: '查看资源'
    },
    teaching: {
      title: '选择课程 · 教学工作台',
      detail: '教学班、作业与学情都挂在具体课程上。',
      icon: 'activity',
      action: '进入工作台'
    },
    courseware: {
      title: '选择课程 · 课件生成与修改',
      detail: '课件按课程与章节生成，历史版本也按课程保存。',
      icon: 'layers',
      action: '管理课件'
    },
    analytics: {
      title: '选择课程 · 建设与学习分析',
      detail: '建设进度与学习数据按课程统计。',
      icon: 'chart',
      action: '查看分析'
    }
  };

  function entry(want) {
    return ENTRIES[want] || { title: '选择课程', detail: '先选一门课程，再继续。', icon: 'book', action: '进入' };
  }

  function counts(course) {
    const stats = course.stats || {};
    if (course.status !== 'ready') return `<span class="muted small">待接入 · 知识图谱与题库尚未生成</span>`;
    return `<span class="small">${stats.sections || 0} 章 <i>·</i> ${stats.knowledge || 0} 个知识点 <i>·</i> ${stats.exercises || 0} 道习题</span>`;
  }

  function card(course, want) {
    const ready = course.status === 'ready';
    // 待接入的课没有内容，点"开始练习/查看图谱"只会得到一个空状态；
    // 这里直接送它去课程概览（那里有建设状态），标签也改成"查看状态"，说到哪就点到哪。
    const href = ready ? A.url(want, course.id) : A.url('overview', course.id);
    return `<a class="pick-card ${ready ? '' : 'is-planned'}" href="${href}" data-course-id="${e(course.id)}">
      <span class="pick-mark">${A.icon(ready ? entry(want).icon : 'clock')}</span>
      <span class="pick-body">
        <b>${e(course.title)}</b>
        <small>${e(course.categoryLabel || course.subject || '')}${course.stage ? ` · ${e(course.stage)}` : ''}</small>
        ${counts(course)}
      </span>
      <span class="pick-go">${ready ? entry(want).action : '查看状态'}${A.icon('arrow')}</span>
    </a>`;
  }

  function groupBlock(title, detail, courses, want) {
    if (!courses.length) return '';
    return `<section class="pick-group"><div class="pick-group-heading"><h2>${title}</h2><p class="muted small">${detail}</p></div>
      <div class="pick-list">${courses.map(course => card(course, want)).join('')}</div></section>`;
  }

  // 34 门待接入课程摊开会把"能用的课"埋掉，收进 details：要看得见，但别挡路。
  function plannedBlock(courses, want) {
    if (!courses.length) return '';
    return `<details class="pick-more"><summary>还有 ${courses.length} 门课程待接入（点击展开）</summary>
      ${groupBlock('待接入', '课程框架已建，内容还在建设；进入后看到的是建设状态与空状态说明。', courses, want)}
    </details>`;
  }

  A.views.pick = function () {
    const want = A.route.want || 'graph';
    const info = entry(want);
    const all = A.allCourses();
    const ready = all.filter(course => course.status === 'ready');
    const planned = all.filter(course => course.status !== 'ready');
    const last = App.lastCourse();
    const lastCourse = last ? all.find(course => course.id === last) : null;
    const resume = lastCourse
      ? `<div class="pick-resume"><span class="pick-resume-text">上次浏览：<b>${e(lastCourse.title)}</b></span>
          <a class="btn small" href="${A.url(want, lastCourse.id)}">继续这门课${A.icon('arrow')}</a></div>`
      : '';

    return `<div class="page-heading"><div><div class="eyebrow">COURSE FIRST</div><h1>${info.title}</h1><p>${info.detail}</p></div>
        <div class="page-heading-aside muted small">共 ${ready.length} 门已接入 · ${planned.length} 门待接入</div></div>
      ${resume}
      <div class="search-input pick-search">${A.icon('search')}<input id="pick-search" data-input="pick-filter" aria-label="搜索课程" placeholder="搜索课程名称，例如 数学分析、同调论…"></div>
      <div id="pick-groups">
        ${groupBlock('已接入', '有知识图谱与题库，可以直接进入。', ready, want)}
        ${plannedBlock(planned, want)}
      </div>
      <div id="pick-empty" hidden>${A.empty('search', '没有找到课程', '换一个关键词，或者回到课程中心看全部课程。', A.link('返回课程中心', 'catalog', null, 'arrow'))}</div>`;
  };

  A.inputs['pick-filter'] = input => {
    const query = CourseCatalog.normalize(input.value || '').toLowerCase();
    const courses = new Map(A.allCourses().map(course => [course.id, course]));
    let visible = 0;
    document.querySelectorAll('.pick-card').forEach(card => {
      const course = courses.get(card.dataset.courseId);
      const haystack = CourseCatalog.normalize(
        `${course.title} ${course.description || ''} ${(course.aliases || []).join(' ')}`);
      const match = haystack.includes(query);
      card.hidden = !match;
      if (match) visible += 1;
    });
    document.querySelectorAll('.pick-group').forEach(group => {
      group.hidden = ![...group.querySelectorAll('.pick-card')].some(card => !card.hidden);
    });
    document.querySelectorAll('.pick-more').forEach(more => {
      more.hidden = !more.querySelector('.pick-card:not([hidden])');
    });
    document.getElementById('pick-empty').hidden = visible > 0;
  };

  // 搜到一门课，回车就走：不要让用户打完字还得去够鼠标。
  // 只取第一张可见卡片（已接入的排在前面，所以命中的一定是能用的课）。
  if (document.addEventListener) {
    document.addEventListener('keydown', event => {
      if (event.key !== 'Enter' || A.route.page !== 'pick') return;
      const input = document.getElementById('pick-search');
      if (!input || document.activeElement !== input) return;
      const first = document.querySelector('.pick-card:not([hidden])');
      if (!first) return;
      event.preventDefault();
      location.hash = first.getAttribute('href');
    });
  }
})();
