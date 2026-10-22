/* 知识点编辑面板（挂在「知识图谱」页，不挂在审核页）。
 *
 * 分工：审核页只审不改（通过/撤销），改结构都回图谱页做——因为改标题、挂题、
 * 解绑、删节点、换父节点这五件事都会让挂着的题退回未核，必须放在同一个视野里，
 * 改之前能看见代价；而"审核"这个动作本身要干净，"顺手改一下"会让留痕说不清。
 *
 * 面板由 js/graph-view.js 注入 #network-edit；审核页只给一个「去图谱里改」的深链。
 * 删除是这里唯一不可逆的动作：有子节点或挂着题时接口先回 409 报数，
 * 这里把数字摆进确认框，确认后带 cascade 重发。
 */
(function () {
  const A = App, e = A.escape;
  const S = () => A.state.review;
  const shared = () => A.reviewShared || {};
  // 图谱页没有"审核课程筛选"这一层，所以由调用方把课程 id 记下来（nodeCourse），
  // 审核页仍然回落到筛选框的值。两处共用一份面板，不能各写一个课程来源。
  const courseId = () => S().nodeCourse || S().filters.course_id;

  function summaryLine(detail) {
    const node = detail.node;
    const bits = [`<code>${e(node.id)}</code>`, `第 ${node.level} 层`];
    if (detail.parent) bits.push(`父级 ${e(detail.parent.title)}`);
    if (detail.chapter && detail.chapter.id !== node.id) bits.push(`章 ${e(detail.chapter.title)}`);
    bits.push(`${detail.counts.children} 个子节点`, `${detail.counts.descendants} 个后代`);
    return `<span class="muted small">${bits.join(' · ')}</span>`;
  }

  function exerciseRows(detail) {
    if (!detail.exercises.length) {
      return '<p class="muted small">这个知识点还没挂题。挂上题以后，学生按知识点刷题才会出现这道题。</p>';
    }
    const rows = detail.exercises.map(row => `<li>
      <code>${e(row.id)}</code>
      <span class="rv-mount-prompt">${e((row.prompt_md || '').replace(/\s+/g, ' ').slice(0, 48))}</span>
      ${shared().tierBadge ? shared().tierBadge(row.tier) : ''}
      ${(row.node_ids || []).length > 1
        ? `<span class="muted small">另挂 ${row.node_ids.length - 1} 处</span>` : ''}
      <button class="btn small text-btn" data-action="node-unmount"
              data-node="${e(detail.node.id)}" data-exercise="${e(row.id)}">解绑</button>
    </li>`).join('');
    return `<ul class="rv-mount-list">${rows}</ul>`;
  }

  function pickerBlock(detail) {
    const picker = S().nodePicker || {};
    const items = picker.items || [];
    const results = items.length ? `<ul class="rv-mount-list">${items.map(row => {
      const mounted = (row.node_ids || []).includes(detail.node.id);
      return `<li><code>${e(row.id)}</code>
        <span class="rv-mount-prompt">${e((row.prompt_md || '').replace(/\s+/g, ' ').slice(0, 40))}</span>
        ${mounted ? '<span class="rv-node-state">已挂在这儿</span>'
          : `<button class="btn small" data-action="node-mount" data-node="${e(detail.node.id)}"
               data-exercise="${e(row.id)}">挂上</button>`}
      </li>`;
    }).join('')}</ul>` : (picker.loading ? '<p class="muted small">正在搜…</p>' : '');
    return `<div class="rv-mount-picker">
      <label class="rv-select"><span>挂一道题</span>
        <input data-input="node-mount-query" value="${e(picker.query || '')}"
               placeholder="题号或题干，如 ex-12 或 极限" autocomplete="off">
      </label>
      <button class="btn small" data-action="node-mount-search" data-node="${e(detail.node.id)}">搜索</button>
      ${picker.error ? `<span class="rv-warn">${e(picker.error)}</span>` : ''}
    </div>
    ${results}
    <p class="muted small">挂上或解绑都会把这道题的人工审核撤销——位置变了，原来"审过了"的前提就没了。已发布的题仍对学生可见。</p>`;
  }

  function moveBlock(detail) {
    const targets = detail.move_targets || [];
    const current = (detail.parent || {}).id || '';
    const label = row => (row.path || []).slice(1).join(' / ') || row.title;
    const options = targets.map(row => `<option value="${e(row.id)}"
      ${row.id === current ? 'selected' : ''}>${e(label(row))}</option>`).join('');
    if (!targets.length) {
      return `<div class="rv-node-move">
        <span class="muted small">没有别的位置可放：这门课只有这一处容器节点，或者目标已达最深层。</span>
      </div>`;
    }
    return `<div class="rv-node-move">
      <label class="rv-select"><span>移到</span>
        <select data-input="node-move-target">${options}</select>
      </label>
      <button class="btn small" data-action="node-move" data-node="${e(detail.node.id)}">移动到这里</button>
      <span class="muted small">这棵子树（含 ${detail.counts.descendants} 个下级）整体挪走，它们的人工审核一并撤销。</span>
    </div>`;
  }

  function dangerBlock(detail) {
    const tip = detail.counts.children
      ? `有 ${detail.counts.children} 个子节点，删除会连带整棵子树`
      : (detail.counts.exercises ? `挂着 ${detail.counts.exercises} 道题，删除后这些题会失去挂载` : '这是叶子节点');
    return `<div class="rv-node-danger">
      <button class="btn small danger" data-action="node-delete" data-node="${e(detail.node.id)}">删除这个节点</button>
      <span class="muted small">${e(tip)}</span>
    </div>`;
  }

  function body(detail, error) {
    if (error) {
      return `<div class="rv-node-panel"><p class="rv-warn">读不到这个节点：${e(error)}</p>
        <button class="btn small text-btn" data-action="node-close">关闭</button></div>`;
    }
    if (!detail) return '<div class="rv-node-panel"><p class="muted small">正在读取节点…</p></div>';
    return `<div class="rv-node-panel">
      <div class="rv-node-panel-head">
        <b>${e(detail.node.title)}</b>
        ${detail.node.verified ? A.badge('已人工通过', 'green') : '<span class="rv-node-state">未审</span>'}
        ${summaryLine(detail)}
        <button class="btn small text-btn" data-action="node-close">关闭</button>
      </div>
      <div class="rv-node-title-edit">
        <label class="rv-select"><span>标题</span>
          <input data-input="node-title" value="${e(S().nodeTitle || detail.node.title)}" autocomplete="off">
        </label>
        <button class="btn small primary" data-action="node-save-title" data-node="${e(detail.node.id)}">保存标题</button>
        <span class="muted small">改标题算内容变化：这条分支（含子树）挂的 ${detail.counts.subtree_exercises ?? detail.counts.exercises} 道题会退回未核。</span>
      </div>
      ${moveBlock(detail)}
      <h4>挂在这儿的题（${detail.counts.exercises}）</h4>
      ${exerciseRows(detail)}
      ${pickerBlock(detail)}
      ${dangerBlock(detail)}
    </div>`;
  }

  function panelFor(nodeId) {
    const state = S();
    if (state.nodePanel !== nodeId) return '';
    return body(state.nodeDetail, state.nodeDetailError);
  }

  async function loadDetail(nodeId, course) {
    if (course) S().nodeCourse = course;
    S().nodeDetail = null;
    S().nodeDetailError = '';
    S().nodePanel = nodeId;
    S().nodeTitle = '';
    S().nodeMoveTarget = '';
    S().nodePicker = { query: '', items: [], loading: false, error: '' };
    A.render();
    try {
      S().nodeDetail = await A.api.nodeDetail(course, nodeId);
    } catch (error) {
      S().nodeDetailError = error.message;
    }
    A.render();
  }

  function refreshGraph() {
    S().status = 'idle';
    if (shared().reload) shared().reload();
    // 图谱页的「已审/未审」徽标来自 /review/graph；改完结构要让它重取一次。
    A.state.networkReviewStale = true;
    A.render();
  }

  A.reviewNode = { panelFor, loadDetail, refreshGraph };

  // 兼容旧链接与返校对：真正打开面板的入口是图谱页的 network-edit。
  A.actions['node-manage'] = function (button) {
    if (S().nodePanel === button.dataset.node) {
      S().nodePanel = '';
      S().nodeDetail = null;
      A.render();
      return;
    }
    loadDetail(button.dataset.node);
  };

  A.actions['node-close'] = function () {
    S().nodePanel = '';
    S().nodeDetail = null;
    S().nodeDetailError = '';
    A.render();
  };

  A.inputs['node-title'] = function (input) {
    S().nodeTitle = input.value;
  };

  A.inputs['node-move-target'] = function (input) {
    S().nodeMoveTarget = input.value;
  };

  A.actions['node-move'] = async function (button) {
    const target = (S().nodeMoveTarget || '').trim();
    if (!target) {
      A.toast('先选一个去处');
      return;
    }
    button.disabled = true;
    try {
      const result = await A.api.patchNode(courseId(), button.dataset.node, { parent_id: target });
      const moved = result.move || {};
      A.toast(`已移动：这棵子树 ${moved.nodes || 1} 个节点的审核已撤销，需要重新看一遍`);
      S().nodeCourse = courseId();
      await loadDetail(button.dataset.node, courseId());
      refreshGraph();
    } catch (error) {
      button.disabled = false;
      A.toast(`移不动：${error.message}`);
    }
  };

  A.inputs['node-mount-query'] = function (input) {
    if (!S().nodePicker) S().nodePicker = { query: '', items: [], loading: false, error: '' };
    S().nodePicker.query = input.value;
  };

  A.actions['node-save-title'] = async function (button) {
    const title = (S().nodeTitle || '').trim();
    if (!title) {
      A.toast('标题不能为空');
      return;
    }
    button.disabled = true;
    try {
      const result = await A.api.patchNode(courseId(), button.dataset.node, { title });
      const reverified = result.title_reverified_reset ? '（这个节点的「通过」也一起撤销了）' : '';
      A.toast(result.exercises_reset
        ? `标题已改，${result.exercises_reset} 道题退回未核${reverified}`
        : `标题已改${reverified || '。改完记得看一遍再通过。'}`);
      S().nodeTitle = '';
      await loadDetail(button.dataset.node);
      refreshGraph();
    } catch (error) {
      button.disabled = false;
      A.toast(`改不了：${error.message}`);
    }
  };

  A.actions['node-mount-search'] = async function (button) {
    const picker = S().nodePicker || { query: '' };
    picker.loading = true;
    picker.error = '';
    A.render();
    try {
      const data = await A.api.exerciseOptions(courseId(), picker.query);
      picker.items = data.items || [];
    } catch (error) {
      picker.error = error.message;
      picker.items = [];
    }
    picker.loading = false;
    A.render();
  };

  A.actions['node-mount'] = async function (button) {
    button.disabled = true;
    try {
      const result = await A.api.mountExercises(courseId(), button.dataset.node, [button.dataset.exercise]);
      A.toast(result.reset.cleared
        ? `已挂上 ${result.attached.length} 道题，${result.reset.cleared} 道的人工审核已撤销`
        : `已挂上 ${result.attached.length} 道题`);
      await loadDetail(button.dataset.node);
      refreshGraph();
    } catch (error) {
      button.disabled = false;
      A.toast(`挂不上：${error.message}`);
    }
  };

  A.actions['node-unmount'] = async function (button) {
    if (!window.confirm(`解除 ${button.dataset.exercise} 与这个知识点的挂载？这道题的人工审核会一并撤销。`)) return;
    button.disabled = true;
    try {
      const result = await A.api.unmountExercise(courseId(), button.dataset.node, button.dataset.exercise);
      A.toast(`已解绑${result.reset.cleared ? '，这道题退回未核' : ''}`);
      await loadDetail(button.dataset.node);
      refreshGraph();
    } catch (error) {
      button.disabled = false;
      A.toast(`解绑失败：${error.message}`);
    }
  };

  A.actions['node-delete'] = async function (button) {
    const nodeId = button.dataset.node;
    button.disabled = true;
    try {
      const result = await A.api.deleteNode(courseId(), nodeId, false);
      A.toast(`已删除 ${result.deleted} 个节点${result.detached_exercises
        ? `，${result.detached_exercises} 道题失去挂载并退回未核` : ''}`);
      S().nodePanel = '';
      S().nodeDetail = null;
      refreshGraph();
    } catch (error) {
      if (error.status === 409 && error.detail) {
        const info = error.detail;
        const ok = window.confirm(
          `「${info.title}」下面还有 ${info.nodes} 个节点（含子节点）、${info.exercises} 道挂着的题。\n`
          + '删除会连带子节点，并让这些题失去挂载、退回未核。确定删除？'
        );
        if (!ok) {
          button.disabled = false;
          return;
        }
        try {
          const result = await A.api.deleteNode(courseId(), nodeId, true);
          A.toast(`已删除 ${result.deleted} 个节点，${result.detached_exercises} 道题退回未核`);
          S().nodePanel = '';
          S().nodeDetail = null;
          refreshGraph();
        } catch (inner) {
          button.disabled = false;
          A.toast(`删不掉：${inner.message}`);
        }
        return;
      }
      button.disabled = false;
      A.toast(`删不掉：${error.message}`);
    }
  };
})();
