/* 登录、会话、按角色的页面闸门。
 *
 * 三件事分开看：
 *   1. 状态：A.state.auth 里只有"我现在是谁"（未登录 / 学生 / 教师 / 管理员）；
 *   2. 入口：登录页 #/login，顶栏的登录/退出按钮；
 *   3. 闸门：A.auth.can(page) 决定菜单显不显示、页面能不能开。
 *
 * 闸门只是体验层——真正的权限在服务端（require()）。前端隐藏菜单是为了
 * 不让人点进 403，不是为了"藏起来就安全"。
 */
(function () {
  const A = App, e = A.escape;

  // 每个页面允许哪些角色。没有列出的页面一律公开（课程、图谱、习题、学习记录）。
  const PAGE_ROLES = {
    teaching: ['teacher', 'admin'],
    courseware: ['teacher', 'admin'],
    resources: ['teacher', 'admin'],
    analytics: ['teacher', 'admin'],
    content: ['teacher', 'admin'],
    review: ['teacher', 'admin'],
    construction: ['teacher', 'admin'],
    accounts: ['admin'],
    'api-config': ['admin']
  };

  const ROLE_LABEL = { student: '学生', teacher: '教师', admin: '管理员' };

  A.state.auth = A.state.auth || {
    status: 'unknown',   // unknown | anonymous | authenticated | error
    actor: null,
    providers: null,
    mustChange: false,
    error: '',
    busy: false,
    next: null
  };

  const S = () => A.state.auth;

  A.auth = {
    actor() { return S().actor; },
    role() { return (S().actor && S().actor.role) || ''; },
    anonymous() { return !S().actor; },
    is(...roles) {
      const role = A.auth.role();
      if (!role) return false;
      if (role === 'admin') return true;   // 管理员拥有一切，与服务端一致
      return roles.includes(role);
    },
    rolesFor(page) { return PAGE_ROLES[page] || null; },
    can(page) {
      const allowed = PAGE_ROLES[page];
      if (!allowed) return true;
      const role = A.auth.role();
      if (!role) return false;
      return role === 'admin' || allowed.includes(role);
    },
    label() { return (S().actor && (S().actor.display_name || S().actor.id)) || ''; },
    roleLabel() { return ROLE_LABEL[A.auth.role()] || ''; },
    // 401 广播：页面已经渲染出来了，跳登录页会把正在编辑的内容打丢。
    // 所以只在顶栏提示并让用户自己点，唯一的例外是"本来就该登录才能看"的页面。
    needed() {
      if (S().actor) return;   // 已经登录，只是这个接口权限不够：不跳
      if (A.route && PAGE_ROLES[A.route.page]) A.auth.goLogin();
    },
    goLogin(next) {
      S().next = next || (location.hash.replace(/^#/, '') || '/');
      location.hash = '#/login';
    },
    async load() {
      try {
        const [me, providers] = await Promise.all([
          A.api.me().catch(() => null),
          A.api.providers().catch(() => null)
        ]);
        if (providers) S().providers = providers;
        S().actor = me && me.authenticated ? me.actor : null;
        S().mustChange = !!(me && me.must_change_password);
        S().status = S().actor ? 'authenticated' : 'anonymous';
      } catch (error) {
        S().status = 'error';
        S().error = error.message;
      }
      return S();
    }
  };

  // ---------------------------------------------------------------------------
  // 登录页
  // ---------------------------------------------------------------------------

  function loginForm() {
    const provider = (S().providers && S().providers.cas) || { available: false, hint: '' };
    return `<div class="auth-card">
      <div class="auth-brand">${A.icon('book')}<div><b>智慧武大</b><small>数学教学平台</small></div></div>
      <h1>登录</h1>
      <p class="muted">教师与管理员请用平台账号登录；学生账号由管理员或课程负责人发放。</p>
      <form data-form="login" autocomplete="on">
        <label class="auth-field"><span>账号</span>
          <input id="auth-username" data-input="auth-username" name="username"
                 autocomplete="username" placeholder="学号或工号" value="${e(S().username || '')}" autofocus>
        </label>
        <label class="auth-field"><span>密码</span>
          <input id="auth-password" data-input="auth-password" name="password" type="password"
                 autocomplete="current-password" placeholder="至少 8 位">
        </label>
        ${S().error ? `<p class="auth-error">${e(S().error)}</p>` : ''}
        <button class="btn primary auth-submit" type="submit" data-action="auth-submit" ${S().busy ? 'disabled' : ''}>
          ${S().busy ? '登录中…' : '登录'}
        </button>
      </form>
      <div class="auth-divider"><span>或</span></div>
      <button class="btn auth-cas" data-action="auth-cas" ${provider.available ? '' : 'disabled'}
              title="${e(provider.hint || '')}">
        ${A.icon('lock')} 学校统一身份认证${provider.available ? '' : '（待接入）'}
      </button>
      <p class="muted small">${provider.available
        ? '使用信息门户账号登录，平台不保存你的统一认证密码。'
        : '统一认证接口已留好，接入后这里会直接跳转学校登录页；在此之前请用平台账号。'}</p>
      ${S().providers && S().providers.auth_mode === 'dev'
        ? `<p class="auth-hint">本地开发模式：演示账号见 docs/账号与权限.md（admin / teacher / student）。</p>`
        : ''}
    </div>`;
  }

  A.views.login = function () {
    if (S().status === 'unknown') A.auth.load().then(() => A.render());
    return `<div class="auth-page">${loginForm()}</div>`;
  };

  A.after.login = function () {
    if (S().status === 'unknown' && !S().loading) {
      S().loading = true;
      A.auth.load().then(() => { S().loading = false; A.render(); });
    }
  };

  // ---------------------------------------------------------------------------
  // 动作
  // ---------------------------------------------------------------------------

  A.actions['auth-login-open'] = () => {
    A.auth.goLogin();
  };

  A.actions['auth-submit'] = async function (button) {
    const username = (document.getElementById('auth-username') || {}).value || '';
    const password = (document.getElementById('auth-password') || {}).value || '';
    if (!username.trim() || !password) {
      S().error = '请填写账号和密码';
      A.render();
      return;
    }
    S().busy = true;
    S().error = '';
    S().username = username.trim();
    A.render();
    try {
      const result = await A.api.login(username.trim(), password, S().next);
      S().actor = result.actor;
      S().mustChange = !!result.must_change_password;
      S().status = 'authenticated';
      S().error = '';
      const target = result.next || S().next || '/';
      S().next = null;
      S().busy = false;
      location.hash = target.startsWith('#') ? target : `#${target.startsWith('/') ? '' : '/'}${target}`;
      A.render();
      A.toast(`已登录：${A.auth.label()}（${A.auth.roleLabel()}）`);
    } catch (error) {
      S().busy = false;
      S().error = error.message;
      A.render();
    }
  };

  A.actions['auth-logout'] = async function (button) {
    button.disabled = true;
    try {
      await A.api.logout();
    } catch (error) {
      console.warn('[auth/logout]', error);
    }
    S().actor = null;
    S().status = 'anonymous';
    S().mustChange = false;
    button.disabled = false;
    A.toast('已退出登录');
    location.hash = '#/catalog';
    A.render();
  };

  A.actions['auth-cas'] = async function () {
    const provider = (S().providers && S().providers.cas) || {};
    if (!provider.available) {
      A.toast('统一身份认证尚未接入：接口与回调位置已留好，见 docs/账号与权限.md');
      return;
    }
    location.href = `${A.api.base()}${provider.login_path || '/api/v1/auth/cas/login'}`;
  };

  A.inputs['auth-username'] = input => { S().username = input.value; };
  A.inputs['auth-password'] = () => {};

  // 改密：登录后随时可用（管理员重置别人的密码在「账号管理」页）
  A.actions['auth-password-open'] = () => {
    A.openDialog('修改密码', `<div class="auth-dialog">
      <label class="auth-field"><span>当前密码</span>
        <input id="pw-current" type="password" autocomplete="current-password"></label>
      <label class="auth-field"><span>新密码（至少 8 位）</span>
        <input id="pw-new" type="password" autocomplete="new-password"></label>
      <label class="auth-field"><span>再输一次</span>
        <input id="pw-confirm" type="password" autocomplete="new-password"></label>
      <p class="muted small">改密后，你在其它设备上的登录会立即失效。</p>
      <div class="button-row"><button class="btn primary" data-action="auth-password-save">保存</button></div>
    </div>`);
  };

  A.actions['auth-password-save'] = async function (button) {
    const read = id => (document.getElementById(id) || {}).value || '';
    const current = read('pw-current');
    const next = read('pw-new');
    if (read('pw-confirm') !== next) {
      A.toast('两次输入的新密码不一致');
      return;
    }
    button.disabled = true;
    try {
      await A.api.changePassword(current, next);
      document.getElementById('dialog').close();
      A.toast('密码已更新，其它设备上的登录已失效');
      S().mustChange = false;
      A.render();
    } catch (error) {
      button.disabled = false;
      A.toast(error.message);
    }
  };

  // 首次登录后提醒改密（不拦截：演示时强制跳转会把人锁在改密页）
  A.mustChangeBanner = function () {
    if (!S().mustChange || !S().actor) return '';
    return `<div class="auth-banner">当前账号还是初始密码，建议 <button class="link-btn" data-action="auth-password-open">立即修改</button>。</div>`;
  };
})();
