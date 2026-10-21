/* 账号管理（仅管理员）：建账号、改角色、停用、重置密码。
 *
 * 为什么放在平台里而不是只靠 SQL：教师每学期都在变，让管理员每次开数据库
 * 是这类平台最常见的"交付即停摆"原因。同理，停用而不是删除——学生的
 * 学习记录还挂在账号上，删掉人会让历史数据悬空。
 */
(function () {
  const A = App, e = A.escape;
  const ROLE_LABEL = { student: '学生', teacher: '教师', admin: '管理员' };

  A.state.accounts = A.state.accounts || {
    status: 'idle', items: [], counts: {}, error: '', filter: '', query: '',
    draft: { username: '', display_name: '', role: 'student', password: '' },
    saving: false
  };
  const S = () => A.state.accounts;

  async function load() {
    S().status = 'loading';
    A.render();
    try {
      const data = await A.api.accounts({ role: S().filter, query: S().query });
      S().items = data.items;
      S().counts = data.counts;
      S().status = 'ready';
      S().error = '';
    } catch (error) {
      S().status = 'error';
      S().error = error.message;
    }
    A.render();
  }

  A.after.accounts = function () {
    if (S().status === 'idle') load();
  };

  function roleBadge(role) {
    const variant = role === 'admin' ? 'blue' : role === 'teacher' ? 'green' : '';
    return A.badge(ROLE_LABEL[role] || role, variant);
  }

  function row(account) {
    const locked = account.locked ? '<span class="ax-warn">锁定中</span>' : '';
    return `<tr class="${account.disabled ? 'ax-disabled' : ''}">
      <td><b>${e(account.username)}</b><small>${e(account.display_name)}</small></td>
      <td>${roleBadge(account.role)}</td>
      <td>${account.has_password ? '平台密码' : '仅统一认证'}</td>
      <td>${account.disabled ? A.badge('已停用') : A.badge('正常', 'green')} ${locked}
        ${account.must_change_password ? '<br><span class="muted small">待改初始密码</span>' : ''}</td>
      <td class="muted small">${account.last_login_at ? e(String(account.last_login_at).slice(0, 16).replace('T', ' ')) : '从未登录'}</td>
      <td class="ax-actions">
        <button class="btn small" data-action="accounts-reset" data-id="${e(account.id)}"
                data-name="${e(account.username)}">重置密码</button>
        <button class="btn small" data-action="accounts-role" data-id="${e(account.id)}"
                data-role="${e(account.role)}">改角色</button>
        <button class="btn small" data-action="accounts-toggle" data-id="${e(account.id)}"
                data-disabled="${account.disabled ? '0' : '1'}">${account.disabled ? '启用' : '停用'}</button>
      </td>
    </tr>`;
  }

  A.views.accounts = function () {
    if (S().status === 'error') {
      return A.sectionTitle('ACCOUNTS', '账号管理', '教师、学生与管理员账号。')
        + `<div class="panel"><h2>读不到账号列表</h2><p class="muted">${e(S().error)}</p>
           ${A.button('重试', 'accounts-refresh', 'refresh')}</div>`;
    }
    const counts = S().counts || {};
    const total = Object.values(counts).reduce((sum, n) => sum + n, 0);
    const rows = S().items.map(row).join('');
    return A.sectionTitle(
      'ACCOUNTS',
      '账号管理',
      '给学生、教师、管理员发账号。停用不会删除人——学习记录还挂在账号上。',
      `<div class="button-row">${A.button('刷新', 'accounts-refresh', 'refresh')}</div>`
    ) + `<div class="panel">
      <div class="ax-stats">
        ${['admin', 'teacher', 'student'].map(role =>
          `<span class="ax-stat"><b>${counts[role] || 0}</b>${ROLE_LABEL[role]}</span>`).join('')}
        <span class="muted small">共 ${total} 个账号</span>
      </div>
      <div class="ax-toolbar">
        <label class="ax-inline">角色
          <select data-input="accounts-filter">
            <option value="">全部</option>
            ${['admin', 'teacher', 'student'].map(role =>
              `<option value="${role}" ${S().filter === role ? 'selected' : ''}>${ROLE_LABEL[role]}</option>`).join('')}
          </select>
        </label>
        <label class="ax-inline">搜索
          <input data-input="accounts-query" value="${e(S().query || '')}" placeholder="账号或姓名">
        </label>
        <button class="btn small" data-action="accounts-refresh">查询</button>
      </div>
      <table class="ax-table">
        <thead><tr><th>账号</th><th>角色</th><th>登录方式</th><th>状态</th><th>最近登录</th><th></th></tr></thead>
        <tbody>${rows || '<tr><td colspan="6" class="muted">没有符合条件的账号。</td></tr>'}</tbody>
      </table>
    </div>
    <div class="panel">
      <h2>新建账号</h2>
      <p class="muted">给学生发账号时，建议勾选"要求首次登录改密"并线下告知初始密码。</p>
      <div class="ax-form">
        <label class="ax-inline">账号<input data-input="accounts-new-username" value="${e(S().draft.username)}" placeholder="学号或工号"></label>
        <label class="ax-inline">姓名<input data-input="accounts-new-name" value="${e(S().draft.display_name)}" placeholder="张三"></label>
        <label class="ax-inline">角色
          <select data-input="accounts-new-role">
            ${['student', 'teacher', 'admin'].map(role =>
              `<option value="${role}" ${S().draft.role === role ? 'selected' : ''}>${ROLE_LABEL[role]}</option>`).join('')}
          </select>
        </label>
        <label class="ax-inline">初始密码<input data-input="accounts-new-password" type="password" placeholder="至少 8 位"></label>
        <button class="btn primary" data-action="accounts-create" ${S().saving ? 'disabled' : ''}>创建</button>
      </div>
    </div>`;
  };

  A.inputs['accounts-filter'] = input => { S().filter = input.value; load(); };
  A.inputs['accounts-query'] = input => { S().query = input.value; };
  A.inputs['accounts-new-username'] = input => { S().draft.username = input.value; };
  A.inputs['accounts-new-name'] = input => { S().draft.display_name = input.value; };
  A.inputs['accounts-new-role'] = input => { S().draft.role = input.value; };
  A.inputs['accounts-new-password'] = input => { S().draft.password = input.value; };

  A.actions['accounts-refresh'] = () => load();

  A.actions['accounts-create'] = async function (button) {
    const draft = S().draft;
    if (!draft.username.trim() || !draft.display_name.trim()) {
      A.toast('账号和姓名都要填');
      return;
    }
    if (draft.password.length < 8) {
      A.toast('初始密码至少 8 位');
      return;
    }
    button.disabled = true;
    S().saving = true;
    try {
      await A.api.createAccount({
        username: draft.username.trim(),
        display_name: draft.display_name.trim(),
        role: draft.role,
        password: draft.password,
        must_change_password: true
      });
      A.toast(`已创建 ${draft.username.trim()}（${ROLE_LABEL[draft.role]}）`);
      S().draft = { username: '', display_name: '', role: 'student', password: '' };
      S().saving = false;
      await load();
    } catch (error) {
      S().saving = false;
      button.disabled = false;
      A.toast(error.message);
    }
  };

  A.actions['accounts-reset'] = async function (button) {
    const password = window.prompt(`给 ${button.dataset.name} 设置新密码（至少 8 位）：`);
    if (!password) return;
    if (password.length < 8) { A.toast('密码至少 8 位'); return; }
    try {
      await A.api.patchAccount(button.dataset.id, { password, must_change_password: true });
      A.toast('密码已重置，该账号原登录已失效');
      await load();
    } catch (error) {
      A.toast(error.message);
    }
  };

  A.actions['accounts-role'] = async function (button) {
    const next = window.prompt(`把角色从「${ROLE_LABEL[button.dataset.role]}」改成：student / teacher / admin`, 'teacher');
    if (!next) return;
    if (!['student', 'teacher', 'admin'].includes(next)) { A.toast('角色只能是 student / teacher / admin'); return; }
    try {
      await A.api.patchAccount(button.dataset.id, { role: next });
      A.toast(`角色已改为${ROLE_LABEL[next]}`);
      await load();
    } catch (error) {
      A.toast(error.message);
    }
  };

  A.actions['accounts-toggle'] = async function (button) {
    const disable = button.dataset.disabled === '1';
    if (disable && !window.confirm('停用后这个人立刻无法登录，已登录的会话也会失效。继续？')) return;
    try {
      await A.api.patchAccount(button.dataset.id, { disabled: disable });
      A.toast(disable ? '账号已停用' : '账号已启用');
      await load();
    } catch (error) {
      A.toast(error.message);
    }
  };
})();
