(function () {
  const A = App, e = A.escape;
  function nodeButtons(node) {
    const marked = !!A.local[A.record('bookmark', node.id)];
    const read = !!A.local[A.record('read', node.id)];
    return `<div class="button-row">${A.button(marked ? '已收藏' : '收藏知识点', 'bookmark-node', 'bookmark', `data-id="${e(node.id)}"`, marked ? 'primary' : '')}${A.button(read ? '已标记读过' : '标记为已读', 'read-status', 'check', `data-id="${e(node.id)}"`)}${A.link('关联习题', 'exercises', A.course.id, 'pen', '', { node: node.id })}${A.link('带着知识点提问', 'ai', A.course.id, 'ai', '', { node: node.id })}</div>`;
  }
  A.actions['read-node'] = function (button) {
    const node = A.node(button.dataset.id); if (!node) return A.toast('未找到这个知识点。');
    A.local[A.record('visited', node.id)] = new Date().toISOString(); A.save();
    const chapter = A.node(node.chapterId);
    const exerciseCount = node.exerciseIds.length;
    const exerciseLink = exerciseCount ? `<a class="reader-exercise-link" href="${A.url('exercises', A.course.id, { node: node.id })}">${exerciseCount} 道关联习题 →</a>` : A.badge('暂无关联习题');
    A.openDialog(e(node.title), `<div class="dialog-meta">${A.badge(e(chapter?.title || '课程总览'), 'blue')}${exerciseLink}${A.badge('内容待教师审核')}</div><div class="reader-source">${e(node.source)}</div>${A.markdown(node.content)}<div class="divider"></div><div id="reader-buttons">${nodeButtons(node)}</div><div class="note-editor"><label for="node-note">我的笔记</label><textarea id="node-note" placeholder="记录你的理解、疑问或推导思路…">${e(A.local[A.record('note', node.id)] || '')}</textarea>${A.button('保存笔记', 'save-node-note', 'check', `data-id="${e(node.id)}"`, 'small')}</div>`, 'reader');
  };
  A.actions['bookmark-node'] = function (button) {
    const node = A.node(button.dataset.id), key = A.record('bookmark', node.id); A.local[key] = !A.local[key]; A.save();
    const buttons = document.getElementById('reader-buttons'); if (buttons) buttons.innerHTML = nodeButtons(node);
    A.toast(A.local[key] ? '已加入收藏' : '已取消收藏');
  };
  A.actions['read-status'] = function (button) {
    const node = A.node(button.dataset.id), key = A.record('read', node.id); A.local[key] = !A.local[key]; A.save();
    document.getElementById('reader-buttons').innerHTML = nodeButtons(node);
  };
  A.actions['save-node-note'] = button => {
    A.local[A.record('note', button.dataset.id)] = document.getElementById('node-note').value;
    if (A.save()) A.toast('笔记已保存在当前浏览器');
  };
  function graphOptions(nodes, selected) {
    return nodes.map(node => `<option value="${e(node.id)}" ${selected === node.id ? 'selected' : ''}>${e(node.title)}</option>`).join('');
  }
  A.views.exercises = function () {
    const c = A.course;
    if (!c.exercises) return A.sectionTitle('EXERCISE LIBRARY', '习题与测验', e(c.title)) + A.empty('pen', '题库等待接入', '导入后支持章节筛选、难度筛选、作答与解答查看。', A.link('导入课程资源', 'import', null, 'upload', 'primary'));
    const node = A.node(A.route.params.get('node'));
    const selected = c.exercises.find(exercise => exercise.id === A.route.params.get('exercise'));
    // 筛选条件与页码按课程记忆：换课再换回来，还是上次的筛选和那一页。
    const memory = A.courseMemory(c.id);
    A.state.exercisePage = selected ? 1 : (memory.exercisePage || 1);
    const context = selected ? `<div class="notice exercise-context">${A.icon('pen')}<span>正在查看所选习题，可在这里作答并按需展开参考简解与来源。</span>${A.link('查看全部习题', 'exercises', c.id, '', 'text-btn')}</div>` : node ? `<div class="notice exercise-context">${A.icon('graph')}<span>正在查看：<b>${e(node.title)}</b> 的关联习题</span>${A.link('查看全部', 'exercises', c.id, '', 'text-btn')}</div>` : '';
    const filters = `<div class="exercise-filters"><div class="search-input">${A.icon('search')}<input id="exercise-search" data-input="exercise-filter" value="${e(memory.exerciseQuery || '')}" placeholder="搜索题干、来源或关键词…" aria-label="搜索习题"></div><select id="exercise-chapter" data-input="exercise-filter" aria-label="按章节筛选"><option value="">全部章节与附录</option>${graphOptions(c.nodes.filter(n => n.level === 2), memory.exerciseChapter || '')}</select><select id="exercise-difficulty" data-input="exercise-filter" aria-label="按难度筛选"><option value="">全部难度</option>${[1, 2, 3, 4, 5].map(n => `<option value="${n}" ${memory.exerciseDifficulty === String(n) ? 'selected' : ''}>难度 ${n} / 5</option>`).join('')}</select></div>`;
    return A.sectionTitle('EXERCISE LIBRARY', '习题与测验', `${c.stats.exercises} 道不同习题 · ${c.stats.associations} 条知识点关联 · 简解与来源均予保留`, A.button('导出练习记录', 'export-records', 'download')) + context + (selected ? '' : filters) + '<div class="exercise-result-label" id="exercise-count"></div><div id="exercise-results"></div><div id="exercise-pagination"></div>';
  };
  function filteredExercises() {
    const selectedId = A.route.params.get('exercise');
    const selected = A.course.exercises.find(exercise => exercise.id === selectedId);
    if (selected) return [selected];
    const q = document.getElementById('exercise-search').value.trim().toLowerCase();
    const chapter = document.getElementById('exercise-chapter').value, difficulty = document.getElementById('exercise-difficulty').value;
    const contextNode = A.route.params.get('node');
    return A.course.exercises.filter(ex => {
      if (contextNode && !ex.nodeIds.includes(contextNode)) return false;
      if (difficulty && !(ex.variants || [ex]).some(variant => variant.difficulty === Number(difficulty))) return false;
      if (chapter && !ex.nodeIds.some(id => A.node(id)?.chapterId === chapter)) return false;
      return !q || (ex.variants || [ex]).some(variant => (variant.prompt + variant.source).toLowerCase().includes(q));
    });
  }
  function exerciseVariant(ex) {
    const nodeId = A.route.params.get('node'), difficulty = Number(document.getElementById('exercise-difficulty')?.value);
    return ex.variants?.find(variant => (!nodeId || variant.nodeIds.includes(nodeId)) && (!difficulty || variant.difficulty === difficulty)) || ex.variants?.find(variant => nodeId && variant.nodeIds.includes(nodeId)) || ex.variants?.[0] || ex;
  }
  function otherVariants(ex, selected) {
    if (!ex.variants) return '';
    const others = ex.variants.filter(variant => variant !== selected);
    return `<details class="exercise-answer"><summary>${A.icon('layers')} 其他 ${others.length} 份表述或解答 · 待教师核对<span>${A.icon('chevron')}</span></summary><div class="answer-content">${others.map(variant => `<section class="exercise-variant"><small>关联知识点：${e(variant.nodeIds.map(id => A.node(id)?.title).join('、'))}</small>${A.markdown(variant.prompt)}${A.markdown(variant.answer)}<div class="exercise-source">${A.markdown(variant.source)}</div></section>`).join('')}</div></details>`;
  }
  // 分层教学：引导 → 作答 → 完整解答。
  // 之前"参考简解"直接放在折叠面板里，学生第一眼就能点开，等于没有分层；
  // 而保存作答的提示又写着"完整解答已解锁"——说的和做的不一致。
  function answerOf(ex) {
    return String(A.local[A.record('answer', ex.id)] || '').trim();
  }
  function isUnlocked(ex) {
    // 三个来源任一成立即解锁：本机作答、本机已点"我卡住了"、后端有本人作答记录。
    // 第三条是为了换浏览器/换设备时不必重新解答——服务端记录比本地缓存可信。
    return Boolean(answerOf(ex))
      || Boolean(A.local[A.record('revealed', ex.id)])
      || Boolean(A.local[A.record('answered-server', ex.id)]);
  }
  function guideBlock(ex, variant) {
    const nodes = ex.nodeIds.map(id => A.node(id)).filter(Boolean);
    const chapter = nodes[0]?.path?.[1];
    const source = String(variant.source || '').split('\n')[0].slice(0, 100);
    return `<details class="exercise-guide"><summary>${A.icon('info')} 引导：先想清楚再动笔<span>${A.icon('chevron')}</span></summary><div class="guide-body">
      <p class="small">${nodes.length ? `这道题用到：<b>${e(nodes.map(node => node.title).join('、'))}</b>` : '这道题的关联知识点还没有标注。'}</p>
      ${chapter ? `<p class="small">所属章节：${e(chapter)}</p>` : ''}
      ${source ? `<p class="small">出处：${e(source)}</p>` : ''}
      <p class="small">先把思路和要用到的定理写下来，再展开完整解答——直接看答案，这道题就失去检验作用了。</p>
    </div></details>`;
  }
  function solutionBlock(variant, revealedOnly) {
    return `<details class="exercise-answer"><summary>${A.icon('book')} 完整解答与来源${revealedOnly ? A.badge('未作答查看', 'amber') : ''}<span>${A.icon('chevron')}</span></summary><div class="answer-content">${A.markdown(variant.answer)}<div class="exercise-source">${A.markdown(variant.source)}</div></div></details>`;
  }
  function lockedBlock(ex) {
    return `<div class="solution-gate">${A.icon('book')}<div class="solution-gate-text"><b>完整解答已锁定</b><p class="small">先写下你的思路（一句话也行），保存后自动解锁完整解答与来源。</p></div><div class="button-row">${A.button('我卡住了，直接看解答', 'reveal-solution', 'info', `data-id="${e(ex.id)}"`, 'small')}</div></div>`;
  }
  function exerciseCard(ex, index) {
    const key = A.record('answer', ex.id), assessment = A.local[A.record('assessment', ex.id)], variant = exerciseVariant(ex);
    const saved = answerOf(ex), unlocked = isUnlocked(ex), revealedOnly = unlocked && !saved;
    return `<article class="exercise-card"><div class="exercise-card-head"><span class="exercise-number">${String(index + 1).padStart(2, '0')}</span><span class="difficulty">${Array.from({ length: 5 }, (_, n) => `<i class="${n < variant.difficulty ? 'filled' : ''}"></i>`).join('')}<small>难度 ${variant.difficulty}/5</small></span><span class="exercise-id">${e(ex.id)}</span></div><div class="exercise-prompt">${A.markdown(variant.prompt)}</div><div class="exercise-nodes">${ex.nodeIds.map(id => `<button data-action="read-node" data-id="${e(id)}">${A.icon('graph')}${e(A.node(id)?.title)}</button>`).join('')}</div>${guideBlock(ex, variant)}<details class="exercise-answer"><summary>${A.icon('pen')} 我的作答 ${saved ? A.badge('已保存', 'green') : ''}<span>${A.icon('chevron')}</span></summary><div class="answer-editor"><textarea id="answer-${e(ex.id)}" placeholder="写下你的思路，支持输入 LaTeX 源码…">${e(A.local[key] || '')}</textarea><div class="button-row">${A.button('保存作答', 'save-answer', 'check', `data-id="${e(ex.id)}"`, 'small')}${A.button('预览公式', 'preview-answer', 'book', `data-id="${e(ex.id)}"`, 'small')}</div></div></details>${unlocked ? solutionBlock(variant, revealedOnly) : lockedBlock(ex)}${otherVariants(ex, variant)}<div class="exercise-bottom"><span>学习自评：<button class="assessment ${assessment === 'review' ? 'active' : ''}" data-action="assess" data-id="${e(ex.id)}" data-value="review">待巩固</button><button class="assessment ${assessment === 'understood' ? 'active' : ''}" data-action="assess" data-id="${e(ex.id)}" data-value="understood">已理解</button></span>${A.link('问这道题', 'qa', A.course.id, 'ai', 'text-btn', { exercise: ex.id })}</div></article>`;
  }
  function renderExercises() {
    const found = filteredExercises(), size = 8, pages = Math.max(1, Math.ceil(found.length / size));
    const selected = found.length === 1 && found[0].id === A.route.params.get('exercise');
    A.state.exercisePage = Math.max(1, Math.min(A.state.exercisePage || 1, pages));
    A.courseMemory().exercisePage = A.state.exercisePage;
    const start = (A.state.exercisePage - 1) * size;
    document.getElementById('exercise-count').textContent = selected ? '所选习题' : `找到 ${found.length} 道习题 · 每道题只计数一次`;
    document.getElementById('exercise-results').innerHTML = found.length ? found.slice(start, start + size).map((ex, i) => exerciseCard(ex, start + i)).join('') : A.empty('pen', '当前没有匹配的习题', '可以调整筛选条件；原资料未提供习题的知识点会保留此空状态。');
    document.getElementById('exercise-pagination').innerHTML = selected ? '' : `<div class="pagination">${A.button('上一页', 'exercise-page', '', `data-step="-1" ${A.state.exercisePage === 1 ? 'disabled' : ''}`, 'small')}<span>${A.state.exercisePage} / ${pages}</span>${A.button('下一页', 'exercise-page', '', `data-step="1" ${A.state.exercisePage === pages ? 'disabled' : ''}`, 'small')}</div>`;
  }
  // 后端已有本人作答记录时也要解锁：换浏览器、换设备后不必再答一遍。
  // 只查一次（按课程），查不到就当作没答过，不影响浏览。
  async function syncServerAttempts() {
    const courseId = A.course?.id;
    if (!courseId) return;
    try {
      const data = await A.api.get(`/api/v1/attempts?course_id=${encodeURIComponent(courseId)}&limit=200`);
      const ids = (data.items || []).map(item => item.exercise_id);
      let changed = false;
      ids.forEach(id => {
        const key = A.record('answered-server', id);
        if (!A.local[key]) { A.local[key] = true; changed = true; }
      });
      if (!changed) return;
      A.save();
      if (A.route.page === 'exercises') renderExercises();
    } catch (error) {
      // 学生身份之外的账号（教师/未登录）会 403；网络失败也一样——都不该打断浏览。
      A.log('attempts/sync', { ok: false, status: error.status });
    }
  }
  A.after.exercises = () => {
    if (!A.course.exercises) return;
    renderExercises();
    syncServerAttempts();
  };
  A.inputs['exercise-filter'] = () => {
    const memory = A.courseMemory();
    memory.exerciseQuery = document.getElementById('exercise-search').value;
    memory.exerciseChapter = document.getElementById('exercise-chapter').value;
    memory.exerciseDifficulty = document.getElementById('exercise-difficulty').value;
    memory.exercisePage = 1;
    A.state.exercisePage = 1;
    renderExercises();
  };
  A.actions['exercise-page'] = button => {
    A.state.exercisePage += Number(button.dataset.step);
    A.courseMemory().exercisePage = A.state.exercisePage;
    renderExercises();
    document.querySelector('.exercise-filters')?.scrollIntoView({ block: 'start' });
  };
  A.actions['save-answer'] = async button => {
    const exerciseId = button.dataset.id;
    const answer = document.getElementById(`answer-${exerciseId}`).value;
    A.local[A.record('answer', exerciseId)] = answer;
    if (!A.save()) return;
    if (!answer.trim()) {
      A.toast('作答为空：完整解答仍然锁定，先写一句思路再保存。');
      return;
    }
    // 先解锁再上传：本地保存永远最先做——断网时学生的字不能丢，
    // 而"是否解锁"不该由网络决定。
    renderExercises();
    try {
      const result = await A.api.submitAttempt({
        courseId: A.course.id, exerciseId, answerMd: answer
      });
      // 后端返回的是 unlocks_full / attempt_no，不是 status。
      // 之前按 result.status 判断，成功时也走 else 分支，弹出"undefined；作答只保存在本机"。
      if (result && result.unlocks_full) {
        A.toast(`已提交到后端（第 ${result.attempt_no} 次），完整解答已解锁。`);
      } else {
        A.toast('已保存在本机，完整解答已解锁（后端未确认提交）。');
      }
    } catch (error) {
      if (error.status === 404) {
        A.toast('这道题还没导入后端题目库：完整解答已在本机解锁，作答只保存在本机。');
      } else if (error.status === 0) {
        A.toast('连不上后端：完整解答已在本机解锁，作答只保存在本机。');
      } else {
        A.toast(`提交失败：${error.message}（完整解答已在本机解锁，作答已保存）`);
      }
    }
  };
  A.actions['reveal-solution'] = button => {
    if (!window.confirm('先自己想一想：直接看解答会跳过思考过程。确定要查看完整解答吗？')) return;
    A.local[A.record('revealed', button.dataset.id)] = new Date().toISOString();
    if (!A.save()) return;
    renderExercises();
    A.toast('已解锁完整解答（记为未作答查看）。');
  };
  A.actions['preview-answer'] = button => A.openDialog('作答预览', A.markdown(document.getElementById(`answer-${button.dataset.id}`).value));
  A.actions.assess = button => {
    A.local[A.record('assessment', button.dataset.id)] = button.dataset.value; A.save();
    button.parentElement.querySelectorAll('.assessment').forEach(el => el.classList.toggle('active', el === button));
  };
})();
