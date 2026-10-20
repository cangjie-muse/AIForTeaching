(function () {
  const A = App, e = A.escape;
  A.views.import = function () {
    return A.sectionTitle('COURSE IMPORT', '课程导入', '填写课程名称并选择标准文件，自动生成知识图谱和题库。') + `<div class="import-layout"><section><div class="panel import-drop"><span class="import-icon">${A.icon('upload')}</span><h2>导入一个课程资源包</h2><p>填写课程名称，选择文件夹；平台立即检查知识节点、正文与习题的关联。</p><div class="import-name"><label for="course-import-title">课程名称</label><input id="course-import-title" data-input="course-import-title" type="text" maxlength="80" placeholder="例如：辛几何与数学物理" autocomplete="off"><small>可先填写；留空选文件后将自动带入资源包中的名称，确认前仍可修改。</small></div><div class="button-row"><label for="course-folder" class="btn primary">${A.icon('folder')}选择资源包文件夹</label><label for="course-files" class="btn">选择标准文件</label></div><input id="course-folder" type="file" webkitdirectory multiple hidden><input id="course-files" type="file" multiple accept=".xlsx,.md,.json,.pdf,.png,.jpg,.jpeg,.webp" hidden><small>确认导入后，课程入口、知识图谱与习题库立即可用；数据保存在当前浏览器。</small></div><div id="import-result" aria-live="polite"></div><section class="panel import-format"><div class="panel-heading"><h2>沿用当前交付格式</h2>${A.badge('资源包 v1', 'blue')}</div><div class="format-file"><span>XLSX</span><div><b>知识节点.xlsx</b><p>6 级节点列＋知识说明＋知识点标签</p></div>${A.icon('check')}</div><div class="format-file"><span>MD</span><div><b>知识点.md</b><p>显式锚点、知识正文、公式与教材出处</p></div>${A.icon('check')}</div><div class="format-file"><span>MD</span><div><b>习题.md</b><p>知识点锚点、题目 ID、题干、答案、难度及来源</p></div>${A.icon('check')}</div><div class="format-file optional"><span>＋</span><div><b>资料/ 与 course.json（可选）</b><p>插图、参考教材，以及课程简介等元信息</p></div></div></section></section><aside><section class="panel"><div class="eyebrow">IMPORT WORKFLOW</div><h3>先检查，再接入</h3>${[['01', '填写课程名称', '已有规划课程按名称接入；新课程导入后新增入口。'], ['02', '核对表头与层级', '检查缺列、不连续层级及内容锚点。'], ['03', '去重并建立关联', '同一道题可关联多个知识点。'], ['04', '确认导入结果', '立即生成课程图谱、知识阅读与习题库。']].map(([n, title, desc]) => `<div class="assistant-step"><span>${n}</span><div><b>${title}</b><p>${desc}</p></div></div>`).join('')}</section><section class="panel import-tip"><h3>教材是参考资料</h3><p>平台直接读取知识图谱和题库文件。更换课程时，无需重新分析教材。</p><div class="divider"></div><h3>后续可逐步扩展</h3><p>当前先保留稳定锚点与题目 ID。先修关系、题型和审核状态可作为可选字段继续完善。</p></section></aside></div>`;
  };
  function relativePaths(files) {
    const workbook = files.filter(file => file.name === '知识节点.xlsx').sort((a, b) => (a.webkitRelativePath || a.name).length - (b.webkitRelativePath || b.name).length)[0];
    if (!workbook) throw new Error('请选择含知识节点.xlsx、知识点.md 和习题.md 的资源包。');
    const root = workbook.webkitRelativePath?.slice(0, -workbook.name.length) || '';
    return files.map(file => ({ file, path: file.webkitRelativePath?.startsWith(root) ? file.webkitRelativePath.slice(root.length) : file.name })).filter(item => !item.path.includes('..') && item.path !== '.DS_Store');
  }
  async function prepareImport(fileList) {
    const result = document.getElementById('import-result');
    result.innerHTML = '<div class="panel import-progress">正在检查课程资源、锚点和习题关联…</div>';
    A.state.pendingImport = null;
    try {
      const entries = relativePaths([...fileList]);
      const get = name => entries.find(item => item.path === name)?.file;
      const core = ['知识节点.xlsx', '知识点.md', '习题.md'];
      for (const name of core) if (!get(name)) throw new Error(`缺少根目录文件：${name}`);
      const rows = await CourseXlsx.readRows(await get('知识节点.xlsx').arrayBuffer());
      const metadata = get('course.json') ? JSON.parse(await get('course.json').text()) : {};
      const course = CourseImport.parsePackage(rows, await get('知识点.md').text(), await get('习题.md').text(), metadata);
      const variantText = course.exercises.flatMap(exercise => (exercise.variants || [exercise]).flatMap(variant => [variant.prompt, variant.answer]));
      const images = CourseImport.referencedImages(...course.nodes.map(node => node.content), ...variantText);
      const missing = images.filter(name => !get(name));
      if (missing.length) throw new Error(`还缺 ${missing.length} 张正文引用的插图，请选择包含插图的完整课程文件夹：${missing.slice(0, 3).join('、')}`);
      const selected = [...core, ...images].map(name => ({ path: name, file: get(name) }));
      if (selected.reduce((sum, item) => sum + item.file.size, 0) > 200 * 1024 * 1024) throw new Error('三份标准文件与引用插图超过 200 MB，请按单门课程拆分。');
      const titleInput = document.getElementById('course-import-title');
      if (!titleInput.value.trim()) titleInput.value = course.title;
      course.title = titleInput.value.trim();
      course.files = core;
      course.attachments = images;
      const record = { course, files: selected.map(item => ({ path: item.path, blob: item.file })) };
      A.state.pendingImport = record;
      renderImportPreview(course);
    } catch (error) {
      console.error('[course/import/validate]', error);
      result.innerHTML = `<section class="panel import-error"><h3>需要先修正资源包</h3><p>${e(error.message)}</p><small>当前课程数据未被改变。修正后重新选择文件即可。</small></section>`;
    }
  }
  function renderImportPreview(course) {
    const replacement = A.courses.some(c => c.id === course.id);
    const planned = CourseCatalog.entries.some(entry => entry.id === course.id || [entry.title, ...entry.aliases].some(title => CourseCatalog.normalize(title) === CourseCatalog.normalize(course.title)));
    const destination = replacement ? '相同课程 ID 已存在。确认后更新课程内容，保留按 ID 关联的学习记录。' : planned ? '已匹配两期规划目录。确认后点亮对应课程，不生成重复卡片。' : '确认后会在课程中心新增一门课程，列于“新导入课程”。';
    const warnings = course.warnings;
    document.getElementById('import-result').innerHTML = `<section class="panel import-success"><div class="panel-heading"><h2>${e(course.title || '请填写课程名称')}</h2>${A.badge('结构检查通过', 'green')}</div><div class="import-numbers"><div><b>${course.stats.knowledge}</b><span>知识点</span></div><div><b>${course.stats.exercises}</b><span>不同习题</span></div><div><b>${course.stats.associations}</b><span>知识点关联</span></div></div><ul>${warnings.map(item => `<li>${e(item)}</li>`).join('')}</ul><p>${destination}知识图谱和题库已从文件生成，无须逐项手工创建。</p><div class="button-row">${A.button(replacement ? '确认更新课程' : '确认导入课程', 'confirm-import', 'check', '', 'primary')}${A.button('导出检查摘要', 'export-import-check', 'download')}</div><small>检查结果仅验证文件结构与引用，不代替数学内容审核。</small></section>`;
  }
  A.after.import = function () {
    document.getElementById('course-files').accept = '.xlsx,.md,.json,.png,.jpg,.jpeg,.webp';
    document.querySelector('.import-drop>small').textContent = '仅保存三份标准文件及正文实际引用的插图；PDF 教材不会复制到离线网站。';
    document.querySelector('.import-format .format-file.optional p').textContent = '正文或习题引用的插图需一并选择；course.json 可提供简介等元信息。';
    ['course-folder', 'course-files'].forEach(id => document.getElementById(id).addEventListener('change', event => { if (event.target.files.length) prepareImport(event.target.files); }));
  };
  A.inputs['course-import-title'] = input => {
    const record = A.state.pendingImport; if (!record) return;
    record.course.title = input.value.trim();
    renderImportPreview(record.course);
  };
  A.actions['confirm-import'] = async function (button) {
    const record = A.state.pendingImport; if (!record) return;
    const name = document.getElementById('course-import-title').value.trim();
    if (name.length < 2) return A.toast('请填写至少 2 个字的课程名称。');
    if (A.courses.some(course => course.id !== record.course.id && CourseCatalog.normalize(course.title) === CourseCatalog.normalize(name))) return A.toast('已有同名课程，请核对资源包或改用不同课程名称。');
    record.course.title = name;
    button.disabled = true; button.textContent = '正在保存…';
    try {
      let persisted = false;
      try { await CourseStore.save(record); persisted = true; }
      catch (error) { console.error('[course/import/persist]', error); }
      A.applyImported(record); A.state.pendingImport = null;
      A.go('overview', {}, record.course.id);
      A.toast(persisted ? '课程已导入并保存在当前浏览器' : '课程已导入本次会话。浏览器保存不可用，下次打开需重新选择资源包。');
    } catch (error) { console.error('[course/import]', error); A.toast(error.message); }
    finally { button.disabled = false; button.textContent = '确认导入课程'; }
  };
  A.actions['export-import-check'] = () => {
    const course = A.state.pendingImport?.course; if (course) A.download(`${course.title}-导入检查.json`, JSON.stringify({ course: course.title, schemaVersion: course.schemaVersion, stats: course.stats, warnings: course.warnings }, null, 2));
  };
})();
