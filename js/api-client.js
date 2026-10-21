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

  A.api = {
    ApiError,
    base,
    get: (path, options) => request('GET', path, options),
    post: (path, body, options) => request('POST', path, { ...options, body }),
    patch: (path, body, options) => request('PATCH', path, { ...options, body }),
    put: (path, body, options) => request('PUT', path, { ...options, body }),
    del: (path, options) => request('DELETE', path, options)
  };
})();
