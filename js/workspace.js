(function () {
  const A = App, e = A.escape;
  function readyCourse() { return A.course || A.featuredCourse(); }
  function courseContext() {
    const c = readyCourse();
    return `<div class="context-select">${A.icon('book')}<span>${e(c?.title || '尚未选择课程')}</span>${A.badge('课程知识库', 'blue')}</div>`;
  }
  function suggestions(course, node, exercise) {
    const leaves = course?.nodes?.filter(item => item.kind === 'knowledge') || [];
    const first = node?.title || leaves.find(item => /定义/.test(item.title))?.title || leaves[0]?.title;
    const theorem = leaves.find(item => /定理|证明/.test(item.title))?.title;
    const topic = exercise ? course.nodes.find(item => item.id === exercise.nodeIds[0])?.title : leaves[Math.min(3, leaves.length - 1)]?.title;
    return [['graph', '理解一个概念', first || '查找一个课程概念'], ['book', '梳理证明思路', theorem || '查找定理与证明'], ['pen', '寻找练习线索', topic || '查找相关练习'], ['layers', '探索知识联系', leaves[Math.floor(leaves.length / 2)]?.title || '查找关联知识点']];
  }
  A.views.ai = function () {
    const c = readyCourse();
    const node = c?.nodes?.find(n => n.id === A.route.params.get('node'));
    const exercise = c?.exercises?.find(x => x.id === A.route.params.get('exercise'));
    A.state.aiContext = { node: node?.id, exercise: exercise?.id };
    return A.sectionTitle('COURSE ASSISTANT', 'AI 学习助手', '把问题放回课程里，让每一次提问都有上下文。', A.badge('AI 模型待接入', 'amber')) + `<div class="ai-layout"><section class="chat-panel">${courseContext()}${node || exercise ? `<div class="chat-context">${A.icon('link')}已携带${node ? `知识点：${e(node.title)}` : `习题：${e(exercise.id)}`}</div>` : ''}<div class="chat-messages" id="chat-messages"><div class="chat-welcome"><span class="assistant-mark">${A.icon('ai')}</span><h2>从你的一个问题开始</h2><p>解释一个概念，梳理证明思路，或寻找相关习题。</p><div class="prompt-grid">${suggestions(c, node, exercise).map(([icon, title, q]) => `<button data-action="ai-prompt" data-prompt="${e(q)}">${A.icon(icon)}<b>${title}</b><small>${e(q)}</small>${A.icon('arrow')}</button>`).join('')}</div><div class="local-search-label">${A.icon('info')}当前可使用本地知识检索；生成式问答将在模型接入后启用。</div></div></div><form class="chat-composer" data-form="chat"><textarea id="chat-input" placeholder="输入问题，或搜索一个课程概念…" aria-label="向课程助教提问" rows="2"></textarea><div><span>本地检索 · 不向外部发送资料</span><button class="btn primary" type="submit">${A.icon('send')}检索课程</button></div></form></section><aside class="ai-side panel"><div class="eyebrow">LEARNING COMPANION</div><h3>围绕课程，循序提问</h3><div class="assistant-step"><span>01</span><div><b>选定学习内容</b><p>可从知识点或习题直接进入，保留当前上下文。</p></div></div><div class="assistant-step"><span>02</span><div><b>提出具体问题</b><p>从定义、条件、例子或某一步推导开始。</p></div></div><div class="assistant-step"><span>03</span><div><b>回到资料验证</b><p>查看知识正文、关联习题和教材出处。</p></div></div><div class="divider"></div><h4>服务状态</h4><div class="service-line"><span>课程知识库</span>${A.badge(c?.nodes ? '已接入' : '待导入', c?.nodes ? 'green' : '')}</div><div class="service-line"><span>生成式模型</span>${A.badge('待接入')}</div><div class="service-line"><span>联网检索</span>${A.badge('未启用')}</div></aside></div>`;
  };
  function findKnowledge(question, c) {
    const words = question.toLowerCase().match(/[a-z]{2,}|[\u4e00-\u9fff]{2,}/g) || [];
    const terms = words.flatMap(word => /[a-z]/.test(word) || word.length <= 3 ? [word] : [...Array(word.length - 1)].map((_, i) => word.slice(i, i + 2)));
    const exercise = c.exercises?.find(item => item.id === A.state.aiContext.exercise);
    const contextIds = new Set([A.state.aiContext.node, ...(exercise?.nodeIds || [])]);
    return (c.nodes || []).filter(n => n.kind === 'knowledge').map(node => {
      const title = node.title.toLowerCase(), text = node.content.toLowerCase();
      const score = terms.reduce((sum, term) => sum + (title.includes(term) ? 5 : text.includes(term) ? 1 : 0), 0);
      return { node, score: score + (contextIds.has(node.id) ? 8 : 0) };
    }).filter(item => item.score > 0).sort((a, b) => b.score - a.score).slice(0, 4);
  }
  A.actions['send-chat'] = function () {
    const input = document.getElementById('chat-input'), q = input.value.trim(); if (!q) return;
    const c = readyCourse(), messages = document.getElementById('chat-messages');
    messages.querySelector('.chat-welcome')?.remove(); input.value = '';
    const results = c ? findKnowledge(q, c) : [];
    const answer = results.length ? `<p>在本课程中找到以下相关内容，可打开原文继续查阅。</p><div class="chat-sources">${results.map(({ node }) => `<a href="${A.url('outline', c.id, { node: node.id })}"><span>${A.icon('book')}<b>${e(node.title)}</b>${A.icon('arrow')}</span><small>${e(node.source)}</small><div class="search-excerpt">${A.markdown(node.content.split(/\n\s*\n/)[0], c)}</div></a>`).join('')}</div>` : '<p>暂未匹配到相关知识点。可以缩短关键词、使用英文术语，或从知识图谱选择一个节点再来提问。</p>';
    messages.insertAdjacentHTML('beforeend', `<div class="chat-user">${e(q)}</div><div class="chat-reply"><span class="reply-icon">${A.icon('ai')}</span><div>${A.badge('本地检索结果 · 非 AI 生成', 'blue')}${answer}<small class="muted">生成式解答暂未启用。</small></div></div>`);
    messages.scrollTop = messages.scrollHeight;
    A.log('localSearch', { courseId: c?.id, results: results.length });
  };
  A.actions['ai-prompt'] = button => { document.getElementById('chat-input').value = button.dataset.prompt; A.actions['send-chat'](); };
  const teachingTabs = ['教学总览', '作业管理', '测验考试', '课堂互动', '讨论区', '通知公告', '班级管理'];
  const teachingDetails = {
    '作业管理': ['pen', '组织课后练习', '按章节与知识点布置作业，后续接入学生提交、教师批阅和反馈。'],
    '测验考试': ['book', '设计课程测验', '组合题目、设置分值与考试时间，后续接入考试和成绩管理。'],
    '课堂互动': ['activity', '连接课堂中的思考', '为随堂测验、投票、签到与提问保留统一入口。'],
    '讨论区': ['user', '让数学问题继续生长', '围绕概念、证明和习题展开讨论，后续接入师生交流。'],
    '通知公告': ['info', '传达教学安排', '集中管理课程通知、作业提醒与学习资料更新。'],
    '班级管理': ['user', '连接课程与教学班', '按学期管理班级、成员与角色。当前还没有教学班。']
  };
  A.views.teaching = function () {
    const c = readyCourse();
    const tab = A.route.params.get('tab') || '教学总览';
    const drafts = A.local[`drafts:${c?.id}`] || [];
    return A.sectionTitle('TEACHING WORKSPACE', '教学工作台', `${e(c?.title || '课程教学')} · 教师视角预览`, A.button('导出教学草稿', 'export-drafts', 'download')) + `<div class="tabs">${teachingTabs.map(name => `<a class="tab ${name === tab ? 'active' : ''}" href="${A.url('teaching', c?.id, { tab: name })}">${name}</a>`).join('')}</div><div class="notice">${A.icon('info')}<span>当前为本地建设版。可创建教学草稿；发布、学生提交与班级同步将在服务接入后启用。</span></div>${tab === '教学总览' ? teachingOverview(c, drafts) : teachingSection(tab, drafts)}`;
  };
  function teachingOverview(c, drafts) {
    return `<div class="teaching-summary"><div class="panel"><small>本地教学草稿</small><strong>${drafts.length}</strong><span>可随时继续编辑</span></div><div class="panel"><small>可用习题</small><strong>${c?.stats?.exercises || 0}</strong><span>来自课程资源包</span></div><div class="panel"><small>教学班</small><strong>—</strong><span>等待班级服务接入</span></div></div><div class="panel courseware-promo"><div><div class="eyebrow">COURSEWARE STUDIO</div><h3>课件生成与修改</h3><p>按课程章节查看生成状态、版本变化和修改建议。</p></div>${A.link('进入课件工作台', 'courseware', c?.id, 'arrow', 'primary')}</div><div class="module-grid">${Object.entries(teachingDetails).map(([title, [icon, sub, desc]]) => `<a class="teaching-tile panel" href="${A.url('teaching', c?.id, { tab: title })}"><span class="module-icon">${A.icon(icon)}</span><h3>${title}</h3><p>${desc}</p><span>${sub} ${A.icon('arrow')}</span></a>`).join('')}</div>`;
  }
  function teachingSection(tab, drafts) {
    const [icon, title, desc] = teachingDetails[tab] || teachingDetails['作业管理'];
    const filtered = drafts.filter(draft => draft.type === tab);
    return `<section class="panel teaching-content"><div class="panel-heading"><div><h2>${title}</h2><p class="muted">${desc}</p></div>${A.button('新建草稿', 'new-draft', 'plus', `data-type="${tab}"`, 'primary')}</div>${filtered.length ? `<div class="draft-grid">${filtered.map(d => `<button class="draft-card" data-action="edit-draft" data-id="${d.id}"><div>${A.badge('本地草稿')}<small>${e(d.updatedAt.slice(0, 10))}</small></div><h3>${e(d.title)}</h3><p>${e(d.description || '暂无说明')}</p><span>继续编辑 ${A.icon('arrow')}</span></button>`).join('')}</div>` : A.empty(icon, `暂无${tab === '讨论区' ? '讨论主题' : tab}`, '可以先保存一份本地草稿，预览未来的教学流程。') }</section>`;
  }
  function draftDialog(draft) {
    A.openDialog(draft.id ? '编辑教学草稿' : '新建教学草稿', `<div class="field"><label>类型</label><input value="${e(draft.type)}" readonly></div><div class="field"><label for="draft-title">标题</label><input id="draft-title" value="${e(draft.title || '')}" placeholder="填写教学活动标题" maxlength="120"></div><div class="field"><label for="draft-description">内容与安排</label><textarea id="draft-description" placeholder="说明任务要求、时间安排或讨论问题…">${e(draft.description || '')}</textarea></div><div class="notice">${A.icon('info')}保存为当前浏览器内的草稿，不会发布或通知学生。</div><div class="dialog-actions">${A.button('保存本地草稿', 'save-draft', 'check', `data-type="${e(draft.type)}" data-id="${e(draft.id || '')}"`, 'primary')}</div>`);
  }
  A.actions['new-draft'] = button => draftDialog({ type: button.dataset.type });
  A.actions['edit-draft'] = button => draftDialog((A.local[`drafts:${readyCourse()?.id}`] || []).find(d => d.id === button.dataset.id));
  A.actions['save-draft'] = button => {
    const title = document.getElementById('draft-title').value.trim(); if (!title) return A.toast('请先填写标题。');
    const key = `drafts:${readyCourse()?.id}`, drafts = A.local[key] || [];
    const draft = { id: button.dataset.id || `draft-${Date.now()}`, type: button.dataset.type, title, description: document.getElementById('draft-description').value, updatedAt: new Date().toISOString() };
    A.local[key] = [...drafts.filter(item => item.id !== draft.id), draft]; A.save();
    document.getElementById('dialog').close(); A.render(); A.toast('教学草稿已保存');
  };
  A.actions['export-drafts'] = () => A.download('课程教学草稿.json', JSON.stringify(A.local[`drafts:${readyCourse()?.id}`] || [], null, 2));
  A.views.resources = function () {
    const courses = A.course ? [A.course] : A.courses;
    const files = courses.flatMap(c => [...(c.files || []), ...(c.attachments || [])].map(name => ({ course: c, name })));
    return A.sectionTitle('COURSE RESOURCES', '课程资源', '课程资料按原始资源包保留，来源清晰，统一查阅。', A.link('导入资源包', 'import', null, 'upload')) + `<div class="resource-summary"><div class="resource-book">${A.icon('book')}</div><div><div class="eyebrow">RESOURCE COLLECTION</div><h2>${A.course ? e(A.course.title) : '数学课程资料库'}</h2><p>${files.length} 份文件 · 知识节点、知识正文、习题与引用插图</p></div></div><section class="panel resource-list"><div class="table-scroll"><table><thead><tr><th>资源名称</th><th>类型</th><th>所属课程</th><th>操作</th></tr></thead><tbody>${files.map(({ course, name }) => `<tr><td><span class="file-icon ${/\.pdf$/i.test(name) ? 'pdf' : ''}">${A.icon(/\.(png|jpg|webp)$/i.test(name) ? 'grid' : 'book')}</span>${e(name.split('/').at(-1))}</td><td><span class="badge">${e(name.split('.').at(-1).toUpperCase())}</span></td><td class="muted">${e(course.title)}</td><td><a class="btn small" target="_blank" rel="noopener" href="${e(A.asset(course, name))}" ${/\.(md|xlsx)$/i.test(name) ? `download="${e(name.split('/').at(-1))}"` : ''}>${/\.(md|xlsx)$/i.test(name) ? '下载原文件' : '打开查看'} ${A.icon('arrow')}</a></td></tr>`).join('')}</tbody></table></div>${!files.length ? A.empty('folder', '尚未添加资源', '正文引用的插图可随课程资源包一起导入。') : ''}</section>`;
  };
  function countRecords(course, kind) {
    const prefix = `${course.id}:${kind}:`; return Object.entries(A.local).filter(([key, value]) => key.startsWith(prefix) && value).length;
  }
  A.views.analytics = function () {
    const courses = A.course ? [A.course] : A.courses;
    const nodes = courses.flatMap(c => c.nodes || []).filter(n => n.kind === 'knowledge');
    const exercises = courses.flatMap(c => c.exercises || []);
    const covered = nodes.filter(n => n.exerciseIds.length).length, rate = nodes.length ? Math.round(covered / nodes.length * 100) : 0;
    const metrics = [[nodes.length, '知识点', '导入资源包实际数量'], [exercises.length, '不同习题', '按来源 ID 去重计数'], [`${rate}%`, '习题覆盖率', `${covered} / ${nodes.length} 个知识点有题`], [courses.filter(c => c.status === 'ready').length, '已接入课程', '支持标准资源包持续扩展']];
    const chapters = courses.flatMap(c => (c.nodes || []).filter(n => n.level === 2).map(n => ({ course: c, node: n })));
    const max = Math.max(1, ...chapters.map(({ course, node }) => course.nodes.filter(n => n.chapterId === node.id && n.kind === 'knowledge').length));
    return A.sectionTitle('COURSE INSIGHTS', A.course ? '课程建设与学习分析' : '平台建设概览', '建设数据来自实际导入内容；学习记录仅统计当前浏览器。') + `<div class="analytics-metrics">${metrics.map(([value, title, desc]) => `<div class="panel"><span>${title}</span><strong>${value}</strong><small>${desc}</small></div>`).join('')}</div><div class="analytics-layout"><section class="panel"><div class="panel-heading"><h2>章节知识覆盖</h2><span class="badge blue">${chapters.length} 个教学单元</span></div><div class="coverage-chart">${chapters.map(({ course, node }) => { const count = course.nodes.filter(n => n.chapterId === node.id && n.kind === 'knowledge').length; return `<a class="coverage-row" href="${A.url('outline', course.id, { chapter: node.id })}"><span>${e(node.title)}</span><div><i style="width:${count / max * 100}%"></i></div><b>${count}</b></a>`; }).join('') || '<p class="muted">课程数据待导入。</p>'}</div></section><aside><section class="panel"><div class="panel-heading"><h2>本机学习记录</h2>${A.icon('activity')}</div>${[['visited', '浏览过的知识点'], ['read', '手动标记已读'], ['bookmark', '收藏知识点'], ['answer', '保存的习题作答']].map(([kind, label]) => `<div class="study-count"><span>${label}</span><b>${courses.reduce((sum, c) => sum + countRecords(c, kind), 0)}</b></div>`).join('')}<p class="small-note">已读和自评不等同于知识掌握度或正式成绩。</p></section><section class="panel student-analysis"><span class="empty-icon">${A.icon('chart')}</span><h3>班级学情分析</h3><p>接入教学班与学习记录后，可查看作业完成、测验表现和知识薄弱点。</p>${A.badge('等待真实教学数据')}</section></aside></div>`;
  };
  A.views.study = function () {
    const items = A.courses.flatMap(c => c.nodes.filter(n => n.kind === 'knowledge').map(n => ({ c, n, saved: A.local[`${c.id}:bookmark:${n.id}`], note: A.local[`${c.id}:note:${n.id}`] }))).filter(item => item.saved || item.note);
    return A.sectionTitle('MY LEARNING', '我的学习', '收藏与笔记跟随知识点，记录你自己的理解。', A.button('导出学习记录', 'export-records', 'download')) + `<div class="notice">${A.icon('info')}<span>这些记录仅保存在当前浏览器。更换电脑前请导出备份。</span><label class="btn small" for="records-file">导入记录</label><input type="file" id="records-file" accept=".json" hidden></div><div class="study-grid">${items.map(({ c, n, saved, note }) => `<a class="panel study-card" href="${A.url('outline', c.id, { node: n.id })}"><div>${A.badge(e(c.title), 'blue')}${saved ? A.icon('bookmark') : A.icon('pen')}</div><h3>${e(n.title)}</h3><p>${e(note || '已收藏，随时回来继续阅读。')}</p><span>继续阅读 ${A.icon('arrow')}</span></a>`).join('')}</div>${!items.length ? A.empty('bookmark', '把值得回看的内容留在这里', '打开一个知识点，点击收藏或写下笔记。', A.link('去发现课程', 'catalog', null, 'arrow', 'primary')) : ''}`;
  };
  A.after.study = function () {
    document.getElementById('records-file').addEventListener('change', async event => {
      const file = event.target.files[0]; if (!file) return;
      try {
        if (file.size > 5 * 1024 * 1024) throw new Error('记录文件过大。');
        const data = JSON.parse(await file.text());
        if (data.schemaVersion !== '1.0' || !data.records || typeof data.records !== 'object' || Array.isArray(data.records)) throw new Error('不是有效的学习记录文件。');
        const allowed = Object.fromEntries(Object.entries(data.records).filter(([key, value]) => !['__proto__', 'constructor', 'prototype'].includes(key) && (typeof value === 'boolean' || typeof value === 'string')));
        Object.assign(A.local, allowed); A.save(); A.render(); A.toast('学习记录已合并');
      } catch (error) { console.error('[records/import]', error); A.toast(error.message); }
    });
  };
  A.views.settings = function () {
    return A.sectionTitle('PLATFORM SETTINGS', '平台设置', '查看当前版本与功能接入状态。') + `<div class="settings-grid"><section class="panel"><h2>当前运行方式</h2><div class="setting-row"><div><b>本地展示版</b><p>在当前电脑浏览课程、知识图谱和习题。</p></div>${A.badge(`v${A.version}`, 'blue')}</div><div class="setting-row"><div><b>当前身份</b><p>${A.auth && !A.auth.anonymous() ? `${A.escape(A.auth.label())} · ${A.auth.roleLabel()}` : '未登录（可浏览公开课程）'}</p></div>${A.auth && !A.auth.anonymous() ? A.button('修改密码', 'auth-password-open', 'lock', '', 'small text-btn') : A.button('登录', 'auth-login-open', 'user', '', 'small')}</div><div class="setting-row"><div><b>学习记录</b><p>保存于当前浏览器，可导入和导出。</p></div>${A.button('导出', 'export-records', 'download', '', 'small')}</div><div class="setting-row"><div><b>课程内容</b><p>使用标准文件导入，保留原文与来源。</p></div>${A.link('课程导入', 'import', null, '', 'small')}</div><div class="setting-row"><div><b>问题诊断</b><p>开启后在浏览器控制台显示操作日志。</p></div><label class="switch"><input type="checkbox" id="debug-setting" data-input="debug-setting" ${A.local.debug ? 'checked' : ''}><span></span></label></div></section><section class="panel"><h2>服务接入</h2>${[['AI 生成式问答', '待连接模型与课程检索服务'], ['作业与考试', '待接入提交、批阅和成绩服务'], ['讨论与通知', '待接入多人协作与消息服务']].map(([title, detail]) => `<div class="setting-row"><div><b>${title}</b><p>${detail}</p></div>${A.badge('待接入')}</div>`).join('')}<div class="button-row">${A.button('查看接口说明', 'api-info', 'link', '', 'text-btn')}${A.button('代码与资料来源', 'credits', 'info', '', 'text-btn')}</div></section></div>`;
  };
  A.inputs['debug-setting'] = input => { A.local.debug = input.checked; A.save(); };
  A.actions['api-info'] = () => A.openDialog('后续服务对接', `<div class="prose"><p>课程内容通过标准资源包独立维护。平台为课程、知识节点、习题、教学班、学习记录和 AI 问答分别保留数据边界。</p><p>未来连接服务时，AI 请求将包含课程 ID、当前知识点或习题 ID、问题和历史消息；返回答案与可点击的引用来源。</p><p>详细字段及示例位于项目中的 <code>docs/接口约定.md</code> 与 <code>config/api-contract.json</code>。本版没有启用外部网络请求，也不保存模型密钥。</p></div>`);
})();
