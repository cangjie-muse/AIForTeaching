/* 建卷前的三件事：建蓝图版本 → 生成候选清单 → 按槽位挑题建成草稿卷。
 *
 * 为什么单独一页而不是塞进卷面编辑器：编辑器只认识"已经建好的卷"。建卷要回答的是
 * 另一组问题——总分怎么分、这次考哪几道题、哪些题被门禁挡住了。把"挑题"写进编辑器
 * 会让人以为改分值就能换题。
 *
 * 三条硬规则：
 *   1. 蓝图是本仓库里唯一"可以从零建"的东西；卷只能从"蓝图版本 + 候选快照 + 选题"
 *      派生，所以页面的顺序就是这三步，不允许跳步（跳步只会在提交审核时炸）。
 *   2. 候选的排除原因是服务端给的原因码，前端只负责翻译成人话——不猜、不吞。
 *   3. 总分与槽位分在前端先用整数分（cents）算平，后端仍会再校验；分值全程是字符串，
 *      绝不让 0.1+0.2 这种事决定一份卷子能不能建。
 */
(function () {
  const A = App;

  const PURPOSE_LABEL = { formal: '正式卷', coursework: '课程作业卷', practice: '练习卷' };
  const REVIEW_LABEL = {
    formal_dual_review: '双人复核（正式卷必须）',
    single_review: '单人复核'
  };

  // 与 server/app/contracts/candidate.py 的 REASON_MEANINGS 一一对应。
  // 前端复制一份是为了"没有网络也能解释清楚"；码值本身仍以服务端为准。
  const REASON_TEXT = {
    outside_release_context: '不在本次冻结的 Release 内',
    exercise_missing: '题目已不存在',
    content_drift: 'Release 冻结后被原地编辑过',
    release_withdrawn: '题目已严重撤回',
    release_not_frozen: '引用了还没冻结的 Release',
    scope_mismatch: 'Release 与这门课/班级不一致',
    review_not_approved: '审核还没通过',
    revoked: '审核结论是撤回',
    answer_missing: '缺答案/解析',
    license_missing: '没填许可',
    license_invalid: '许可不允许进卷',
    language_pair_invalid: '语言不匹配',
    render_not_ready: '题干为空或无法渲染',
    exposure_conflict: '曝光冲突（同族题已公开）',
    hidden_evaluation: '属于隐藏评测集',
    private_draft: '私人草稿',
    missing_approved_instance: '缺已审核实例',
    dependencies_incomplete: '依赖不完整',
    purpose_not_allowed: '这个用途不允许'
  };

  const SLUG_PATTERN = /^[A-Za-z0-9._-]{1,64}$/;
  const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const SCORE_PATTERN = /^\d{1,6}(\.\d{1,2})?$/;

  function escape(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /** 分值 → 整数分。用整数做和与比较，避免 0.1+0.2 决定一份卷子能不能建。 */
  function cents(value) {
    const text = String(value == null ? '' : value).trim();
    if (!SCORE_PATTERN.test(text)) {
      throw new Error(`分值必须是两位以内小数：${text || '（空）'}`);
    }
    const [whole, fraction = ''] = text.split('.');
    return Number(whole) * 100 + Number((fraction + '00').slice(0, 2));
  }

  function shortId(id) {
    return String(id || '').slice(0, 8);
  }

  A.paperBuildReasonText = code => REASON_TEXT[code] || String(code || '');

  // ---------------------------------------------------------------------------
  // 表单：默认值与请求体（纯函数）
  // ---------------------------------------------------------------------------

  A.paperBuildDefaults = function () {
    return {
      slug: 'manual',
      purpose: 'formal',
      review: 'formal_dual_review',
      offering: '',
      release: '',
      total: '100.00',
      duration: '7200',
      baseVersionId: null,
      sections: [
        { code: 'A', title: '基础题', slots: [{ code: 'A1', score: '40.00' }, { code: 'A2', score: '60.00' }] }
      ]
    };
  };

  /** 以一份已有蓝图版本为底，建它的下一版（内容复制，身份换成 base_version_id）。 */
  A.paperBuildFromVersion = function (version) {
    const content = (version && version.content) || {};
    const defaults = A.paperBuildDefaults();
    const sections = (content.sections || []).map(section => ({
      code: String(section.code || ''),
      title: String(section.title || ''),
      slots: (section.slots || []).map(slot => ({
        code: String(slot.code || ''),
        score: String(slot.score || '0.00')
      }))
    }));
    return {
      ...defaults,
      purpose: content.purpose || defaults.purpose,
      review: content.review_policy || defaults.review,
      offering: (version && version.offering_ref) || '',
      release: (version && version.release_context_id) || '',
      total: String((version && version.total_score) || defaults.total),
      duration: String((version && version.duration_seconds) || defaults.duration),
      baseVersionId: (version && version.id) || null,
      sections: sections.length ? sections : defaults.sections
    };
  };

  function checkSections(sections) {
    if (!Array.isArray(sections) || !sections.length) throw new Error('至少要有一个分节');
    const sectionCodes = new Set();
    const slotCodes = new Set();
    let total = 0;
    sections.forEach(section => {
      const code = String(section.code || '').trim();
      if (!code) throw new Error('分节码不能为空');
      if (sectionCodes.has(code)) throw new Error(`分节码重复：${code}`);
      sectionCodes.add(code);
      if (!String(section.title || '').trim()) throw new Error(`分节 ${code} 缺标题`);
      if (!Array.isArray(section.slots) || !section.slots.length) {
        throw new Error(`分节 ${code} 至少要有一个槽位`);
      }
      section.slots.forEach(slot => {
        const slotCode = String(slot.code || '').trim();
        if (!slotCode) throw new Error(`分节 ${code} 有槽位没填编号`);
        if (slotCodes.has(slotCode)) throw new Error(`槽位编号重复：${slotCode}`);
        slotCodes.add(slotCode);
        total += cents(slot.score);
      });
    });
    return { total, slotCodes: [...slotCodes] };
  }

  A.paperBuildPayload = function (form) {
    const value = form || {};
    const slug = String(value.slug || '').trim();
    if (!SLUG_PATTERN.test(slug)) {
      throw new Error('蓝图别名只能用字母、数字、点、下划线、短横线（1–64 位）');
    }
    const release = String(value.release || '').trim();
    if (!UUID_PATTERN.test(release)) {
      throw new Error('Release Context 要填冻结后拿到的 UUID');
    }
    const duration = Number(value.duration);
    if (!Number.isInteger(duration) || duration <= 0) throw new Error('考试时长要填正整数字符秒数');
    const purpose = String(value.purpose || 'formal');
    const review = String(value.review || 'formal_dual_review');
    const offering = String(value.offering || '').trim();
    if (purpose === 'formal' && !offering) throw new Error('正式卷必须先选班级');
    if (purpose === 'formal' && review !== 'formal_dual_review') {
      throw new Error('正式卷必须走双人复核（formal_dual_review）');
    }
    const { total } = checkSections(value.sections);
    const declared = cents(value.total);
    if (total !== declared) {
      throw new Error(`总分与槽位分之和不一致：声明 ${(declared / 100).toFixed(2)}，`
        + `槽位合计 ${(total / 100).toFixed(2)}`);
    }
    return {
      slug,
      offering_ref: offering || null,
      release_context_id: release,
      purpose,
      total_score: String(value.total).trim(),
      duration_seconds: duration,
      sections: value.sections.map(section => ({
        code: String(section.code).trim(),
        title: String(section.title).trim(),
        slots: section.slots.map(slot => ({
          code: String(slot.code).trim(),
          score: String(slot.score).trim()
        }))
      })),
      review_policy: review,
      base_version_id: value.baseVersionId || null
    };
  };

  /** 每份蓝图的槽位表（拍平），供"按槽位挑题"用。 */
  A.paperBuildSlots = function (version) {
    const content = (version && version.content) || {};
    const rows = [];
    (content.sections || []).forEach(section => {
      (section.slots || []).forEach(slot => {
        rows.push({
          code: String(slot.code || ''),
          score: String(slot.score || ''),
          sectionCode: String(section.code || ''),
          sectionTitle: String(section.title || '')
        });
      });
    });
    return rows;
  };

  /** 候选快照 → 展示行。标签按 exercise_id 连接（与快照里的 uuid 同源）。 */
  A.paperBuildCandidates = function (snapshot, labels) {
    const byId = labels || {};
    const labelOf = id => byId[String(id)] || {};
    const accepted = ((snapshot && snapshot.accepted) || []).map(record => {
      const evidence = record.evidence || {};
      const label = labelOf(evidence.exercise_id);
      return {
        exerciseVersionId: String(evidence.exercise_version_id || ''),
        exerciseId: String(evidence.exercise_id || ''),
        exerciseRef: label.exercise_ref || '',
        title: label.title || `题目 ${shortId(evidence.exercise_id)}`,
        kind: String(evidence.exercise_kind || ''),
        difficulty: evidence.difficulty_label || '',
        license: record.license || '',
        reviewStatus: String(record.review_status || '')
      };
    });
    const rejected = ((snapshot && snapshot.rejected) || []).map(record => {
      const label = labelOf(record.exercise_id);
      return {
        exerciseId: String(record.exercise_id || ''),
        exerciseVersionId: String(record.exercise_version_id || ''),
        exerciseRef: label.exercise_ref || '',
        title: label.title || `题目 ${shortId(record.exercise_id)}`,
        reasons: (record.reasons || []).map(code => ({
          code: String(code), text: A.paperBuildReasonText(code)
        }))
      };
    });
    return { accepted, rejected };
  };

  /** 建卷命令：蓝图版本 + 候选快照 + 已挑好的槽位。 */
  A.paperBuildPaperBody = function (state) {
    const snapshot = state && state.snapshot;
    if (!snapshot || !snapshot.id) throw new Error('先生成候选清单，才能建卷');
    const slots = A.paperBuildSlots(state.selected);
    if (!slots.length) throw new Error('这份蓝图没有槽位');
    const selection = (state && state.selection) || {};
    const selections = slots
      .filter(slot => selection[slot.code])
      .map(slot => ({ slot_code: slot.code, exercise_version_id: selection[slot.code] }));
    return {
      blueprint_version_id: String(state.selected.id),
      candidate_snapshot_id: String(snapshot.id),
      selections
    };
  };

  A.paperBuildError = function (error) {
    const code = (error && error.code) || (error && error.detail && error.detail.code);
    const messages = {
      idempotency_key_required: '缺少幂等键：请刷新后重试（避免重复建卷）。',
      base_version_required: '这份蓝图已经有历史版本：先在上面点「以它为基础改一版」，再保存。',
      stale_base_version: '有人刚改过这份蓝图：刷新列表后重新以最新版为基础。',
      paper_blueprint_denied: '你没有这门课的命题权限。',
      course_scope_denied: '你不在这一批班级的授权范围内。',
      course_not_found: '这门课不存在，或你的账号看不到它。',
      blueprint_invalid: '蓝图内容不合规：检查总分、槽位分与班级设置。',
      release_context_not_found: '蓝图画的那次 Release 冻结不存在：先冻结 Release 再建蓝图。',
      snapshot_mismatch: '候选快照与这份蓝图对不上：重新生成候选清单。',
      candidate_snapshot_not_found: '这份蓝图还没有候选清单：先点「生成候选清单」。',
      candidate_not_in_snapshot: '选的题不在这次候选清单里：重新生成候选清单再挑。',
      candidate_excluded: '这个槽位明确排除了该题。',
      candidate_not_mandatory: '这个槽位指定了必选题目。',
      source_incomplete: '这道题缺少固定来源字段，不能进卷。',
      duplicate_slot_selection: '同一个槽位被选了两道题。',
      unknown_slot_code: '选题里有蓝图没有的槽位：蓝图可能改过，刷新后再挑。',
      paper_not_found: '卷子不存在或已删除。'
    };
    if (code && messages[code]) return messages[code];
    const raw = (error && error.message) || '';
    if (raw === 'candidate_snapshot_not_found') return messages.candidate_snapshot_not_found;
    return raw || '操作未完成';
  };

  // ---------------------------------------------------------------------------
  // 渲染
  // ---------------------------------------------------------------------------

  function blueprintRows(rows, selectedId) {
    return rows.map(row => `<tr data-blueprint="${escape(row.id)}">
      <td>${escape(PURPOSE_LABEL[row.purpose] || row.purpose)}</td>
      <td>v${escape(row.version_no)}</td>
      <td>${escape(row.total_score)}</td>
      <td>${escape(row.duration_seconds)} 秒</td>
      <td>${escape(row.offering_ref || '（未绑定班级）')}</td>
      <td>${String(row.id) === String(selectedId)
        ? '<span class="muted">已选中</span>'
        : `<button type="button" class="link-btn" data-action="build-select"
             data-version="${escape(row.id)}">选这份</button>
           <button type="button" class="link-btn" data-action="build-base"
             data-version="${escape(row.id)}">以它为基础改一版</button>`}</td>
    </tr>`).join('');
  }

  function blueprintList(state) {
    if (!state.blueprints.length) {
      return `<p class="muted">这门课还没有蓝图版本：用下面的表单建第一份。</p>`;
    }
    return `<table class="build-table"><thead><tr>
        <th>用途</th><th>版本</th><th>总分</th><th>时长</th><th>班级</th><th>操作</th>
      </tr></thead><tbody>${blueprintRows(state.blueprints, state.selectedId)}</tbody></table>`;
  }

  function sectionEditor(section, index) {
    const slots = section.slots.map((slot, slotIndex) => `
      <li class="build-slot">
        <label>槽位号
          <input value="${escape(slot.code)}" data-input="build-slot-code"
                 data-section="${index}" data-slot="${slotIndex}"></label>
        <label>分值
          <input value="${escape(slot.score)}" inputmode="decimal"
                 data-input="build-slot-score" data-section="${index}" data-slot="${slotIndex}"></label>
        <button type="button" class="link-btn" data-action="build-remove-slot"
                data-section="${index}" data-slot="${slotIndex}">删除</button>
      </li>`).join('');
    return `<fieldset class="build-section" data-section="${index}">
      <legend>分节 ${index + 1}</legend>
      <label>分节码
        <input value="${escape(section.code)}" data-input="build-section-code"
               data-section="${index}"></label>
      <label>分节标题
        <input value="${escape(section.title)}" data-input="build-section-title"
               data-section="${index}"></label>
      <ol class="build-slots">${slots}</ol>
      <div class="button-row">
        <button type="button" class="link-btn" data-action="build-add-slot"
                data-section="${index}">加一个槽位</button>
        <button type="button" class="link-btn" data-action="build-remove-section"
                data-section="${index}">删掉这个分节</button>
      </div>
    </fieldset>`;
  }

  function blueprintForm(state) {
    const form = state.form;
    const base = form.baseVersionId
      ? `<p class="muted small">这是在本课程已有蓝图的第 ${escape(
        (state.blueprints.find(row => row.id === form.baseVersionId) || {}).version_no || '?')}`
        + ` 版基础上追加的新版本（base_version_id 已带上）。</p>`
      : `<p class="muted small">同一别名再次保存时，需要先点上面的「以它为基础改一版」。</p>`;
    const offerings = state.classes.length
      ? `<select data-input="build-offering">
          <option value="">（选择班级）</option>
          ${state.classes.map(item => `<option value="${escape(item.id)}"
            ${item.id === form.offering ? 'selected' : ''}>${escape(item.name || item.id)}</option>`).join('')}
        </select>`
      : `<input value="${escape(form.offering)}" data-input="build-offering"
                placeholder="班级 id（这门课还没有班级：先在「班级与选课」里建）">`;
    return `<section class="panel build-form" aria-label="新建蓝图版本">
      <h4>新建蓝图版本</h4>
      ${base}
      <div class="build-grid">
        <label>蓝图别名<input value="${escape(form.slug)}" data-input="build-slug"
               placeholder="manual / midterm / final"></label>
        <label>用途<select data-input="build-purpose">
          ${Object.keys(PURPOSE_LABEL).map(key => `<option value="${key}"
            ${key === form.purpose ? 'selected' : ''}>${PURPOSE_LABEL[key]}</option>`).join('')}
        </select></label>
        <label>复核方式<select data-input="build-review">
          ${Object.keys(REVIEW_LABEL).map(key => `<option value="${key}"
            ${key === form.review ? 'selected' : ''}>${REVIEW_LABEL[key]}</option>`).join('')}
        </select></label>
        <label>班级${offerings}</label>
        <label>Release Context（UUID）<input value="${escape(form.release)}"
               data-input="build-release" placeholder="冻结 Release 后拿到的 id"></label>
        <label>总分<input value="${escape(form.total)}" inputmode="decimal"
               data-input="build-total"></label>
        <label>时长（秒）<input value="${escape(form.duration)}" inputmode="numeric"
               data-input="build-duration"></label>
      </div>
      ${form.sections.map(sectionEditor).join('')}
      <div class="button-row">
        <button type="button" class="btn" data-action="build-add-section">加一个分节</button>
        <button type="button" class="btn primary" data-action="build-save-blueprint"
                ${state.busy ? 'disabled' : ''}>保存蓝图版本</button>
        <button type="button" class="link-btn" data-action="build-reset">清空重填</button>
      </div>
      <p class="muted small">总分必须等于所有槽位分之和（两位以内小数）。正式卷必须选班级并走双人复核。</p>
    </section>`;
  }

  function candidateOptions(slots, accepted, selection) {
    const options = accepted.map(item => `<option value="${escape(item.exerciseVersionId)}"
      ${item.exerciseVersionId === selection[slots.code] ? 'selected' : ''}>
      ${escape(item.exerciseRef ? `${item.exerciseRef} · ${item.title}` : item.title)}</option>`).join('');
    return `<option value="">（这个槽位先空着）</option>${options}`;
  }

  function rejectedBlock(rejected) {
    if (!rejected.length) return '';
    return `<details class="build-rejected"><summary>被挡住的题目 ${rejected.length} 道</summary>
      <ul>${rejected.map(item => `<li><b>${escape(item.exerciseRef || shortId(item.exerciseId))}</b>
        ${escape(item.title)}<ul>${item.reasons.map(reason => `<li>
          <code>${escape(reason.code)}</code> ${escape(reason.text)}</li>`).join('')}</ul></li>`)
        .join('')}</ul></details>`;
  }

  function candidateBlock(state) {
    const slots = A.paperBuildSlots(state.selected);
    const snapshot = state.snapshot;
    if (!snapshot) {
      return `<section class="panel build-candidates" aria-label="候选清单">
        <h4>候选清单</h4>
        <p class="muted">还没有候选清单：按这份蓝图的 Release 与课程范围生成一次，
          才能看到哪些题能进卷、哪些被门禁挡住。</p>
        <div class="button-row">
          <button type="button" class="btn primary" data-action="build-snapshot"
                  ${state.busy ? 'disabled' : ''}>生成候选清单</button>
        </div>
      </section>`;
    }
    const { accepted, rejected } = A.paperBuildCandidates(snapshot, state.labels);
    const picks = slots.map(slot => `<li class="build-pick">
      <span class="build-pick-code">${escape(slot.code)}</span>
      <span class="muted small">${escape(slot.sectionTitle)} · ${escape(slot.score)} 分</span>
      <select data-input="build-pick" data-slot="${escape(slot.code)}"
              aria-label="${escape(slot.code)} 选哪道题">
        ${candidateOptions(slot, accepted, state.selection)}
      </select>
    </li>`).join('');
    return `<section class="panel build-candidates" aria-label="候选清单">
      <h4>候选清单</h4>
      <p class="muted small">候选 ${accepted.length} 道可进卷、${rejected.length} 道被挡。
        清单按内容哈希去重，同一份候选不会落两行。</p>
      ${picks}
      ${rejectedBlock(rejected)}
      <div class="button-row">
        <button type="button" class="btn primary" data-action="build-create-paper"
                ${state.busy ? 'disabled' : ''}>建成草稿卷</button>
        <button type="button" class="link-btn" data-action="build-snapshot"
                ${state.busy ? 'disabled' : ''}>重新生成候选清单</button>
      </div>
      <p class="muted small">空着的槽位会在草稿卷里留成"显式缺口"，
        之后可以进卷面编辑器补题；提交审核前必须填满。</p>
    </section>`;
  }

  function builtBlock(state) {
    if (!state.built) return '';
    const courseId = (A.course && A.course.id) || state.courseId;
    return `<section class="panel build-result" aria-label="建好的草稿卷">
      <h4>草稿卷已建好</h4>
      <p>第 ${escape(state.built.version_no || 1)} 版 · ${escape(state.built.total_score)} 分 ·
        ${escape((state.built.sections || []).length)} 个分节</p>
      <div class="button-row">
        <a class="btn primary" href="${A.url('paper-editor', courseId,
          { version: state.built.id })}">进卷面编辑器</a>
        <a class="btn" href="${A.url('paper-ops', courseId,
          { version: state.built.id })}">审核与发布</a>
      </div>
    </section>`;
  }

  function notice(state) {
    if (state.error) return `<div class="notice bad" role="alert">${escape(state.error)}</div>`;
    if (state.message) return `<div class="notice" role="status">${escape(state.message)}</div>`;
    return '';
  }

  A.paperBuildHtml = function (state) {
    if (!state) return '<div class="build empty"><p>还没有读取到建卷数据。</p></div>';
    const selected = state.selected;
    const head = selected
      ? `<p class="ops-status" role="status">当前蓝图：
          ${escape(PURPOSE_LABEL[selected.purpose] || selected.purpose)}
          · 第 ${escape(selected.version_no)} 版 · ${escape(selected.total_score)} 分
          · ${escape(selected.duration_seconds)} 秒
          · 班级 ${escape(selected.offering_ref || '未绑定')}</p>`
      : '<p class="muted">先选一份蓝图版本（或在下面建一份）。</p>';
    return `<div class="build">
      ${notice(state)}
      <section class="panel build-blueprints" aria-label="蓝图版本">
        <h4>蓝图版本</h4>${head}${blueprintList(state)}
      </section>
      ${state.showForm ? blueprintForm(state) : ''}
      ${selected ? candidateBlock(state) : ''}
      ${builtBlock(state)}
    </div>`;
  };

  // ---------------------------------------------------------------------------
  // 页面状态与动作
  // ---------------------------------------------------------------------------

  function S() {
    return A.state.paperBuild = A.state.paperBuild || {
      courseId: null, blueprints: [], selectedId: null, selected: null,
      snapshot: null, labels: {}, classes: [], loading: false, loaded: false,
      error: null, message: null, busy: null, built: null, showForm: true,
      selection: {}, form: A.paperBuildDefaults()
    };
  }

  function labelMap(payload) {
    const map = {};
    ((payload && payload.items) || []).forEach(item => { map[item.exercise_id] = item; });
    return map;
  }

  /** 读某份蓝图版本的候选清单与题目标签；没有快照时 snapshot=null（不是错误）。 */
  function loadCandidates(state) {
    if (!state.selected) return Promise.resolve();
    const id = state.selected.id;
    return Promise.all([
      A.api.candidateSnapshot(id).catch(error => {
        const code = (error && (error.code || (error.detail && error.detail.code)))
          || error.message;
        if (String(code).includes('candidate_snapshot_not_found')) return null;
        throw error;
      }),
      A.api.candidateLabels(id).catch(() => null)
    ]).then(([snapshot, labels]) => {
      state.snapshot = snapshot;
      state.labels = labelMap(labels);
      state.selection = {};
    });
  }

  A.loadPaperBuild = function (courseId, { force = false } = {}) {
    const state = S();
    if (!courseId) return;
    // 每次渲染都会跑一次 after 钩子：已经读过的课程不再重复打接口，否则渲染→取数
    // →再渲染会互相触发。换课程或 force 时才重新读。
    const sameCourse = state.courseId === courseId;
    if (!force && sameCourse && (state.loading || state.loaded)) return;
    state.courseId = courseId;
    state.loading = true;
    state.error = null;
    A.render();
    return Promise.all([
      A.api.courseBlueprints(courseId),
      A.api.classes({ course_id: courseId }).catch(() => null)
    ]).then(([blueprints, classes]) => {
      state.blueprints = Array.isArray(blueprints) ? blueprints : [];
      state.classes = (classes && classes.items) || [];
      const keep = state.blueprints.find(row => row.id === state.selectedId);
      state.selected = keep || state.blueprints[0] || null;
      state.selectedId = state.selected ? state.selected.id : null;
      return loadCandidates(state);
    }).then(() => {
      state.loading = false;
      state.loaded = true;
      A.render();
    }).catch(error => {
      state.loading = false;
      state.error = A.paperBuildError(error);
      A.render();
    });
  };

  function run(action, task) {
    const state = S();
    if (state.busy) return Promise.resolve();
    state.busy = action;
    state.error = null;
    state.message = null;
    A.render();
    return Promise.resolve().then(task).then(message => {
      state.busy = null;
      if (message) state.message = message;
      A.render();
    }).catch(error => {
      state.busy = null;
      state.error = A.paperBuildError(error);
      A.render();
    });
  }

  function newKey(action) {
    return `paper-build-${action}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  }

  function sectionIndex(button) {
    return Number(button && button.dataset ? button.dataset.section : -1);
  }

  A.views['paper-build'] = function () {
    const state = S();
    const course = A.course;
    const heading = A.sectionTitle(
      'PAPER BUILD',
      '建卷',
      '建蓝图版本 → 生成候选清单 → 按槽位挑题 → 建成草稿卷。挑完再进卷面编辑器改分值或调序。',
      `<div class="button-row">${A.button('重新读取', 'build-refresh', 'refresh')}</div>`
    );
    if (!course) {
      return heading + `<div class="panel"><p class="muted">先选择一门课程。</p></div>`;
    }
    if (state.courseId !== course.id) {
      // 课程切换后旧数据不能留在页面上：那会让教师把别的课的蓝图当成这门课的。
      state.courseId = course.id;
      state.blueprints = [];
      state.selected = null;
      state.selectedId = null;
      state.snapshot = null;
      state.labels = {};
      state.selection = {};
      state.built = null;
      state.loaded = false;
      state.message = null;
      state.error = null;
      state.form = A.paperBuildDefaults();
      state.showForm = true;
    }
    if (state.loading && !state.blueprints.length) {
      return heading + `<div class="panel"><p class="muted">正在读取蓝图与候选…</p></div>`;
    }
    if (state.error && !state.blueprints.length) {
      return heading + `<div class="panel"><h2>读不到建卷数据</h2>
        <p class="muted">${escape(state.error)}</p>
        <div class="button-row">${A.button('重试', 'build-refresh', 'refresh')}</div></div>`;
    }
    return heading + `<div class="panel">${A.paperBuildHtml(state)}</div>`;
  };

  A.after['paper-build'] = function () {
    A.loadPaperBuild(A.course && A.course.id);
  };

  // --- 表单输入（按 data-section / data-slot 定位，不重新渲染，避免打字时丢焦点） ---

  A.inputs['build-slug'] = input => { S().form.slug = input.value; };
  A.inputs['build-purpose'] = input => { S().form.purpose = input.value; };
  A.inputs['build-review'] = input => { S().form.review = input.value; };
  A.inputs['build-offering'] = input => { S().form.offering = input.value; };
  A.inputs['build-release'] = input => { S().form.release = input.value; };
  A.inputs['build-total'] = input => { S().form.total = input.value; };
  A.inputs['build-duration'] = input => { S().form.duration = input.value; };
  A.inputs['build-section-code'] = input => {
    S().form.sections[Number(input.dataset.section)].code = input.value;
  };
  A.inputs['build-section-title'] = input => {
    S().form.sections[Number(input.dataset.section)].title = input.value;
  };
  A.inputs['build-slot-code'] = input => {
    const section = S().form.sections[Number(input.dataset.section)];
    section.slots[Number(input.dataset.slot)].code = input.value;
  };
  A.inputs['build-slot-score'] = input => {
    const section = S().form.sections[Number(input.dataset.section)];
    section.slots[Number(input.dataset.slot)].score = input.value;
  };
  A.inputs['build-pick'] = input => {
    const state = S();
    const slot = input.dataset.slot;
    if (input.value) state.selection[slot] = input.value;
    else delete state.selection[slot];
  };

  // --- 动作 -------------------------------------------------------------------

  A.actions['build-refresh'] = function () {
    A.loadPaperBuild(A.course && A.course.id, { force: true });
  };

  A.actions['build-select'] = function (button) {
    const state = S();
    const row = state.blueprints.find(item => item.id === button.dataset.version);
    if (!row) return;
    state.selected = row;
    state.selectedId = row.id;
    state.built = null;
    return run('select', () => loadCandidates(state).then(() => null));
  };

  A.actions['build-base'] = function (button) {
    const state = S();
    const row = state.blueprints.find(item => item.id === button.dataset.version);
    if (!row) return;
    state.form = A.paperBuildFromVersion(row);
    state.showForm = true;
    state.message = `已复制第 ${row.version_no} 版内容；改完保存就是它的下一版。`;
    A.render();
  };

  A.actions['build-reset'] = function () {
    S().form = A.paperBuildDefaults();
    S().showForm = true;
    A.render();
  };

  A.actions['build-add-section'] = function () {
    const state = S();
    const index = state.form.sections.length;
    const code = String.fromCharCode(65 + (index % 26));
    state.form.sections.push({ code, title: '新分节', slots: [{ code: `${code}1`, score: '0.00' }] });
    A.render();
  };

  A.actions['build-add-slot'] = function (button) {
    const state = S();
    const section = state.form.sections[sectionIndex(button)];
    if (!section) return;
    section.slots.push({ code: `${section.code}${section.slots.length + 1}`, score: '0.00' });
    A.render();
  };

  A.actions['build-remove-slot'] = function (button) {
    const state = S();
    const section = state.form.sections[sectionIndex(button)];
    if (!section) return;
    if (section.slots.length <= 1) {
      state.error = '每个分节至少要留一个槽位';
      A.render();
      return;
    }
    section.slots.splice(Number(button.dataset.slot), 1);
    A.render();
  };

  A.actions['build-remove-section'] = function (button) {
    const state = S();
    if (state.form.sections.length <= 1) {
      state.error = '至少要留一个分节';
      A.render();
      return;
    }
    state.form.sections.splice(sectionIndex(button), 1);
    A.render();
  };

  A.actions['build-save-blueprint'] = function () {
    const state = S();
    let body;
    try {
      body = A.paperBuildPayload(state.form);
    } catch (error) {
      state.error = error.message;
      A.render();
      return Promise.resolve();
    }
    return run('save', () => A.api.createCourseBlueprint(state.courseId, body, newKey('blueprint'))
      .then(row => {
        // 先把新版本设为"当前蓝图"，再读它的候选：反过来会读到旧版本的候选清单。
        state.selected = row;
        state.selectedId = row.id;
        state.form = A.paperBuildFromVersion(row);
        state.showForm = false;
        state.built = null;
        return loadCandidates(state).then(() =>
          `蓝图第 ${row.version_no} 版已保存；下一步生成候选清单。`);
      }));
  };

  A.actions['build-snapshot'] = function () {
    const state = S();
    if (!state.selected) {
      state.error = '先选一份蓝图版本';
      A.render();
      return Promise.resolve();
    }
    return run('snapshot', () => A.api.createCandidateSnapshot(state.selected.id, newKey('snapshot'))
      .then(snapshot => {
        state.snapshot = snapshot;
        state.message = (snapshot && snapshot.created === false)
          ? '候选清单没有变化（按内容哈希去重），沿用同一份快照。'
          : `候选清单已生成：${((snapshot && snapshot.accepted) || []).length} 道可进卷。`;
        return null;
      }));
  };

  A.actions['build-create-paper'] = function () {
    const state = S();
    let body;
    try {
      body = A.paperBuildPaperBody(state);
    } catch (error) {
      state.error = error.message;
      A.render();
      return Promise.resolve();
    }
    return run('create', () => A.api.createPaperDraft(body, newKey('paper')).then(paper => {
      state.built = paper;
      return body.selections.length
        ? `草稿卷已建好（已填 ${body.selections.length} 个槽位）。`
        : '草稿卷已建好，但所有槽位都是空的：进编辑器挑题或重新生成候选后再挑。';
    }));
  };
})();
