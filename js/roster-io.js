(function () {
  /**
   * Excel 导入/导出：账号、班级、班级名单三处共用一套。
   *
   * 三条规矩，三处一样：
   *   1. 先预览再落库。上传后只做校验，屏幕上逐行写着"可导入 / 跳过 / 为什么错"，
   *      点了确认才真的写。一次两百个人，导错了再回滚，老师分不清是哪一行。
   *   2. 账号导出永不含密码。文件会被转发到群里。
   *   3. 学生名单里库里没有的学号，自动建账号并生成随机初始密码，只在导入结果
   *      里出现一次，给一份「初始密码发放表」下载。
   *
   * 表头按名字找列，不要求顺序——老师常把模板列调来调去。
   */
  const A = window.App;
  const XLSX = window.AppXlsx || {};

  const KINDS = {
    accounts: {
      title: '批量导入账号',
      endpoint: 'accounts',
      sheet: '账号',
      file: '账号模板.xlsx',
      header: ['账号', '姓名', '角色', '初始密码'],
      aliases: {
        username: ['账号', '学号', '工号', '用户名', 'username'],
        display_name: ['姓名', '名字', 'name', 'display_name'],
        role: ['角色', '身份', 'role'],
        password: ['初始密码', '密码', 'password']
      },
      required: ['username', 'display_name'],
      columns: [['username', '账号'], ['display_name', '姓名'], ['role', '角色']],
      sample: [['2023301001', '张三', '学生', ''], ['teacher02', '李四', '教师', 'InitPass2026']],
      notes: [
        '账号：学号或工号，只能用字母、数字、点、下划线、短横线。已存在的账号会被跳过（不改密码、不改角色）。',
        '角色：学生 / 教师 / 管理员，留空按学生。',
        '初始密码：留空就由系统生成一个随机密码，学生首次登录必须改密。填了就按你填的（至少 8 位）。',
        '导入完成后会给出每个新账号的初始密码，可以下载成表格线下发给学生。'
      ]
    },
    classes: {
      title: '批量导入班级',
      endpoint: 'classes',
      sheet: '班级',
      file: '班级模板.xlsx',
      header: ['课程', '班级名称', '学年', '学期', '任课教师', '备注'],
      aliases: {
        course: ['课程', '课程id', '课程名称', 'course'],
        name: ['班级名称', '班级', 'name'],
        academic_year: ['学年', 'academic_year'],
        term_kind: ['学期', 'term', 'term_kind'],
        teacher: ['任课教师', '教师', '教师账号', 'teacher'],
        note: ['备注', 'note']
      },
      required: ['course', 'name', 'academic_year', 'term_kind'],
      columns: [['course', '课程'], ['name', '班级'], ['academic_year', '学年'],
        ['term_kind', '学期'], ['teacher', '任课教师']],
      sample: [['数学分析II', '数学分析II-01班', '2026-2027', '秋季学期', '', '']],
      notes: [
        '课程：填课程 id 或课程全名（一字不差）。',
        '学年：写成 2026-2027。学期：秋季学期 / 春季学期 / 夏季学期。',
        '任课教师：填教师账号或姓名；留空就是你自己。',
        '同一课程、同一学期、同名的班级已存在时跳过。'
      ]
    },
    students: {
      title: '导入名单',
      endpoint: 'students',
      sheet: '名单',
      file: '名单模板.xlsx',
      header: ['学号', '姓名', '初始密码'],
      aliases: {
        username: ['学号', '账号', 'student_id', 'username'],
        display_name: ['姓名', 'name', 'display_name'],
        password: ['初始密码', '密码', 'password']
      },
      required: ['username'],
      columns: [['username', '学号'], ['display_name', '姓名']],
      sample: [['2023301001', '张三', ''], ['2023301002', '李四', '']],
      notes: [
        '学号：库里已有这个学生账号，就直接拉进班；没有这个账号，会自动建一个学生账号。',
        '姓名：新建账号时必须有；账号已存在时可以不填。',
        '初始密码：留空就生成随机密码，并要求首次登录改密。',
        '导入结果里的「初始密码发放表」只在这一次出现，别关窗口再找。'
      ]
    }
  };

  const STATUS = {
    ok: { label: '可导入', cls: 'green' },
    skip: { label: '跳过', cls: '' },
    error: { label: '有问题', cls: 'amber' }
  };

  function state(kind) {
    A.state.roster = A.state.roster || {};
    if (!A.state.roster[kind]) {
      A.state.roster[kind] = {
        step: 'idle', busy: false, error: '', filename: '', rows: [],
        preview: null, result: null, classId: ''
      };
    }
    return A.state.roster[kind];
  }

  function e(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function normalizeHeader(text) {
    return String(text || '').replace(/\u3000/g, ' ').replace(/\s+/g, '').toLowerCase();
  }

  /** 按表头找列，返回 {列名: 下标}。缺必填列就报错，不猜。 */
  function locateHeader(headerRow, spec) {
    const found = {};
    const normalized = headerRow.map(normalizeHeader);
    Object.keys(spec.aliases).forEach(key => {
      const aliases = spec.aliases[key].map(normalizeHeader);
      const index = normalized.findIndex(title => aliases.includes(title));
      if (index >= 0) found[key] = index;
    });
    const missing = spec.required.filter(key => found[key] === undefined);
    if (missing.length) {
      const names = missing.map(key => spec.aliases[key][0]).join('、');
      return { error: `表头里找不到「${names}」。第一行应该是：${spec.header.join('、')}` };
    }
    return { found };
  }

  /** 二维表 → 待传的行。行号用 Excel 里的真实行号，报错才指得准。 */
  function parseTable(grid, kind) {
    const spec = KINDS[kind];
    const rows = (grid && grid.rows) || [];
    if (!rows.length) return { error: '这个工作表是空的' };
    const located = locateHeader(rows[0], spec);
    if (located.error) return { error: located.error };
    const mapped = [];
    for (let index = 1; index < rows.length; index += 1) {
      const cells = {};
      Object.keys(located.found).forEach(key => {
        cells[key] = rows[index][located.found[key]] || '';
      });
      const empty = Object.values(cells).every(value => !String(value).trim());
      if (!empty) mapped.push({ line: index + 1, cells });
    }
    if (!mapped.length) return { error: '除了表头没有数据行' };
    return { rows: mapped };
  }

  async function pickFile(kind, file) {
    const current = state(kind);
    if (!file) return;
    current.step = 'busy';
    current.error = '';
    current.preview = null;
    current.result = null;
    current.filename = file.name;
    current.rows = [];
    A.render();
    try {
      if (!XLSX.readGrid) throw new Error('Excel 读写模块没加载（js/xlsx-sheet.js）');
      const grid = await XLSX.readGrid(await file.arrayBuffer());
      const parsed = parseTable(grid, kind);
      if (parsed.error) throw new Error(parsed.error);
      current.rows = parsed.rows;
      const preview = await post(kind, current.rows, true, current.classId);
      current.preview = preview;
      current.step = 'preview';
    } catch (error) {
      current.step = 'idle';
      current.error = error.message;
    }
    A.render();
  }

  async function post(kind, rows, dryRun, classId) {
    const payload = { rows, dry_run: dryRun, create_missing: true };
    if (kind === 'accounts') return A.api.importAccounts(payload);
    if (kind === 'classes') return A.api.importClasses(payload);
    return A.api.importStudents(classId, payload);
  }

  // ---------------------------------------------------------------------------
  // 渲染
  // ---------------------------------------------------------------------------

  function summaryLine(kind, preview) {
    const summary = preview.summary || {};
    const parts = [`<b>${summary.rows || 0}</b> 行`,
      `可导入 <b>${summary.ok || 0}</b>`];
    if (summary.skip) parts.push(`跳过 ${summary.skip}`);
    if (summary.error) parts.push(`<span class="rv-warn">有问题 ${summary.error}</span>`);
    let extra = '';
    if (kind === 'students') {
      const willCreate = (preview.results || [])
        .filter(row => row.will_create_account).length;
      if (willCreate) extra = `<span class="muted">其中 <b>${willCreate}</b> 个学号库里没有，将自动建账号</span>`;
    }
    return `<div class="ri-summary">${parts.join(' · ')} ${extra}</div>`;
  }

  function resultRow(kind, row) {
    const spec = KINDS[kind];
    const cells = row.values || {};
    const status = STATUS[row.status] || STATUS.error;
    const cellsHtml = spec.columns
      .map(([key]) => `<td>${e(cells[key] || '')}</td>`).join('');
    const badge = row.status === 'ok'
      ? A.badge(row.will_create_account || row.will_create_password ? '将新建' : status.label, 'green')
      : A.badge(status.label, status.cls);
    return `<tr>${['<td class="muted">第 ' + e(row.line) + ' 行</td>', cellsHtml].join('')}
      <td>${badge}</td><td>${e(row.reason || '')}</td></tr>`;
  }

  function previewTable(kind) {
    const current = state(kind);
    const preview = current.preview || {};
    const spec = KINDS[kind];
    const head = spec.columns.map(([, label]) => `<th>${label}</th>`).join('');
    const rows = (preview.results || []).map(row => resultRow(kind, row)).join('');
    return `<table class="ax-table ri-table">
      <thead><tr><th>行</th>${head}<th>状态</th><th>说明</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
  }

  function passwordBlock(kind) {
    const rows = (state(kind).result || {}).passwords || [];
    if (!rows.length) return '';
    const body = rows.map(row => `<tr><td>${e(row.username)}</td>
      <td>${e(row.display_name || '')}</td><td><code>${e(row.password)}</code></td>
      <td class="muted">首次登录需改密</td></tr>`).join('');
    return `<div class="ri-passwords">
      <h4>初始密码（只显示这一次）</h4>
      <p class="muted small">把这些密码发下去，学生首次登录会被要求改密。关掉页面就查不到了，
        现在下载成表格更保险。</p>
      <table class="ax-table">
        <thead><tr><th>${kind === 'accounts' ? '账号' : '学号'}</th><th>姓名</th>
          <th>初始密码</th><th>说明</th></tr></thead>
        <tbody>${body}</tbody>
      </table>
      <button class="btn small primary" data-action="roster-passwords" data-kind="${kind}">
        下载初始密码表（${rows.length} 个）</button>
    </div>`;
  }

  function doneBlock(kind) {
    const result = state(kind).result;
    if (!result) return '';
    const summary = result.summary || {};
    const tone = summary.error ? 'amber' : 'green';
    return `<div class="notice ri-done">${A.badge(tone === 'green' ? '已完成' : '完成但有错误', tone)}
      <span>新增 <b>${summary.created || 0}</b> · 已处理 <b>${summary.ok || 0}</b>
      · 跳过 ${summary.skip || 0} · 有问题 ${summary.error || 0}</span></div>
      ${passwordBlock(kind)}`;
  }

  function actions(kind) {
    const current = state(kind);
    if (current.step === 'busy') {
      return '<span class="muted small">正在读取文件并校验…</span>';
    }
    if (current.step === 'preview') {
      const ok = (current.preview.summary || {}).ok || 0;
      return `<div class="button-row">
        <button class="btn primary" data-action="roster-import" data-kind="${kind}"
          data-class="${e(current.classId || '')}" ${ok ? '' : 'disabled'}>确认导入 ${ok} 行</button>
        <button class="btn small text-btn" data-action="roster-reset" data-kind="${kind}">取消</button>
      </div>`;
    }
    if (current.step === 'done') {
      return `<button class="btn small" data-action="roster-reset" data-kind="${kind}">再导一份</button>`;
    }
    return '';
  }

  /**
   * 一块完整的导入区：选文件 → 预览 → 确认 → 结果。
   * options: { exportLabel, rows } 决定右上角要不要放导出按钮。
   */
  function block(kind, options = {}) {
    const spec = KINDS[kind];
    const current = state(kind);
    const current0 = state(kind);
    if (options.classId) current0.classId = options.classId;
    const inputId = `roster-file-${kind}`;
    const exportButton = options.exportLabel
      ? `<button class="btn small" data-action="roster-export" data-kind="${kind}"
           data-class="${e(options.classId || '')}">${e(options.exportLabel)}</button>`
      : '';
    return `<div class="ri-block" data-kind="${kind}">
      <div class="ri-toolbar">
        <label class="btn small" for="${inputId}">${A.icon('upload')}选择 Excel</label>
        <input type="file" id="${inputId}" accept=".xlsx" hidden data-input="${inputId}">
        <button class="btn small text-btn" data-action="roster-template" data-kind="${kind}">下载模板</button>
        ${exportButton}
        ${current.filename ? `<span class="muted small">${e(current.filename)}</span>` : ''}
      </div>
      ${current.error ? `<div class="notice amber">${A.icon('info')}<span>${e(current.error)}</span></div>` : ''}
      ${current.preview ? summaryLine(kind, current.preview) : ''}
      ${current.preview ? previewTable(kind) : ''}
      ${doneBlock(kind)}
      ${actions(kind)}
    </div>`;
  }

  // ---------------------------------------------------------------------------
  // 下载
  // ---------------------------------------------------------------------------

  function save(filename, options) {
    if (!XLSX.downloadSheet) {
      A.toast('下载失败：Excel 模块没加载');
      return;
    }
    XLSX.downloadSheet(filename, options).catch(error => A.toast(`下载失败：${error.message}`));
  }

  function template(kind) {
    const spec = KINDS[kind];
    save(spec.file, {
      name: spec.sheet, header: spec.header, rows: spec.sample, notes: spec.notes
    });
    A.toast(`已下载模板：第一行是表头，可以直接往下面填`);
  }

  function exportAccounts(items) {
    save('账号清单.xlsx', {
      name: '账号',
      header: ['账号', '姓名', '角色', '状态', '登录方式', '最近登录', '创建时间'],
      rows: items.map(row => [
        row.username, row.display_name, row.role_label || row.role,
        row.disabled ? '已停用' : '正常',
        row.has_password ? '本地密码' : '仅统一认证',
        (row.last_login_at || '').slice(0, 19).replace('T', ' '),
        (row.created_at || '').slice(0, 10)
      ])
    });
    A.toast(`已导出 ${items.length} 个账号（不含密码）`);
  }

  function exportClasses(items) {
    save('班级清单.xlsx', {
      name: '班级',
      header: ['课程', '班级名称', '学年', '学期', '任课教师', '人数', '备注'],
      rows: items.map(row => [
        row.course_title || row.course_id, row.name, row.academic_year,
        row.term || row.term_kind, row.teacher_name || '', row.student_count || 0,
        row.note || ''
      ])
    });
    A.toast(`已导出 ${items.length} 个班级`);
  }

  function exportRoster(classInfo, items) {
    save(`${classInfo.name || '班级'}-名单.xlsx`, {
      name: '名单',
      header: ['学号', '姓名', '加入时间', '加入人'],
      rows: items.map(row => [
        row.username, row.display_name,
        (row.enrolled_at || '').slice(0, 19).replace('T', ' '), row.added_by || ''
      ])
    });
    A.toast(`已导出 ${items.length} 人`);
  }

  function passwordSheet(kind) {
    const rows = (state(kind).result || {}).passwords || [];
    if (!rows.length) return;
    save(kind === 'accounts' ? '初始密码表.xlsx' : '名单初始密码表.xlsx', {
      name: '初始密码',
      header: ['账号', '姓名', '初始密码', '说明'],
      rows: rows.map(row => [row.username, row.display_name || '', row.password,
        '首次登录需改密'])
    });
  }

  A.state.roster = A.state.roster || {};
  A.rosterIo = { KINDS, block, template, state, parseTable, setClass,
    exportAccounts, exportClasses, exportRoster, passwordSheet, pickFile };

  /** 打开某个班的名单时记下班 id：导入要打到这个班，不能靠渲染时的副作用。 */
  function setClass(kind, classId) {
    state(kind).classId = classId || '';
  }

  A.inputs['roster-file-accounts'] = input => pickFile('accounts', input.files[0]);
  A.inputs['roster-file-classes'] = input => pickFile('classes', input.files[0]);
  A.inputs['roster-file-students'] = input => pickFile('students', input.files[0]);

  A.actions['roster-template'] = button => { template(button.dataset.kind); };

  A.actions['roster-reset'] = button => {
    const kind = button.dataset.kind;
    A.state.roster[kind] = { step: 'idle', busy: false, error: '', filename: '',
      rows: [], preview: null, result: null, classId: '' };
    A.render();
  };

  A.actions['roster-passwords'] = button => { passwordSheet(button.dataset.kind); };

  A.actions['roster-export'] = async function (button) {
    const kind = button.dataset.kind;
    try {
      if (kind === 'accounts') return exportAccounts(A.state.accounts.items || []);
      if (kind === 'classes') return exportClasses(A.state.classes.items || []);
      const classId = button.dataset.class;
      const data = await A.api.classStudents(classId);
      const klass = (A.state.classes.items || []).find(row => row.id === classId) || {};
      return exportRoster(klass, data.items || []);
    } catch (error) {
      A.toast(`导出失败：${error.message}`);
      return null;
    }
  };

  A.actions['roster-import'] = async function (button) {
    const kind = button.dataset.kind;
    const current = state(kind);
    if (!current.rows.length) {
      A.toast('没有待导入的行，请重新选择文件');
      return;
    }
    button.disabled = true;
    current.error = '';
    try {
      current.result = await post(kind, current.rows, false, current.classId || button.dataset.class);
      current.step = 'done';
      current.preview = null;
      const summary = current.result.summary || {};
      A.toast(`导入完成：新增 ${summary.created || 0}，跳过 ${summary.skip || 0}，有问题 ${summary.error || 0}`);
      await refreshViews(kind, current);
    } catch (error) {
      current.error = error.message;
      button.disabled = false;
    }
    A.render();
  };

  /** 导完把列表刷一遍，不然界面上还是旧的。 */
  async function refreshViews(kind, current) {
    if (kind === 'accounts' && A.actions['accounts-refresh']) await A.actions['accounts-refresh']();
    if (kind === 'classes' && A.actions['classes-refresh']) await A.actions['classes-refresh']();
    if (kind === 'students' && A.actions['classes-roster-refresh'] && current.classId) {
      await A.actions['classes-roster-refresh']({ dataset: { id: current.classId } });
    }
  }

})();
