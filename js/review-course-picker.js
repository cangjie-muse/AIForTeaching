/* 审核页的课程选择器：一个框，既能点也能打字。
 *
 * 二十门课时，在下拉框里滚着找已经很难受；分成「下拉选课 + 旁边再放个筛选框」
 * 更别扭——两个控件管一件事，人得先想"该用哪个"。所以合成一个组合框：
 * 点一下展开全部，打字只留匹配的，点一项即选定。
 *
 * 没有用原生 select 是因为 select 不能输入。这里自己接键盘：
 * 方向键从输入框直接落到选项上，回车选第一项，Esc 收起。
 */
(function () {
  const A = App, e = A.escape;
  const S = () => (A.state.review = A.state.review || {
    filters: { course_id: '', category: '', priority: '' }
  });
  const INPUT = '[data-input="review-course-query"]';

  function norm(value) {
    return String(value || '').trim().toLowerCase();
  }

  function courses() {
    return S().courses || [];
  }

  function picked() {
    const id = S().filters.course_id;
    return id ? courses().find(course => course.id === id) : null;
  }

  function emptyLabel() {
    return S().courseEmptyLabel || `全部课程（${courses().length}）`;
  }

  /* 选项只留匹配的；当前选中的那门永远留着——筛到一半选中项消失，
     人会以为"课没了"，其实是自己打字打出来的。 */
  A.courseMenuHtml = function (list, query, keepId, empty) {
    const word = norm(query);
    const rows = (list || []).filter(course =>
      course.id === keepId || !word || norm(course.title).includes(word));
    const head = `<button type="button" role="option" class="rv-combo-item ${keepId ? '' : 'active'}"
      data-action="review-course-pick" data-id="">${e(empty || '全部课程')}</button>`;
    return head + rows.map(course => `<button type="button" role="option"
      class="rv-combo-item ${course.id === keepId ? 'active' : ''}"
      data-action="review-course-pick" data-id="${e(course.id)}"
      >${e(course.title)}（${Number(course.exercise_count) || 0} 题）</button>`).join('');
  };

  A.reviewCoursePicker = function (options = {}) {
    const state = S();
    const list = courses();
    if (!list.length) return '';
    state.courseEmptyLabel = options.emptyLabel || `全部课程（${list.length}）`;
    const current = state.filters.course_id;
    // 刚导入的课可能还没进缓存：补一项，否则这个框里根本选不到它
    if (current && !list.some(course => course.id === current)) {
      list.unshift({ id: current, title: `${current}（刚导入）`, exercise_count: 0 });
    }
    const pickedCourse = list.find(course => course.id === current);
    const open = Boolean(state.courseOpen);
    const value = open ? (state.courseQuery || '') : (pickedCourse ? pickedCourse.title : '');
    return `<div class="rv-course-filter">
      <span class="rv-combo-label">课程</span>
      <div class="rv-combo">
        <input class="rv-combo-input" data-input="review-course-query" value="${e(value)}"
          placeholder="${e(open ? '输入课程名…' : state.courseEmptyLabel)}" autocomplete="off"
          role="combobox" aria-expanded="${open}" aria-label="选择课程，可直接输入课程名筛选">
        <button type="button" class="rv-combo-caret" data-action="review-course-open"
          aria-label="展开或收起课程列表">${A.icon('arrow')}</button>
        <div class="rv-combo-menu"${open ? '' : ' hidden'}>
          ${A.courseMenuHtml(list, state.courseQuery || '', current, state.courseEmptyLabel)}
        </div>
      </div>
    </div>`;
  };

  function menuEl() {
    return document.querySelector('.rv-combo-menu');
  }

  function paint(query) {
    const menu = menuEl();
    if (!menu) return;
    menu.innerHTML = A.courseMenuHtml(courses(), query, S().filters.course_id, emptyLabel());
    menu.hidden = false;
  }

  // 收起时把框还原成"你选的是哪门课"，不然打字残留在里面，看着像没选
  function restore() {
    const state = S();
    const menu = menuEl();
    if (menu) menu.hidden = true;
    const input = document.querySelector(INPUT);
    if (input) {
      input.value = picked() ? picked().title : '';
      input.placeholder = emptyLabel();
      input.setAttribute('aria-expanded', 'false');
    }
    state.courseOpen = false;
    state.courseQuery = '';
  }

  A.inputs['review-course-query'] = function (input) {
    const state = S();
    state.courseQuery = input.value;
    state.courseOpen = true;
    input.setAttribute('aria-expanded', 'true');
    paint(input.value);
  };

  A.actions['review-course-open'] = function () {
    const state = S();
    if (state.courseOpen) {
      restore();
      return;
    }
    state.courseOpen = true;
    state.courseQuery = '';
    A.render();
  };

  A.actions['review-course-pick'] = function (button) {
    const state = S();
    state.filters.course_id = button.dataset.id || '';
    state.courseQuery = '';
    state.courseOpen = false;
    state.graph = null;
    state.editingNode = '';
    if (state.filters.course_id) A.loadPublish(state.filters.course_id).then(() => A.render());
    if (A.actions['review-refresh']) A.actions['review-refresh']();
    else A.render();
  };

  if (typeof document.addEventListener !== 'function') return;

  // 点进框里就展开：不给"先点箭头"这一步
  document.addEventListener('focusin', event => {
    const input = event.target.closest && event.target.closest(INPUT);
    if (!input) return;
    const state = S();
    if (state.courseOpen) return;
    state.courseOpen = true;
    state.courseQuery = '';
    input.value = '';
    input.setAttribute('aria-expanded', 'true');
    paint('');
  });

  document.addEventListener('click', event => {
    const state = A.state.review;
    if (!state || !state.courseOpen) return;
    if (event.target.closest && event.target.closest('.rv-combo')) return;
    restore();
  });

  // 输入框里按方向键没法选，回车按"第一项"算，Esc 收起
  document.addEventListener('keydown', event => {
    const state = A.state.review;
    if (!state || !state.courseOpen) return;
    if (event.key === 'Escape') {
      restore();
      return;
    }
    if (event.key !== 'Enter') return;
    const input = document.activeElement;
    if (!input || !input.matches || !input.matches(INPUT)) return;
    const first = menuEl() && menuEl().querySelector('.rv-combo-item');
    if (!first) return;
    event.preventDefault();
    A.actions['review-course-pick'](first);
  });
})();
