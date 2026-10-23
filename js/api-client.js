'use strict';
/* 前端唯一的后端调用入口。
 *
 * 为什么单独抽出来：配置页和审核页都要发请求，两处各写一份 fetch 就会出现
 * "一处编码了中文身份、另一处忘了"这种问题——而请求头只能是 ASCII，
 * 忘了编码的后果是浏览器直接抛 TypeError，且只在真实浏览器里才复现。
 *
 * 约定：
 *   - 所有请求都带 X-Actor / X-Roles（开发期身份），生产改由统一认证下发
 *   - 失败一律抛 ApiError，带 status 与 detail，调用方只处理一种错误形态
 *   - 页面自己不拼 URL，只写 /api/v1/... 路径
 */
(function () {
  const A = App;

  class ApiError extends Error {
    constructor(message, status, detail) {
      super(message);
      this.name = 'ApiError';
      this.status = status;
      this.detail = detail;
    }
  }

  function base() {
    return (A.local.apiBase || '').replace(/\/+$/, '');
  }

  // 只有显式选择"用请求头冒充身份"时才发 X-Actor/X-Roles。
  // 默认不发：否则本地开发里"没登录也是 admin"，登录按钮就成了装饰，
  // 权限也就无从验证。生产（AUTH_MODE=password/cas）服务端根本不看这两个头。
  function headerIdentity() {
    return A.local.devActor === true;
  }

  function headers(extra) {
    const result = { 'Content-Type': 'application/json', ...(extra || {}) };
    if (headerIdentity()) {
      // 头值只能是 ASCII：中文姓名必须编码，服务端 unquote 还原
      result['X-Actor'] = encodeURIComponent(A.local.actorId || '本地调试');
      result['X-Roles'] = (A.local.actorRoles || []).join(',');
    }
    return result;
  }

  async function request(method, path, options = {}) {
    const url = `${base()}${path}`;
    let response;
    try {
      response = await fetch(url, {
        method,
        headers: headers(options.headers),
        credentials: 'same-origin',  // 会话 cookie：登录态靠它，不靠请求头
        body: options.body === undefined ? undefined : JSON.stringify(options.body)
      });
    } catch (cause) {
      // 网络层失败（后端没起、跨域被拒）与业务失败要区分开：
      // 前者给"先连后端"的引导，后者给服务端的具体原因。
      throw new ApiError(
        `无法连接后端（${base() || '同源'}）：${cause.message}`,
        0,
        '请确认后端已启动，或在页面上填写正确的后端地址'
      );
    }
    const text = await response.text();
    let data = {};
    if (text) {
      try { data = JSON.parse(text); } catch { data = { detail: text.slice(0, 300) }; }
    }
    if (!response.ok) {
      // detail 可能是一段话，也可能是一个对象（例如 409 删除确认要带计数）。
      // 对象直接拼进 message 会变成 "[object Object]"，所以取它的 detail 字段或整体序列化，
      // 同时把原始 detail 留在 error.detail 上给页面读。
      const raw = data.detail;
      const message = typeof raw === 'string'
        ? raw
        : (raw && typeof raw === 'object'
          ? (typeof raw.detail === 'string' ? raw.detail : JSON.stringify(raw))
          : `请求失败（HTTP ${response.status}）`);
      const error = new ApiError(message || `请求失败（HTTP ${response.status}）`, response.status, raw);
      // 401 只有一个意思：没登录或会话过期。统一在这里广播，页面不必各自判断。
      if (response.status === 401 && A.auth && !options.noAuthRedirect) A.auth.needed();
      throw error;
    }
    return data;
  }

  // ---------------------------------------------------------------------
  // 课程 id 对不上是当前的真实问题：前端课程包用 analysis-ii，后端导入后
  // 可能是另一个 id。拿前端 id 直接请求，后端只会说"没有这门课"，而人看到
  // 的是"模型不会答"。所以统一在这里做标题归一化匹配。
  // ---------------------------------------------------------------------

  function courseTitle(courseId) {
    // 课程包没加载时（测试环境、导入前）不能因此报错
    try {
      return A.courseData?.(courseId)?.title || courseId;
    } catch {
      return courseId;
    }
  }

  function normalizeTitle(text) {
    return String(text || '').toLowerCase().replace(/[\s（）()《》·—\-_.]/g, '');
  }

  function matchCourse(courseId, courses) {
    if (!courseId) return { id: null, note: '' };
    const title = courseTitle(courseId);
    const matched = (courses || []).find(course => normalizeTitle(course.title) === normalizeTitle(title));
    if (matched) return { id: matched.id, note: '' };
    return {
      id: null,
      note: `后端没有与「${title}」对应的课程，检索范围暂为全部课程`
    };
  }

  let courseCache = null;

  async function courseList(force = false) {
    if (!courseCache || force) {
      const data = await request('GET', '/api/v1/courses');
      courseCache = data.items || [];
    }
    return courseCache;
  }

  async function submitAttempt({ courseId, exerciseId, answerMd, durationSec }) {
    const matched = matchCourse(courseId, await courseList());
    if (!matched.id) return { status: 'no-course', reason: matched.note };
    const attempt = await request('POST', '/api/v1/attempts', {
      body: {
        course_id: matched.id,
        exercise_id: exerciseId,
        answer_md: answerMd,
        duration_sec: durationSec ?? null
      }
    });
    return { status: 'submitted', attempt };
  }

  // ---------------------------------------------------------------------
  // 教材上传与生成
  //
  // 上传发的是文件原始字节，不是 multipart：后端为此省掉 python-multipart
  // 依赖（服务器上少一个可能装不上的包）。所以这里不能用 request()，
  // 它会 JSON.stringify 请求体。
  // ---------------------------------------------------------------------
  async function uploadTextbook({ file, courseId, title, authors, edition, pageOffset }) {
    const query = new URLSearchParams({
      title,
      course_id: courseId,
      ...(authors ? { authors } : {}),
      ...(edition ? { edition } : {}),
      page_offset: String(pageOffset || 0)
    });
    let response;
    try {
      response = await fetch(`${base()}/api/v1/textbooks?${query}`, {
        method: 'POST',
        headers: headers({ 'Content-Type': 'application/pdf' }),
        body: file
      });
    } catch (cause) {
      throw new ApiError(`无法连接后端（${base() || '同源'}）：${cause.message}`, 0,
        '请确认后端已启动，或在页面上填写正确的后端地址');
    }
    const text = await response.text();
    let data = {};
    if (text) {
      try { data = JSON.parse(text); } catch { data = { detail: text.slice(0, 300) }; }
    }
    if (!response.ok) {
      throw new ApiError(data.detail || `上传失败（HTTP ${response.status}）`, response.status, data.detail);
    }
    return data;
  }

  function startGeneration(textbookId, body) {
    return request('POST', `/api/v1/textbooks/${encodeURIComponent(textbookId)}/generate`, { body });
  }

  // 整本：平台自己切章，逐章生成。切章结果先给老师看一眼，再决定跑不跑。
  function textbookChapters(textbookId, window) {
    const query = window ? `?window=${encodeURIComponent(window)}` : '';
    return request('GET', `/api/v1/textbooks/${encodeURIComponent(textbookId)}/chapters${query}`);
  }

  function startBookGeneration(textbookId, body) {
    return request('POST', `/api/v1/textbooks/${encodeURIComponent(textbookId)}/generate-book`, { body });
  }

  function jobChildren(jobId) {
    return request('GET', `/api/v1/jobs/${encodeURIComponent(jobId)}/children`);
  }

  function retryJob(jobId) {
    return request('POST', `/api/v1/jobs/${encodeURIComponent(jobId)}/retry`, {});
  }

  function job(jobId) {
    return request('GET', `/api/v1/jobs/${encodeURIComponent(jobId)}`);
  }

  function jobList(status) {
    const query = status ? `?status=${encodeURIComponent(status)}` : '';
    return request('GET', `/api/v1/jobs${query}`);
  }

  // ---------------------------------------------------------------------
  // 课程内容（资源包导入 / 导出 / 在线修改）
  //
  // 资源包在浏览器里解析（CourseImport 已经有整套校验），只把解析结果发给后端。
  // 服务端不重复实现 xlsx 与 Markdown 的解析规则，也不用装 openpyxl。
  // ---------------------------------------------------------------------
  function courseContent(courseId, includeRetired = false) {
    const query = includeRetired ? '?include_retired=true' : '';
    return request('GET', `/api/v1/courses/${encodeURIComponent(courseId)}/content${query}`);
  }

  function importContent(courseId, payload) {
    return request('POST', `/api/v1/courses/${encodeURIComponent(courseId)}/content/import`, { body: payload });
  }

  /* 只上传 知识点.md 或 习题.md 时走这条：按锚点改已有内容，结构不动。
     dry_run 默认 true，所以"先看会改什么"和"真改"是同一个接口。 */
  function importDocs(courseId, payload) {
    return request('POST', `/api/v1/courses/${encodeURIComponent(courseId)}/content/import-docs`, { body: payload });
  }

  function exportContent(courseId) {
    return request('GET', `/api/v1/courses/${encodeURIComponent(courseId)}/content/export`);
  }

  function patchNode(courseId, nodeId, body) {
    return request('PATCH', `/api/v1/courses/${encodeURIComponent(courseId)}/nodes/${encodeURIComponent(nodeId)}`, { body });
  }

  function patchExercise(courseId, exerciseId, body) {
    return request('PATCH', `/api/v1/courses/${encodeURIComponent(courseId)}/exercises/${encodeURIComponent(exerciseId)}`, { body });
  }

  function createNode(courseId, body) {
    return request('POST', `/api/v1/courses/${encodeURIComponent(courseId)}/nodes`, { body });
  }

  function createExercise(courseId, body) {
    return request('POST', `/api/v1/courses/${encodeURIComponent(courseId)}/exercises`, { body });
  }

  function retireNode(courseId, nodeId) {
    return request('POST', `/api/v1/courses/${encodeURIComponent(courseId)}/nodes/${encodeURIComponent(nodeId)}/retire`, {});
  }

  function retireExercise(courseId, exerciseId) {
    return request('POST', `/api/v1/courses/${encodeURIComponent(courseId)}/exercises/${encodeURIComponent(exerciseId)}/retire`, {});
  }

  // 库里有哪些课（含未发布）及其状态。目录页据此区分"学生能看的"和"建设中"。
  // 注意：这两个接口都返回 {items: [...]}。以前目录合并直接拿返回值当数组用
  // （.length / .map），永远是 undefined，于是"库里的已发布课程覆盖静态包"那段
  // 从来没执行过——库改了学生端不认，根子在这儿。所以统一在这里拆包。
  function courses() {
    return request('GET', '/api/v1/courses').then(data => data.items || []);
  }

  function catalog() {
    return request('GET', '/api/v1/catalog').then(data => data.items || []);
  }

  // ---------------------------------------------------------------------
  // 学生端发布出口
  //
  // 目录接口不带鉴权（学生要能读），只返回已发布课程。前端启动时把它合并进
  // A.courses：库里有已发布内容的课程覆盖静态包，没有的继续用静态包。
  // ---------------------------------------------------------------------
  function catalogCourse(courseId) {
    return request('GET', `/api/v1/catalog/${encodeURIComponent(courseId)}`);
  }

  function publishStatus(courseId) {
    return request('GET', `/api/v1/courses/${encodeURIComponent(courseId)}/publish/status`);
  }

  function publishCourse(courseId, note) {
    return request('POST', `/api/v1/courses/${encodeURIComponent(courseId)}/publish`, { body: note ? { note } : {} });
  }

  function unpublishCourse(courseId, note) {
    return request('POST', `/api/v1/courses/${encodeURIComponent(courseId)}/unpublish`, { body: note ? { note } : {} });
  }

  // 学生可见范围：验证等级闸。courseId 空 = 全局口径，非空 = 这门课的覆盖。
  function visibility(courseId) {
    const suffix = courseId ? `?course_id=${encodeURIComponent(courseId)}` : '';
    return request('GET', `/api/v1/visibility${suffix}`);
  }

  function saveVisibility(policy, courseId) {
    const suffix = courseId ? `?course_id=${encodeURIComponent(courseId)}` : '';
    return request('PUT', `/api/v1/visibility${suffix}`, { body: policy });
  }

  // 取消某门课的单独设置，回到跟随全局。
  function resetVisibility(courseId) {
    return request('DELETE', `/api/v1/visibility?course_id=${encodeURIComponent(courseId)}`);
  }

  // 知识图谱人工审核：列表 + 按章/按节点标记。
  function reviewGraph(courseId) {
    const suffix = courseId ? `?course_id=${encodeURIComponent(courseId)}` : '';
    return request('GET', `/api/v1/review/graph${suffix}`);
  }

  // ---------------- 知识图谱的节点管理 ----------------

  function nodeDetail(courseId, nodeId) {
    return request('GET', `/api/v1/review/nodes/${encodeURIComponent(courseId)}`
      + `/${encodeURIComponent(nodeId)}`);
  }

  function mountExercises(courseId, nodeId, exerciseIds) {
    return request('POST', `/api/v1/review/nodes/${encodeURIComponent(courseId)}`
      + `/${encodeURIComponent(nodeId)}/exercises`, { body: { exercise_ids: exerciseIds } });
  }

  function unmountExercise(courseId, nodeId, exerciseId) {
    return request('DELETE', `/api/v1/review/nodes/${encodeURIComponent(courseId)}`
      + `/${encodeURIComponent(nodeId)}/exercises/${encodeURIComponent(exerciseId)}`);
  }

  function deleteNode(courseId, nodeId, cascade) {
    const suffix = cascade ? '?cascade=true' : '';
    return request('DELETE', `/api/v1/review/nodes/${encodeURIComponent(courseId)}`
      + `/${encodeURIComponent(nodeId)}${suffix}`);
  }

  function nodeOptions(courseId, query) {
    const suffix = query ? `&q=${encodeURIComponent(query)}` : '';
    return request('GET', `/api/v1/review/node-options?course_id=${encodeURIComponent(courseId)}${suffix}`);
  }

  function exerciseOptions(courseId, query) {
    const suffix = query ? `&q=${encodeURIComponent(query)}` : '';
    return request('GET', `/api/v1/review/exercise-options?course_id=${encodeURIComponent(courseId)}&limit=10${suffix}`);
  }

  function verifyGraph(payload) {
    return request('POST', '/api/v1/review/graph/verify', { body: payload });
  }

  // ---------------------------------------------------------------------
  // 登录与会话
  // ---------------------------------------------------------------------

  function providers() {
    return request('GET', '/api/v1/auth/providers', { noAuthRedirect: true });
  }

  function me() {
    return request('GET', '/api/v1/auth/me', { noAuthRedirect: true });
  }

  function login(username, password, next) {
    return request('POST', '/api/v1/auth/login', {
      body: { username, password, next: next || null },
      noAuthRedirect: true
    });
  }

  function logout() {
    return request('POST', '/api/v1/auth/logout', { noAuthRedirect: true });
  }

  function changePassword(currentPassword, newPassword) {
    return request('POST', '/api/v1/auth/password', {
      body: { current_password: currentPassword, new_password: newPassword },
      noAuthRedirect: true
    });
  }

  function accounts(params) {
    const query = new URLSearchParams();
    Object.entries(params || {}).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== '') query.set(key, value);
    });
    return request('GET', `/api/v1/auth/accounts?${query.toString()}`);
  }

  function createAccount(payload) {
    return request('POST', '/api/v1/auth/accounts', { body: payload });
  }

  function importAccounts(payload) {
    return request('POST', '/api/v1/accounts/import', { body: payload });
  }

  function patchAccount(accountId, payload) {
    return request('PATCH', `/api/v1/auth/accounts/${encodeURIComponent(accountId)}`, { body: payload });
  }

  // ---- 班级与选课（教师 / 管理员） ----
  function queryString(params) {
    const query = new URLSearchParams();
    Object.entries(params || {}).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== '') query.set(key, value);
    });
    const text = query.toString();
    return text ? `?${text}` : '';
  }
  function classes(params) {
    return request('GET', `/api/v1/classes${queryString(params)}`);
  }
  function createClass(payload) {
    return request('POST', '/api/v1/classes', { body: payload });
  }
  function patchClass(classId, payload) {
    return request('PATCH', `/api/v1/classes/${encodeURIComponent(classId)}`, { body: payload });
  }
  function deleteClass(classId) {
    return request('DELETE', `/api/v1/classes/${encodeURIComponent(classId)}`);
  }
  function importClasses(payload) {
    return request('POST', '/api/v1/classes/import', { body: payload });
  }
  function importStudents(classId, payload) {
    return request('POST',
      `/api/v1/classes/${encodeURIComponent(classId)}/students/import`, { body: payload });
  }
  function classStudents(classId) {
    return request('GET', `/api/v1/classes/${encodeURIComponent(classId)}/students`);
  }
  function addStudents(classId, studentIds) {
    return request('POST', `/api/v1/classes/${encodeURIComponent(classId)}/students`,
      { body: { student_ids: studentIds } });
  }
  function removeStudent(classId, studentId) {
    return request('DELETE',
      `/api/v1/classes/${encodeURIComponent(classId)}/students/${encodeURIComponent(studentId)}`);
  }
  function students(params) {
    return request('GET', `/api/v1/students${queryString(params)}`);
  }

  // 一门课全部题目的审核总表（不是队列：含已发布、含没人审过的）。
  function reviewLedger(params) {
    const query = new URLSearchParams();
    Object.entries(params || {}).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== '') query.set(key, value);
    });
    return request('GET', `/api/v1/review/ledger?${query.toString()}`);
  }

  // 单道题的审核记录：等级、谁在什么时候审的、Lean 状态、审核后有没有被改。
  function exerciseVerification(courseId, exerciseId) {
    return request('GET', `/api/v1/review/exercises/${encodeURIComponent(courseId)}`
      + `/${encodeURIComponent(exerciseId)}/verification`);
  }

  A.api = {
    ApiError,
    base,
    courseList,
    matchCourse,
    submitAttempt,
    uploadTextbook,
    startGeneration,
    textbookChapters,
    startBookGeneration,
    jobChildren,
    retryJob,
    job,
    jobList,
    courseContent,
    importContent,
    importDocs,
    exportContent,
    patchNode,
    patchExercise,
    createNode,
    createExercise,
    retireNode,
    retireExercise,
    courses,
    catalog,
    catalogCourse,
    publishStatus,
    publishCourse,
    unpublishCourse,
    visibility,
    saveVisibility,
    resetVisibility,
    reviewGraph,
    nodeDetail,
    mountExercises,
    unmountExercise,
    deleteNode,
    nodeOptions,
    exerciseOptions,
    verifyGraph,
    reviewLedger,
    exerciseVerification,
    providers,
    me,
    login,
    logout,
    changePassword,
    accounts,
    classes, createClass, patchClass, deleteClass,
    classStudents, addStudents, removeStudent, students,
    createAccount,
    patchAccount,
    importAccounts, importClasses, importStudents,
    get: (path, options) => request('GET', path, options),
    post: (path, body, options) => request('POST', path, { ...options, body }),
    patch: (path, body, options) => request('PATCH', path, { ...options, body }),
    put: (path, body, options) => request('PUT', path, { ...options, body }),
    del: (path, options) => request('DELETE', path, options)
  };

  // 后端地址输入框在配置页与审核页都会出现，处理逻辑只写一份，
  // 避免两处各存一份 A.local.apiBase 的写入口径。
  A.inputs['api-base'] = function (input) {
    A.local.apiBase = input.value.trim();
    courseCache = null;
    A.save();
  };
})();
