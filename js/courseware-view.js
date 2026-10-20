(function () {
  'use strict';
  const A = App, e = A.escape;
  const manifests = window.COURSEWARE_DATA || [];
  const manifestFor = (courseId, chapterId) => manifests.find(item => item.courseId === courseId && item.chapterId === chapterId);
  const baseFor = manifest => `courseware/${encodeURIComponent(manifest.courseId)}/${encodeURIComponent(manifest.chapterId)}/`;
  const currentCourse = () => A.course || A.featuredCourse();
  const chaptersOf = course => (course?.nodes || []).filter(node => node.level === 2);
  const context = () => {
    const course = currentCourse(), chapters = chaptersOf(course);
    const chapter = chapters.find(item => item.id === A.route.params.get('chapter')) || chapters[0];
    const manifest = chapter && manifestFor(course.id, chapter.id);
    const versions = manifest?.versions || [];
    const version = versions.find(item => item.id === A.route.params.get('version')) || versions.at(-1);
    const slide = Math.min(Math.max(Number(A.route.params.get('slide')) || 1, 1), version?.slideCount || 1);
    return { course, chapters, chapter, manifest, versions, version, slide };
  };
  const feedbackKey = (courseId, chapterId, versionId) => `courseware:feedback:${courseId}:${chapterId}:${versionId || 'initial'}`;
  function courseSelect(course) {
    return `<label class="courseware-select">当前课程<select data-input="courseware-course" aria-label="选择课件所属课程">${A.allCourses().map(item => `<option value="${e(item.id)}" ${item.id === course.id ? 'selected' : ''}>${e(item.title)}${item.status === 'ready' ? '' : ' · 待接入'}</option>`).join('')}</select></label>`;
  }
  function chapterList(course, chapters, active) {
    if (!chapters.length) return A.empty('layers', '暂无章节', '请先为这门课程导入标准资源包。');
    return `<nav class="courseware-chapters" aria-label="课件章节列表">${chapters.map((chapter, index) => {
      const count = manifestFor(course.id, chapter.id)?.versions.length || 0;
      return `<a class="courseware-chapter ${chapter.id === active?.id ? 'active' : ''}" href="${A.url('courseware', course.id, { chapter: chapter.id })}"><span>${String(index + 1).padStart(2, '0')}</span><b>${e(chapter.title)}</b><small>${count ? `已生成 ${count} 版` : '未生成'}</small></a>`;
    }).join('')}</nav>`;
  }
  function versionList(course, chapter, versions, selected) {
    if (!versions.length) return `<div class="courseware-empty-version">${A.icon('book')}<h3>尚无课件版本</h3><p>本章知识点已列出。未来接入 AI 后，可从本章标准资源包生成首版课件。</p></div>`;
    return `<div class="courseware-versions" aria-label="课件版本">${versions.map(version => `<a class="courseware-version ${version.id === selected.id ? 'active' : ''}" href="${A.url('courseware', course.id, { chapter: chapter.id, version: version.id })}"><span>V${version.number}</span><div><b>${e(version.title)}</b><small>${version.slideCount} 页 · ${e(version.createdAt)} · ${version.status === 'needs-review' ? '待教师审核' : '演示样例'}</small></div>${A.icon('arrow')}</a>`).join('')}</div>`;
  }
  function slidePreview(context) {
    const { course, chapter, manifest, version, slide } = context;
    if (!version) return '';
    const prefix = baseFor(manifest), image = prefix + version.files.previews[slide - 1];
    const route = number => A.url('courseware', course.id, { chapter: chapter.id, version: version.id, slide: number });
    return `<section class="panel courseware-preview"><div class="panel-heading"><div><div class="eyebrow">SLIDE PREVIEW</div><h2>V${version.number} · ${e(version.title)}</h2></div><span>${slide} / ${version.slideCount} 页</span></div><img src="${e(image)}" alt="${e(chapter.title)} V${version.number} 第 ${slide} 页预览"><div class="courseware-preview-actions"><a class="btn small" href="${route(Math.max(1, slide - 1))}" ${slide === 1 ? 'aria-disabled="true"' : ''}>上一页</a><a class="btn small" href="${route(Math.min(version.slideCount, slide + 1))}" ${slide === version.slideCount ? 'aria-disabled="true"' : ''}>下一页</a><a class="btn small primary" href="${e(prefix + version.files.pptx)}" download="${e(course.title)}-${e(chapter.title)}-${version.id}.pptx">${A.icon('download')}下载此版 PPTX</a></div></section>`;
  }
  function versionNotes(version) {
    if (!version) return '';
    const feedback = version.feedback ? `<div class="courseware-teacher-feedback"><b>${e(version.feedback.author)}的修订建议</b><p>${e(version.feedback.displayText || version.feedback.quotedText || '')}</p></div>` : '';
    const reviewLabel = version.review.author === '待教师终审' ? '后续审核重点' : `${e(version.review.author)}的下一轮建议`;
    return `<section class="panel courseware-revisions"><div class="eyebrow">VERSION RECORD</div><h2>V${version.number} 的修改记录</h2>${feedback}<ul>${version.changes.map(change => `<li>${e(change)}</li>`).join('')}</ul><div class="courseware-review"><b>${reviewLabel}</b><p>${e(version.review.suggestion)}</p></div></section>`;
  }
  function feedback(context) {
    const { course, chapter, version } = context;
    const saved = A.local[feedbackKey(course.id, chapter.id, version?.id)] || '';
    return `<section class="panel courseware-feedback"><div class="eyebrow">REVISE WITH FEEDBACK</div><h2>提出修改建议</h2><p>建议按课程、章节与所选版本保存在当前浏览器。接入 AI 后，生成服务将连同课件源版本和课程资料一起处理。</p><label for="courseware-feedback">给 ${version ? `V${version.number}` : '首版'} 的建议</label><textarea id="courseware-feedback" placeholder="例如：补充 Galilei 变换的推导，并加入一题课堂练习…">${e(saved)}</textarea><div class="button-row">${A.button('保存本地建议', 'courseware-save-feedback', 'check', '', 'small')}${A.button(version ? '按建议生成新版' : '生成本章首版', 'courseware-request', 'ai', '', 'primary')}</div><small>当前 AI 课件生成服务尚未接入；右侧按钮可查看将来提交的任务内容，不会生成新的文件。</small></section>`;
  }
  A.views.courseware = function () {
    const ctx = context(), { course, chapters, chapter, manifest, versions, version } = ctx;
    return A.sectionTitle('COURSEWARE STUDIO', '课件生成与修改', '按课程与章节整理课件、修改建议和历史版本。', A.badge('AI 服务待接入', 'amber')) +
      `<div class="courseware-top">${courseSelect(course)}<p>当前仅展示已制作的示范课件；“生成新版”尚不调用在线模型。</p></div>` +
      `<div class="courseware-layout"><aside class="panel courseware-side"><div class="eyebrow">CHAPTERS</div><h2>章节列表</h2>${chapterList(course, chapters, chapter)}</aside><div class="courseware-main">${chapter ? `<section class="panel courseware-heading"><div><span>${e(course.title)} · ${e(chapter.title)}</span><h2>${e(chapter.title)}</h2><p>${course.nodes.filter(node => node.chapterId === chapter.id && node.kind === 'knowledge').length} 个知识点 · ${new Set(course.nodes.filter(node => node.chapterId === chapter.id).flatMap(node => node.exerciseIds)).size} 道关联习题</p></div>${A.badge(versions.length ? `已生成 ${versions.length} 版 · ${version?.status === 'needs-review' ? '待审核' : '演示'}` : '未生成', versions.length ? 'blue' : '')}</section><section class="panel courseware-history"><div class="panel-heading"><div><div class="eyebrow">VERSION HISTORY</div><h2>课件版本</h2></div><span>${versions.length} 个版本</span></div>${versionList(course, chapter, versions, version)}</section>${slidePreview(ctx)}${versionNotes(version)}${feedback(ctx)}${manifest?.notice ? `<p class="courseware-notice">${e(manifest.notice)}</p>` : ''}` : A.empty('book', '请选择有章节的课程', '导入课程资源包后，章节会自动显示在这里。')}</div></div>`;
  };
  A.inputs['courseware-course'] = input => A.go('courseware', {}, input.value);
  A.actions['courseware-save-feedback'] = () => {
    const { course, chapter, version } = context();
    const value = document.getElementById('courseware-feedback').value.trim();
    const key = feedbackKey(course.id, chapter.id, version?.id);
    if (value) A.local[key] = value;
    else delete A.local[key];
    if (A.save()) A.toast(value ? '修改建议已保存在当前浏览器' : '已清空本地建议');
  };
  A.actions['courseware-request'] = () => {
    const { course, chapter, version } = context();
    const instruction = document.getElementById('courseware-feedback').value.trim();
    const nodes = course.nodes.filter(node => node.chapterId === chapter.id && node.kind === 'knowledge');
    const request = { courseId: course.id, chapterId: chapter.id, mode: version ? 'revise' : 'create', baseVersionId: version?.id || null, formulaMode: 'office-math-omml', instruction: instruction || '根据本章资源包生成适合本科高年级的课件', sourceNodeIds: nodes.map(node => node.id), sourceExerciseIds: [...new Set(nodes.flatMap(node => node.exerciseIds))] };
    A.openDialog('课件生成任务预览', `<div class="prose"><p>这是未来 AI 服务的任务内容预览。当前本地版不会发送请求，也不会生成文件。</p><pre>${e(JSON.stringify(request, null, 2))}</pre><p>生成与修订规则已记录在 <code>prompts/courseware-generate.md</code> 和 <code>prompts/courseware-revise.md</code>；接口契约见 <code>config/api-contract.json</code>。</p></div>`);
  };
})();
