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
          : '选择文件夹或三件套文件；平台先在浏览器里校验锚点、层级与题目关联，再提交到服务器。'}</p>
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

  function previewPanel(preview) {
    const warnings = preview.warnings || [];
    return `<div class="panel">
      <div class="panel-heading"><h2>提交到哪门课</h2>${A.badge(`${preview.nodes.length} 知识点 / ${preview.exercises.length} 题`, 'blue')}</div>
      <div class="field">
        <label for="content-target">目标课程</label>
        <select id="content-target" data-input="content-target">
          ${courseOptions(S().courseId)}
          <option value="__new__" ${S().courseId === '__new__' ? 'selected' : ''}>＋ 新建课程（用资源包里的课程 id：${e(preview.id)}）</option>
        </select>
        <small>选已有课程 = 整体更新那门课；选新建 = 按资源包里的 id 建一门新课。</small>
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
    return `<div class="panel">
      <div class="panel-heading"><h2>导入完成</h2>${A.badge(`批次 ${result.batch}`, 'green')}</div>
      <table class="data-table"><tbody>${rows.map(([label, value]) =>
        `<tr><th>${label}</th><td>${Number(value) || 0}</td></tr>`).join('')}</tbody></table>
      <p class="muted small">${result.mode === 'replace'
        ? '整体更新：文件里没有的旧内容已标记为「已停用」，仍在库里可追溯，学生端不再出现。'
        : '只增改：没有处理文件里缺失的内容。'}</p>
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
    try {
      const entries = relativePaths(files);
      const get = name => entries.find(item => item.path === name)?.file;
      for (const name of ['知识节点.xlsx', '知识点.md', '习题.md']) {
        if (!get(name)) throw new Error(`缺少根目录文件：${name}`);
      }
      const rows = await CourseXlsx.readRows(await get('知识节点.xlsx').arrayBuffer());
      const metadata = get('course.json') ? JSON.parse(await get('course.json').text()) : {};
      const pkg = CourseImport.parsePackage(rows, await get('知识点.md').text(),
        await get('习题.md').text(), metadata);
      S().preview = pkg;
      S().previewFiles = { knowledge: get('知识点.md').name, exercises: get('习题.md').name };
      if (!S().courseId || S().courseId === '__new__') S().courseId = pkg.id;
      A.toast(`解析完成：${pkg.nodes.length} 个知识点、${pkg.exercises.length} 道题`);
    } catch (error) {
      S().preview = null;
      S().importError = error.message;
    }
    A.render();
  }

  function relativePaths(files) {
    const workbook = files.filter(file => file.name === '知识节点.xlsx')
      .sort((a, b) => (a.webkitRelativePath || a.name).length - (b.webkitRelativePath || b.name).length)[0];
    if (!workbook) throw new Error('请选择含知识节点.xlsx、知识点.md 和习题.md 的资源包。');
    const root = workbook.webkitRelativePath?.slice(0, -workbook.name.length) || '';
    return files.map(file => ({
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
        ? '学生端直接读库里的内容：改知识点正文、标签或题干会立刻生效（题目本身仍须是「已发布」状态）。新导入的题要等它在「内容审核」里被发布才会出现。'
        : '发布后学生端会读这门课；只有「内容审核」里点过发布的题会出现，其余仍然隐藏。'}</p>
      <div class="button-row">
        ${published
          ? `<button class="btn" data-action="content-unpublish" ${S().saving ? 'disabled' : ''}>撤回发布</button>`
          : `<button class="btn primary" data-action="content-publish" ${S().saving || !Number(state.published) ? 'disabled' : ''}>发布到学生端</button>`}
        ${Number(state.published) ? '' : `<span class="muted small">还没有题通过审核发布——先去「内容审核」处理。</span>`}
      </div>
    </div>`;
  }

  function nodeList(content) {
    const rows = content.nodes.map(node => {
      const editing = S().editKey === `node:${node.id}`;
      return `<div class="content-row" style="padding-left:${Math.max(0, node.level - 1) * 14}px">
        <div class="content-row-main">
          <b>${e(node.title)}</b>
          ${A.badge((STATUS[node.review_status] || [node.review_status, ''])[0], (STATUS[node.review_status] || ['', ''])[1])}
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

  function exerciseList(content) {
    const rows = content.exercises.map(ex => {
      const editing = S().editKey === `ex:${ex.id}`;
      return `<div class="content-row">
        <div class="content-row-main">
          <b>${e(ex.id)}</b>
          ${A.badge((STATUS[ex.review_status] || [ex.review_status, ''])[0], (STATUS[ex.review_status] || ['', ''])[1])}
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
    A.render();
  };

  A.inputs['content-target'] = input => { S().courseId = input.value; };
  A.inputs['content-course'] = input => { S().courseId = input.value; S().content = null; };

  // 没有 change 监听：文件选择靠 input 事件触发（与教材生成页一致）
  A.inputs['content-folder'] = input => { parseFiles(input.files); };
  A.inputs['content-files'] = input => { parseFiles(input.files); };

  A.actions['content-import'] = async () => {
    const pkg = S().preview;
    if (!pkg) throw new Error('先选择资源包');
    if (!S().courseId) throw new Error('先选目标课程');
    const courseId = S().courseId === '__new__' ? pkg.id : S().courseId;
    S().importing = true;
    A.render();
    try {
      S().importResult = await A.api.importContent(courseId, toPayload(pkg));
      S().courseId = courseId;
      S().preview = null;
      A.api.courseList(true).then(courses => { S().courses = courses; }).catch(() => {});
      S().notice = '导入完成。旧版本里没有的内容已标记为「已停用」。';
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
      S().notice = `已发布：学生端可见 ${result.exercises} 道题、${result.nodes} 个知识点；` +
        `${result.drafts_hidden} 道未审核发布的题仍然隐藏。`;
      S().publish = await A.api.publishStatus(S().courseId);
    } catch (error) {
      S().error = error.message;
    } finally {
      S().saving = false;
      A.render();
    }
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
