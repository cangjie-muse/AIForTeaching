/* 课程发布：审核页那一块。
 *
 * 为什么从「课程内容」挪到这里：上传只是把内容放进库，审完才谈得上发给学生。
 * 按钮留在上传页上，等于把"最后一步决定"混进"导入文件"的流程里——
 * 老师刚点完上传，手边就是一个发给学生的按钮，那不是个好位置。
 *
 * 两个动作分开，不合并：
 *   发布到学生端        —— 把「人工已审」的题一起放出（审核是逐题动作，发布是整门课开关）
 *   放出全部题目并发布  —— 跳过逐题审核，一次放出；可信度不会变成"已人工校验"
 */
(function () {
  const A = App, e = A.escape;

  A.state.publish = A.state.publish || {
    courseId: '', status: null, loading: false, saving: false, error: '', notice: ''
  };
  const S = () => A.state.publish;

  function courseTitle(courseId) {
    const lists = [A.state.review && A.state.review.courses, A.state.content && A.state.content.courses];
    const found = lists.filter(Boolean).flat().find(course => course.id === courseId);
    return found ? found.title : courseId;
  }

  // 课程选择变了、或者审完一批题，都要重新问一次"现在能放出多少"
  A.loadPublish = async function (courseId) {
    if (!courseId) {
      S().courseId = ''; S().status = null; S().error = '';
      return;
    }
    S().courseId = courseId;
    S().loading = true;
    try {
      S().status = await A.api.publishStatus(courseId);
      S().error = '';
    } catch (error) {
      S().status = null;
      S().error = error.message;
    } finally {
      S().loading = false;
    }
  };

  A.publishPanel = function (courseId) {
    const head = '<div class="panel-heading"><h2>课程发布</h2>';
    if (!courseId) {
      return `<div class="panel"><div class="panel-heading"><h2>课程发布</h2>
        ${A.badge('先选一门课', 'blue')}</div>
        <p class="muted small">发布是整门课的开关，没有"全部课程"这一档。
          在上面「课程」里选一门，这里会出现它的发布状态。</p></div>`;
    }
    if (S().error) {
      return `<div class="panel"><div class="panel-heading"><h2>课程发布</h2>
        ${A.badge('读不到状态', 'amber')}</div>
        <p class="muted small">${e(S().error)}</p>
        <div class="button-row"><button class="btn" data-action="publish-reload">重试</button></div></div>`;
    }
    const status = S().status;
    if (!status) {
      return `<div class="panel"><div class="panel-heading"><h2>课程发布</h2></div>
        <p class="muted small">${S().loading ? '正在读取发布状态…' : '还没有读到发布状态。'}</p></div>`;
    }
    const published = Boolean(status.published_at);
    const drafts = Number(status.hidden) || 0;
    return `<div class="panel"><div class="panel-heading"><h2>课程发布 · ${e(courseTitle(courseId))}</h2>
        ${published ? A.badge('已发布给学生', 'green') : A.badge('未发布', 'amber')}</div>
      <table class="data-table"><tbody>
        <tr><th>知识点（学生可见）</th><td>${Number(status.nodes) || 0}</td></tr>
        <tr><th>已发布的题</th><td>${Number(status.published) || 0}</td></tr>
        <tr><th>不会出现的题（草稿/待审/停用）</th><td>${drafts}</td></tr>
        ${published && status.published_at ? `<tr><th>发布时间</th><td>${e(String(status.published_at).slice(0, 19).replace('T', ' '))}</td></tr>` : ''}
      </tbody></table>
      <p class="muted small">${published
        ? '学生端直接读库里的内容：改知识点正文、标签或题干会立刻生效（题目本身仍须是「已发布」）。'
        : '发布 = 把「人工已审」的题一起放出，其余仍然隐藏。审核是逐题的动作，发布是整门课的开关。'}</p>
      ${S().notice ? `<p class="muted small">${e(S().notice)}</p>` : ''}
      <div class="button-row">
        ${published
          ? `<button class="btn" data-action="publish-retract" ${S().saving ? 'disabled' : ''}>撤回发布</button>`
          : `<button class="btn primary" data-action="publish-to-students" ${S().saving ? 'disabled' : ''}>发布到学生端</button>
             <button class="btn" data-action="publish-all" ${S().saving ? 'disabled' : ''}>放出全部题目并发布</button>`}
      </div>
      ${published ? '' : `<p class="muted small">两条路：① 上面勾选后「批量通过」，再点「发布到学生端」（已审的题会在这一步一起放出）；
        ② 点「放出全部题目并发布」——跳过逐题审核，可信度仍显示「未经人工校验」，审计日志记下是你做的。</p>`}
    </div>`;
  };

  async function afterPublish() {
    if (S().courseId) await A.loadPublish(S().courseId);
    if (A.loadCatalog) A.loadCatalog();   // 课程中心的「建设中」要跟着消失
    A.render();
  }

  A.actions['publish-reload'] = async function () {
    await A.loadPublish(S().courseId);
    A.render();
  };

  A.actions['publish-to-students'] = async function () {
    if (!S().courseId) return;
    S().saving = true; A.render();
    try {
      const result = await A.api.publishCourse(S().courseId);
      const hidden = Number(result.drafts_hidden) ? `；${result.drafts_hidden} 道未审核发布的题仍然隐藏` : '';
      S().notice = `已发布：学生端可见 ${result.exercises} 道题、${result.nodes} 个知识点${hidden}。`
        + (result.warning ? `注意：${result.warning}` : '');
    } catch (error) {
      S().notice = `发布失败：${error.message}`;
    } finally {
      S().saving = false;
    }
    await afterPublish();
  };

  A.actions['publish-all'] = async function () {
    if (!S().courseId) return;
    const status = S().status || {};
    const hidden = Number(status.hidden) || 0;
    const nodes = Number(status.nodes) || 0;
    const answer = window.confirm(`把《${courseTitle(S().courseId)}》的 ${hidden} 道题全部标为「已发布」，`
      + '并把这门课发布到学生端？\n\n'
      + `· 学生将看到 ${nodes} 个知识点与 ${hidden} 道题；\n`
      + '· 这会跳过逐题审核，可信度仍显示「未经人工校验」；\n'
      + '· 审计日志会记下是你做的，事后查得到。');
    if (!answer) return;
    S().saving = true; A.render();
    try {
      const result = await A.api.publishAllExercises(S().courseId, '整课放出（跳过逐题审核）');
      const published = await A.api.publishCourse(S().courseId, '整课放出后发布');
      S().notice = `已放出 ${result.published} 道题，课程已发布：`
        + `学生端可见 ${published.nodes} 个知识点、${published.exercises} 道题。`
        + (result.published
          ? '这些题没有人工校验标记，可见范围设成「仅人工审核」时仍不会放出来。'
          : '这门课一道题都没有，学生只能看知识点。');
    } catch (error) {
      S().notice = `没发出去：${error.message}`;
    } finally {
      S().saving = false;
    }
    await afterPublish();
  };

  A.actions['publish-retract'] = async function () {
    if (!S().courseId) return;
    if (!window.confirm('撤回发布？学生端将立刻看不到这门课。')) return;
    S().saving = true; A.render();
    try {
      await A.api.unpublishCourse(S().courseId);
      S().notice = '已撤回发布，学生端不再出现这门课。';
    } catch (error) {
      S().notice = `撤回失败：${error.message}`;
    } finally {
      S().saving = false;
    }
    await afterPublish();
  };
})();
