/* 作业批改页：学生拍照上传 → 后台转写与判分 → 教师复核。
 *
 * 这一页要同时说清三件事，少一件学生就会误解结果：
 *   1. 分数是"AI 初评"，教师确认之前不算成绩；
 *   2. 识别错了可以重传——所以每题都显示识别出的题干，而不是只给一个分；
 *   3. 批改要几十秒，不是点完就出结果，所以有进度文案和轮询。
 */
(function () {
  const A = App, e = A.escape;

  const STATUS = {
    submitted: '待批改', grading: '批改中', 'ai-graded': 'AI 初评',
    'teacher-confirmed': '教师已确认', returned: '已退回', failed: '批改失败'
  };
  const VERDICT = {
    correct: { label: '正确', tone: 'ok' },
    partial: { label: '部分正确', tone: 'warn' },
    wrong: { label: '错误', tone: 'bad' },
    unjudged: { label: '无法判定', tone: 'muted' }
  };

  function S() {
    return A.state.homework = A.state.homework || {
      status: 'idle', error: '', items: [], isTeacher: false, detail: null,
      progress: '', courseId: '', form: { courseId: '', title: '', files: [] }
    };
  }
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const isTeacher = () => !!(A.auth && A.auth.is('teacher', 'admin'));

  async function load() {
    const state = S();
    state.status = 'loading';
    try {
      const data = await A.api.homeworkList(state.courseId ? { course_id: state.courseId } : {});
      state.items = data.items || [];
      state.isTeacher = !!data.is_teacher;
      state.status = 'ready';
      state.error = '';
    } catch (error) {
      state.status = 'error';
      state.error = error.message;
    }
    A.render();
  }
  A.loadHomework = load;

  async function openDetail(id) {
    const state = S();
    state.detail = { status: 'loading', id };
    A.render();
    try {
      state.detail = await A.api.homeworkGet(id);
    } catch (error) {
      state.detail = { status: 'error', id, error: error.message };
    }
    A.render();
    // 还在批改：继续轮询，别让人盯着一个不动的页面猜。
    const status = state.detail && state.detail.submission && state.detail.submission.status;
    if (status === 'submitted' || status === 'grading') watch(id);
  }
  A.openHomework = openDetail;

  async function watch(id) {
    for (let attempt = 0; attempt < 60; attempt += 1) {
      await sleep(3000);
      if (!S().detail || S().detail.id !== id) return;   // 用户已经离开这一页
      try {
        const detail = await A.api.homeworkGet(id);
        S().detail = detail;
        if (detail.submission.status !== 'submitted' && detail.submission.status !== 'grading') {
          await load();
          return;
        }
      } catch (error) {
        return;
      }
      A.render();
    }
  }

  function badge(status) {
    const tone = status === 'failed' ? 'bad'
      : status === 'ai-graded' ? 'warn'
        : status === 'teacher-confirmed' ? 'ok' : 'muted';
    return A.badge(STATUS[status] || status, tone);
  }

  function uploadCard(state) {
    const courses = A.allCourses ? A.allCourses() : [];
    const options = courses.map(course => `<option value="${e(course.id)}"
      ${state.form.courseId === course.id ? 'selected' : ''}>${e(course.title)}</option>`).join('');
    const count = state.form.files.length;
    return `<section class="hw-card hw-upload">
      <h3>上传作业照片</h3>
      <p class="hw-hint">一页一张，最多 20 页；请拍正、拍清。识别出的是卷面上的题与你的作答，不会替你改答案。</p>
      <div class="hw-form">
        <label>课程<select data-change="homework-course">
          <option value="">选择课程…</option>${options}
        </select></label>
        <label>标题（可空）<input data-input="homework-title" value="${e(state.form.title)}"
          maxlength="40" placeholder="例如：第三章习题"></label>
        <label class="hw-file">选择照片
          <input type="file" accept="image/jpeg,image/png,image/webp" multiple data-change="homework-files">
        </label>
      </div>
      <div class="hw-actions">
        <span class="hw-picked">${count ? `已选 ${count} 页` : '还没选择照片'}</span>
        <button class="btn primary" data-action="homework-submit"
          ${state.progress ? 'disabled' : ''}>${state.progress || '提交批改'}</button>
      </div>
    </section>`;
  }

  function listCard(state) {
    if (state.status === 'loading') return '<p class="muted">正在读取作业…</p>';
    if (state.status === 'error') {
      return A.empty('info', '读不到作业列表', state.error || '后端没有响应。');
    }
    if (!state.items.length) {
      return A.empty('pen', '还没有提交过作业', '上传一张作业照片，AI 会先转写卷面，再逐题对照题库参考答案给初评。');
    }
    return `<div class="hw-list">${state.items.map(row => `
      <button class="hw-row" data-action="homework-open" data-id="${e(row.id)}">
        <span class="hw-row-main">
          <b>${e(row.title || '未命名作业')}</b>
          <small>${row.page_count} 页 · ${String(row.submitted_at || '').slice(0, 16).replace('T', ' ')}${
            state.isTeacher ? ` · ${e(row.student_id)}` : ''}</small>
        </span>
        <span class="hw-row-side">${badge(row.status)}${
          row.status === 'failed' && row.error ? `<small class="hw-error">${e(row.error.slice(0, 60))}</small>` : ''}</span>
      </button>`).join('')}</div>`;
  }

  function scoreLine(score) {
    if (!score) return '';
    const auto = score.auto === null ? '—' : score.auto;
    const teacher = score.teacher === null ? '—' : score.teacher;
    return `<div class="hw-scores">
      <div><span>AI 初评</span><b>${auto}</b><small>满分 100</small></div>
      <div class="${score.confirmed ? 'confirmed' : ''}"><span>教师确认分</span><b>${teacher}</b>
        <small>${score.confirmed ? '已确认，可作为成绩' : '尚未确认，仅供自测'}</small></div>
      ${score.needs_review ? `<div class="hw-flag"><span>需人工复核</span><b>${score.needs_review}</b>
        <small>模型没有把握的题</small></div>` : ''}
    </div>`;
  }

  function itemCard(item, state) {
    const verdict = VERDICT[item.verdict] || { label: item.verdict || '未判', tone: 'muted' };
    const low = item.confidence !== null && item.confidence !== undefined && item.confidence < 0.6;
    return `<article class="hw-item">
      <header>
        <span class="hw-no">第 ${e(item.question_no || item.ord)} 题</span>
        ${A.badge(verdict.label, verdict.tone)}
        <span class="hw-item-score">${item.score === null ? '未给分' : `${item.score} / ${item.full_score}`}</span>
        ${item.teacher_score !== null ? `<span class="hw-teacher">教师改判 ${item.teacher_score}</span>` : ''}
        ${low ? '<span class="hw-low">识别存疑</span>' : ''}
      </header>
      <div class="hw-q">
        <div><span class="hw-label">识别出的题目</span><p>${e(item.question || '（未识别到题干）')}</p></div>
        <div><span class="hw-label">你的作答</span><p>${e(item.student_answer || '（空白）')}</p></div>
      </div>
      ${item.comment ? `<p class="hw-comment">${e(item.comment)}</p>` : ''}
      ${item.teacher_comment ? `<p class="hw-comment teacher">教师评语：${e(item.teacher_comment)}</p>` : ''}
      <div class="hw-item-actions">
        ${item.exercise_id ? `<span class="hw-bank">已对上题库题 ${e(item.exercise_id)}</span>`
          : '<span class="hw-bank muted">题库里没有对应题，按模型自己的推导判断</span>'}
        ${state.isTeacher ? `<button class="btn small" data-action="homework-review"
          data-id="${e(state.detail.submission.id)}" data-ord="${item.ord}"
          data-score="${item.teacher_score !== null ? item.teacher_score : (item.score !== null ? item.score : '')}"
          data-comment="${e(item.teacher_comment || '')}">改判 / 评语</button>` : ''}
      </div>
    </article>`;
  }

  function detailView(state) {
    const detail = state.detail;
    if (!detail) return '';
    if (detail.status === 'loading') return '<p class="muted">正在打开…</p>';
    if (detail.status === 'error') return A.empty('info', '打不开这次作业', detail.error || '');
    const sub = detail.submission;
    const grading = sub.status === 'submitted' || sub.status === 'grading';
    return `<section class="hw-detail">
      <div class="hw-detail-head">
        <button class="btn small" data-action="homework-back">${A.icon('arrow')} 返回列表</button>
        <h3>${e(sub.title || '作业')}</h3>
        ${badge(sub.status)}
      </div>
      ${grading ? `<p class="hw-grading">${A.icon('activity')}正在批改：转写卷面 → 对照题库参考答案 → 逐题给初评。
        一页大约十几秒，可以先去做别的，这一页会自动刷新。</p>` : ''}
      ${sub.error ? `<p class="hw-error">批改失败：${e(sub.error)}</p>` : ''}
      ${scoreLine(detail.score)}
      ${state.isTeacher && sub.status === 'ai-graded' ? `<div class="hw-actions">
        <span class="hw-hint">确认后这些 AI 分才会变成正式成绩；没把握的题建议先改判。</span>
        <button class="btn primary" data-action="homework-confirm" data-id="${e(sub.id)}">全部确认</button>
      </div>` : ''}
      <div class="hw-pages">${(detail.pages || []).map(page => `<button class="hw-page"
        data-action="homework-page" data-id="${e(sub.id)}" data-page="${page.page_no}">
        ${A.icon('layers')}第 ${page.page_no} 页</button>`).join('')}</div>
      ${(detail.items || []).map(item => itemCard(item, state)).join('')
        || '<p class="muted">这次作业还没有识别结果。</p>'}
    </section>`;
  }

  A.views.homework = function () {
    const state = S();
    return `${A.sectionTitle('HOMEWORK · AI 初评', '作业批改',
      '拍照上传，AI 先把卷面转写成文字，再逐题对照题库里的参考答案给初评；教师确认后才计入成绩。')}
      ${state.detail ? detailView(state) : `${uploadCard(state)}${listCard(state)}`}`;
  };

  A.after.homework = function () {
    const state = S();
    if (state.status === 'idle') load();
  };

  // ------------------------------------------------------------------ 交互
  A.actions['homework-course'] = function (select) {
    S().form.courseId = select.value;
    A.render();
  };
  A.actions['homework-files'] = function (input) {
    S().form.files = Array.from(input.files || []);
    A.render();
  };
  A.inputs['homework-title'] = function (input) {
    S().form.title = input.value;
  };

  A.actions['homework-submit'] = async function (button) {
    const state = S();
    const courseId = state.form.courseId;
    const files = state.form.files || [];
    if (!courseId) { A.toast('先选一门课程：批改要按这门课的题库对答案'); return; }
    if (!files.length) { A.toast('先选择作业照片'); return; }

    button.disabled = true;
    try {
      state.progress = '正在创建提交…';
      A.render();
      const created = await A.api.homeworkCreate(courseId, state.form.title || '');
      for (let index = 0; index < files.length; index += 1) {
        state.progress = `正在上传第 ${index + 1}/${files.length} 页…`;
        A.render();
        await A.api.uploadHomeworkPage(created.id, index + 1, files[index]);
      }
      state.progress = '已排队，等待批改…';
      A.render();
      await A.api.homeworkGrade(created.id);
      state.form = { courseId, title: '', files: [] };
      state.progress = '';
      await load();
      await openDetail(created.id);
    } catch (error) {
      state.progress = '';
      button.disabled = false;
      A.render();
      A.toast(`没提交成功：${error.message}`);
    }
  };

  A.actions['homework-open'] = function (button) {
    openDetail(button.dataset.id);
  };

  A.actions['homework-back'] = function () {
    S().detail = null;
    A.render();
    load();
  };

  A.actions['homework-page'] = async function (button) {
    // 图片接口要带会话 cookie，不能直接 <img src>：先取 blob 再开新窗口显示。
    try {
      // 图片也要带会话 cookie，所以不能直接 <img src>：取 blob 再开新窗口。
      const blob = await A.api.homeworkPageBlob(button.dataset.id, button.dataset.page);
      window.open(URL.createObjectURL(blob), '_blank');
    } catch (error) {
      A.toast(`打不开这一页：${error.message}`);
    }
  };

  A.actions['homework-review'] = function (button) {
    const { id, ord } = button.dataset;
    A.state.homeworkReview = {
      id, ord,
      score: button.dataset.score || '',
      comment: button.dataset.comment || ''
    };
    A.openDialog(`改判第 ${ord} 题`, `<div class="field">
      <label for="hw-score">教师给分（满分 10，留空表示不打分）</label>
      <input id="hw-score" data-input="hw-review-score" value="${e(button.dataset.score || '')}" inputmode="decimal">
      <label for="hw-comment">评语（学生会看到）</label>
      <input id="hw-comment" data-input="hw-review-comment" value="${e(button.dataset.comment || '')}" maxlength="200">
      </div><div class="button-row">
      <button class="btn primary" data-action="homework-review-save">保存改判</button></div>`);
  };
  A.inputs['hw-review-score'] = input => { A.state.homeworkReview.score = input.value; };
  A.inputs['hw-review-comment'] = input => { A.state.homeworkReview.comment = input.value; };

  A.actions['homework-review-save'] = async function (button) {
    const review = A.state.homeworkReview || {};
    const raw = String(review.score || '').trim();
    const score = raw === '' ? null : Number(raw);
    if (score !== null && (Number.isNaN(score) || score < 0 || score > 10)) {
      A.toast('分数要在 0 到 10 之间'); return;
    }
    button.disabled = true;
    try {
      await A.api.homeworkReview(review.id, Number(review.ord), {
        teacher_score: score,
        teacher_comment: review.comment || '',
        verdict: score === null ? undefined : (score >= 10 ? 'correct' : (score > 0 ? 'partial' : 'wrong'))
      });
      const dialog = document.getElementById('dialog');
      if (dialog && dialog.open) dialog.close();
      A.toast('已保存改判');
      await openDetail(review.id);
    } catch (error) {
      button.disabled = false;
      A.toast(`没保存成功：${error.message}`);
    }
  };

  A.actions['homework-confirm'] = async function (button) {
    const id = button.dataset.id;
    if (!window.confirm('确认这些 AI 初评分？确认后学生看到的就是最终成绩。')) return;
    button.disabled = true;
    try {
      await A.api.homeworkConfirm(id);
      A.toast('已确认');
      await openDetail(id);
    } catch (error) {
      button.disabled = false;
      A.toast(`没确认成功：${error.message}`);
    }
  };
})();
