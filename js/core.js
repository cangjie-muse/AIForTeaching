'use strict';
const App = window.App = {
  version: '0.11',
  views: {}, actions: {}, after: {}, inputs: {}, state: {}, course: null, route: {},
  courses: [...(window.COURSE_DATA || [])], assets: new Map()
};
App.escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
App.readLocal = function () {
  try { return JSON.parse(localStorage.getItem('whu-math-learning-v1')) || {}; }
  catch (error) { console.warn('[storage/read]', error); return {}; }
};
App.local = App.readLocal();
App.save = function () {
  try { localStorage.setItem('whu-math-learning-v1', JSON.stringify(App.local)); return true; }
  catch (error) { console.error('[storage/save]', error); App.toast('浏览器无法保存记录，请使用“导出学习记录”。'); return false; }
};
App.log = (action, data) => { if (App.local.debug) console.info(new Date().toISOString(), action, data); };
App.allCourses = () => CourseCatalog.merge(App.courses);

// 已发布的课程覆盖静态包。抽成函数是为了能单独测：合并规则错一处，
// 学生端看到的就是另一门课的内容，而这种错在页面上不容易发现。
// resourceBase / textbook / attachments 只在静态包里有（数据库不存这些），
// 所以"新值为空就保留旧值"——否则学生端会变成一堆裂图和空书名。
App.mergePublishedCourses = function (courses) {
  let changed = 0;
  (courses || []).filter(Boolean).forEach(course => {
    const index = App.courses.findIndex(entry => entry.id === course.id);
    if (index >= 0) {
      const old = App.courses[index];
      App.courses[index] = {
        ...old,
        ...course,
        resourceBase: course.resourceBase || old.resourceBase || '',
        textbook: course.textbook || old.textbook || '',
        attachments: (course.attachments && course.attachments.length)
          ? course.attachments
          : (old.attachments || [])
      };
    } else {
      App.courses.push(course);
    }
    changed += 1;
  });
  return changed;
};
App.featuredCourse = () => App.courses.find(course => course.id === 'course-symplectic-math-physics') || App.courses[0];
// 需要"先选课程"的页面。以前这些链接带着 A.featuredCourse() 的默认课程id，
// 点「知识图谱」会直接掉进辛几何那门课——像是平台替你选了课，实际上你还没选。
// 现在没有当前课程时，它们一律指向 #/pick/<page>（选课页）。
App.coursePages = ['overview', 'outline', 'graph', 'exercises', 'ai', 'qa',
  'resources', 'teaching', 'courseware', 'analytics'];
