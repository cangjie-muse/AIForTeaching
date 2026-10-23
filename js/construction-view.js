(function () {
  const A = App, e = A.escape;
  const format = value => Number(value || 0).toLocaleString('zh-CN');
  function metric(value, label, detail) {
    return `<div class="panel construction-metric"><span>${label}</span><strong>${format(value)}</strong><small>${detail}</small></div>`;
  }
  /* 一个方向一张卡片：接入进度按学科看，比按建设批次看更有意义 */
  function directionCard(direction) {
    const ratio = direction.total ? direction.ready / direction.total * 100 : 0;
    return `<div class="panel construction-phase-card"><div><span>${e(direction.label)}</span><strong>${direction.ready} <small>/ ${direction.total}</small></strong></div><div class="construction-bar" role="img" aria-label="${e(direction.label)}已接入${direction.ready}门，共${direction.total}门"><i style="width:${ratio}%"></i></div><p>${direction.total - direction.ready} 门待接入</p></div>`;
  }
  function courseRow(course, index) {
    const linked = course.nodes.filter(node => node.kind === 'knowledge' && node.exerciseIds.length).length;
    return `<tr><td class="construction-number">${String(index + 1).padStart(2, '0')}</td><td><a class="construction-course-link" href="${A.url('overview', course.id)}">${e(course.title)}${A.icon('arrow')}</a></td><td>${e(course.direction || '待归类')}</td><td class="construction-count">${format(course.stats.sections)}</td><td class="construction-count">${format(course.stats.knowledge)}</td><td class="construction-count">${format(course.stats.exercises)}</td><td class="construction-count">${format(linked)} / ${format(course.stats.knowledge)}</td><td>${A.badge(e(course.reviewStatus || '待审核'), 'amber')}</td></tr>`;
  }
  function pendingCourses(pending) {
    if (!pending.length) return '<p>规划课程均已有资源包。</p>';
    return `<details class="construction-pending"><summary>查看 ${pending.length} 门待接入课程 ${A.icon('chevron')}</summary><div>${pending.map(course => `<span>${e(course.title)}</span>`).join('')}</div></details>`;
  }
  /* 原来在「平台设置」页。那一页学生也能进，而且四块内容各有更合适的家：
     版本与服务接入属于"建设到哪一步"，放这里；学习记录导出在「我的学习」，
     改密在顶栏，课程导入在「课程内容」。诊断开关只给管理员。 */
  function runtimePanel() {
    const services = [
      ['AI 生成式问答', '已接入：模型问答走服务端，课程检索按可见范围过滤', 'blue'],
      ['作业与考试', '待接入：提交、批阅与成绩服务', ''],
      ['讨论与通知', '待接入：多人协作与消息服务', '']
    ];
    return `<section class="panel"><div class="eyebrow">RUNTIME</div><h2>运行方式与服务接入</h2>
      <div class="setting-row"><div><b>${A.dataSource === 'db' ? '在线模式（读数据库）' : '离线演示数据'}</b>
        <p>${A.dataSource === 'db'
          ? '内容来自服务端数据库；审核与可见范围此时生效。'
          : '后端连不上，页面用的是打包在文件里的旧内容；审核与可见范围此时不生效。'}</p></div>
        ${A.badge(`v${A.version}`, 'blue')}</div>
      ${services.map(([title, detail, variant]) => `<div class="setting-row"><div><b>${title}</b><p>${detail}</p></div>`
        + `${variant ? A.badge('已接入', variant) : A.badge('待接入')}</div>`).join('')}
      ${A.auth && A.auth.is('admin') ? `<div class="setting-row"><div><b>问题诊断</b>
        <p>开启后在浏览器控制台显示操作日志（只对当前浏览器有效）。</p></div>
        <label class="switch"><input type="checkbox" id="debug-setting" data-input="debug-setting"
          ${A.local.debug ? 'checked' : ''}><span></span></label></div>` : ''}
      <div class="button-row">${A.button('查看接口说明', 'api-info', 'link', '', 'text-btn')}
        ${A.button('代码与资料来源', 'credits', 'info', '', 'text-btn')}</div>
    </section>`;
  }

  A.inputs['debug-setting'] = input => { A.local.debug = input.checked; A.save(); };

  A.views.construction = function () {
    const report = ConstructionModel.summarize(A.allCourses());
    const t = report.totals, pending = report.plannedTotal - report.plannedReady;
    const ratio = report.plannedTotal ? Math.round(report.plannedReady / report.plannedTotal * 100) : 0;
    return A.sectionTitle('BUILD STATUS · INTERNAL', '建设进度汇总', '仅建设过程使用。数字来自当前实际接入的课程资源包，随导入内容更新。', A.badge('资源建设视角', 'blue')) +
      `<section class="construction-lead"><div><span>规划课程资源接入</span><strong>${report.plannedReady}<small> / ${report.plannedTotal} 门</small></strong><p>资源接入比例 ${ratio}% · 尚有 ${pending} 门规划课待接入</p></div><div class="construction-lead-track" role="img" aria-label="规划课程${report.plannedTotal}门，已接入${report.plannedReady}门"><i style="width:${ratio}%"></i></div><small>“已接入”表示标准文件已解析并进入网站；不表示教材、习题已完成教师审核或课程已正式开课。</small></section>` +
      `<div class="construction-metrics">${metric(report.plannedTotal, '规划课程', `${report.directions.length} 个学科方向`)}${metric(report.ready.length, '已接入课程', `${report.additionalReady ? `含 ${report.additionalReady} 门补充导入` : '均来自规划目录'}`)}${metric(t.knowledge, '知识点合计', `${format(t.chapters)} 个章节与附录`)}${metric(t.exercises, '各课去重习题合计', `${format(t.associations)} 条知识点—题目关联`)}</div>` +
      `<div class="construction-phases">${report.directions.map(directionCard).join('')}</div>` +
      `<section class="panel construction-table-panel"><div class="panel-heading"><div><div class="eyebrow">COURSE-BY-COURSE</div><h2>已接入课程明细</h2></div>${A.badge(`${report.ready.length} 门课程`, 'blue')}</div><div class="table-scroll"><table class="construction-table"><caption>逐门课程资源统计；知识点与习题数量来自实际导入文件</caption><thead><tr><th>序号</th><th>课程</th><th>学科方向</th><th>章节</th><th>知识点</th><th>习题</th><th>有题知识点</th><th>内容状态</th></tr></thead><tbody>${report.ready.map(courseRow).join('')}</tbody><tfoot><tr><th colspan="3">合计</th><td>${format(t.chapters)}</td><td>${format(t.knowledge)}</td><td>${format(t.exercises)}</td><td>${format(t.coveredKnowledge)} / ${format(t.knowledge)}</td><td>—</td></tr></tfoot></table></div></section>` +
      `<div class="construction-followup">${runtimePanel()}<section class="panel"><div class="eyebrow">CHECKPOINTS</div><h2>建设中待核对</h2><div class="construction-check"><span>课程内容标记为待教师审核</span><b>${format(t.pendingReview)} 门</b></div><div class="construction-check"><span>暂未关联习题的知识点</span><b>${format(t.knowledge - t.coveredKnowledge)} 个</b></div><div class="construction-check"><span>同源题不同表述 · 同名路径</span><b>${format(t.variantGroups)} 组 · ${format(t.duplicatePaths)} 组</b></div><p>无关联题不自动判定为缺题；结构差异统计不代替数学内容审核。</p></section><section class="panel"><div class="eyebrow">UP NEXT</div><h2>待接入课程</h2><p>${report.directions.map(d => `${e(d.label)}尚有 ${d.total - d.ready} 门`).join('，')}。此处只反映资源接入，不预测完成时间。</p>${pendingCourses(report.pending)}</section></div>` +
      '<p class="construction-method">统计口径：习题按每门课程内的 source-exercise ID 去重，各课程之间不跨课去重；知识点—题目关联按节点计数。学生人数、成绩与掌握度尚无真实数据，因此不在此页展示。</p>';
  };
})();
