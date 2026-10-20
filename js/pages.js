(function () {
  const A = App, e = A.escape;
  function moduleTile(title, description, page, icon, status = '可浏览') {
    return `<a class="module-tile" href="${A.url(page, A.course.id)}"><span class="module-icon">${A.icon(icon)}</span><div><h3>${title}</h3><p>${description}</p><span class="module-status">${status}</span></div>${A.icon('chevron')}</a>`;
  }
  function courseMetrics(course) {
    const stats = course.stats || { knowledge: 0, exercises: 0, sections: 0 };
    return `<div class="metric-row"><div>${A.icon('layers')}<strong>${stats.sections}</strong><span>章节与附录</span></div><div>${A.icon('graph')}<strong>${stats.knowledge}</strong><span>实质知识点</span></div><div>${A.icon('pen')}<strong>${stats.exercises}</strong><span>去重习题</span></div><div>${A.icon('book')}<strong>${course.status === 'ready' ? '3' : '0'}</strong><span>标准内容文件</span></div></div>`;
  }
  A.views.overview = function () {
    const c = A.course, ready = c.status === 'ready';
    const chapters = c.nodes?.filter(node => node.level === 2) || [];
    return `<div class="course-hero"><div><div class="eyebrow">${e(c.subject || '专业课程')} <span>／</span> ${e(c.audience)}</div><h1>${e(c.title)}</h1><p>${e(c.description)}</p><div class="course-hero-tags">${A.badge(ready ? '课程内容已接入' : '课程待建设', ready ? 'blue' : '')}${A.badge(ready ? '内容待教师审核' : '框架已就绪')}<span>自主学习 · 概念关联 · 数学探索</span></div><div class="hero-actions">${A.link(ready ? '浏览课程大纲' : '查看课程框架', 'outline', c.id, 'book', 'primary')}${A.link('探索知识图谱', 'graph', c.id, 'graph')}</div></div><div class="math-emblem" aria-hidden="true"><span>${e(c.symbol || (c.title === '辛几何与数学物理' ? 'ω' : '𝑓'))}</span><i>mathematical structures</i></div></div>${courseMetrics(c)}
      <div class="section-heading"><div><h2>课程学习空间</h2><p>从知识理解到练习巩固，保持上下文连贯。</p></div></div><div class="module-grid">${moduleTile('知识图谱', '沿章节结构发现知识之间的联系', 'graph', 'graph', ready ? `${c.stats.knowledge} 个知识点` : '待导入知识点')}${moduleTile('习题与测验', '按知识点与难度筛选，查看简解', 'exercises', 'pen', ready ? `${c.stats.exercises} 道习题` : '待导入题库')}${moduleTile('AI 学习助手', '携带课程内容提问，追溯参考资料', 'ai', 'ai', '本地检索可用 · 模型待接入')}${moduleTile('课程资源', '教材、知识内容与习题集中查阅', 'resources', 'folder', ready ? '标准资源包已接入' : '待添加资源')}${moduleTile('教学工作台', '作业、课堂互动、讨论与教学通知', 'teaching', 'activity', '框架预览')}${moduleTile('学习与建设分析', '查看资源覆盖与当前学习记录', 'analytics', 'chart', '查看数据')}</div>
      <div class="overview-bottom"><section class="panel"><div class="panel-heading"><h2>课程内容</h2>${A.link('全部章节', 'outline', c.id, 'arrow', 'text-btn')}</div>${chapters.length ? `<div class="chapter-preview">${chapters.slice(0, 4).map((n, i) => `<a href="${A.url('outline', c.id, { chapter: n.id })}"><span>${String(i + 1).padStart(2, '0')}</span><div><b>${e(n.title)}</b><small>${c.nodes.filter(x => x.chapterId === n.id && x.kind === 'knowledge').length} 个知识点</small></div>${A.icon('chevron')}</a>`).join('')}</div>` : A.empty('book', '等待课程资料', '导入标准资源包后自动生成章节结构。')}</section><aside class="panel course-note"><div class="eyebrow">COURSE NOTES</div><h2>读懂结构，理解方法</h2><p>${ready ? e(c.textbook || '课程内容来自导入的标准资源包，原始条件、来源和解答说明均予保留。') : '这门课程已拥有完整的学习空间。添加知识点和题库后，内容将出现在各个模块中。'}</p><div class="note-line">${A.icon('info')}<span>学习记录仅保存在当前浏览器，可在“我的学习”中导出。</span></div>${A.link('管理课程内容', 'import', null, 'upload', 'text-btn')}</aside></div>`;
  };
  function mapSection(course, chapter) {
    const leaves = course.nodes.filter(node => node.chapterId === chapter.id && node.kind === 'knowledge');
    const questionCount = new Set(leaves.flatMap(node => node.exerciseIds)).size;
    const open = A.route.params.get('chapter') === chapter.id || A.route.params.get('node') && leaves.some(n => n.id === A.route.params.get('node'));
    return `<details class="outline-chapter" ${open ? 'open' : ''}><summary><span class="chapter-num">${e(chapter.title.match(/(?:第|附录)(\d+)/)?.[1] || '·')}</span><div><h3>${e(chapter.title)}</h3><p>${leaves.length} 个知识点 <i>·</i> ${questionCount} 道关联习题</p></div>${A.icon('chevron')}</summary><div class="outline-nodes">${leaves.map(node => `<button class="outline-node" data-action="read-node" data-id="${e(node.id)}"><span class="node-dot"></span><span>${e(node.title)}</span><small>${node.exerciseIds.length ? `${node.exerciseIds.length} 题` : '阅读'}</small>${A.icon('chevron')}</button>`).join('')}</div></details>`;
  }
  A.views.outline = function () {
    const c = A.course;
    if (!c.nodes) return A.sectionTitle('COURSE OUTLINE', '课程大纲', e(c.title)) + A.empty('book', '课程内容即将加入', '章节、知识点与学习资料将在导入资源包后显示。', A.link('导入课程资源', 'import', null, 'upload', 'primary'));
    const chapters = c.nodes.filter(n => n.level === 2);
    return A.sectionTitle('COURSE OUTLINE', '课程大纲', `${e(c.title)} · 按原始资源包的章节层级组织`, A.link('切换到知识图谱', 'graph', c.id, 'graph')) + `<div class="outline-layout"><section><div class="outline-label">${A.icon('book')} 正文与附录 <span>${chapters.length} 个单元</span></div>${chapters.map(ch => mapSection(c, ch)).join('')}</section><aside class="panel sticky-card"><div class="eyebrow">LEARNING GUIDE</div><h3>从章节进入知识</h3><p>展开章节，选择一个知识点。正文、相关习题与来源页码会集中呈现。</p><div class="divider"></div><p>附录保留进阶内容，可根据教学安排选读。</p>${A.link('浏览全部习题', 'exercises', c.id, 'pen', 'text-btn')}<div class="divider"></div>${A.badge(c.reviewStatus)}</aside></div>`;
  };
  A.after.outline = function () {
    const id = A.route.params.get('node');
    if (id && A.node(id)) A.actions['read-node']({ dataset: { id } });
  };
  A.actions['credits'] = function () {
    const body = `<div class="provenance-groups">
      <section><h3>页面与业务程序</h3><p>本项目独立编写（AI 辅助）：页面布局、课程导航、图谱展示、题目关联、文件导入与学习记录。课程封面由本项目的数学绘图代码生成，界面图标由 SVG 路径绘制。</p></section>
      <section><h3>开源基础组件</h3><p>KaTeX 用于公式排版，Marked 与 DOMPurify 用于 Markdown 解析和内容清理，ExcelJS 与 JSZip 用于读取课程文件。五项组件均保留原作者的许可证；它们不属于本项目原创代码。</p></section>
      <section><h3>课程内容与教材</h3><p>知识内容、题库、参考教材和课程插图来自教师提供的资源包。原有作者、出处、题目来源和解答说明均予保留。</p></section>
      <section><h3>校园摄影</h3><p><a href="https://commons.wikimedia.org/wiki/File:Wuhan_University_Old_Library.jpg" target="_blank" rel="noopener">Wuhan University Old Library</a>，Howchou，2012。按 <a href="https://creativecommons.org/licenses/by-sa/3.0/" target="_blank" rel="noopener">CC BY-SA 3.0</a> 使用；页面作了 CSS 裁切、饱和度调整和蓝色叠层。</p></section>
      <section><h3>来源核验</h3><p>项目代码、开源组件与课程资料分别登记来源。<a href="reports/source-provenance.json" download>下载本次来源清单</a>。</p></section>
    </div>`;
    A.openDialog('代码与资料来源', body);
  };
})();
