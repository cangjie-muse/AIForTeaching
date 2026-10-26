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

  A.state.apiConfig = A.state.apiConfig || {
    status: 'idle', items: [], me: null, error: '',
    // 问答语料：corpus 为 null 表示没读到（页面照常显示端点，不整页失败）
    corpus: null, corpusError: '', corpusBusy: false, corpusNote: '',
    // 平台开关：null 表示没读到；forced=true 表示被 .env 一票否决
    qaFallback: null, qaFallbackError: '', qaFallbackBusy: false,
    // 出校域名：added 是页面上加的（可撤销），builtin 是 .env/默认的（改不了）
    hosts: null, hostsError: '', hostsBusy: false, hostsNote: '' 
  };

  async function load() {
    const state = A.state.apiConfig;
    if (state.status === 'loading') return;
    state.status = 'loading'; state.error = '';
    state.corpusBusy = false;
    A.render();
    try {
      const [me, list, corpus, fallback, hosts] = await Promise.all([
        A.api.get('/api/v1/admin/whoami'),
        A.api.get('/api/v1/admin/model-endpoints'),
        // 语料读不到不该拖垮整页：端点配置本身是能用的
        A.api.get('/api/v1/admin/qa-corpus').catch(error => ({ __error: error.message })),
        A.api.get('/api/v1/admin/settings/qa-fallback').catch(error => ({ __error: error.message })),
        A.api.get('/api/v1/admin/settings/approved-hosts').catch(error => ({ __error: error.message }))
      ]);
      state.me = me; state.items = list.items || []; state.status = 'ready';
      if (corpus && !corpus.__error) {
        state.corpus = corpus; state.corpusError = '';
      } else {
        state.corpus = null; state.corpusError = corpus?.__error || '';
      }
      if (fallback && !fallback.__error) {
        state.qaFallback = fallback; state.qaFallbackError = '';
      } else {
        state.qaFallback = null; state.qaFallbackError = fallback?.__error || '';
      }
      if (hosts && !hosts.__error) {
        state.hosts = hosts; state.hostsError = '';
      } else {
        state.hosts = null; state.hostsError = hosts?.__error || '';
      }
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
  const KIND_LABEL = { chat: '对话', embedding: '向量', vision: '视觉', rerank: '重排' };
  const kindLabel = kind => KIND_LABEL[kind] || kind;

    return `<tr>
      <td><b>${e(item.name)}</b><small>${kindLabel(item.kind)}${item.enabled ? ' · 启用中' : ''}</small></td>
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
        <input id="api-base-input" data-input="api-base" value="${e(A.api.base())}" placeholder="http://127.0.0.1:8000" autocomplete="off">
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

  function corpusRow(item) {
    const ready = (item.chunks || 0) > 0;
    return `<tr>
      <td><b>${e(item.title || item.course_id)}</b><small>${e(item.course_id)}</small></td>
      <td>${item.nodes_with_text} / ${item.nodes}</td>
      <td>${ready ? item.chunks : '<span class="muted">0</span>'}</td>
      <td>${ready ? A.badge('已入库', 'green') : A.badge('未入库', 'amber')}</td>
    </tr>`;
  }

  /* 问答语料面板。
   *
   * 这段是 2026-10-22 "问答说没有知识片段"的直接产物：那一页会拒答，唯一原因
   * 是 textbook_chunk 一行都没有，而界面上又看不到这件事。所以把"每门课切了
   * 多少块"摆出来，并给管理员一个按钮——否则用户只能靠猜。 */

  /* 出校域名：为什么它要出现在这里。
   *
   * 每换一家模型供应商就要登服务器改 .env 再重启，这一步以前完全卡在运维身上。
   * 但"允许谁接收课程内容"本身是个教学决定，该由管理员当场拍板；
   * 所以搬到页面上，配一句必须填的理由，写进 audit_log。
   *
   * 内置白名单单独列、且不给撤销按钮：它来自 .env，页面上假装能撤只是骗人。 */
  /* 三种提示各占一行：拆出来既是为了复杂度，也是为了"什么情况下说什么话"能单独看。 */
  function hostsNotice(state) {
    if (!state.hostsNote) return '';
    return `<p class="${state.hostsBusy ? 'muted' : 'api-note'}">${e(state.hostsNote)}</p>`;
  }

  function hostsLock(data) {
    return data.can_edit ? '' : `<p class="api-warn">${e(data.reason)}</p>`;
  }

  function hostsAffected(data, hasBlocked) {
    const names = data.affected || [];
    if (!hasBlocked || !names.length) return '';
    return `<p class="api-warn">⚠️ 正在使用的端点被屏蔽了：${names.map(e).join('、')}。
      问答与自检会立刻报「供应商已被管理员屏蔽」，恢复后才可用。</p>`;
  }

  /* 一张"域名 / 理由 / 操作人 / 时间 / 按钮"的表。两处用它，只差按钮文案。 */
  function hostRows(items, action, label, enabled, actorField = 'added_by') {
    if (!items.length) {
      return `<tr><td colspan="5" class="muted">还没有在页面上加过域名。下面内置的可用，换新供应商时才需要加。</td></tr>`;
    }
    return items.map(item => `<tr>
      <td><code>${e(item.host)}</code></td>
      <td>${e(item.note || '（没写理由）')}</td>
      <td class="muted">${e(item[actorField] || '')}</td>
      <td class="muted">${item.created_at ? e(String(item.created_at).slice(0, 10)) : ''}</td>
      <td><button class="btn small text-btn" data-action="${action}"
                  data-host="${e(item.host)}" ${enabled ? '' : 'disabled'}>${label}</button></td>
    </tr>`).join('');
  }

  /* 内置域名来自 .env，页面改不了文件，但可以屏蔽——把它当成"临时撤掉"。
     按钮写"屏蔽"而不是"删除"，是因为文件里那一行还在，说删除是骗人的。 */
  function builtinRows(hosts, enabled) {
    if (!hosts.length) return '<tr><td colspan="2" class="muted">空</td></tr>';
    return hosts.map(host => `<tr>
      <td><code>${e(host)}</code></td>
      <td><button class="btn small text-btn" data-action="api-config-host-prefill"
                  data-host="${e(host)}" ${enabled ? '' : 'disabled'}>屏蔽</button></td>
    </tr>`).join('');
  }

  function hostsPanel() {
    const state = A.state.apiConfig;
    const data = state.hosts;
    const head = `<div class="panel-heading">
        <h2>出校域名</h2>
        <div class="button-row">
          ${data ? A.badge(data.mode === 'allowlist' ? '白名单模式' : data.mode, data.can_edit ? '' : 'amber') : ''}
        </div>
      </div>`;

    if (state.hostsError || !data) {
      return `<section class="panel">${head}
        <p class="muted">${state.hostsError
          ? `读取失败：<code>${e(state.hostsError)}</code>（不影响上面的端点配置）`
          : '正在读取白名单…'}</p>
      </section>`;
    }

    const editable = canEdit() && data.can_edit && !state.hostsBusy;
    // 屏蔽只会让数据少发出去，所以它不受 internal-only 限制
    const canBlock = canEdit() && !state.hostsBusy;
    const blockedRows = data.blocked?.length
      ? hostRows(data.blocked, 'api-config-host-unblock', '恢复', canBlock, 'blocked_by')
      : '';

    return `<section class="panel">${head}
      <p class="muted">换一家模型供应商时，先把它的域名加进来，不用登服务器也不用重启。
      加完立刻能用；撤销、屏蔽也都是立刻生效。</p>
      ${hostsLock(data)}

      <div class="api-base-row">
        <label for="host-input">新域名</label>
        <input id="host-input" data-input="approved-host" placeholder="api.example.com"
               autocomplete="off" value="${e(state.draftHost || '')}" ${editable ? '' : 'disabled'}>
        <label for="host-note">理由</label>
        <input id="host-note" data-input="approved-host-note" placeholder="例：SophNet，替代火山方舟"
               autocomplete="off" value="${e(state.draftNote || '')}" ${editable ? '' : 'disabled'}>
        <button class="btn primary" data-action="api-config-host-add" ${editable ? '' : 'disabled'}>添加</button>
        <button class="btn" data-action="api-config-host-block" ${canBlock ? '' : 'disabled'}>屏蔽</button>
      </div>
      <p class="muted small">只填域名（<code>api.example.com</code>）。粘整条地址也行，会自动去掉
      <code>https://</code> 和后面的路径。通配符、IP、校内域名会被拒绝。</p>
      ${hostsNotice(state)}

      <h3 class="api-subhead">页面上加的</h3>
      <div class="api-table-wrap"><table class="api-table">
        <thead><tr><th>域名</th><th>理由</th><th>添加人</th><th>时间</th><th></th></tr></thead>
        <tbody>${hostRows(data.added || [], 'api-config-host-remove', '撤销', editable)}</tbody>
      </table></div>

      ${blockedRows ? `<h3 class="api-subhead">已屏蔽（优先于上面的一切）</h3>
      <div class="api-table-wrap"><table class="api-table">
        <thead><tr><th>域名</th><th>理由</th><th>操作人</th><th>时间</th><th></th></tr></thead>
        <tbody>${blockedRows}</tbody>
      </table></div>
      ${hostsAffected(data, true)}` : ''}

      <h3 class="api-subhead">内置（来自服务器 .env）</h3>
      <p class="muted small">这些改不了 .env，但可以屏蔽：屏蔽后立刻失效，需要时再恢复。</p>
      <div class="api-table-wrap"><table class="api-table">
        <thead><tr><th>域名</th><th></th></tr></thead>
        <tbody>${builtinRows(data.builtin || [], canBlock)}</tbody>
      </table></div>
    </section>`;
  }

  function corpusPanel() {
    const state = A.state.apiConfig;
    const corpus = state.corpus;
    const disabled = canEdit() ? '' : 'disabled';
    // 缺语料 = 有正文的知识点，但一块都没切出来。这些课问答一律拒答，
    // 而它们看起来"导入成功了"，最容易被当成模型不 work。
    const items = (corpus && corpus.items) || [];
    const missing = items.filter(item => Number(item.nodes_with_text || 0) > 0
      && Number(item.chunks || 0) === 0);
    const head = `<div class="panel-heading">
        <h2>问答语料</h2>
        <div class="button-row">
          ${missing.length ? A.button(`补齐缺语料的课（${missing.length}）`,
            'api-config-corpus-fill', 'refresh',
            state.corpusBusy ? 'disabled' : disabled, 'primary') : ''}
          ${A.button('全部重建', 'api-config-corpus-rebuild', 'refresh', state.corpusBusy ? 'disabled' : disabled, missing.length ? 'light' : 'primary')}
        </div>
      </div>`;

    if (!corpus) {
      return `<section class="panel">${head}
        <p class="muted">${state.corpusError
          ? `读取语料状态失败：<code>${e(state.corpusError)}</code>（不影响上面的端点配置）`
          : '正在读取语料状态…'}</p>
      </section>`;
    }


    const note = state.corpusNote
      ? `<p class="corpus-note">${e(state.corpusNote)}</p>`
      : '';
    return `<section class="panel">${head}
      <p class="muted">问答只会引用这里切出来的片段：教材 PDF 切片带页码，知识点正文切成
        「知识点」块（没有精确页码，引用只标知识点路径）。一门课一块都没有时，
        问答会直接说"没有依据"，不猜。<b>当前 ${corpus.courses_ready} / ${items.length} 门课有语料，共 ${corpus.total_chunks} 块。</b></p>
      ${missing.length ? `<p class="corpus-note warn">有 ${missing.length} 门课导入了正文却没切过片段：
        ${missing.map(item => item.title || item.course_id).slice(0, 8).join('、')}
        ${missing.length > 8 ? '…' : ''}。这些课的问答会直接拒答，点「补齐缺语料的课」即可。</p>` : ''}
      ${note}
      ${items.length ? `<div class="api-table-wrap"><table class="api-table">
        <thead><tr><th>课程</th><th>有正文的知识点</th><th>片段</th><th>状态</th></tr></thead>
        <tbody>${items.map(corpusRow).join('')}</tbody>
      </table></div>` : A.empty('book', '还没有课程', '先在课程内容工作台导入课程，再回到这里切语料。')}
    </section>`;
  }

  /* 平台开关：只有管理员能改，所以整块面板在只读身份下也是灰的。
   *
   * 这里刻意不写成"开关关了就等于没有模型"——它管的是"没依据时说不说"，
   * 文案必须让人一眼看懂代价：关了会拒答，开了可能是错的但会标注。 */
  function policyPanel() {
    const state = A.state.apiConfig;
    const item = state.qaFallback;
    const editable = canEdit() && item && !item.forced;
    const head = `<div class="panel-heading">
        <h2>问答策略</h2>
      </div>`;

    if (state.qaFallbackError || !item) {
      return `<section class="panel">${head}
        <p class="muted">${state.qaFallbackError
          ? `读取失败：<code>${e(state.qaFallbackError)}</code>（不影响上面的端点配置）`
          : '正在读取策略…'}</p>
      </section>`;
    }

    const source = item.forced
      ? '服务器 .env 强制关闭'
      : item.source === 'db' ? '管理员设置' : '平台默认';
    const state_badge = item.enabled
      ? A.badge('已开启', 'green') : A.badge('已关闭', 'amber');

    return `<section class="panel">${head}
      <div class="setting-row">
        <div>
          <b>知识库找不到依据时，改用通用大模型回答</b>
          <p>开启：课程材料里检索不到时，用模型的通用知识回答，并在正文和标签上标注
          「通用知识·未依据教材」，数据库里记 <code>basis=model</code>。
          关闭：只说"没有找到依据"，不猜。</p>
          <p class="muted">当前状态：${state_badge}
            <span>来源：${e(source)}</span>
            ${item.forced ? `<br>${e(item.note)}` : ''}</p>
        </div>
        <button class="btn ${item.enabled ? 'text-btn' : 'primary'}"
                data-action="api-config-qa-fallback"
                data-enabled="${item.enabled ? 'true' : 'false'}"
                ${editable && !state.qaFallbackBusy ? '' : 'disabled'}>
          ${state.qaFallbackBusy ? '保存中…' : (item.enabled ? '关闭' : '开启')}
        </button>
      </div>
      ${item.forced ? '' : `<p class="muted">⚠️ 关掉之后，超纲问题会直接拒答。答辩、评审、给校外看的时候建议关；
        日常教学建议开着，否则学生会以为这个助手什么都不会。</p>`}
    </section>`;
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

    return heading + identityPanel() + policyPanel() + hostsPanel() + `
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

      ${corpusPanel()}

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
  A.actions['api-config-corpus-fill'] = async function () {
    const state = A.state.apiConfig;
    if (state.corpusBusy) return;
    state.corpusBusy = true;
    state.corpusNote = '正在给缺语料的课切分…';
    A.render();
    try {
      const result = await A.api.post('/api/v1/admin/qa-corpus/fill-missing', {});
      const failed = (result.failed || []).length;
      state.corpusNote = result.courses
        ? `已补 ${result.courses} 门课，共 ${result.chunks} 块`
          + (failed ? `；${failed} 门失败：${(result.failed || []).map(item => item.course_id).join('、')}` : '。')
        : '没有缺语料的课，不用补。';
      reload();
    } catch (error) {
      state.corpusBusy = false;
      state.corpusNote = `补齐失败：${error.message}`;
      A.render();
    }
  };

  A.actions['api-config-corpus-rebuild'] = async function () {
    const state = A.state.apiConfig;
    if (state.corpusBusy) return;
    state.corpusBusy = true;
    state.corpusNote = '正在切分全部课程（14 门课约 10 秒）…';
    A.render();
    try {
      const result = await A.api.post('/api/v1/admin/qa-corpus/rebuild', {});
      const failed = (result.failed || []).length;
      state.corpusNote = `已重建 ${result.courses} 门课，共 ${result.chunks} 块`
        + (failed ? `；${failed} 门失败：${(result.failed || []).map(item => item.course_id).join('、')}` : '。');
      reload();
    } catch (error) {
      state.corpusBusy = false;
      state.corpusNote = `重建失败：${error.message}`;
      A.render();
    }
  };

  A.inputs['approved-host'] = function (input) {
    A.state.apiConfig.draftHost = input.value;
  };
  A.inputs['approved-host-note'] = function (input) {
    A.state.apiConfig.draftNote = input.value;
  };

  A.actions['api-config-host-add'] = async function () {
    const state = A.state.apiConfig;
    if (state.hostsBusy) return;
    const host = (A.state.apiConfig.draftHost || '').trim();
    const note = (A.state.apiConfig.draftNote || '').trim();
    if (!host) { state.hostsNote = '先填域名'; A.render(); return; }
    if (!note) { state.hostsNote = '理由要写一句：以后审计会问"为什么放行它"'; A.render(); return; }
    state.hostsBusy = true; state.hostsNote = '正在添加…';
    A.render();
    try {
      state.hosts = await A.api.post('/api/v1/admin/settings/approved-hosts', { host, note });
      state.draftHost = ''; state.draftNote = '';
      state.hostsNote = `${host} 已加入，立刻生效`;
    } catch (error) {
      state.hostsNote = `添加失败：${error.message}`;
    }
    state.hostsBusy = false;
    A.render();
  };

  A.actions['api-config-host-prefill'] = function (button) {
    const state = A.state.apiConfig;
    state.draftHost = button.dataset.host;
    state.hostsNote = `已填入 ${button.dataset.host}，写一句理由后点「屏蔽」`;
    A.render();
    document.getElementById('host-note')?.focus();
  };

  A.actions['api-config-host-block'] = async function () {
    const state = A.state.apiConfig;
    if (state.hostsBusy) return;
    const host = (state.draftHost || '').trim();
    const note = (state.draftNote || '').trim();
    if (!host) { state.hostsNote = '先填域名'; A.render(); return; }
    if (!note) { state.hostsNote = '屏蔽要写理由：以后有人要恢复，得先知道为什么屏蔽'; A.render(); return; }
    state.hostsBusy = true; state.hostsNote = '正在屏蔽…';
    A.render();
    try {
      state.hosts = await A.api.post('/api/v1/admin/settings/blocked-hosts', { host, note });
      state.draftHost = ''; state.draftNote = '';
      state.hostsNote = `${host} 已屏蔽，立刻生效`;
    } catch (error) {
      state.hostsNote = `屏蔽失败：${error.message}`;
    }
    state.hostsBusy = false;
    A.render();
  };

  A.actions['api-config-host-unblock'] = async function (button) {
    const state = A.state.apiConfig;
    if (state.hostsBusy) return;
    const host = button.dataset.host;
    state.hostsBusy = true; state.hostsNote = `正在恢复 ${host}…`;
    A.render();
    try {
      state.hosts = await A.api.del(`/api/v1/admin/settings/blocked-hosts/${encodeURIComponent(host)}`);
      state.hostsNote = `${host} 已恢复`;
    } catch (error) {
      state.hostsNote = `恢复失败：${error.message}`;
    }
    state.hostsBusy = false;
    A.render();
  };

  A.actions['api-config-host-remove'] = async function (button) {
    const state = A.state.apiConfig;
    if (state.hostsBusy) return;
    const host = button.dataset.host;
    state.hostsBusy = true; state.hostsNote = `正在撤销 ${host}…`;
    A.render();
    try {
      state.hosts = await A.api.del(`/api/v1/admin/settings/approved-hosts/${encodeURIComponent(host)}`);
      state.hostsNote = `${host} 已撤销，立刻生效`;
    } catch (error) {
      state.hostsNote = `撤销失败：${error.message}`;
    }
    state.hostsBusy = false;
    A.render();
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
        <option value="vision" ${item?.kind === 'vision' ? 'selected' : ''}>视觉（作业拍照转写）</option>
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
        await A.api.post('/api/v1/admin/model-endpoints', payload);
        A.toast('端点已登记，接着配置密钥');
      } else {
        await A.api.patch(`/api/v1/admin/model-endpoints/${encodeURIComponent(button.dataset.name)}`, payload);
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
      await A.api.put(`/api/v1/admin/model-endpoints/${encodeURIComponent(button.dataset.name)}/key`, { api_key: value });
      document.getElementById('dialog').close();
      A.toast('密钥已保存（不会再显示）');
      reload();
    } catch (error) { A.toast(error.message); }
  };

  A.actions['api-config-key-clear'] = async function (button) {
    try {
      await A.api.del(`/api/v1/admin/model-endpoints/${encodeURIComponent(button.dataset.name)}/key`);
      document.getElementById('dialog').close();
      A.toast('密钥已删除');
      reload();
    } catch (error) { A.toast(error.message); }
  };

  A.actions['api-config-test'] = async function (button) {
    button.disabled = true;
    button.textContent = '自检中…';
    try {
      const result = await A.api.post(`/api/v1/admin/model-endpoints/${encodeURIComponent(button.dataset.name)}/test`);
      A.toast(result.ok ? `自检通过：${result.note}` : `自检失败：${result.note}`);
      reload();
    } catch (error) {
      A.toast(error.message);
      button.disabled = false;
      button.textContent = '自检';
    }
  };

  A.actions['api-config-qa-fallback'] = async function (button) {
    const state = A.state.apiConfig;
    const next = button.dataset.enabled !== 'true';
    state.qaFallbackBusy = true;
    A.render();
    try {
      const result = await A.api.put('/api/v1/admin/settings/qa-fallback', { enabled: next });
      state.qaFallback = result;
      A.toast(next ? '已开启：无依据时用通用模型回答（会标注）' : '已关闭：无依据时直接拒答');
    } catch (error) {
      A.toast(error.message);
    } finally {
      state.qaFallbackBusy = false;
      A.render();
    }
  };

  A.actions['api-config-toggle'] = async function (button) {
    const enable = button.dataset.enabled !== 'true';
    try {
      await A.api.patch(`/api/v1/admin/model-endpoints/${encodeURIComponent(button.dataset.name)}`, { enabled: enable });
      A.toast(enable ? '已启用' : '已停用');
      reload();
    } catch (error) { A.toast(error.message); }
  };

})();
