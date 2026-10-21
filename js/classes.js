/* 班级与选课（教师 / 管理员）：把学生加进某门课的教学班。
 *
 * 这一页只管"名单与归属"，不决定学生能看到哪些课——可见范围仍由课程发布状态
 * 与可见范围口径决定（见 docs/审核分层与可见范围.md）。以后要按选课收紧，
 * 改 catalog 的过滤条件即可，名单数据已经就位。
 *
 * 为什么不是"管理员点一个班、手工输入学号"：一学期几十上百人，靠手打学号必然
 * 出错。这里从账号库里搜人、勾选、批量加入；加错可以单人移出，且每一步都留审计。
 */
(function () {
  const A = App, e = A.escape;
  const TERM_RE = /^\d{4}-\d{4}-[12]$/;

  A.state.classes = A.state.classes || {
    status: 'idle', error: '',
    courseId: '',
    items: [], total: 0, seats: 0,
    openClassId: '',
    roster: { status: 'idle', error: '', items: [] },
    form: { courseId: '', name: '', term: '', note: '' },
    picker: { open: false, query: '', status: 'idle', error: '', items: [], selected: [] },
    saving: false
  };
  const S = () => A.state.classes;

  function defaultTerm() {
    const now = new Date();
    const year = now.getFullYear();
    // 9 月到次年 1 月是第一学期，2 月到 8 月是第二学期
    const month = now.getMonth() + 1;
    return month >= 9 ? `${year}-${year + 1}-1`
      : month === 1 ? `${year - 1}-${year}-1`
        : `${year - 1}-${year}-2`;
  }

  function courseOptions(selected) {
    // 已发布的课 + 建设中（未发布）的课都要能建班：老师常常先建班再发布。
    const courses = [...A.courses, ...(A.draftCourses || [])];
    const seen = new Set();
    return courses.filter(course => {
      if (seen.has(course.id)) return false;
      seen.add(course.id);
      return true;
    }).map(course =>
      `<option value="${e(course.id)}" ${course.id === selected ? 'selected' : ''}>${e(course.title)}</option>`
    ).join('');
  }

  async function load() {
    S().status = 'loading';
    A.render();
    try {
      const data = await A.api.classes({ course_id: S().courseId });
      S().items = data.items;
      S().total = data.total;
      S().seats = data.students;
      S().status = 'ready';
      S().error = '';
    } catch (error) {
      S().status = 'error';
      S().error = error.message;
    }
    A.render();
  }

  async function loadRoster() {
    const classId = S().openClassId;
    if (!classId) return;
    S().roster.status = 'loading';
    A.render();
    try {
      const data = await A.api.classStudents(classId);
      if (S().openClassId !== classId) return;   // 期间切了班级，别把旧名单画上去
      S().roster.items = data.items;
      S().roster.status = 'ready';
      S().roster.error = '';
    } catch (error) {
      S().roster.status = 'error';
      S().roster.error = error.message;
    }
    A.render();
  }

  async function searchStudents() {
    S().picker.status = 'loading';
    A.render();
    try {
      const data = await A.api.students({ query: S().picker.query });
      S().picker.items = data.items;
      S().picker.status = 'ready';
      S().picker.error = '';
    } catch (error) {
      S().picker.status = 'error';
      S().picker.error = error.message;
    }
    A.render();
  }

  A.after.classes = function () {
    if (S().status === 'idle') load();
    if (!S().form.term) S().form.term = defaultTerm();
  };

  // ---------------- 视图 ----------------

  function classRow(item) {
    const open = S().openClassId === item.id;
    return `<tr class="${open ? 'cls-open' : ''}">
      <td><b>${e(item.name)}</b><small>${e(item.course_title)}</small></td>
      <td>${e(item.term)}</td>
      <td>${item.teacher_name ? e(item.teacher_name) : '<span class="muted">未指定</span>'}</td>
      <td>${item.student_count} 人</td>
      <td class="cls-actions">
        <button class="btn small" data-action="classes-open" data-id="${e(item.id)}">${open ? '收起名单' : '查看名单'}</button>
        <button class="btn small text-btn" data-action="classes-delete" data-id="${e(item.id)}"
                data-name="${e(item.name)}" data-count="${item.student_count}">删除班级</button>
      </td>
    </tr>`;
  }

  function rosterRow(student) {
    return `<tr>
      <td><b>${e(student.username)}</b></td>
      <td>${e(student.display_name)}${student.disabled ? ' <span class="muted small">（已停用）</span>' : ''}</td>
      <td class="muted small">${e(String(student.enrolled_at).slice(0, 10))}</td>
      <td class="muted small">${e(student.added_by || '')}</td>
      <td class="cls-actions">
        <button class="btn small text-btn" data-action="classes-remove"
                data-student="${e(student.student_id)}" data-name="${e(student.username)}">移出</button>
      </td>
    </tr>`;
  }

  function pickerRow(student) {
    const chosen = S().picker.selected.includes(student.id);
    return `<label class="cls-pick-row">
      <input type="checkbox" data-input="classes-pick" value="${e(student.id)}" ${chosen ? 'checked' : ''}>
      <span><b>${e(student.username)}</b> ${e(student.display_name)}
        ${student.disabled ? '<em class="muted small">已停用</em>' : ''}</span>
    </label>`;
  }

  function pickerBlock(currentClass) {
    const picker = S().picker;
    if (!picker.open) {
      return `<button class="btn primary" data-action="classes-picker-open">添加学生</button>`;
    }
    const inClass = new Set(S().roster.items.map(student => student.student_id));
    const candidates = picker.items.filter(student => !inClass.has(student.id));
    const list = candidates.length
      ? candidates.map(pickerRow).join('')
      : `<p class="muted">${picker.status === 'loading' ? '正在搜索…'
        : picker.query ? '没有匹配的学生，换个学号或姓名试试。' : '输入学号或姓名搜索学生。'}</p>`;
    return `<div class="cls-picker">
      <div class="cls-picker-head">
        <input data-input="classes-picker-query" value="${e(picker.query)}"
               placeholder="搜索学号或姓名（留空列出全部）" aria-label="搜索学生">
        <button class="btn small" data-action="classes-picker-search">搜索</button>
        <button class="btn small primary" data-action="classes-add-selected"
                ${S().picker.selected.length ? '' : 'disabled'}>加入所选（${S().picker.selected.length}）</button>
        <button class="btn small text-btn" data-action="classes-picker-close">取消</button>
      </div>
      <div class="cls-pick-list">${list}</div>
      ${picker.error ? `<p class="cls-error">${e(picker.error)}</p>` : ''}
      <p class="muted small">加入「${e(currentClass.name)}」后，学生立刻出现在名单里；学习记录不会因此改变。</p>
    </div>`;
  }

  function rosterPanel() {
    const currentClass = S().items.find(item => item.id === S().openClassId);
    if (!currentClass) return '';
    const roster = S().roster;
    const rows = roster.items.map(rosterRow).join('');
    return `<div class="panel">
      <div class="cls-roster-head">
        <h2>${e(currentClass.name)} · 名单</h2>
        <span class="muted small">${e(currentClass.course_title)} · ${e(currentClass.term)} · ${roster.items.length} 人</span>
      </div>
      ${roster.error ? `<p class="cls-error">${e(roster.error)}</p>` : ''}
      <table class="ax-table">
        <thead><tr><th>学号</th><th>姓名</th><th>加入时间</th><th>由谁加入</th><th></th></tr></thead>
        <tbody>${rows || `<tr><td colspan="5" class="muted">${roster.status === 'loading' ? '正在读名单…' : '这个班还没有学生。'}</td></tr>`}</tbody>
      </table>
      ${pickerBlock(currentClass)}
    </div>`;
  }

  function formPanel() {
    const form = S().form;
    const courseId = form.courseId || A.course?.id || (A.courses[0] || {}).id || '';
    const invalid = form.term && !TERM_RE.test(form.term.trim());
    return `<div class="panel">
      <h2>新建班级</h2>
      <p class="muted">一个班对应「一门课 + 一个学期」。同一门课同一学期不允许重名；任课教师默认是你自己。</p>
      <div class="cls-form">
        <label class="ax-inline">课程<select data-input="classes-form-course">${courseOptions(courseId)}</select></label>
        <label class="ax-inline">班级名称<input data-input="classes-form-name" value="${e(form.name)}" placeholder="如 数学分析II-01班"></label>
        <label class="ax-inline">学期<input data-input="classes-form-term" value="${e(form.term || defaultTerm())}" placeholder="2026-2027-1"></label>
        <button class="btn primary" data-action="classes-create" ${S().saving ? 'disabled' : ''}>建班</button>
      </div>
      ${invalid ? '<p class="cls-error">学期格式应为 2026-2027-1（学年-学年-学期）</p>' : ''}
    </div>`;
  }

  A.views.classes = function () {
    if (S().status === 'error') {
      return A.sectionTitle('TEACHING', '班级与选课', '把学生加进某门课的教学班。')
        + `<div class="panel"><h2>读不到班级列表</h2><p class="muted">${e(S().error)}</p>
           ${A.button('重试', 'classes-refresh', 'refresh')}</div>`;
    }
    const rows = S().items.map(classRow).join('');
    return A.sectionTitle(
      'TEACHING',
      '班级与选课',
      '学生由管理员发账号，教师把学生加进课程班级；学生自己没有建班或加人的权限。',
      `<div class="button-row">${A.button('刷新', 'classes-refresh', 'refresh')}</div>`
    ) + `<div class="panel">
      <div class="ax-stats">
        <span class="ax-stat"><b>${S().total}</b>个班级</span>
        <span class="ax-stat"><b>${S().seats}</b>人次选课</span>
      </div>
      <div class="ax-toolbar">
        <label class="ax-inline">课程
          <select data-input="classes-filter-course">
            <option value="">全部课程</option>
            ${courseOptions(S().courseId)}
          </select>
        </label>
        <span class="muted small">先建班，再从班级里加学生。</span>
      </div>
      <table class="ax-table">
        <thead><tr><th>班级</th><th>学期</th><th>任课教师</th><th>人数</th><th></th></tr></thead>
        <tbody>${rows || `<tr><td colspan="5" class="muted">${S().status === 'loading' ? '正在读取…' : '还没有班级。先在下面建一个。'}</td></tr>`}</tbody>
      </table>
    </div>
    ${rosterPanel()}
    ${formPanel()}`;
  };

  // ---------------- 交互 ----------------

  A.inputs['classes-filter-course'] = input => { S().courseId = input.value; load(); };
  A.inputs['classes-form-course'] = input => { S().form.courseId = input.value; };
  A.inputs['classes-form-name'] = input => { S().form.name = input.value; };
  A.inputs['classes-form-term'] = input => { S().form.term = input.value; };
  A.inputs['classes-picker-query'] = input => { S().picker.query = input.value; };
  A.inputs['classes-pick'] = input => {
    const id = input.value;
    const selected = new Set(S().picker.selected);
    if (input.checked) selected.add(id); else selected.delete(id);
    S().picker.selected = [...selected];
    A.render();
  };

  A.actions['classes-refresh'] = () => load();

  A.actions['classes-create'] = async function (button) {
    const form = S().form;
    const courseId = form.courseId || A.course?.id || (A.courses[0] || {}).id || '';
    const name = form.name.trim();
    const term = (form.term || defaultTerm()).trim();
    if (!courseId) { A.toast('先选一门课'); return; }
    if (!name) { A.toast('班级名称要填'); return; }
    if (!TERM_RE.test(term)) { A.toast('学期格式应为 2026-2027-1'); return; }
    button.disabled = true;
    S().saving = true;
    try {
      const created = await A.api.createClass({ course_id: courseId, name, term });
      A.toast(`已建班：${created.course_title} · ${created.name}`);
      S().form = { courseId, name: '', term, note: '' };
      S().saving = false;
      S().openClassId = created.id;
      S().roster = { status: 'idle', error: '', items: [] };
      await load();
      await searchStudents();
    } catch (error) {
      S().saving = false;
      button.disabled = false;
      A.toast(error.message);
    }
  };

  A.actions['classes-open'] = async function (button) {
    const id = button.dataset.id;
    if (S().openClassId === id) {
      S().openClassId = '';
      S().picker = { open: false, query: '', status: 'idle', error: '', items: [], selected: [] };
      A.render();
      return;
    }
    S().openClassId = id;
    S().roster = { status: 'idle', error: '', items: [] };
    S().picker = { open: false, query: '', status: 'idle', error: '', items: [], selected: [] };
    await loadRoster();
  };

  A.actions['classes-delete'] = async function (button) {
    const name = button.dataset.name;
    const count = Number(button.dataset.count || 0);
    const message = count
      ? `删除班级「${name}」会同时移除 ${count} 名学生的选课记录（学习记录不受影响）。继续？`
      : `删除班级「${name}」？`;
    if (!window.confirm(message)) return;
    try {
      await A.api.deleteClass(button.dataset.id);
      A.toast('班级已删除');
      if (S().openClassId === button.dataset.id) S().openClassId = '';
      await load();
    } catch (error) {
      A.toast(error.message);
    }
  };

  A.actions['classes-remove'] = async function (button) {
    if (!S().openClassId) return;
    if (!window.confirm(`把 ${button.dataset.name} 从名单里移出？`)) return;
    try {
      await A.api.removeStudent(S().openClassId, button.dataset.student);
      A.toast('已移出名单');
      await loadRoster();
      await load();
    } catch (error) {
      A.toast(error.message);
    }
  };

  A.actions['classes-picker-open'] = async function () {
    S().picker = { open: true, query: '', status: 'idle', error: '', items: [], selected: [] };
    await searchStudents();
  };

  A.actions['classes-picker-close'] = () => {
    S().picker = { open: false, query: '', status: 'idle', error: '', items: [], selected: [] };
    A.render();
  };

  A.actions['classes-picker-search'] = () => searchStudents();

  A.actions['classes-add-selected'] = async function (button) {
    const selected = S().picker.selected;
    if (!selected.length || !S().openClassId) return;
    button.disabled = true;
    try {
      const result = await A.api.addStudents(S().openClassId, selected);
      A.toast(result.added
        ? `已加入 ${result.added} 人${result.skipped ? `，${result.skipped} 人本来就在班上` : ''}`
        : '这些学生本来就在班上');
      S().picker.selected = [];
      await loadRoster();
      await load();
    } catch (error) {
      button.disabled = false;
      A.toast(error.message);
    }
  };
})();
