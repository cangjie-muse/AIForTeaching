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
    A.state.exercisePage = 1;
    const context = selected ? `<div class="notice exercise-context">${A.icon('pen')}<span>正在查看所选习题，可在这里作答并按需展开参考简解与来源。</span>${A.link('查看全部习题', 'exercises', c.id, '', 'text-btn')}</div>` : node ? `<div class="notice exercise-context">${A.icon('graph')}<span>正在查看：<b>${e(node.title)}</b> 的关联习题</span>${A.link('查看全部', 'exercises', c.id, '', 'text-btn')}</div>` : '';
    const filters = `<div class="exercise-filters"><div class="search-input">${A.icon('search')}<input id="exercise-search" data-input="exercise-filter" placeholder="搜索题干、来源或关键词…" aria-label="搜索习题"></div><select id="exercise-chapter" data-input="exercise-filter" aria-label="按章节筛选"><option value="">全部章节与附录</option>${graphOptions(c.nodes.filter(n => n.level === 2), '')}</select><select id="exercise-difficulty" data-input="exercise-filter" aria-label="按难度筛选"><option value="">全部难度</option>${[1, 2, 3, 4, 5].map(n => `<option value="${n}">难度 ${n} / 5</option>`).join('')}</select></div>`;
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
  function exerciseCard(ex, index) {
    const key = A.record('answer', ex.id), assessment = A.local[A.record('assessment', ex.id)], variant = exerciseVariant(ex);
    return `<article class="exercise-card"><div class="exercise-card-head"><span class="exercise-number">${String(index + 1).padStart(2, '0')}</span><span class="difficulty">${Array.from({ length: 5 }, (_, n) => `<i class="${n < variant.difficulty ? 'filled' : ''}"></i>`).join('')}<small>难度 ${variant.difficulty}/5</small></span><span class="exercise-id">${e(ex.id)}</span></div><div class="exercise-prompt">${A.markdown(variant.prompt)}</div><div class="exercise-nodes">${ex.nodeIds.map(id => `<button data-action="read-node" data-id="${e(id)}">${A.icon('graph')}${e(A.node(id)?.title)}</button>`).join('')}</div><details class="exercise-answer"><summary>${A.icon('pen')} 我的作答 ${A.local[key] ? A.badge('已保存', 'green') : ''}<span>${A.icon('chevron')}</span></summary><div class="answer-editor"><textarea id="answer-${e(ex.id)}" placeholder="写下你的思路，支持输入 LaTeX 源码…">${e(A.local[key] || '')}</textarea><div class="button-row">${A.button('保存作答', 'save-answer', 'check', `data-id="${e(ex.id)}"`, 'small')}${A.button('预览公式', 'preview-answer', 'book', `data-id="${e(ex.id)}"`, 'small')}</div></div></details><details class="exercise-answer"><summary>${A.icon('book')} 参考简解与来源<span>${A.icon('chevron')}</span></summary><div class="answer-content">${A.markdown(variant.answer)}<div class="exercise-source">${A.markdown(variant.source)}</div></div></details>${otherVariants(ex, variant)}<div class="exercise-bottom"><span>学习自评：<button class="assessment ${assessment === 'review' ? 'active' : ''}" data-action="assess" data-id="${e(ex.id)}" data-value="review">待巩固</button><button class="assessment ${assessment === 'understood' ? 'active' : ''}" data-action="assess" data-id="${e(ex.id)}" data-value="understood">已理解</button></span>${A.link('带题目提问', 'ai', A.course.id, 'ai', 'text-btn', { exercise: ex.id })}</div></article>`;
  }
  function renderExercises() {
    const found = filteredExercises(), size = 8, pages = Math.max(1, Math.ceil(found.length / size));
    const selected = found.length === 1 && found[0].id === A.route.params.get('exercise');
    A.state.exercisePage = Math.min(A.state.exercisePage, pages);
    const start = (A.state.exercisePage - 1) * size;
    document.getElementById('exercise-count').textContent = selected ? '所选习题' : `找到 ${found.length} 道习题 · 每道题只计数一次`;
    document.getElementById('exercise-results').innerHTML = found.length ? found.slice(start, start + size).map((ex, i) => exerciseCard(ex, start + i)).join('') : A.empty('pen', '当前没有匹配的习题', '可以调整筛选条件；原资料未提供习题的知识点会保留此空状态。');
    document.getElementById('exercise-pagination').innerHTML = selected ? '' : `<div class="pagination">${A.button('上一页', 'exercise-page', '', `data-step="-1" ${A.state.exercisePage === 1 ? 'disabled' : ''}`, 'small')}<span>${A.state.exercisePage} / ${pages}</span>${A.button('下一页', 'exercise-page', '', `data-step="1" ${A.state.exercisePage === pages ? 'disabled' : ''}`, 'small')}</div>`;
  }
  A.after.exercises = () => { if (A.course.exercises) renderExercises(); };
  A.inputs['exercise-filter'] = () => { A.state.exercisePage = 1; renderExercises(); };
  A.actions['exercise-page'] = button => { A.state.exercisePage += Number(button.dataset.step); renderExercises(); document.querySelector('.exercise-filters').scrollIntoView({ block: 'start' }); };
  A.actions['save-answer'] = button => {
    A.local[A.record('answer', button.dataset.id)] = document.getElementById(`answer-${button.dataset.id}`).value;
    if (A.save()) A.toast('作答已保存。此处为个人练习记录，未提交到教学班。');
  };
  A.actions['preview-answer'] = button => A.openDialog('作答预览', A.markdown(document.getElementById(`answer-${button.dataset.id}`).value));
  A.actions.assess = button => {
    A.local[A.record('assessment', button.dataset.id)] = button.dataset.value; A.save();
    button.parentElement.querySelectorAll('.assessment').forEach(el => el.classList.toggle('active', el === button));
  };
})();
