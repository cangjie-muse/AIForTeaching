/* 卷面编辑器：在一个表里看槽位、改分值、调顺序，保存时走"乐观锁 + 幂等键"。
 *
 * 为什么不给"直接改库"的入口：编辑必须带 base_version（当前 edit_seq）。两个人
 * 同时打开这份卷，后保存的那个必须知道自己覆盖了谁——服务端会返回 409
 * stale_version，页面据它提示"别人改过了，刷新再改"。
 *
 * 本文件只做三件事：渲染卷面、把界面改动收集成 operations、调接口保存。
 * 校验规则（分值两位小数、只能在分节内调序）在前端先挡一遍，服务端仍会再挡：
 * 前端挡是为了少一次往返，服务端挡才是保证。
 */
(function () {
  const A = App;

  function escape(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  const SCORE_PATTERN = /^\d+(\.\d{1,2})?$/;

  A.isValidScore = value => SCORE_PATTERN.test(String(value == null ? '' : value).trim());

  /* 卷面 → 表格。每个槽位一行：题号、编码、分值输入、来源完整度、上下移动。
   *
   * "来源完整"不是装饰：已填槽位必须带 exercise_version_id 与 release 来源，
   * 否则发布时会被服务端以 source_incomplete 拒掉。这里直接标出来，让教师在
   * 保存前就知道哪一格是空的。 */
  A.paperEditorHtml = function (paper) {
    if (!paper || !Array.isArray(paper.sections)) {
      return '<div class="editor empty"><p>还没有读取到卷面。</p></div>';
    }
    const head = `<div class="editor-head"><h3>卷面 · ${escape(paper.status || '')}</h3>
      <p class="muted">总分 ${escape(paper.total_score)} · 编辑序号 ${escape(paper.edit_seq)}</p></div>`;
    if (!paper.sections.length) {
      return `<div class="editor">${head}<p class="empty">这份卷还没有分节。</p></div>`;
    }

    const sections = paper.sections.map(section => {
      const slots = (section.slots || []).map(slot => {
        const sourceReady = !!(slot.exercise_version_id && slot.release_context_id);
        const missing = sourceReady ? ''
          : '<span class="badge warn">缺来源</span>';
        return `<tr data-slot="${escape(slot.id)}" data-section="${escape(section.code)}"
              data-slot-no="${escape(slot.slot_no)}">
            <td>${escape(slot.slot_no)}</td>
            <td>${escape(slot.code)}</td>
            <td><input type="text" inputmode="decimal" data-field="score"
                       value="${escape(slot.score)}" aria-label="第 ${escape(slot.slot_no)} 题分值"></td>
            <td>${missing || '完整'}</td>
            <td class="ops">
              <button data-action="paper-slot-up" data-id="${escape(slot.id)}">上移</button>
              <button data-action="paper-slot-down" data-id="${escape(slot.id)}">下移</button>
            </td>
          </tr>`;
      }).join('');
      return `<tr class="section-row"><th colspan="5">${escape(section.code)} · ${escape(section.title)}
          <span class="muted">（${escape(section.section_score)} 分）</span></th></tr>${slots}`;
    }).join('');

    return `<div class="editor">${head}
      <table class="editor-table"><thead><tr>
        <th>题序</th><th>编码</th><th>分值</th><th>来源</th><th>调整</th>
      </tr></thead><tbody>${sections}</tbody></table>
      <div class="button-row">${A.button('保存草稿', 'paper-save', 'save')}</div></div>`;
  };

  /* 界面改动 → PATCH 载荷。
   *
   * `changes` 形如 { score: {slotId: '45.00'}, move: [{slotId, to}] }。
   * 只把**真的变了**的东西写进 operations：没变的槽位也发一条 replace/score，
   * 会让 edit_seq 白涨一次，还会让"谁改了什么"的变更集充满噪音。 */
  A.paperEditPayload = function (paper, changes) {
    const payload = { base_version: paper && paper.edit_seq, operations: [] };
    const byId = {};
    (paper && paper.sections || []).forEach(section => (section.slots || [])
      .forEach(slot => { byId[slot.id] = { slot, section }; }));

    const scores = (changes && changes.score) || {};
    Object.keys(scores).forEach(id => {
      const entry = byId[id];
      if (!entry) return;
      const next = String(scores[id]).trim();
      if (!A.isValidScore(next)) {
        throw new Error(`分值必须是两位以内的小数：第 ${entry.slot.slot_no} 题收到 ${next}`);
      }
      const current = String(entry.slot.score);
      // 数值相同就不发操作（"30.0" 与 "30.00" 是同一次编辑，不该让 edit_seq 白涨）
      if (Number(current) === Number(next)) return;
      payload.operations.push({
        kind: 'score', slot_id: id, score: Number(next).toFixed(2)
      });
    });

    ((changes && changes.move) || []).forEach(move => {
      const entry = byId[move.slotId];
      if (!entry) return;
      const siblings = entry.section.slots || [];
      let target = Number(move.to);
      if (!Number.isInteger(target) || target < 1 || target > siblings.length) {
        throw new Error(`只能在本分节内 1–${siblings.length} 之间调序`);
      }
      if (target === entry.slot.slot_no) return;
      payload.operations.push({ kind: 'move', slot_id: move.slotId, target_slot_no: target });
    });

    return payload;
  };

  const MESSAGES = {
    stale_version: '别人刚刚改过这份卷。刷新后再改，否则会覆盖对方的改动。',
    paper_not_draft: '已提交审核的卷子不能直接改，请克隆一份新卷。',
    idempotency_conflict: '同一个请求编号被用于不同内容，请重试。',
    source_incomplete: '有槽位缺固定来源，先补齐再保存。'
  };

  A.paperEditError = function (error) {
    const code = error && error.code;
    return MESSAGES[code] || (error && error.message) || '保存失败';
  };
})();
