'use strict';
/* 模型接口配置界面。
 *
 * 权限的双轨设计（这点必须说清楚，否则会误以为前端拦住了什么）：
 *   前端：根据 /whoami 返回的角色把按钮置灰，避免"点了才报 403"的挫败感；
 *   后端：每个写接口都有 require("admin")，前端置灰只是体验，不是安全边界。
 *
 * 密钥：界面只能写入和删除，永远不回显。列表里只显示"已配置/未配置"。
 */
(function () {
  const A = App, e = A.escape;

  A.state.apiConfig = A.state.apiConfig || { status: 'idle', items: [], me: null, error: '' };

  function base() { return (A.local.apiBase || '').replace(/\/+$/, ''); }
  function actorId() { return A.local.devActor === false ? '平台用户' : (A.local.actorId || '信息化专员'); }
  function roles() { return A.local.devActor === false ? [] : (A.local.actorRoles || ['admin']); }

  async function api(path, options = {}) {
    const headers = { 'Content-Type': 'application/json' };
    if (A.local.devActor !== false) {
      headers['X-Actor'] = actorId();
      headers['X-Roles'] = roles().join(',');
    }
    const response = await fetch(`${base()}${path}`, { ...options, headers });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || `请求失败（HTTP ${response.status}）`);
    return data;
  }

  async function load() {
    const state = A.state.apiConfig;
    if (state.status === 'loading') return;
    state.status = 'loading'; state.error = '';
    A.render();
    try {
      const [me, list] = await Promise.all([
        api('/api/v1/admin/whoami'),
        api('/api/v1/admin/model-endpoints')
      ]);
      state.me = me; state.items = list.items || []; state.status = 'ready';
    } catch (error) {
      // 失败后停在 error 状态。若这里置回 idle，A.after 会立刻重新请求，
      // 后端没起来时会变成死循环，页面也会一直闪。
      state.error = error.message; state.status = 'error';
    }
    A.render();
  }

  function reload() {
    A.state.apiConfig.status = 'idle';
    load();
  }

  function canEdit() { return !!A.state.apiConfig.me?.can_edit; }

  function scopeBadge(item) {
    return item.data_scope === 'internal'
      ? A.badge('校内', 'green')
      : A.badge('出校', 'amber');
  }

  function keyCell(item) {
    if (!item.key_configured) return A.badge('未配置', 'amber');
    return `<span class="api-key-ok">${A.icon('check')}已配置</span>`;
  }

  function testCell(item) {
    if (!item.last_test_at) return '<span class="muted">未自检</span>';
    const when = new Date(item.last_test_at).toLocaleString('zh-CN', { hour12: false });
    return item.last_test_ok
      ? `<span class="api-test-ok" title="${e(item.last_test_note || '')}">通过 · ${e(when)}</span>`
      : `<span class="api-test-fail" title="${e(item.last_test_note || '')}">失败 · ${e(when)}</span>`;
  }

  function row(item) {
    const disabled = canEdit() ? '' : 'disabled';
    return `<tr>
      <td><b>${e(item.name)}</b><small>${item.kind === 'chat' ? '对话' : '向量'}${item.enabled ? ' · 启用中' : ''}</small></td>
      <td><code>${e(item.model)}</code><small>${e(item.base_url)}</small></td>
      <td>${scopeBadge(item)}${item.kind === 'embedding' ? `<small>${e(item.dim || 1024)} 维</small>` : ''}</td>
      <td>${keyCell(item)}</td>
      <td>${testCell(item)}</td>
      <td class="api-row-actions">
        <button class="btn small" data-action="api-config-test" data-name="${e(item.name)}" ${disabled}>自检</button>
        <button class="btn small" data-action="api-config-key" data-name="${e(item.name)}" ${disabled}>密钥</button>
        <button class="btn small" data-action="api-config-edit" data-name="${e(item.name)}" ${disabled}>编辑</button>
        <button class="btn small ${item.enabled ? 'text-btn' : ''}" data-action="api-config-toggle" data-name="${e(item.name)}" data-enabled="${item.enabled}" ${disabled}>${item.enabled ? '停用' : '启用'}</button>
      </td>
    </tr>`;
  }

  function connectPanel(error) {
    return `<section class="panel">
      <h2>先连接后端 API</h2>
      <p class="muted">这个页面配置的是服务器上的模型接口，需要后端运行才能读写。当前请求失败：<code>${e(error)}</code></p>
      <div class="api-base-row">
        <label for="api-base-input">后端地址</label>
        <input id="api-base-input" data-input="api-base" value="${e(base())}" placeholder="http://127.0.0.1:8000" autocomplete="off">
        <button class="btn primary" data-action="api-config-refresh">重试</button>
      </div>
      <p class="muted small">本地启动：<code>uvicorn app.main:app --app-dir server --reload</code>。留空表示同源访问。</p>
    </section>`;
  }

  function identityPanel() {
    const me = A.state.apiConfig.me;
    if (!me) return '';
    const devMode = A.local.devActor !== false;
    const current = (A.local.actorRoles || ['admin']).join(',');
    const switcher = devMode
      ? `<div class="api-role-switch">
          <span>开发期预览角色：</span>
          ${[['admin', '管理员'], ['reviewer', '审核员'], ['student', '学生（无权限）']]
            .map(([role, label]) => `<button class="chip ${current === role ? 'active' : ''}" data-action="api-config-role" data-roles="${role}">${label}</button>`)
            .join('')}
        </div>`
      : '';
    return `<div class="notice api-actor">
      ${A.icon('info')}
      <div>
        <span><b>${e(me.actor)}</b> · 角色 ${me.roles.length ? me.roles.map(r => `<code>${e(r)}</code>`).join(' ') : '无'}
        · ${me.can_edit ? '可修改' : '只读'}</span>
        ${devMode ? '<span class="api-dev-flag">开发模式：身份取自请求头，生产由统一认证颁发</span>' : ''}
        ${switcher}
      </div>
    </div>`;
  }

  A.views['api-config'] = function () {
    const state = A.state.apiConfig;
    const heading = A.sectionTitle(
      'MODEL GATEWAY',
      '模型接口配置',
      '管理生成与向量模型。密钥只写入服务器文件，界面永不回显。',
      A.button('刷新', 'api-config-refresh', 'refresh')
    );

    if (state.status === 'loading' && !state.items.length) {
      return heading + `<div class="panel"><p class="muted">正在读取配置…</p></div>`;
    }
    if (state.status === 'error' && !state.items.length) {
      return heading + connectPanel(state.error);
    }

    return heading + identityPanel() + `
      <section class="panel">
        <div class="panel-heading">
          <h2>已登记的端点</h2>
          <div class="button-row">
            ${A.button('新增端点', 'api-config-new', 'plus', canEdit() ? '' : 'disabled', 'primary')}
          </div>
        </div>
        ${state.items.length ? `<div class="api-table-wrap"><table class="api-table">
          <thead><tr><th>名称 / 用途</th><th>模型 / 地址</th><th>数据范围</th><th>密钥</th><th>最近自检</th><th></th></tr></thead>
          <tbody>${state.items.map(row).join('')}</tbody>
        </table></div>` : A.empty('settings', '还没有登记任何端点', '先新增一个对话端点（如 DeepSeek），再新增一个向量端点（如百炼 qwen），填好密钥后点“自检”。')}
      </section>

      <div class="settings-grid">
        <section class="panel">
          <h2>三条固定规则</h2>
          <div class="setting-row"><div><b>维度固定 1024</b><p>数据库里是 vector(1024)，改维度要重建整列并重算全库向量，所以界面不允许改成别的值。</p></div></div>
          <div class="setting-row"><div><b>出校端点必须写说明</b><p>审计会问“为什么教材内容会出现在这家公司的服务器上”，说明字段就是答案。</p></div></div>
          <div class="setting-row"><div><b>同类只能启用一个</b><p>同时只能有一个对话端点、一个向量端点，避免“到底在用哪个模型”说不清。</p></div></div>
        </section>
        <section class="panel">
          <h2>权限</h2>
          <div class="setting-row"><div><b>管理员</b><p>新增、修改、启停、配密钥、自检。</p></div>${A.badge('可写', 'green')}</div>
          <div class="setting-row"><div><b>审核员 / 教师</b><p>只能查看当前用的是哪个模型，看不到密钥。</p></div>${A.badge('只读', 'amber')}</div>
          <div class="setting-row"><div><b>身份来源</b><p>${e(A.local.devActor === false ? '统一认证（未接入时不可用）' : '开发模式：请求头，仅本地使用')}</p></div></div>
        </section>
      </div>`;
  };

  A.after['api-config'] = function () {
    const state = A.state.apiConfig;
    if (state.status === 'idle') load();
  };

  // ---- 权限切换（开发期用于验证只读分支）----
  A.actions['api-config-refresh'] = function () {
    reload();
  };
  A.actions['api-config-role'] = function (button) {
    A.local.actorRoles = button.dataset.roles ? button.dataset.roles.split(',') : [];
    A.save();
    reload();
  };  function formBody(item) {
    const isNew = !item;
    const value = (key, fallback = '') => e(item?.[key] ?? fallback);
    return `<div class="api-form">
      <label>名称<input id="f-name" value="${value('name')}" ${isNew ? '' : 'disabled'} placeholder="deepseek-chat / bailian-embedding"></label>
      <label>用途<select id="f-kind" ${isNew ? '' : 'disabled'}>
        <option value="chat" ${item?.kind === 'chat' ? 'selected' : ''}>对话（生成回答）</option>
        <option value="embedding" ${item?.kind === 'embedding' ? 'selected' : ''}>向量（教材检索）</option>
      </select></label>
      <label>接口地址<input id="f-base-url" value="${value('base_url')}" placeholder="https://api.deepseek.com"></label>
      <label>模型标识<input id="f-model" value="${value('model')}" placeholder="deepseek-flash / qwen3.7-text-embedding"></label>
      <label>数据范围<select id="f-data-scope">
        <option value="external" ${item?.data_scope !== 'internal' ? 'selected' : ''}>出校（调用校外 API）</option>
        <option value="internal" ${item?.data_scope === 'internal' ? 'selected' : ''}>校内（私网 / *.edu.cn）</option>
      </select></label>
      <label>向量维度<input id="f-dim" type="number" value="${value('dim', item?.kind === 'embedding' ? 1024 : '')}" ${isNew ? '' : ''} placeholder="向量端点填 1024"></label>
      <label>最大上下文<input id="f-max-context" type="number" value="${value('max_context')}" placeholder="可选，如 32768"></label>
      <label class="api-form-wide">说明（出校端点必填）<textarea id="f-notes" rows="2" placeholder="为什么允许它接收校内内容">${value('notes')}</textarea></label>
      <label class="api-form-wide api-checkbox"><input type="checkbox" id="f-enabled" ${item ? (item.enabled ? 'checked' : '') : 'checked'}> 保存后立即启用</label>
    </div>
    <div class="button-row api-form-actions">
      <button class="btn primary" data-action="api-config-save" data-mode="${isNew ? 'create' : 'edit'}" data-name="${e(item?.name || '')}">保存</button>
      <button class="btn text-btn" data-action="close-dialog">取消</button>
    </div>`;
  }

  A.actions['api-config-new'] = function () {
    A.openDialog('新增模型端点', formBody(null));
  };

  A.actions['api-config-edit'] = function (button) {
    const item = A.state.apiConfig.items.find(row => row.name === button.dataset.name);
    if (!item) return;
    A.openDialog(`编辑 ${e(item.name)}`, formBody(item));
  };

  A.actions['api-config-save'] = async function (button) {
    const mode = button.dataset.mode;
    const read = id => document.getElementById(id);
    const payload = {
      base_url: read('f-base-url').value.trim(),
      model: read('f-model').value.trim(),
      data_scope: read('f-data-scope').value,
      max_context: Number(read('f-max-context').value) || null,
      notes: read('f-notes').value.trim() || null,
      enabled: read('f-enabled').checked
    };
    const kind = read('f-kind').value;
    const dimRaw = read('f-dim').value.trim();
    if (kind === 'embedding') payload.dim = Number(dimRaw) || null;
    if (mode === 'create') {
      payload.name = read('f-name').value.trim();
      payload.kind = kind;
    }
    button.disabled = true;
    try {
      if (mode === 'create') {
        await api('/api/v1/admin/model-endpoints', { method: 'POST', body: JSON.stringify(payload) });
        A.toast('端点已登记，接着配置密钥');
      } else {
        await api(`/api/v1/admin/model-endpoints/${encodeURIComponent(button.dataset.name)}`, {
          method: 'PATCH', body: JSON.stringify(payload)
        });
        A.toast('已保存');
      }
      document.getElementById('dialog').close();
      reload();
    } catch (error) {
      A.toast(error.message);
      button.disabled = false;
    }
  };

  A.actions['api-config-key'] = function (button) {
    const name = button.dataset.name;
    A.openDialog(`配置密钥 · ${e(name)}`, `
      <p class="muted">密钥写入服务器 <code>secrets/${e(name)}.key</code>（权限 600），不存数据库、不回显、不写日志。迁移服务器时要单独搬这个目录。</p>
      <label class="api-form-wide">API Key<input id="f-api-key" type="password" autocomplete="new-password" placeholder="粘贴后保存，不会显示第二次"></label>
      <div class="button-row api-form-actions">
        <button class="btn primary" data-action="api-config-key-save" data-name="${e(name)}">保存密钥</button>
        <button class="btn text-btn" data-action="api-config-key-clear" data-name="${e(name)}">删除密钥</button>
        <button class="btn text-btn" data-action="close-dialog">取消</button>
      </div>`);
  };

  A.actions['api-config-key-save'] = async function (button) {
    const value = document.getElementById('f-api-key').value.trim();
    if (value.length < 8) { A.toast('密钥太短'); return; }
    try {
      await api(`/api/v1/admin/model-endpoints/${encodeURIComponent(button.dataset.name)}/key`, {
        method: 'PUT', body: JSON.stringify({ api_key: value })
      });
      document.getElementById('dialog').close();
      A.toast('密钥已保存（不会再显示）');
      reload();
    } catch (error) { A.toast(error.message); }
  };

  A.actions['api-config-key-clear'] = async function (button) {
    try {
      await api(`/api/v1/admin/model-endpoints/${encodeURIComponent(button.dataset.name)}/key`, { method: 'DELETE' });
      document.getElementById('dialog').close();
      A.toast('密钥已删除');
      reload();
    } catch (error) { A.toast(error.message); }
  };

  A.actions['api-config-test'] = async function (button) {
    button.disabled = true;
    button.textContent = '自检中…';
    try {
      const result = await api(`/api/v1/admin/model-endpoints/${encodeURIComponent(button.dataset.name)}/test`, { method: 'POST' });
      A.toast(result.ok ? `自检通过：${result.note}` : `自检失败：${result.note}`);
      reload();
    } catch (error) {
      A.toast(error.message);
      button.disabled = false;
      button.textContent = '自检';
    }
  };

  A.actions['api-config-toggle'] = async function (button) {
    const enable = button.dataset.enabled !== 'true';
    try {
      await api(`/api/v1/admin/model-endpoints/${encodeURIComponent(button.dataset.name)}`, {
        method: 'PATCH', body: JSON.stringify({ enabled: enable })
      });
      A.toast(enable ? '已启用' : '已停用');
      reload();
    } catch (error) { A.toast(error.message); }
  };

  A.inputs['api-base'] = function (input) {
    A.local.apiBase = input.value.trim();
    A.save();
  };
})();
