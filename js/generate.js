'use strict';
/* 教材生成入口：上传 PDF → 排队 → 调模型 → 生成知识点与习题 → 进审核队列。
 *
 * 设计取舍：
 *   1. 上传的是 PDF，但模型读不了 PDF。服务器先抽成带页号的文本，再一章一次调用。
 *      所以页数、页码偏移、起止页必须在这里问清楚——它们决定了"书页 12 页"
 *      到底是 PDF 第几页，猜错了整章溯源都会偏。
 *   2. 生成结果默认 draft、进审核队列，不直接进学生端。页面上写清楚这件事，
 *      免得老师以为点完就上线了。
 *   3. 没有 pypdf 的机器上，生成任务会因为抽不出文字而失败。失败原因原样展示，
 *      并给出装依赖的那条命令——比"生成失败，请重试"有用。
 *
 * 进度靠轮询：一章几十秒到几分钟，不做 WebSocket，省一条长连接。
 */
(function () {
  const A = App, e = A.escape;

  const STATUS_LABELS = {
    queued: ['排队中', ''],
    running: ['生成中', 'amber'],
    succeeded: ['已完成', 'green'],
    failed: ['失败', ''],
    cancelled: ['已取消', '']
  };

  A.state.generate = A.state.generate || {
    courses: [],
    loaded: false,
    upload: null,      // 上传结果（页数、有没有文字层）
    job: null,
    error: '',
    busy: false,
    timer: null
  };

  const S = () => A.state.generate;

  function stopPolling() {
    if (S().timer) { clearInterval(S().timer); S().timer = null; }
  }

  function statusBadge(status) {
    const [label, variant] = STATUS_LABELS[status] || [status, ''];
    return A.badge(label, variant);
  }

  function courseField() {
    const options = S().courses.map(course =>
      `<option value="${e(course.id)}" ${S().courseId === course.id ? 'selected' : ''}>${e(course.title)}（${e(course.id)}）</option>`
    ).join('');
    return `<div class="field">
      <label for="gen-course">目标课程</label>
      <select id="gen-course" data-input="gen-course">${options || '<option value="">（读不到课程列表）</option>'}</select>
      <small>生成的知识点与习题会挂在这门课下。课程不存在时会用新课程 id 建课。</small>
    </div>`;
  }

  function formPanel() {
    const upload = S().upload;
    return `<div class="panel">
      <h2>上传教材</h2>
      <p class="muted">选好课程和页码范围，上传 PDF。PDF 会被抽成带页号的文本，再按章调用模型。</p>
      <div class="button-row" style="gap:16px;flex-wrap:wrap;align-items:flex-start">
        ${courseField()}
        <div class="field">
          <label for="gen-title">教材名称</label>
          <input id="gen-title" value="${e(S().title || '')}" placeholder="数学分析（第三版）上册" data-input="gen-title">
          <small>会写进每道题的出处，尽量写全。</small>
        </div>
        <div class="field">
          <label for="gen-authors">作者 / 版次（可选）</label>
          <input id="gen-authors" value="${e(S().authors || '')}" placeholder="陈纪修、於崇华、金路；第三版" data-input="gen-authors">
        </div>
      </div>
      <div class="button-row" style="gap:16px;flex-wrap:wrap;align-items:flex-start">
        <div class="field">
          <label for="gen-from">起始 PDF 页</label>
          <input id="gen-from" type="number" min="1" value="${e(String(S().pageFrom || 1))}" data-input="gen-from">
        </div>
        <div class="field">
          <label for="gen-to">结束 PDF 页</label>
          <input id="gen-to" type="number" min="1" value="${e(String(S().pageTo || 20))}" data-input="gen-to">
          <small>一次生成一章。整本书分几次跑，失败只需重跑那一章。</small>
        </div>
        <div class="field">
          <label for="gen-offset">页码偏移</label>
          <input id="gen-offset" type="number" value="${e(String(S().pageOffset || 0))}" data-input="gen-offset">
          <small>书页 = PDF 页 − 偏移。前言占了 8 页就填 8。</small>
        </div>
        <div class="field">
          <label for="gen-hint">章节提示（可选）</label>
          <input id="gen-hint" value="${e(S().hint || '')}" placeholder="第一章 实数与函数" data-input="gen-hint">
        </div>
      </div>
      <div class="field">
        <label for="gen-file">教材 PDF</label>
        <input id="gen-file" type="file" accept="application/pdf">
        <small>扫描件需要先 OCR，否则抽不到文字。上传后会显示"有没有文字层"。</small>
      </div>
      <div class="button-row">
        ${A.button('上传并开始生成', 'generate-start', 'upload', '', 'primary')}
        ${upload ? `<span class="muted small">已上传：${upload.page_count ?? '?'} 页，${upload.has_text_layer ? '有文字层' : '无文字层（可能需要 OCR）'}；可用解析引擎：${e((upload.engines_available || []).join('、') || '无')}</span>` : ''}
      </div>
      ${S().error ? `<p class="muted small">${e(S().error)}</p>` : ''}
    </div>`;
  }

  function jobPanel() {
    const job = S().job || {};
    const result = job.result || {};
    const running = job.status === 'queued' || job.status === 'running';
    const warnings = result.warnings || [];
    return `<div class="panel">
      <h2>生成任务 ${statusBadge(job.status)}
        <small class="muted">#${e(String(job.id || '?'))}</small></h2>
      <progress max="100" value="${Number(job.progress || 0)}" style="width:100%"></progress>
      <p class="muted small">进度 ${Number(job.progress || 0)}% · 尝试 ${Number(job.attempts || 0)} 次</p>
      ${job.error ? `<p class="muted small">失败原因：${e(job.error)}</p>
        <p class="muted small">如果提示缺 PDF 解析引擎，在服务器上执行 <code>pip install pypdf</code>，或先在本地跑
        <code>python scripts/extract_pdf.py 教材.pdf</code> 再把文本放进教材记录。</p>` : ''}
      ${job.status === 'succeeded' ? `
        <p>生成结果：<b>${Number(result.nodes || 0)}</b> 个知识点、<b>${Number(result.exercises || 0)}</b> 道题，
        全部进入 <b>draft（待审）</b>，不会直接出现在学生端。</p>
        ${warnings.length ? `<details><summary>${warnings.length} 条提示</summary><ul class="muted small">${warnings.map(w => `<li>${e(w)}</li>`).join('')}</ul></details>` : ''}
        <p class="muted small">资源包：${e(result.pack_dir || '（未记录）')}</p>
        <div class="button-row">
          ${A.link('去审核这些题', 'review', null)}
          ${A.button('再生成一章', 'generate-reset', 'refresh')}
        </div>` : `
        <div class="button-row">
          ${running ? '<span class="muted small">worker 正在处理，页面会自动刷新。</span>' : ''}
          ${A.button('重新提交', 'generate-reset', 'refresh')}
        </div>`}
    </div>`;
  }

  A.views.generate = function () {
    const heading = A.sectionTitle(
      'COURSE BUILDER',
      '教材生成',
      '上传教材 PDF，排队调用模型生成知识点与习题。生成结果一律先入审核队列，审核通过才进学生端。'
    );
    return heading + (S().job ? jobPanel() : formPanel());
  };

  A.after.generate = function () {
    if (S().loaded) return;
    S().loaded = true;
    A.api.courseList().then(courses => {
      S().courses = courses || [];
      if (!S().courseId && S().courses.length) S().courseId = S().courses[0].id;
      A.render();
    }).catch(error => {
      S().error = `读不到课程列表：${error.message}`;
      A.render();
    });
  };

  // ---------------------------------------------------------------------------
  // 输入
  // ---------------------------------------------------------------------------
  const FIELDS = {
    'gen-title': 'title',
    'gen-authors': 'authors',
    'gen-from': 'pageFrom',
    'gen-to': 'pageTo',
    'gen-offset': 'pageOffset',
    'gen-hint': 'hint',
    'gen-course': 'courseId'
  };

  Object.keys(FIELDS).forEach(id => {
    A.inputs[id] = input => {
      const key = FIELDS[id];
      S()[key] = input.type === 'number' ? Number(input.value) : input.value.trim();
    };
  });

  // ---------------------------------------------------------------------------
  // 动作
  // ---------------------------------------------------------------------------
  function pickFile() {
    const input = document.getElementById('gen-file');
    const file = input && input.files && input.files[0];
    if (!file) throw new Error('先选一个 PDF 文件');
    if (!/\.pdf$/i.test(file.name) && file.type !== 'application/pdf') throw new Error('只接受 PDF 文件');
    return file;
  }

  function poll(jobId) {
    stopPolling();
    const tick = async () => {
      if (A.route.page !== 'generate') { stopPolling(); return; }
      try {
        S().job = await A.api.job(jobId);
        A.render();
        if (S().job.status !== 'queued' && S().job.status !== 'running') stopPolling();
      } catch (error) {
        S().error = `读不到任务进度：${error.message}`;
        stopPolling();
        A.render();
      }
    };
    S().timer = setInterval(tick, 2000);
    tick();
  }

  A.actions['generate-start'] = async function (button) {
    if (S().busy) return;
    button.disabled = true;
    S().busy = true;
    S().error = '';
    try {
      const file = pickFile();
      if (!S().courseId) throw new Error('先选目标课程');
      if (!S().title) throw new Error('教材名称要填：它会写进每道题的出处');
      if (!S().pageFrom || !S().pageTo) throw new Error('起止页要填');
      if (S().pageTo < S().pageFrom) throw new Error('结束页不能小于起始页');

      const upload = await A.api.uploadTextbook({
        file,
        courseId: S().courseId,
        title: S().title,
        authors: S().authors || '',
        pageOffset: S().pageOffset || 0
      });
      S().upload = upload;
      if (upload.has_text_layer === false) {
        A.toast('这份 PDF 没有文字层，生成多半会失败——需要先 OCR');
      }

      const started = await A.api.startGeneration(upload.textbook_id, {
        page_from: S().pageFrom,
        page_to: S().pageTo,
        chapter_hint: S().hint || '',
        new_course_title: (S().courses.find(c => c.id === S().courseId) || {}).title || ''
      });
      S().job = started.job;
      A.toast(`已排队：任务 #${started.job.id}`);
      A.render();
      poll(started.job.id);
    } catch (error) {
      S().error = error.message;
      A.toast(`提交失败：${error.message}`);
      A.render();
    } finally {
      S().busy = false;
      button.disabled = false;
    }
  };

  A.actions['generate-reset'] = function () {
    stopPolling();
    S().job = null;
    S().upload = null;
    S().error = '';
    A.render();
  };
})();
