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

  function devMode() {
    return A.local.devActor !== false;
  }

  function headers(extra) {
    const result = { 'Content-Type': 'application/json', ...(extra || {}) };
    if (devMode()) {
      // 头值只能是 ASCII：中文姓名必须编码，服务端 unquote 还原
      result['X-Actor'] = encodeURIComponent(A.local.actorId || '信息化专员');
      result['X-Roles'] = (A.local.actorRoles || ['admin']).join(',');
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
      throw new ApiError(data.detail || `请求失败（HTTP ${response.status}）`, response.status, data.detail);
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
    exportContent,
    patchNode,
    patchExercise,
    createNode,
    createExercise,
    retireNode,
    retireExercise,
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
