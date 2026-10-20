(function () {
  const A = App, e = A.escape;
  const format = value => Number(value || 0).toLocaleString('zh-CN');
  function metric(value, label, detail) {
    return `<div class="panel construction-metric"><span>${label}</span><strong>${format(value)}</strong><small>${detail}</small></div>`;
  }
  function phaseCard(phase, label, detail) {
    const ratio = phase.total ? phase.ready / phase.total * 100 : 0;
    return `<div class="panel construction-phase-card"><div><span>${label}</span><strong>${phase.ready} <small>/ ${phase.total}</small></strong></div><div class="construction-bar" role="img" aria-label="${label}已接入${phase.ready}门，共${phase.total}门"><i style="width:${ratio}%"></i></div><p>${detail}</p></div>`;
  }
  function courseRow(course, index) {
    const linked = course.nodes.filter(node => node.kind === 'knowledge' && node.exerciseIds.length).length;
    return `<tr><td class="construction-number">${String(index + 1).padStart(2, '0')}</td><td><a class="construction-course-link" href="${A.url('overview', course.id)}">${e(course.title)}${A.icon('arrow')}</a></td><td>${e(course.stage || '补充导入')}</td><td class="construction-count">${format(course.stats.sections)}</td><td class="construction-count">${format(course.stats.knowledge)}</td><td class="construction-count">${format(course.stats.exercises)}</td><td class="construction-count">${format(linked)} / ${format(course.stats.knowledge)}</td><td>${A.badge(e(course.reviewStatus || '待审核'), 'amber')}</td></tr>`;
  }
  function pendingCourses(pending) {
    if (!pending.length) return '<p>规划课程均已有资源包。</p>';
    return `<details class="construction-pending"><summary>查看 ${pending.length} 门待接入课程 ${A.icon('chevron')}</summary><div>${pending.map(course => `<span>${e(course.title)}</span>`).join('')}</div></details>`;
  }
  A.views.construction = function () {
    const report = ConstructionModel.summarize(A.allCourses());
    const t = report.totals, pending = report.plannedTotal - report.plannedReady;
    const ratio = report.plannedTotal ? Math.round(report.plannedReady / report.plannedTotal * 100) : 0;
    return A.sectionTitle('BUILD STATUS · INTERNAL', '建设进度汇总', '仅建设过程使用。数字来自当前实际接入的课程资源包，随导入内容更新。', A.badge('资源建设视角', 'blue')) +
      `<section class="construction-lead"><div><span>规划课程资源接入</span><strong>${report.plannedReady}<small> / ${report.plannedTotal} 门</small></strong><p>资源接入比例 ${ratio}% · 尚有 ${pending} 门规划课待接入</p></div><div class="construction-lead-track" role="img" aria-label="规划课程${report.plannedTotal}门，已接入${report.plannedReady}门"><i style="width:${ratio}%"></i></div><small>“已接入”表示标准文件已解析并进入网站；不表示教材、习题已完成教师审核或课程已正式开课。</small></section>` +
      `<div class="construction-metrics">${metric(report.plannedTotal, '规划课程', '一期10门 · 二期26门')}${metric(report.ready.length, '已接入课程', `${report.additionalReady ? `含 ${report.additionalReady} 门补充导入` : '均来自规划目录'}`)}${metric(t.knowledge, '知识点合计', `${format(t.chapters)} 个章节与附录`)}${metric(t.exercises, '各课去重习题合计', `${format(t.associations)} 条知识点—题目关联`)}</div>` +
      `<div class="construction-phases">${phaseCard(report.phase[0], '基础课程（一期核心课程）', '按已导入标准课程资源包计数')}${phaseCard(report.phase[1], '专业课程（二期重点课程）', '按已导入标准课程资源包计数')}</div>` +
      `<section class="panel construction-table-panel"><div class="panel-heading"><div><div class="eyebrow">COURSE-BY-COURSE</div><h2>已接入课程明细</h2></div>${A.badge(`${report.ready.length} 门课程`, 'blue')}</div><div class="table-scroll"><table class="construction-table"><caption>逐门课程资源统计；知识点与习题数量来自实际导入文件</caption><thead><tr><th>序号</th><th>课程</th><th>建设阶段</th><th>章节</th><th>知识点</th><th>习题</th><th>有题知识点</th><th>内容状态</th></tr></thead><tbody>${report.ready.map(courseRow).join('')}</tbody><tfoot><tr><th colspan="3">合计</th><td>${format(t.chapters)}</td><td>${format(t.knowledge)}</td><td>${format(t.exercises)}</td><td>${format(t.coveredKnowledge)} / ${format(t.knowledge)}</td><td>—</td></tr></tfoot></table></div></section>` +
      `<div class="construction-followup"><section class="panel"><div class="eyebrow">CHECKPOINTS</div><h2>建设中待核对</h2><div class="construction-check"><span>课程内容标记为待教师审核</span><b>${format(t.pendingReview)} 门</b></div><div class="construction-check"><span>暂未关联习题的知识点</span><b>${format(t.knowledge - t.coveredKnowledge)} 个</b></div><div class="construction-check"><span>同源题不同表述 · 同名路径</span><b>${format(t.variantGroups)} 组 · ${format(t.duplicatePaths)} 组</b></div><p>无关联题不自动判定为缺题；结构差异统计不代替数学内容审核。</p></section><section class="panel"><div class="eyebrow">UP NEXT</div><h2>待接入课程</h2><p>一期尚有 ${report.phase[0].total - report.phase[0].ready} 门，二期尚有 ${report.phase[1].total - report.phase[1].ready} 门。此处只反映资源接入，不预测完成时间。</p>${pendingCourses(report.pending)}</section></div>` +
      '<p class="construction-method">统计口径：习题按每门课程内的 source-exercise ID 去重，各课程之间不跨课去重；知识点—题目关联按节点计数。学生人数、成绩与掌握度尚无真实数据，因此不在此页展示。</p>';
  };
})();
