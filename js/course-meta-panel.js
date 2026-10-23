/* 课程名称与学科方向：课程内容页「修改与导出」顶部那一行。
 *
 * 这两项原来都改不了：名字只能靠重新导入（会把整门课的内容与审核状态重写一遍），
 * 归类更是只存在于前端静态表里。挪到这儿，是因为它们属于"这门课是什么"，
 * 和"这门课里有什么"（知识点、习题）分得开，也不该混进导入流程。
 *
 * 保存后要重拉课程目录：方向改了，课程中心的分组就要跟着动。
 */
(function () {
  const A = App, e = A.escape;

  A.state.meta = A.state.meta || { courseId: '', title: null, direction: null, saving: false, notice: '' };
  const S = () => A.state.meta;

  function course(courseId) {
    const lists = [A.state.content && A.state.content.courses];
    return lists.filter(Boolean).flat().find(item => item.id === courseId) || null;
  }

  /* 课程对象上的方向可能来自两个地方：目录合并后的 directionId（分组 id），
     或 /api/v1/courses 直接给的 direction（也是 id）。取到哪个算哪个。 */
  function directionIdOf(found) {
    const list = groups();
    if (found.directionId && list.some(group => group.id === found.directionId)) return found.directionId;
    if (found.direction && list.some(group => group.id === found.direction)) return found.direction;
    if (found.directionId) return found.directionId;
    return '';
  }

  function groups() {
    return (window.CourseCatalog && window.CourseCatalog.groups) || [];
  }

  function options(current) {
    const rows = groups().map(group =>
      `<option value="${e(group.id)}"${group.id === current ? ' selected' : ''}>${e(group.label)}</option>`);
    return `<option value=""${current ? '' : ' selected'}>待归类</option>` + rows.join('');
  }

  /* 面板只在选定了一门课时出现：没有课就没有"这门课叫什么"可言。
     state 里 null = 还没动过这一项（显示库里的值），'' = 明确选了「待归类」。
     两者必须分开，否则点开面板就把方向清空了。 */
  A.courseMetaPanel = function (courseId) {
    const found = course(courseId);
    if (!courseId || !found) return '';
    const state = S();
    if (state.courseId !== courseId) {
      state.courseId = courseId;
      state.title = null;
      state.direction = null;
      state.notice = '';
    }
    const title = state.title === null ? found.title : state.title;
    const direction = state.direction === null ? directionIdOf(found) : state.direction;
    return `<div class="panel">
      <div class="panel-heading"><h2>课程名称与归类</h2>
        ${A.badge(found.direction || '待归类', 'blue')}</div>
      <div class="field">
        <label for="meta-title">课程名称</label>
        <input id="meta-title" data-input="meta-title" value="${e(title)}" maxlength="120"
               placeholder="这门课在课程中心与审核页显示的名字">
      </div>
      <div class="field">
        <label for="meta-direction">学科方向</label>
        <select id="meta-direction" data-input="meta-direction">${options(direction)}</select>
        <small>决定它出现在课程中心的哪一组；「待归类」会让它落在目录末尾。</small>
      </div>
      <div class="button-row">
        <button class="btn primary" data-action="meta-save" ${state.saving ? 'disabled' : ''}>保存名称与归类</button>
      </div>
      ${state.notice ? `<p class="small">${e(state.notice)}</p>` : ''}
    </div>`;
  };

  A.inputs['meta-title'] = input => { S().title = input.value; };
  A.inputs['meta-direction'] = input => { S().direction = input.value; };

  A.actions['meta-save'] = async function () {
    const state = S();
    const courseId = state.courseId || (A.state.content || {}).courseId;
    const found = course(courseId);
    if (!courseId || !found) return;
    const title = String(state.title === null ? found.title : state.title).trim();
    const direction = state.direction === null ? directionIdOf(found) : state.direction;
    state.saving = true;
    A.render();
    try {
      const result = await A.api.updateCourse(courseId, { title, direction });
      state.notice = result.changed && result.changed.length
        ? `已保存：${result.changed.map(name => (name === 'title' ? '课程名称' : '学科方向')).join('、')}。`
        : '没有变化，未写库。';
      state.courseId = '';
      state.title = null;
      state.direction = null;
      // 目录、审核页的课程列表、内容页自己那份列表都可能是旧的
      if (typeof A.loadCatalog === 'function') await A.loadCatalog();
      if (A.state.review) A.state.review.status = 'idle';
      const content = A.state.content;
      if (content) content.courses = await A.api.courseList(true).catch(() => content.courses);
    } catch (error) {
      state.notice = `没保存成功：${error.message}`;
    } finally {
      state.saving = false;
      A.render();
    }
  };
})();
