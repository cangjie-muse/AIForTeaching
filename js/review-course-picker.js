/* 审核页的课程选择器：下拉 + 输入筛选。
 *
 * 二十门课的时候，在下拉框里滚着找一门课就已经很难受了；课程中心的目标是
 * 三十几门。所以这里多给一个输入框：打字缩小选项，再在下拉里点——
 * 不改成自绘菜单，是因为原生 select 的键盘、触屏、读屏行为都是免费的。
 */
(function () {
  const A = App, e = A.escape;
  const S = () => (A.state.review = A.state.review || { filters: { course_id: '' } });

  function label(course) {
    return `${course.title}（${Number(course.exercise_count) || 0} 题）`;
  }

  function norm(value) {
    return String(value || '').trim().toLowerCase();
  }

  /* 选项只留匹配的；当前选中的那门永远留着——否则筛选一半，选中项消失，
     人会以为"课没了"，其实是自己打字打出来的。 */
  A.courseOptionsHtml = function (courses, query, keepId) {
    const word = norm(query);
    const shown = (courses || []).filter(course =>
      course.id === keepId || !word || norm(course.title).includes(word));
    return shown.map(course => `<option value="${e(course.id)}"${course.id === keepId ? ' selected' : ''}>`
      + `${e(label(course))}</option>`).join('');
  };

  A.reviewCoursePicker = function () {
    const state = S();
    const courses = state.courses || [];
    if (!courses.length) return '';
    const current = state.filters.course_id;
    // 刚导入的课可能还没进缓存：补一项，否则下拉框根本选不到它
    if (current && !courses.some(course => course.id === current)) {
      courses.unshift({ id: current, title: `${current}（刚导入）`, exercise_count: 0 });
    }
    const query = state.courseQuery || '';
    const total = courses.length;
    return `<div class="rv-course-filter">
      <label class="rv-select">
        <span>课程</span>
        <select data-input="review-course">
          <option value="">全部课程（${total}）</option>
          ${A.courseOptionsHtml(courses, query, current)}
        </select>
      </label>
      <label class="rv-course-search">
        <span>筛选</span>
        <input data-input="review-course-query" value="${e(query)}" placeholder="输入课程名…" autocomplete="off">
      </label>
    </div>`;
  };

  /* 只重建 select 的选项，不动整页：整页重渲染会把输入框的焦点弄丢，
     打一个字就断一次，等于没这个功能。 */
  A.inputs['review-course-query'] = function (input) {
    const state = S();
    state.courseQuery = input.value;
    const select = document.querySelector('[data-input="review-course"]');
    if (!select) return;
    const keep = select.value;
    select.innerHTML = `<option value="">全部课程（${(state.courses || []).length}）</option>`
      + A.courseOptionsHtml(state.courses || [], input.value, keep);
    select.value = keep;
  };
})();
