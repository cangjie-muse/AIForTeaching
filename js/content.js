'use strict';
/* 课程内容工作台：把「课程导入」和「教材生成」合成一个入口。
 *
 * 为什么合：这两件事本来就是同一个动作的两条路径——把课程内容弄进平台。
 * 分开做的时候，导入是纯前端的（解析完存浏览器 IndexedDB，只对当前浏览器有效），
 * 生成是后端的（写数据库）。老师改一份，另一份不变；学生端读的又是静态课程包。
 * 三份内容互不知道，这是"哪门课的习题不对"这类问题的根因。
 *
 * 现在：数据库是唯一真源。
 *   生成 —— PDF → 切章 → 逐章调模型 → draft 进审核队列
 *   导入 —— 已有三件套 → 浏览器校验 → 落库（整体更新时缺的标 retired，不删）
 *   修改 —— 直接改知识点与习题；导出三件套（xlsx + 两份 md）改完再导入
 *
 * 三个标签页的关系不是并列功能，而是同一条链上的三站：
 * 老师手上有什么决定从哪一站起来，改坏了随时可以回到第二站重来。
 */
(function () {
  const A = App, e = A.escape;

  const TABS = [
    ['generate', 'AI 生成', '上传教材 PDF，平台自动切章并逐章生成'],
    ['import', '导入资源包', '已有 知识节点.xlsx + 知识点.md + 习题.md 的课，直接导进来'],
    ['edit', '修改与导出', '在线改知识点和习题；导出 Excel 改完再导入']
  ];

  const STATUS = {
    draft: ['草稿', 'amber'],
    'machine-checked': ['机器校验', 'blue'],
    'teacher-reviewed': ['教师已审', 'green'],
    published: ['已发布', 'green'],
    retired: ['已停用', '']
  };

  A.state.content = A.state.content || {
    tab: 'generate',
    courses: [],
    loaded: false,
    courseId: '',
    // 导入
    preview: null,       // 解析出来的资源包（还没提交）
    previewFiles: null,  // 三件套原文，提交后仍可回显缺什么
    docPreview: null,    // 单文件增量导入的预览报告（还没提交）
    docFiles: null,      // 这次上传了哪几个 md
    docResult: null,     // 单文件导入落库后的结果
    courseName: '',      // 导入页上填的课程名称（默认取资源包里的）
    importError: '',
    importing: false,
    importResult: null,
    mode: 'replace',
    // 修改
    content: null,
    editKey: '',         // 正在编辑的项：'node:kp-1' / 'ex:e-1' / '' 
    editDraft: null,
    saving: false,
    error: '',
    notice: ''
  };

  const S = () => A.state.content;

  function tabBar() {
    return `<div class="button-row content-tabs">${TABS.map(([value, title, note]) => `
      <button class="btn ${S().tab === value ? 'primary' : ''}" data-action="content-tab"
              data-tab="${value}" title="${e(note)}">${e(title)}</button>`).join('')}</div>
      <p class="muted small">${e((TABS.find(t => t[0] === S().tab) || TABS[0])[2])}</p>`;
  }

  function courseOptions(selected) {
    return S().courses.map(course =>
      `<option value="${e(course.id)}" ${course.id === selected ? 'selected' : ''}>${e(course.title)}（${e(course.id)}）</option>`
    ).join('');
  }

  function notice() {
    if (!S().notice) return '';
    return `<div class="panel"><p class="small">${e(S().notice)}</p></div>`;
  }

  // ---------------------------------------------------------------------------
  // 导入
  // ---------------------------------------------------------------------------

  function importPanel() {
    const preview = S().preview;
    return `<div class="import-layout"><section>
      <div class="panel import-drop">
        <span class="import-icon">${A.icon('upload')}</span>
        <h2>${preview ? '核对资源包' : '选择一个课程资源包'}</h2>
        <p>${preview
          ? `已解析：${preview.nodes.length} 个知识点、${preview.exercises.length} 道题。确认目标课程后提交。`
          : '选择文件夹或三件套文件；也可以只选 知识点.md 或 习题.md，按锚点增量改一门已有的课。'}</p>
        <div class="button-row">
          <label for="content-folder" class="btn ${preview ? '' : 'primary'}">${A.icon('folder')}选择资源包文件夹</label>
          <label for="content-files" class="btn">选择标准文件</label>
        </div>
        <input id="content-folder" type="file" webkitdirectory multiple hidden
               data-input="content-folder">
        <input id="content-files" type="file" multiple accept=".xlsx,.md,.json" hidden
               data-input="content-files">
        <small>导入后内容进入数据库，本次浏览器无需保留；旧版本里没再出现的知识点与习题会标记为「已停用」。</small>
      </div>
      ${S().importError ? `<div class="panel"><p class="small">${e(S().importError)}</p></div>` : ''}
      ${preview ? previewPanel(preview) : ''}
      ${S().importResult ? importResultPanel(S().importResult) : ''}
      ${S().docPreview ? docPreviewPanel(S().docPreview) : ''}
      ${S().docResult ? docResultPanel(S().docResult) : ''}
    </section><aside>
      <section class="panel import-format">
        <div class="panel-heading"><h2>沿用当前交付格式</h2>${A.badge('资源包 v1', 'blue')}</div>
        <div class="format-file"><span>XLSX</span><div><b>知识节点.xlsx</b><p>6 级节点列＋知识说明＋知识点标签</p></div>${A.icon('check')}</div>
        <div class="format-file"><span>MD</span><div><b>知识点.md</b><p>显式锚点、知识正文、公式与教材出处</p></div>${A.icon('check')}</div>
        <div class="format-file"><span>MD</span><div><b>习题.md</b><p>知识点锚点、题目 ID、题干、答案、难度及来源</p></div>${A.icon('check')}</div>
        <div class="format-file optional"><span>＋</span><div><b>course.json（可选）</b><p>课程简介、教材名等元信息</p></div></div>
      </section>
      <section class="panel">
        <div class="eyebrow">HOW UPDATE WORKS</div>
        <h3>整体更新不等于覆盖删除</h3>
        ${[['01', '按 ID 对齐', '同一个知识点/题目 ID 视为同一条记录，直接改写。'],
           ['02', '缺的标停用', '文件里没有的旧行标为 retired，学生作答与审核痕迹都留着。'],
           ['03', '只退休导入过的', '模型生成、还没审的草稿不会被一次导入顺手清掉。']]
          .map(([n, title, desc]) => `<div class="assistant-step"><span>${n}</span><div><b>${title}</b><p>${desc}</p></div></div>`).join('')}
      </section>
    </aside></div>`;
  }

  /* 单文件导入的预览：数字比文字重要，老师扫一眼就知道会不会动到别处 */
  function targetTitle(courseId) {
    const course = (S().courses || []).find(item => item.id === courseId);
    return course ? course.title : courseId;
  }

  function docPreviewPanel(report) {
    const rows = [];
    if (report.knowledge) {
      rows.push(['知识点正文更新', report.knowledge.changed]);
      rows.push(['正文没变', report.knowledge.unchanged]);
    }
    if (report.exercises) {
      rows.push(['题目新增', report.exercises.added]);
      rows.push(['题目改写', report.exercises.updated]);
      rows.push(['题目没变', report.exercises.unchanged]);
      rows.push(['从知识点上摘下的题', report.exercises.unlinked]);
    }
    const warnings = report.warnings || [];
    return `<div class="panel">
      <div class="panel-heading"><h2>核对这次改动</h2>${A.badge(e(report.course_id), 'blue')}</div>
      <p class="muted small">写入《${e(targetTitle(report.course_id))}》。只改上传文件里出现的锚点，其他知识点一个字不动；结构（层级、父子）不变。</p>
      <table class="data-table"><tbody>${rows.map(([label, value]) =>
        `<tr><th>${label}</th><td>${Number(value) || 0}</td></tr>`).join('')}</tbody></table>
      ${warnings.length ? `<details class="panel"><summary>${warnings.length} 条提示</summary>
        <ul class="muted small">${warnings.map(text => `<li>${e(text)}</li>`).join('')}</ul></details>` : ''}
      <div class="button-row">
        <button class="btn primary" data-action="content-apply-docs" ${S().importing ? 'disabled' : ''}>
          ${S().importing ? '正在写入…' : '确认写入'}</button>
        <button class="btn" data-action="content-clear">取消</button>
      </div>
    </div>`;
  }

  function docResultPanel(result) {
    const rows = [
      ['知识点正文更新', result.applied?.content_updated],
      ['解除的题目关联', result.applied?.unlinked],
      ['停用的孤儿题', result.applied?.retired]
    ];
    return `<div class="panel">
      <div class="panel-heading"><h2>已写入</h2>${A.badge('单文件增量导入', 'green')}</div>
      <table class="data-table"><tbody>${rows.map(([label, value]) =>
        `<tr><th>${label}</th><td>${Number(value) || 0}</td></tr>`).join('')}</tbody></table>
      <p class="muted small">被摘下来的题如果不再属于任何知识点，会标为「已停用」而不是删除——学生作答记录还在。</p>
      <div class="button-row">
        ${A.link('看知识图谱', 'graph', result.course_id)}
        ${A.link('去审核', 'review', null)}
      </div>
    </div>`;
  }

  /* 课程名称这一栏要回答两件事：和资源包里的名字对不对得上、课程中心里有没有。
     两件都在这里实时说，不等到点下确认才报错。 */
  function sameCourseName(left, right) {
    const clean = text => String(text || '').normalize('NFKC').replace(/[\s《》()（）]/g, '').toLowerCase();
    return clean(left) === clean(right);
  }

  function findByName(name) {
    return (S().courses || []).find(course => sameCourseName(course.title, name));
  }

  function nameHint(preview) {
    const name = String(S().courseName || preview.title).trim();
    const existing = findByName(name);
    const parts = [];
    if (sameCourseName(name, preview.title)) {
      parts.push(`与资源包里的课程名一致：${preview.title}。`);
    } else {
      parts.push(`资源包里的课程名是《${preview.title}》，你填的是《${name}》，确认时会再问一遍。`);
    }
    if (existing) {
      parts.push(`课程中心里已经有《${existing.title}》（id：${existing.id}），这次导入会更新它。`);
    } else {
      parts.push(`课程中心里还没有这门课，导入后会新建（id：${preview.id}）。`);
    }
    return parts.join('');
  }

  function previewPanel(preview) {
    const warnings = preview.warnings || [];
    return `<div class="panel">
      <div class="panel-heading"><h2>提交到哪门课</h2>${A.badge(`${preview.nodes.length} 知识点 / ${preview.exercises.length} 题`, 'blue')}</div>
      <div class="field">
        <label for="content-name">课程名称</label>
        <input id="content-name" data-input="content-name" value="${e(S().courseName || preview.title)}"
               placeholder="例如 数学分析 I" autocomplete="off">
        <small>${nameHint(preview)}</small>
      </div>
      <div class="field">
        <label>更新方式</label>
        <div class="button-row">
          <button class="btn ${S().mode === 'replace' ? 'primary' : ''}" data-action="content-mode" data-mode="replace">整体更新（缺的标停用）</button>
          <button class="btn ${S().mode === 'merge' ? 'primary' : ''}" data-action="content-mode" data-mode="merge">只增改（不处理缺的）</button>
        </div>
      </div>
      ${warnings.length ? `<details class="panel"><summary>${warnings.length} 条提示</summary><ul class="muted small">${warnings.map(w => `<li>${e(w)}</li>`).join('')}</ul></details>` : ''}
      <div class="button-row">
        <button class="btn primary" data-action="content-import" ${S().importing ? 'disabled' : ''}>${S().importing ? '正在导入…' : '确认导入并更新课程'}</button>
        <button class="btn" data-action="content-clear">清除选择</button>
      </div>
    </div>`;
  }

  function importResultPanel(result) {
    const rows = [
      ['知识点写入', result.nodes_upserted],
      ['知识点停用', result.nodes_retired],
      ['习题写入', result.exercises_upserted],
      ['习题停用', result.exercises_retired]
    ];
    const created = result.course_created;
    return `<div class="panel">
      <div class="panel-heading"><h2>${created ? '已新建课程' : '已更新课程'}</h2>${A.badge(`批次 ${result.batch}`, 'green')}</div>
      <table class="data-table"><tbody>
        <tr><th>课程</th><td>${e(result.course_title || result.course_id)}（id：${e(result.course_id)}）</td></tr>
        <tr><th>课程状态</th><td>${created ? '新建' : '已存在，本次为更新'}</td></tr>
        ${rows.map(([label, value]) => `<tr><th>${label}</th><td>${Number(value) || 0}</td></tr>`).join('')}
      </tbody></table>
      <p class="muted small">${result.mode === 'replace'
        ? '整体更新：文件里没有的旧内容已标记为「已停用」，仍在库里可追溯，学生端不再出现。'
        : '只增改：没有处理文件里缺失的内容。'}
        课程内容进来后是「待审核」，审核并发布后才会出现在课程中心。</p>
      <div class="button-row">
        ${A.link('看知识图谱', 'graph', result.course_id)}
        ${A.link('去审核', 'review', null)}
        <button class="btn" data-action="content-edit-refresh">在「修改与导出」里查看</button>
      </div>
    </div>`;
  }

  async function parseFiles(fileList) {
    S().importError = '';
    S().importResult = null;
    const files = [...fileList];
    if (!files.length) return;
    // 分步记下"做到哪一步失败"。原来只有一句 "Cannot read properties of undefined"，
    // 既不知道是哪个文件，也不知道是我们的代码还是解析库内部——排查全靠猜。
    let stage = '读取文件列表';
    try {
      stage = '识别资源包结构';
      const entries = relativePaths(files);
      const get = name => entries.find(item => item.path === name)?.file;
      if (!get('知识节点.xlsx')) {
        // 没有知识节点表 = 单文件增量导入：只改锚点对应的正文/题目，不动结构
        const knowledge = get('知识点.md'), exercises = get('习题.md');
        if (!knowledge && !exercises) {
          throw new Error('请选择含知识节点.xlsx、知识点.md 和习题.md 的资源包；'
            + '只改内容时可以只传 知识点.md 或 习题.md。');
        }
        const target = (S().courses || []).find(course => course.id === S().courseId);
        if (!target) {
          throw new Error('单文件导入要指定写入哪门课：先去「修改与导出」标签选一门课'
            + '（新建课程必须带知识节点.xlsx）。');
        }
        // 原文留在内存里：确认写入时要用同一份文本，否则两次读取之间文件被
        // 改动，预览看到的和写进去的就不是一回事了
        const knowledgeText = knowledge ? await knowledge.text() : null;
        const exercisesText = exercises ? await exercises.text() : null;
        S().docPreview = await A.api.importDocs(S().courseId, {
          knowledge_md: knowledgeText, exercises_md: exercisesText, dry_run: true
        });
        S().docFiles = { knowledge: knowledgeText, exercises: exercisesText };
        A.toast(`已解析：${summaryOf(S().docPreview)}`);
        A.render();
        return;
      }
      for (const name of ['知识节点.xlsx', '知识点.md', '习题.md']) {
        if (!get(name)) throw new Error(`缺少根目录文件：${name}`);
      }
      stage = '读取 知识节点.xlsx';
      let rows;
      try {
        rows = await CourseXlsx.readRows(await get('知识节点.xlsx').arrayBuffer());
      } catch (error) {
        // WPS / 程序生成的 xlsx 会在这里报一句看不懂的错（例如 reading 'name'）。
        // 说清楚"换 Excel 另存一次"比把库的异常原样抛给老师有用。
        throw new Error(`知识节点.xlsx 读不出来：${error.message}。`
          + '如果是 WPS 或其他工具生成的，用 Excel 打开后「另存为」一次再上传。');
      }
      stage = '读取 course.json';
      const metadata = get('course.json') ? JSON.parse(await get('course.json').text()) : {};
      stage = '解析 知识点.md / 习题.md';
      const knowledgeFile = get('知识点.md'), exerciseFile = get('习题.md');
      const pkg = CourseImport.parsePackage(rows, await knowledgeFile.text(),
        await exerciseFile.text(), metadata);
      S().preview = pkg;
      S().courseName = pkg.title;
      S().previewFiles = { knowledge: knowledgeFile.name, exercises: exerciseFile.name };
      S().courseId = S().courses.some(course => course.id === pkg.id) ? pkg.id : '__new__';
      S().courseName = pkg.title;
      A.toast(`解析完成：${pkg.nodes.length} 个知识点、${pkg.exercises.length} 道题`);
    } catch (error) {
      S().preview = null;
      S().importError = `${stage}时失败：${error.message}`;
    }
    A.render();
  }

  function summaryOf(report) {
    const parts = [];
    if (report.knowledge) parts.push(`${report.knowledge.changed} 个知识点正文有改动`);
    if (report.exercises) parts.push(`${report.exercises.added + report.exercises.updated} 道题新增或改写`);
    return parts.join('，') || '没有需要改动的内容';
  }

  function relativePaths(files) {
    const named = files.filter(file => file && typeof file.name === 'string');
    const workbook = named.filter(file => file.name === '知识节点.xlsx')
      .sort((a, b) => (a.webkitRelativePath || a.name).length - (b.webkitRelativePath || b.name).length)[0];
    // 没有 xlsx 时只可能是单个 md：根目录就取选择器给的相对路径前缀
    const root = workbook
      ? workbook.webkitRelativePath?.slice(0, -workbook.name.length) || ''
      : (named.find(file => file.webkitRelativePath)?.webkitRelativePath
        ?.split('/')?.slice(0, -1).join('/') + '/' || '');
    return named.map(file => ({
      file,
      path: file.webkitRelativePath?.startsWith(root) ? file.webkitRelativePath.slice(root.length) : file.name
    })).filter(item => !item.path.includes('..') && item.path !== '.DS_Store');
  }

  function toPayload(pkg) {
    return {
      course: {
        title: pkg.title, description: pkg.description, audience: pkg.audience,
        subject: pkg.subject, schema_version: pkg.schemaVersion
      },
      nodes: pkg.nodes.map(node => ({
        id: node.id, title: node.title, path: node.path, level: node.level,
        parent_id: node.parentId, chapter_id: node.chapterId,
        content_md: node.content || '', exercise_anchor: node.exerciseAnchor,
        tags: node.tags || [], source_raw: node.source || null
      })),
      exercises: pkg.exercises.map(exercise => ({
        id: exercise.id, prompt_md: exercise.prompt, answer_md: exercise.answer,
        difficulty: exercise.difficulty, source_raw: exercise.source,
        node_ids: exercise.nodeIds || []
      })),
      mode: S().mode
    };
  }

  // ---------------------------------------------------------------------------
  // 修改与导出
  // ---------------------------------------------------------------------------

  function editPanel() {
    const content = S().content;
    const heading = `<div class="panel">
      <div class="panel-heading"><h2>修改与导出</h2>${content ? A.badge(`${content.nodes.length} 知识点 / ${content.exercises.length} 题`, 'blue') : ''}</div>
      <div class="field">
        <label for="content-course">课程</label>
        <select id="content-course" data-input="content-course">${courseOptions(S().courseId)}</select>
        <small>这里改的是数据库里的内容，改完立刻生效；学生端要等「发布」之后才会变。</small>
      </div>
      <div class="button-row">
        <button class="btn" data-action="content-load">读取内容</button>
        <button class="btn primary" data-action="content-export" ${content ? '' : 'disabled'}>导出三件套（Excel + 两份 md）</button>
      </div>
      ${S().error ? `<p class="small">${e(S().error)}</p>` : ''}
    </div>`;
    if (!content) return heading;
    return heading + publishPanel() + nodeList(content) + exerciseList(content);
  }

  // 发布是"课程级"开关：条目级发布（审核页逐题点）只决定单条能不能露出来，
  // 这一层决定整门课在学生端存不存在。没发布时公开目录里根本不出现这门课。
  function publishPanel() {
    const state = S().publish;
    if (!state) return `<div class="panel"><p class="muted small">读取发布状态…</p></div>`;
    const published = Boolean(state.published_at);
    return `<div class="panel">
      <div class="panel-heading"><h2>学生端发布</h2>
        ${published ? A.badge('已发布', 'green') : A.badge('未发布', 'amber')}</div>
      <table class="data-table"><tbody>
        <tr><th>知识点（学生可见）</th><td>${Number(state.nodes) || 0}</td></tr>
        <tr><th>已审核发布的题</th><td>${Number(state.published) || 0}</td></tr>
        <tr><th>不会出现的题（草稿/待审/停用）</th><td>${Number(state.hidden) || 0}</td></tr>
        ${published && state.published_at ? `<tr><th>发布时间</th><td>${e(String(state.published_at).slice(0, 19).replace('T', ' '))}</td></tr>` : ''}
      </tbody></table>
      <p class="muted small">${published
        ? '学生端直接读库里的内容：改知识点正文、标签或题干会立刻生效（题目本身仍须是「已发布」状态）。新导入的题要等它被发布才会出现。'
        : '发布 = 把「人工已审」的题一起放出，其余仍然隐藏。审核是逐题的动作（「内容审核」里通过或批量通过），发布是整门课的开关。'}</p>
      <div class="button-row">
        ${published
          ? `<button class="btn" data-action="content-unpublish" ${S().saving ? 'disabled' : ''}>撤回发布</button>`
          : `<button class="btn primary" data-action="content-publish" ${S().saving ? 'disabled' : ''}>发布到学生端</button>
             <button class="btn" data-action="content-publish-all" ${S().saving ? 'disabled' : ''}>放出全部题目并发布</button>`}
      </div>
      ${published ? '' : `<p class="muted small">还没有题被发布时，两条路：① 去「内容审核」勾选后批量通过，再回来点发布（已审的题会在这一步一起放出）；② 点「放出全部题目并发布」——跳过逐题审核，可信度仍显示「未经人工校验」，审计日志记下是你做的。</p>`}
    </div>`;
  }

  function nodeList(content) {
    const rows = content.nodes.map(node => {
      const editing = S().editKey === `node:${node.id}`;
      return `<div class="content-row" style="padding-left:${Math.max(0, node.level - 1) * 14}px">
        <div class="content-row-main">
          <b>${e(node.title)}</b>
          ${A.badge((STATUS[node.review_status] || [node.review_status, ''])[0], (STATUS[node.review_status] || ['', ''])[1])}
          ${node.human_verified_at ? A.badge('图谱已审', 'green') : A.badge('图谱未审', '')}
          <span class="muted small">${e((node.tags || []).join('、'))}</span>
          <span class="muted small">${e(node.id)}</span>
          <button class="btn small" data-action="content-node-edit" data-id="${e(node.id)}">${editing ? '收起' : '编辑'}</button>
          <button class="btn small" data-action="content-node-retire" data-id="${e(node.id)}">停用</button>
        </div>
        ${editing ? nodeEditor(node) : `<p class="muted small">${e((node.content_md || '（暂无正文）').slice(0, 160))}</p>`}
      </div>`;
    }).join('');
    return `<div class="panel"><div class="panel-heading"><h2>知识点（${content.nodes.length}）</h2></div>${rows || '<p class="muted">这门课还没有知识点。</p>'}</div>`;
  }

  function nodeEditor(node) {
    const draft = S().editDraft || {};
    return `<div class="panel">
      <div class="field"><label for="node-title">标题</label>
        <input id="node-title" data-input="node-title" value="${e(draft.title ?? node.title)}"></div>
      <div class="field"><label for="node-tags">标签（用、分隔）</label>
        <input id="node-tags" data-input="node-tags" value="${e((draft.tags ?? node.tags ?? []).join('、'))}"></div>
      <div class="field"><label for="node-content">正文（Markdown，可用 $公式$）</label>
        <textarea id="node-content" data-input="node-content" rows="10">${e(draft.content_md ?? node.content_md ?? '')}</textarea></div>
      <div class="button-row">
        <button class="btn primary" data-action="content-node-save" data-id="${e(node.id)}" ${S().saving ? 'disabled' : ''}>保存</button>
        <button class="btn" data-action="content-cancel">取消</button>
      </div>
    </div>`;
  }

  // 审核状态直接标在行上：改内容的人要知道"这条审过没有"，否则会顺手改坏已审核的内容。
  const TIER_SHORT = {
    none: ['未审核', ''],
    lean: ['Lean 校验', 'green'],
    human: ['人工审核通过', 'green'],
    strongest: ['Lean + 人工', 'green']
  };

  function tierBadge(tier) {
    const [label, variant] = TIER_SHORT[tier] || TIER_SHORT.none;
    return A.badge(label, variant);
  }

  function exerciseList(content) {
    const rows = content.exercises.map(ex => {
      const editing = S().editKey === `ex:${ex.id}`;
      return `<div class="content-row">
        <div class="content-row-main">
          <b>${e(ex.id)}</b>
          ${A.badge((STATUS[ex.review_status] || [ex.review_status, ''])[0], (STATUS[ex.review_status] || ['', ''])[1])}
          ${tierBadge(ex.tier)}
          ${A.badge(`难度 ${ex.difficulty ?? '—'}`, '')}
          <span class="muted small">${e(ex.source_raw || '未标注')}</span>
          <button class="btn small" data-action="content-ex-edit" data-id="${e(ex.id)}">${editing ? '收起' : '编辑'}</button>
          <button class="btn small" data-action="content-ex-retire" data-id="${e(ex.id)}">停用</button>
        </div>
        ${editing ? exerciseEditor(ex) : `<p class="muted small">${e((ex.prompt_md || '').slice(0, 160))}</p>`}
      </div>`;
    }).join('');
    return `<div class="panel"><div class="panel-heading"><h2>习题（${content.exercises.length}）</h2></div>${rows || '<p class="muted">这门课还没有习题。</p>'}</div>`;
  }

  function exerciseEditor(ex) {
    const draft = S().editDraft || {};
    return `<div class="panel">
      <div class="field"><label for="ex-prompt">题干（Markdown）</label>
        <textarea id="ex-prompt" data-input="ex-prompt" rows="4">${e(draft.prompt_md ?? ex.prompt_md ?? '')}</textarea></div>
      <div class="field"><label for="ex-answer">答案</label>
        <textarea id="ex-answer" data-input="ex-answer" rows="4">${e(draft.answer_md ?? ex.answer_md ?? '')}</textarea></div>
      <div class="field"><label for="ex-difficulty">难度 1–5</label>
        <input id="ex-difficulty" type="number" min="1" max="5" data-input="ex-difficulty" value="${e(String(draft.difficulty ?? ex.difficulty ?? 3))}"></div>
      <div class="field"><label for="ex-source">出处（会重新解析书页与题号）</label>
        <input id="ex-source" data-input="ex-source" value="${e(draft.source_raw ?? ex.source_raw ?? '')}"></div>
      <div class="button-row">
        <button class="btn primary" data-action="content-ex-save" data-id="${e(ex.id)}" ${S().saving ? 'disabled' : ''}>保存</button>
        <button class="btn" data-action="content-cancel">取消</button>
      </div>
    </div>`;
  }

  // ---------------------------------------------------------------------------
  // 导出
  // ---------------------------------------------------------------------------

  async function exportBundle() {
    const courseId = S().courseId;
    if (!courseId || courseId === '__new__') throw new Error('先选一门课程');
    const bundle = await A.api.exportContent(courseId);
    const rows = exportRows(bundle);
    const workbook = new ExcelJS.Workbook();
    const sheet = workbook.addWorksheet('知识节点');
    const headers = CourseImport.headers;
    sheet.addRow(headers);
    rows.forEach(row => sheet.addRow(row));
    sheet.getRow(1).font = { bold: true };
    headers.forEach((_, index) => { sheet.getColumn(index + 1).width = index < 6 ? 18 : 46; });
    const buffer = await workbook.xlsx.writeBuffer();
    const base = `${bundle.course.title}`;
    A.download(`${base}-知识节点.xlsx`, buffer, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
    A.download(`${base}-知识点.md`, bundle.knowledge_md, 'text/markdown');
    A.download(`${base}-习题.md`, bundle.exercise_md, 'text/markdown');
    S().notice = `已导出 ${bundle.nodes.length} 个知识点与 ${bundle.exercises.length} 道题。用 Excel 改完，回到「导入资源包」把三件套选回来即可整体更新。`;
    A.toast('三件套已导出');
  }

  function exportRows(bundle) {
    // 与导入器同一套列：1–6 级节点 / 知识说明 / 知识点标签
    // 知识说明里必须同时有 知识点.md 与 习题.md 的引用，否则导入器会拒。
    return bundle.nodes
      .map(node => {
        const cells = ['', '', '', '', '', ''];
        node.path.forEach((part, index) => { if (index < 6) cells[index] = part; });
        const anchor = `知识点.md#${node.id}`;
        const exercise = `习题.md#${node.exercise_anchor || node.id}`;
        const source = node.source_raw ? `${node.source_raw}。` : '';
        return [...cells, `${source}${anchor}；${exercise}`, (node.tags || []).join('、')];
      });
  }

  // ---------------------------------------------------------------------------
  // 视图与动作
  // ---------------------------------------------------------------------------

  A.views.content = function () {
    const heading = A.sectionTitle(
      'COURSE CONTENT',
      '课程内容',
      '上传教材让平台生成，或导入已经切好的资源包；改完可以导出 Excel 再导回来。生成与导入的内容都先进审核队列，审核通过才进学生端。'
    );
    let panel;
    if (S().tab === 'generate') panel = A.panels.generate();
    else if (S().tab === 'import') panel = importPanel();
    else panel = editPanel();
    return heading + tabBar() + notice() + panel;
  };

  A.after.content = function () {
    if (!S().loaded) {
      S().loaded = true;
      A.api.courseList().then(courses => {
        S().courses = courses || [];
        if (!S().courseId && S().courses.length) S().courseId = S().courses[0].id;
        A.render();
      }).catch(error => {
        S().error = `读不到课程列表：${error.message}`;
        A.render();
      });
    }
    if (S().tab === 'generate' && A.after.generate) A.after.generate();
  };

  A.actions['content-tab'] = button => {
    S().tab = button.dataset.tab;
    S().notice = '';
    if (S().tab === 'edit' && !S().content) loadContent();
    else A.render();
  };

  A.actions['content-mode'] = button => { S().mode = button.dataset.mode; A.render(); };

  A.actions['content-clear'] = () => {
    S().preview = null;
    S().importError = '';
    S().importResult = null;
    S().docPreview = null;
    S().docResult = null;
    A.render();
  };

  A.actions['content-apply-docs'] = async () => {
    const report = S().docPreview;
    if (!report) throw new Error('先选择要导入的文件');
    S().importing = true;
    A.render();
    try {
      const files = S().docFiles || {};
      S().docResult = await A.api.importDocs(S().courseId, {
        knowledge_md: files.knowledge || null,
        exercises_md: files.exercises || null,
        dry_run: false
      });
      S().docPreview = null;
      S().notice = '增量导入完成：只改了上传文件里出现的锚点。';
      A.api.courseList(true).then(courses => { S().courses = courses; }).catch(() => {});
    } catch (error) {
      S().importError = error.message;
    } finally {
      S().importing = false;
      A.render();
    }
  };

  A.inputs['content-name'] = input => {
    S().courseName = input.value;
    // 只重画提示那一行：重画整页会把输入框的焦点和光标位置弄丢
    const hint = input.parentElement && input.parentElement.querySelector('small');
    if (hint && S().preview) hint.textContent = nameHint(S().preview);
  };

  A.inputs['content-course'] = input => { S().courseId = input.value; S().content = null; };

  // 没有 change 监听：文件选择靠 input 事件触发（与教材生成页一致）
  A.inputs['content-folder'] = input => { parseFiles(input.files); };
  A.inputs['content-files'] = input => { parseFiles(input.files); };

  /* 导入前的确认：只弹一次框，把"名字对不对、课在不在、会怎么改"写在同一段话里。
     两个 confirm 连着弹，人会条件反射地点确定。 */
  function importConfirmText(preview, name, existing) {
    const lines = [];
    if (!sameCourseName(name, preview.title)) {
      lines.push(`你填的课程名称是《${name}》，资源包里的课程名是《${preview.title}》。`);
    }
    if (existing) {
      lines.push(`课程中心里已经有《${existing.title}》，这次导入会更新它：`
        + '文件里没再出现的旧知识点与习题会标为停用。');
    } else {
      lines.push(`课程中心里还没有《${name}》，导入后会新建一门课程。`);
    }
    lines.push(S().mode === 'replace' ? '更新方式：整体更新（缺的标停用）。'
      : '更新方式：只增改（不处理文件里缺的）。');
    lines.push('确认导入？');
    return lines.join('\n\n');
  }

  /* 导入改的是"库里有什么"，所以课程中心与课程总览都得重新拉一次。
     只改本地这份列表的话，总览上的知识点与习题数还是旧的。 */
  async function refreshAfterImport() {
    S().courses = await A.api.courseList(true).catch(() => S().courses);
    if (typeof A.loadCatalog === 'function') await A.loadCatalog();
    else A.render();
  }

  A.actions['content-import'] = async () => {
    const pkg = S().preview;
    if (!pkg) throw new Error('先选择资源包');
    const name = String(S().courseName || '').trim();
    if (!name) throw new Error('先填写课程名称');
    S().importing = true;
    A.render();
    try {
      // 存在与否只认库里那份：手上的列表可能是几分钟前拉的
      S().courses = await A.api.courseList(true).catch(() => S().courses);
      const existing = findByName(name);
      const sameId = (S().courses || []).find(course => course.id === pkg.id);
      const courseId = existing ? existing.id : (sameId ? sameId.id : pkg.id);
      if ((existing || !sameCourseName(name, pkg.title))
          && !window.confirm(importConfirmText(pkg, name, existing))) {
        S().importing = false;
        A.render();
        return;
      }
      S().importResult = await A.api.importContent(courseId, {
        ...toPayload(pkg),
        title_override: sameCourseName(name, pkg.title) ? null : name
      });
      S().courseId = courseId;
      S().preview = null;
      S().content = null;   // 修改与导出标签里的缓存一并作废
      await refreshAfterImport();
      const result = S().importResult || {};
      S().notice = result.course_created
        ? `已新建课程《${result.course_title || name}》，内容进入数据库，待审核后发布。`
        : `已更新课程《${result.course_title || name}》；文件里没有的旧内容标为「已停用」。`;
    } catch (error) {
      S().importError = error.message;
    } finally {
      S().importing = false;
      A.render();
    }
  };

  A.actions['content-load'] = async () => { await loadContent(true); };

  async function loadContent(force = false) {
    if (!S().courseId || S().courseId === '__new__') throw new Error('先选一门课程');
    S().error = '';
    S().publish = null;
    try {
      S().content = await A.api.courseContent(S().courseId, force);
      S().editKey = '';
      S().editDraft = null;
      S().publish = await A.api.publishStatus(S().courseId);
    } catch (error) {
      S().content = null;
      S().error = error.message;
    }
    A.render();
  }

  A.actions['content-publish'] = async () => {
    S().saving = true;
    try {
      const result = await A.api.publishCourse(S().courseId);
      const hiddenNote = result.drafts_hidden ? `；${result.drafts_hidden} 道未审核发布的题仍然隐藏` : '';
      S().notice = `已发布：学生端可见 ${result.exercises} 道题、${result.nodes} 个知识点`
        + hiddenNote + '。' + (result.warning ? `注意：${result.warning}` : '');
      S().publish = await A.api.publishStatus(S().courseId);
      if (A.loadCatalog) A.loadCatalog();  // 目录里的「建设中」要跟着消失
    } catch (error) {
      S().error = error.message;
    } finally {
      S().saving = false;
      A.render();
    }
  };

  /* 整本书的题一次放出。可信度不会因此变成"已人工校验"——
     发布与验证是两件事，替 486 道题盖章只会让"人工已审"这个标记变得不可信。 */
  A.actions['content-publish-all'] = async () => {
    const hidden = Number((S().publish || {}).hidden) || 0;
    const nodes = Number((S().publish || {}).nodes) || 0;  // 确认框里要写清会放出什么
    const answer = window.confirm(`把本课 ${hidden} 道题全部标为「已发布」，并把这门课发布到学生端？\n\n`
      + `· 学生将看到 ${nodes} 个知识点与 ${hidden} 道题；\n`
      + '· 这会跳过逐题审核，可信度仍显示「未经人工校验」；\n'
      + '· 审计日志会记下是你做的，事后查得到。');
    if (!answer) return;
    S().saving = true;
    try {
      const result = await A.api.publishAllExercises(S().courseId, '整课放出（跳过逐题审核）');
      const published = await A.api.publishCourse(S().courseId, '整课放出后发布');
      S().notice = `已放出 ${result.published} 道题，课程已发布：`
        + `学生端可见 ${published.nodes} 个知识点、${published.exercises} 道题。`
        + (result.published
          ? '这些题没有人工校验标记，可见范围若设成「仅人工审核」仍不会放出来。'
          : '这门课一道题都没有，学生只能看知识点。');
      S().publish = await A.api.publishStatus(S().courseId);
      if (A.loadCatalog) A.loadCatalog();
    } catch (error) { S().error = error.message; } finally { S().saving = false; A.render(); }
  };

  A.actions['content-unpublish'] = async () => {
    if (!window.confirm('撤回发布？学生端将立刻看不到这门课。')) return;
    S().saving = true;
    try {
      await A.api.unpublishCourse(S().courseId);
      S().notice = '已撤回发布，学生端不再出现这门课。';
      S().publish = await A.api.publishStatus(S().courseId);
    } catch (error) {
      S().error = error.message;
    } finally {
      S().saving = false;
      A.render();
    }
  };

  A.actions['content-export'] = async () => {
    try { await exportBundle(); }
    catch (error) { S().error = error.message; }
    A.render();
  };

  A.actions['content-node-edit'] = button => {
    if (S().editKey === `node:${button.dataset.id}`) {
      S().editKey = ''; S().editDraft = null;
    } else {
      S().editKey = `node:${button.dataset.id}`; S().editDraft = {};
    }
    A.render();
  };

  A.actions['content-ex-edit'] = button => {
    if (S().editKey === `ex:${button.dataset.id}`) {
      S().editKey = ''; S().editDraft = null;
    } else {
      S().editKey = `ex:${button.dataset.id}`; S().editDraft = {};
    }
    A.render();
  };

  A.actions['content-cancel'] = () => { S().editKey = ''; S().editDraft = null; A.render(); };

  A.inputs['node-title'] = input => { S().editDraft = S().editDraft || {}; S().editDraft.title = input.value; };
  A.inputs['node-tags'] = input => {
    S().editDraft = S().editDraft || {}; S().editDraft.tags = input.value.split(/[、,，\s]+/).map(tag => tag.trim()).filter(Boolean);
  };
  A.inputs['node-content'] = input => { S().editDraft = S().editDraft || {}; S().editDraft.content_md = input.value; };
  A.inputs['ex-prompt'] = input => { S().editDraft = S().editDraft || {}; S().editDraft.prompt_md = input.value; };
  A.inputs['ex-answer'] = input => { S().editDraft = S().editDraft || {}; S().editDraft.answer_md = input.value; };
  A.inputs['ex-difficulty'] = input => { S().editDraft = S().editDraft || {}; S().editDraft.difficulty = Number(input.value); };
  A.inputs['ex-source'] = input => { S().editDraft = S().editDraft || {}; S().editDraft.source_raw = input.value; };

  A.actions['content-node-save'] = async button => {
    const draft = S().editDraft || {};
    S().saving = true;
    try {
      await A.api.patchNode(S().courseId, button.dataset.id, draft);
      S().notice = '知识点已保存。';
      S().editKey = ''; S().editDraft = null;
      S().content = await A.api.courseContent(S().courseId);
    } catch (error) { S().error = error.message; }
    finally { S().saving = false; A.render(); }
  };

  A.actions['content-ex-save'] = async button => {
    const draft = S().editDraft || {};
    S().saving = true;
    try {
      // 习题的编辑接口在审核模块（同一套修订历史），这里复用它，不另开一条写路径。
      await A.api.patch(`/api/v1/review/exercises/${encodeURIComponent(S().courseId)}/${encodeURIComponent(button.dataset.id)}`, {
        prompt_md: draft.prompt_md, answer_md: draft.answer_md,
        difficulty: draft.difficulty, es_source: draft.source_raw
      });
      S().notice = '习题已保存（生成了一条修订记录）。';
      S().editKey = ''; S().editDraft = null;
      S().content = await A.api.courseContent(S().courseId);
    } catch (error) { S().error = error.message; }
    finally { S().saving = false; A.render(); }
  };

  A.actions['content-node-retire'] = async button => {
    if (!window.confirm('停用这个知识点？它不会物理删除，学生端不再出现。')) return;
    try {
      await A.api.retireNode(S().courseId, button.dataset.id);
      S().content = await A.api.courseContent(S().courseId);
      S().notice = '知识点已停用。';
    } catch (error) { S().error = error.message; }
    A.render();
  };

  A.actions['content-ex-retire'] = async button => {
    if (!window.confirm('停用这道题？它不会物理删除，学生端不再出现。')) return;
    try {
      await A.api.retireExercise(S().courseId, button.dataset.id);
      S().content = await A.api.courseContent(S().courseId);
      S().notice = '习题已停用。';
    } catch (error) { S().error = error.message; }
    A.render();
  };

  A.actions['content-edit-refresh'] = async () => {
    S().tab = 'edit';
    await loadContent(true);
  };
})();
