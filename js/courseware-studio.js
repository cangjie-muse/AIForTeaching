/* 课件工坊：按章/节生成初版 PPT，再按教师意见逐版修改。
 *
 * 这一页要让人一眼看清三件事：
 *   1. 哪一版是现在的、哪一版出过问题（状态 + 页数 + 时间）；
 *   2. AI 到底改了什么（change_log 由服务端 diff 两版计划得到，不是模型自述）；
 *   3. 下一步做什么——没版本就"生成初版"，有版本就"提意见再改一版"。
 *
 * 预览是 HTML 而不是图片：服务器上没有 LibreOffice，渲染 pptx 成图要额外一条链路。
 * 用 HTML 摆出同样顺序的标题/要点/公式，教师能判断内容，图片留给真正需要投屏时。
 */
(function () {
  const A = App, e = A.escape;

  const STATUS = {
    pending: { label: '生成中', tone: 'warn' },
    'needs-review': { label: '待教师确认', tone: 'muted' },
    approved: { label: '已确认', tone: 'ok' },
    failed: { label: '失败', tone: 'bad' }
  };
  const KIND_LABEL = { add: '新增', delete: '删除', edit: '修改', move: '改标题', create: '初版' };

  function S() {
    return A.state.studio = A.state.studio || {
      status: 'idle', error: '', courseId: '', chapterId: '', chapters: [],
      deck: null, versions: [], selected: null,
      form: { target: '', pages: 12, focus: '', instruction: '' },
      progress: ''
    };
  }
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const courseOf = () => A.course || A.featuredCourse();
  const canEdit = () => !!(A.auth && A.auth.is('teacher', 'admin'));

  function chapterOptions(state) {
    const options = [];
    (state.chapters || []).forEach(chapter => {
      options.push({ id: chapter.id, label: `第${options.length ? '' : ''}${''}${chapter.title}`, level: 2 });
      (chapter.sections || []).forEach(section => {
        options.push({ id: section.id, label: `　└ ${section.title}`, level: 3 });
      });
    });
    return options.map(option => `<option value="${e(option.id)}" ${
      state.form.target === option.id ? 'selected' : ''}>${e(option.label)}</option>`).join('');
  }

  async function load() {
    const state = S();
    const course = courseOf();
    if (!course) return;
    state.courseId = course.id;
    state.status = 'loading';
    try {
      const data = await A.api.coursewareChapters(course.id);
      state.chapters = data.chapters || [];
      const wanted = A.route.params.get('chapter');
      const known = state.chapters.some(chapter => chapter.id === wanted);
      if (!state.form.target || !state.chapters.some(chapter =>
        chapter.id === state.form.target ||
        (chapter.sections || []).some(section => section.id === state.form.target))) {
        state.form.target = known ? wanted : (state.chapters[0] || {}).id || '';
      }
      await loadDeck(state);
      state.status = 'ready';
    } catch (error) {
      state.status = 'error';
      state.error = error.message;
    }
    A.render();
  }
  A.loadStudio = load;

  async function loadDeck(state) {
    const target = state.form.target;
    const chapter = (state.chapters || []).find(item => item.id === target);
    const section = (state.chapters || []).flatMap(item => item.sections || [])
      .find(item => item.id === target);
    const deckRef = (chapter || section || {}).deck;
    if (!deckRef || !deckRef.deck_id) {
      state.deck = null;
      state.versions = [];
      state.selected = null;
      return;
    }
    const data = await A.api.coursewareDeck(deckRef.deck_id);
    state.deck = data.deck;
    state.versions = data.versions || [];
    state.selected = data.selected || null;
  }

  function versionRow(state, version) {
    const status = STATUS[version.status] || { label: version.status, tone: 'muted' };
    const active = state.selected && state.selected.id === version.id;
    return `<button class="cw-version ${active ? 'active' : ''}" data-action="studio-open"
      data-id="${e(version.id)}">
      <span class="cw-vnum">V${version.number}</span>
      <span class="cw-vmain"><b>${e(version.title || '未命名')}</b>
        <small>${version.slide_count || 0} 页 · ${String(version.created_at || '').slice(0, 16).replace('T', ' ')}${
          version.based_on ? ` · 基于 V${(state.versions.find(v => v.id === version.based_on) || {}).number || '?'}` : ''}</small>
        ${version.error ? `<small class="cw-err">${e(String(version.error).slice(0, 60))}</small>` : ''}
      </span>
      <span class="cw-vside">${A.badge(status.label, status.tone)}</span>
    </button>`;
  }

  function slideCard(slide, index) {
    const equations = (slide.equations || []).map(item => item.text).filter(Boolean);
    return `<article class="cw-slide">
      <header><span class="cw-pageno">${index + 1}</span><b>${e(slide.title || '')}</b>
        ${slide.bookPages ? `<small>${e(slide.bookPages)}</small>` : ''}</header>
      ${(slide.paragraphs || []).map(text => `<p>${e(text)}</p>`).join('')}
      ${(slide.bullets || []).length ? `<ul>${slide.bullets.map(text => `<li>${e(text)}</li>`).join('')}</ul>` : ''}
      ${equations.map(text => `<div class="cw-math">${e(text)}</div>`).join('')}
      ${slide.speakerNote ? `<p class="cw-note">讲稿：${e(slide.speakerNote)}</p>` : ''}
      ${(slide.sourceNodeIds || []).length ? `<p class="cw-src">来源：${e(slide.sourceNodeIds.join('、'))}</p>` : ''}
    </article>`;
  }

  function selectedPanel(state) {
    const version = state.selected;
    if (!version) return `<div class="cw-empty">${A.icon('layers')}
      <p>${state.deck ? '这份课件还没有生成成功的版本。' : '这一章还没有课件：先在上面生成初版。'}</p></div>`;
    if (version.status === 'pending') {
      return `<div class="cw-empty">${A.icon('activity')}<p>正在生成：先转成页面计划，再渲染 pptx。大约半分钟到两分钟，这一页会自动刷新。</p></div>`;
    }
    if (version.status === 'failed') {
      return `<div class="cw-empty">${A.icon('info')}<p>这一版生成失败：${e(version.error || '原因未记录')}</p>
        <p class="muted">改一下侧重点或换一个更小的范围（比如某一节），再生成一版。</p></div>`;
    }
    const plan = version.plan || {};
    const slides = plan.slides || [];
    const changes = version.change_log || [];
    return `<div class="cw-selected-head">
      <div><b>V${version.number} · ${e(version.title || '')}</b>
        <small>${slides.length} 页 · ${version.model || '模型未记录'}</small></div>
      <div class="cw-actions-inline">
        ${version.status === 'needs-review' && canEdit()
          ? `<button class="btn small primary" data-action="studio-confirm" data-id="${e(version.id)}">确认这一版</button>` : ''}
        <a class="btn small" href="${e(A.api.coursewarePptxUrl(version.id))}" download>${A.icon('download')}下载 PPTX</a>
      </div>
    </div>
    ${version.source_changed ? `<p class="cw-warn">这一版生成之后，本章知识点或习题有改动：内容可能已过期，建议重新生成一版。</p>` : ''}
    ${changes.length ? `<details class="cw-changes" ${changes.length < 12 ? 'open' : ''}>
      <summary>这一版改了什么（${changes.length} 处）</summary>
      <ul>${changes.map(change => `<li><span>${e(KIND_LABEL[change.kind] || change.kind)}${change.page ? ` 第${change.page}页` : ''}</span>
        ${e(change.summary || change.title || '')}</li>`).join('')}</ul></details>` : ''}
    ${version.instruction ? `<p class="cw-instruction">上一版收到的意见：${e(version.instruction)}</p>` : ''}
    <div class="cw-slides">${slides.map(slideCard).join('')}</div>`;
  }

  A.coursewareStudio = function () {
    const state = S();
    const course = courseOf();
    if (!course || !A.auth || !A.auth.is('teacher', 'admin')) return '';
    // idle 与 loading 都要显示"读取中"：首次渲染还没发请求，直接显示"还没有章节"
    // 会闪一下假空状态，看起来像这门课没导入。
    if (state.status === 'idle' || state.status === 'loading') {
      return '<section class="panel cw-studio"><p class="muted">正在读取课件数据…</p></section>';
    }
    if (state.status === 'error') {
      return `<section class="panel cw-studio">${A.empty('info', '课件数据读不到', state.error)}</section>`;
    }
    if (!state.chapters.length) {
      return `<section class="panel cw-studio">${A.empty('layers', '这门课还没有章节',
        '先导入标准资源包，章节有了才能按节生成课件。')}</section>`;
    }
    return `<section class="panel cw-studio">
      <div class="panel-heading">
        <div><div class="eyebrow">AI COURSEWARE STUDIO</div><h2>按章/节生成与修订</h2></div>
        <span>只读本地知识库：本章知识点正文 + 已发布习题</span>
      </div>
      <div class="cw-form">
        <label>生成范围<select data-change="studio-target">${chapterOptions(state)}</select></label>
        <label>目标页数<input data-input="studio-pages" value="${e(state.form.pages)}" inputmode="numeric" maxlength="2"></label>
        <label class="cw-wide">侧重点（可空）<textarea data-input="studio-focus" rows="2"
          placeholder="例如：多给一个反例；把可积条件的证明讲细；删掉数值计算">${e(state.form.focus)}</textarea></label>
        <div class="cw-form-actions">
          <button class="btn primary" data-action="studio-generate" ${state.progress ? 'disabled' : ''}>
            ${state.progress || '生成初版'}</button>
          <small>AI 出初稿，你在下面提意见逐版改；每一版都留着，可随时回到旧版。</small>
        </div>
      </div>
      ${state.versions.length ? `<div class="cw-versions">${state.versions.map(version => versionRow(state, version)).join('')}</div>` : ''}
      ${selectedPanel(state)}
      ${state.selected && ['needs-review', 'approved'].includes(state.selected.status) ? `<div class="cw-revise">
        <label>修改意见<textarea data-input="studio-instruction" rows="2"
          placeholder="例如：第 3 页的公式写错了，应该是 ω = dθ；课堂练习太难，换一道简单的；把小结拆成两页">${e(state.form.instruction)}</textarea></label>
        <button class="btn" data-action="studio-revise" ${state.progress ? 'disabled' : ''}>按建议改一版</button>
      </div>` : ''}
    </section>`;
  };

  A.after.courseware = function () {
    const state = S();
    if (state.status === 'idle') load();
  };

  A.actions['studio-target'] = async function (select) {
    const state = S();
    state.form.target = select.value;
    state.selected = null;
    A.render();
    try {
      await loadDeck(state);
    } catch (error) {
      state.error = error.message;
    }
    A.render();
  };
  A.inputs['studio-pages'] = input => { S().form.pages = input.value; };
  A.inputs['studio-focus'] = input => { S().form.focus = input.value; };
  A.inputs['studio-instruction'] = input => { S().form.instruction = input.value; };

  async function watch(versionId) {
    // 只等"这一次"的版本：列表里已经没有它（被删、或者换了范围）就停下来，
    // 否则会空转两分钟，测试里也会挂住进程。
    for (let attempt = 0; attempt < 20; attempt += 1) {
      await sleep(3000);
      const state = S();
      if (!state.deck) return;
      try {
        const data = await A.api.coursewareDeck(state.deck.id);
        state.versions = data.versions || [];
        const target = state.versions.find(version => version.id === versionId);
        if (!target) { A.render(); return; }
        if (target.status === 'pending') { A.render(); continue; }
        const detail = await A.api.coursewareDeck(state.deck.id, versionId);
        state.selected = detail.selected || target;
        state.versions = detail.versions || state.versions;
        A.render();
        return;
      } catch (error) {
        A.render();
        return;
      }
    }
  }

  async function submit(button, body, kind) {
    const state = S();
    if (!state.form.target) { A.toast('先选一个章或节'); return; }
    const pages = Number(state.form.pages);
    if (!Number.isFinite(pages) || pages < 6 || pages > 40) { A.toast('目标页数要在 6 到 40 之间'); return; }
    button.disabled = true;
    state.progress = kind === 'revise' ? '正在改一版…' : '正在生成初版…';
    A.render();
    try {
      const result = kind === 'revise'
        ? await A.api.coursewareRevise(state.deck.id, body)
        : await A.api.coursewareCreate(body);
      state.progress = '';
      state.deck = state.deck || { id: result.deck_id };
      await A.api.coursewareChapters(state.courseId).then(data => { state.chapters = data.chapters || []; });
      await loadDeck(state);
      A.render();
      A.toast(kind === 'revise' ? '已排队，正在改这一版' : '已排队，正在生成初版');
      watch(result.version_id);
    } catch (error) {
      state.progress = '';
      button.disabled = false;
      A.render();
      A.toast(`没跑起来：${error.message}`);
    }
  }

  A.actions['studio-generate'] = function (button) {
    const state = S();
    return submit(button, {
      course_id: state.courseId,
      chapter_id: state.form.target,
      pages: Number(state.form.pages),
      focus: state.form.focus || null
    }, 'generate');
  };

  A.actions['studio-revise'] = function (button) {
    const state = S();
    const instruction = String(state.form.instruction || '').trim();
    if (instruction.length < 4) { A.toast('先写一句具体的修改意见'); return; }
    return submit(button, { instruction, base_version: state.selected && state.selected.id }, 'revise');
  };

  A.actions['studio-open'] = async function (button) {
    const state = S();
    try {
      const data = await A.api.coursewareDeck(state.deck.id, button.dataset.id);
      state.selected = data.selected;
      state.versions = data.versions || state.versions;
      state.form.instruction = '';
      A.render();
    } catch (error) {
      A.toast(`打不开这一版：${error.message}`);
    }
  };

  A.actions['studio-confirm'] = async function (button) {
    if (!window.confirm('确认这一版可以拿去上课？确认后状态变成「已确认」，仍可继续改。')) return;
    button.disabled = true;
    try {
      await A.api.coursewareStatus(button.dataset.id, 'approved');
      const data = await A.api.coursewareDeck(S().deck.id, button.dataset.id);
      S().selected = data.selected;
      S().versions = data.versions || S().versions;
      A.render();
      A.toast('已确认');
    } catch (error) {
      button.disabled = false;
      A.toast(`没确认成功：${error.message}`);
    }
  };
})();