// 路由解析抽成纯函数：`#/course/x/graph`、`#/pick/graph`、`#/catalog` 三种形态
// 各自的含义要能单独验证——路由错一处，用户点「知识图谱」就会落到别的课程或空白页。
App.parseHash = function (hash) {
  const [path, query] = (String(hash || '').replace(/^#/, '') || '/catalog').split('?');
  const segments = path.split('/').filter(Boolean).map(segment => {
    try { return decodeURIComponent(segment); } catch (error) { return segment; }
  });
  const isCourse = segments[0] === 'course';
  const isPick = segments[0] === 'pick';
  return {
    isCourse,
    isPick,
    courseId: isCourse ? segments[1] || '' : '',
    page: isPick ? 'pick' : (isCourse ? segments[2] || 'overview' : segments[0] || 'catalog'),
    want: isPick ? segments[1] || 'graph' : '',
    params: new URLSearchParams(query || '')
  };
};
// 把解析结果收敛成"这次到底渲染哪一页"。
// 老书签 `#/graph` 这类没有课程 id 的课程页，不该报错，也不该猜一门课，
// 一律转成选课页并记住用户想去哪个功能。
App.normalizeRoute = function (route) {
  if (route.isPick) return { page: 'pick', want: route.want || 'graph', params: route.params };
  // 只有"不是课程路由"的课程页才需要拦；#/course/x/graph 当然是直接渲染。
  if (!route.isCourse && App.coursePages.includes(route.page)) {
    return { page: 'pick', want: route.page, params: route.params };
  }
  return { page: route.page, params: route.params };
};
App.url = function (page, courseId, params = {}) {
  const scoped = App.coursePages.includes(page);
  const root = courseId ? `#/course/${encodeURIComponent(courseId)}/${page}`
    : (scoped ? `#/pick/${page}` : `#/${page}`);
  const query = new URLSearchParams(Object.entries(params).filter(([, value]) => value != null && value !== ''));
  return root + (query.size ? `?${query}` : '');
};
// 记住上次打开的课程，只为在选课页给一个"继续上次"的入口。
// 注意：它不会自动跳转——选课是用户的动作，不能替他做。
App.lastCourse = function () {
  try { return localStorage.getItem('whu-math-last-course') || ''; } catch (error) { return ''; }
};
App.rememberCourse = function (id) {
  try { if (id) localStorage.setItem('whu-math-last-course', id); } catch (error) { /* 隐私模式下忽略 */ }
};
App.go = (page, params = {}, courseId = App.course?.id) => { location.hash = App.url(page, courseId, params); };
App.button = (text, action, icon = '', extra = '', variant = '') => `<button class="btn ${variant}" data-action="${action}" ${extra}>${icon ? App.icon(icon) : ''}${text}</button>`;
App.link = (text, page, courseId, icon = '', variant = '', params = {}) => `<a class="btn ${variant}" href="${App.url(page, courseId, params)}">${icon ? App.icon(icon) : ''}${text}</a>`;
App.badge = (text, variant = '') => `<span class="badge ${variant}">${text}</span>`;
App.sectionTitle = (eyebrow, title, detail = '', aside = '') => `<div class="page-heading"><div><div class="eyebrow">${eyebrow}</div><h1>${title}</h1>${detail ? `<p>${detail}</p>` : ''}</div>${aside}</div>`;
App.empty = (icon, title, description, action = '') => `<div class="empty-state"><span class="empty-icon">${App.icon(icon)}</span><h3>${title}</h3><p>${description}</p>${action}</div>`;
App.toast = function (message) {
  const el = document.getElementById('toast'); el.textContent = message; el.classList.add('visible');
  clearTimeout(App.toastTimer); App.toastTimer = setTimeout(() => el.classList.remove('visible'), 3500);
};
App.openDialog = function (title, body, variant = '') {
  const dialog = document.getElementById('dialog');
  if (dialog.open) dialog.close();
  dialog.className = variant;
  dialog.innerHTML = `<div class="dialog-head"><h2>${title}</h2><button class="icon-btn" data-action="close-dialog" aria-label="关闭">${App.icon('close')}</button></div><div class="dialog-body">${body}</div>`;
  dialog.showModal();
};
App.download = function (filename, data, mime = 'application/json') {
  const url = URL.createObjectURL(new Blob([data], { type: mime }));
  const link = document.createElement('a'); link.href = url; link.download = filename; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
App.asset = function (course, relative) {
  if (!relative || /^(?:[a-z]+:|\/\/)/i.test(relative)) return '';
  const [raw, fragment] = relative.split('#');
  let safe;
  try { safe = decodeURIComponent(raw).replace(/^\.\//, ''); }
  catch (error) { App.log('invalidAssetPath', error.message); return ''; }
  if (/^(?:[a-z]+:|\/|\\)/i.test(safe) || safe.split('/').includes('..')) return '';
  const included = [...(course.files || []), ...(course.attachments || [])];
  if (included.length && !included.includes(safe)) return '';
  const url = App.assets.get(`${course.id}/${safe}`) || (course.resourceBase ? course.resourceBase + safe : '');
  return url && fragment ? `${url}#${fragment}` : url;
};
App.markdown = function (source, course = App.course) {
  const math = [];
  let text = String(source || '').replace(/\$\$([\s\S]*?)\$\$|(?<!\\)\$([^$\n]+?)\$/g, (raw, block, inline) => {
    const token = `MATHSAVEDTOKEN${math.length}END`;
    try { math.push(katex.renderToString(block ?? inline, { displayMode: block != null, throwOnError: true, strict: 'ignore', trust: false, output: 'htmlAndMathml' })); }
    catch (error) { console.error('[math/render]', error); math.push(`<code class="math-error">${App.escape(raw)}</code>`); }
    return block != null ? `\n\n${token}\n\n` : token;
  });
  text = marked.parse(text, { breaks: false, gfm: true });
  text = text.replace(/MATHSAVEDTOKEN(\d+)END/g, (_, n) => math[Number(n)]);
  const safe = DOMPurify.sanitize(text, { ADD_ATTR: ['target'], FORBID_TAGS: ['style', 'form', 'input', 'button', 'iframe'] });
  const template = document.createElement('template'); template.innerHTML = safe;
  template.content.querySelectorAll('a').forEach(a => {
    const rawHref = a.getAttribute('href') || '';
    let href;
    try { href = decodeURIComponent(rawHref); }
    catch { a.removeAttribute('href'); return; }
    const nodeId = href.split('#')[1];
    if (course && /^(?:知识点\.md|习题\.md|#)/.test(href) && nodeId) {
      a.href = App.url(href.startsWith('习题') ? 'exercises' : 'outline', course.id, { node: nodeId });
    } else if (/^https?:\/\//.test(href)) { a.target = '_blank'; a.rel = 'noopener noreferrer'; }
    else if (course) {
      const asset = App.asset(course, rawHref);
      if (!asset && /\.pdf(?:#.*)?$/i.test(href)) {
        const note = document.createElement('span'); note.className = 'resource-missing';
        note.textContent = `${a.textContent}（PDF 原书未随离线站点提供）`; a.replaceWith(note);
      } else { a.href = asset || '#'; a.target = '_blank'; a.rel = 'noopener'; }
    }
  });
  template.content.querySelectorAll('img').forEach(img => {
    const src = course && App.asset(course, img.getAttribute('src'));
    if (src) { img.src = src; img.loading = 'lazy'; }
    else { const missing = document.createElement('span'); missing.className = 'resource-missing'; missing.textContent = `插图未附：${img.alt || '图片'}`; img.replaceWith(missing); }
  });
  return `<div class="prose">${template.innerHTML}</div>`;
};
App.summary = (source, max = 100) => String(source || '').replace(/\*\*[^*]+\*\*/g, '').replace(/\$[^$]*\$/g, '〔公式〕').replace(/[#*\n]/g, ' ').slice(0, max);
App.courseData = id => App.allCourses().find(course => course.id === id || course.catalogId === id);
App.node = id => App.course?.nodes?.find(node => node.id === id);
App.record = (kind, id) => `${App.course?.id}:${kind}:${id}`;
App.actions['close-dialog'] = () => document.getElementById('dialog').close();
App.actions['skip-main'] = () => document.getElementById('main').focus();
App.actions['toggle-menu'] = button => { const open = document.body.classList.toggle('menu-open'); button.setAttribute('aria-expanded', String(open)); };
App.actions['export-records'] = () => App.download('数学课程-学习记录.json', JSON.stringify({ schemaVersion: '1.0', exportedAt: new Date().toISOString(), records: App.local }, null, 2));
App.actions['help'] = () => App.openDialog('使用指南', `<div class="help-list"><p><b>从课程中心开始</b><br>进入“辛几何与数学物理”，浏览课程大纲、知识图谱与习题。</p><p><b>所有课程模块均可浏览</b><br>标注“待接入”的功能已预留页面；AI 当前提供本地资料检索，尚未连接模型。</p><p><b>保存你的学习记录</b><br>笔记与收藏保存在当前浏览器，也可从“我的学习”导出备份。</p><p><b>接入另一门课</b><br>在“课程导入”中选择资源包文件夹，核对结果后导入。沿用知识节点.xlsx、知识点.md、习题.md。</p></div>`);
