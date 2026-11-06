/* 约束组卷（阶段 B）：启动一次求解 → 读诊断 → 比较方案 → 选一个落成草稿卷。
 *
 * 这一页存在的理由：建卷页（paper-build）是"人手挑题"，本页是"求解器挑题"。
 * 两者的输入都是蓝图版本与候选清单，但教师面对的问题完全不同——
 *   建卷页问的是"这一槽放哪道题"；
 *   本页问的是"为什么组不出来 / 这个方案凭什么"。
 * 把后者塞进建卷页，会让人以为调一下权重就能立刻看到新方案，而实际上要重跑一次运行。
 *
 * 三条不可谈判的显示规则：
 *   1. **可行 ≠ 最优。** 服务端给 `optimal_proven=false` 时，界面必须写"可行，未证明最优"，
 *      不能统一显示成"已优化"。这道信息决定教师敢不敢直接拿去考试。
 *   2. **无解不是错误。** `infeasible` 是一次成功的运行，界面上要把原因逐条列出来
 *      （哪一槽没有候选、时长下界多少），而不是弹一个红框说"出错了"。
 *   3. **没有方案就没有按钮。** `solved` 为假时不渲染"用这个方案建卷"，
 *      服务端也会 409——但界面上不该先给出一个必然失败的入口。
 */
(function () {
  const A = App;

  const STATUS_LABEL = {
    queued: '排队中',
    running: '求解中',
    optimal: '最优（已证明）',
    feasible: '可行（未证明最优）',
    infeasible: '无解（约束互相冲突）',
    timeout: '超时',
    failed: '失败',
    cancelled: '已取消'
  };

  // 与 server/app/assembly/diagnostics.py 的问题码一一对应。前端复制一份是为了
  // "没有网络也能解释清楚"；码值本身仍以服务端为准。
  const ISSUE_TEXT = {
    blueprint_score_mismatch: '蓝图总分与槽位分之和对不上',
    section_score_mismatch: '某个分节的槽位分之和与声明值对不上',
    slot_has_no_candidate: '这一槽按当前条件没有可选题目',
    not_enough_candidates: '候选题目比槽位还少',
    duration_impossible: '每槽取最短题目，总时长仍然超上限',
    coverage_unreachable: '覆盖要求超出所有槽位能提供的上限',
    kind_requirement_unreachable: '题型数量要求超出可放该题型的槽位数',
    kind_requirement_invalid: '题型数量区间写反了（最少大于最多）',
    mutually_exclusive_mandatory: '两道必选题落在同一个互斥组里'
  };

  // 运行失败时的原因码（服务端 diagnostics.reason）。
  const REASON_TEXT = {
    precheck_proved: '求解前预检已经证明无解',
    solver_reported: '求解器报告无解（预检没找到单一原因，冲突在组合里）',
    solver_error: '求解器无法运行或中途出错',
    hard_constraints: '硬约束互相冲突',
    model_invalid: '模型本身非法（这是缺陷，不是数据问题）',
    time_limit_no_solution: '时间用完但一个方案都没找到'
  };

  const SOLVED = { optimal: true, feasible: true };

  function escape(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function errorText(error) {
    if (!error) return '未知错误';
    const detail = error.detail || {};
    const code = detail.code || error.code;
    const messages = {
      assembly_run_not_found: '这次求解运行不存在（可能已被清理）。',
      assembly_run_not_solved: '这次运行没有可选方案，不能建卷。',
      solution_index_invalid: '方案序号越界：下拉列表可能已经过期，刷新后再选。',
      solution_manifest_mismatch: '方案内容与记录的哈希不一致：这份方案不能用来建卷。',
      assembly_validation_blocked: '方案没通过整卷复验，卷子停在草稿，不能提交审核。',
      idempotency_conflict: '同一把幂等键被用于不同的蓝图版本。',
      idempotency_key_required: '缺少 Idempotency-Key：重试会跑出第二次求解。',
      solver_config_invalid: '求解参数不合法（方案数上限是 3，时限至少 1 秒）。',
      blueprint_not_solvable: '这份蓝图不能交给求解器（内容或模式不支持）。',
      assembly_run_not_solved_unused: ''
    };
    if (code && messages[code]) return messages[code];
    if (detail.message) return detail.message;
    return errorTextFallback(error);
  }

  function errorTextFallback(error) {
    if (error.message) return error.message;
    return '请求失败，请稍后重试。';
  }

  function S() {
    return A.state.paperAssembly = A.state.paperAssembly || {
      courseId: null, blueprints: [], selectedId: null, selected: null,
      runs: [], run: null, supply: null, comparison: null, pair: null, queued: null,
      loading: false, loaded: false,
      busy: null, error: null, message: null, created: null,
      config: { random_seed: '20261105', time_limit_seconds: '30', max_solutions: '3' }
    };
  }

  function newKey(action) {
    return `paper-assembly-${action}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  }

  // ---------------------------------------------------------------------------
  // 纯函数（视图测试直接断言这些）
  // ---------------------------------------------------------------------------

  /** 运行状态的诚实标签：可行与最优必须分开显示。 */
  A.paperAssemblyStatus = function (run) {
    if (!run) return STATUS_LABEL.infeasible;
    const label = STATUS_LABEL[run.status] || run.status;
    if (run.status === 'feasible') return '可行（未证明最优）';
    return label;
  };

  A.paperAssemblyTone = function (run) {
    if (!run) return 'muted';
    if (run.status === 'optimal') return 'green';
    if (run.status === 'feasible') return 'blue';
    if (run.status === 'infeasible' || run.status === 'failed') return 'red';
    return 'amber';
  };

  /** 求解器不可用时，界面要给出可执行的下一步，而不是只说"失败了"。 */
  A.paperAssemblyDegraded = function (run) {
    const diagnostics = (run && run.diagnostics) || {};
    const detail = String(diagnostics.detail || '');
    return diagnostics.reason === 'solver_error' && /ortools|requirements-assembly/i.test(detail);
  };

  A.paperAssemblyIssueText = function (code) {
    return ISSUE_TEXT[code] || '未识别的诊断项（以服务端说明为准）';
  };

  /**
   * 这次运行的"一句话结论"。措辞只在这里写一次：界面、测试、将来的导出都取这一份。
   *
   * 四种结论必须分得开，因为它们对教师意味着完全不同的下一步：
   *   optimal     → 已证明最优，可以选
   *   feasible    → 有方案但没证明最优（敢不敢拿去考试，是教师自己判断）
   *   infeasible  → 原硬约束无法满足，而且**系统不会自动放松**；要人改蓝图
   *   timeout 无解 → 没找到方案，同样没有试卷
   * 把 timeout 有解合并进 feasible 是可以的（有方案），但"超时"这件事要留在状态里。
   */
  A.paperAssemblyVerdict = function (run) {
    if (!run) return '还没有运行记录。';
    const solutions = (run.solutions || []).length;
    if (run.status === 'optimal') return '已证明最优；可以选一个方案建卷。';
    if (run.status === 'feasible') {
      return '可行，未证明最优；可以选方案，但界面不会把它说成最优。';
    }
    if (run.status === 'timeout') {
      return solutions
        ? '超时；找到的方案可用，但没有最优性结论。'
        : '超时，无可行解；未创建试卷。';
    }
    if (run.status === 'infeasible') return '原硬约束不可行；未自动放松，也未创建试卷。';
    if (run.status === 'queued' || run.status === 'running') {
      return '求解还在队列里或正在跑：现在没有结论，也不会有卷子。';
    }
    if (run.status === 'failed') return '这次运行失败；未创建试卷。';
    if (run.status === 'cancelled') return '运行已取消；未创建试卷。';
    return `运行进行中：${A.paperAssemblyStatus(run)}。`;
  };

  A.paperAssemblyReasonText = function (reason) {
    return REASON_TEXT[reason] || '未识别的原因（以服务端说明为准）';
  };

  /** 方案的可读摘要：给教师比较方案用，不是给机器读的。 */
  A.paperAssemblySolutions = function (run) {
    if (!run || !run.solutions) return [];
    return run.solutions.map((solution, index) => ({
      index,
      label: solution.variant_label || `方案 ${index + 1}`,
      selections: (solution.selections || []).length,
      objective: solution.objective_value,
      optimal: solution.optimal_proven === true,
      status: SOLUTION_STATUS[solution.solver_status] || solution.solver_status,
      hash: String(solution.manifest_hash || '').slice(0, 12)
    }));
  };

  const SOLUTION_STATUS = {
    optimal: '最优',
    feasible: '可行'
  };

  /** 求解参数 → 请求体；不合法时抛错（在界面里显示，不发请求）。 */
  A.paperAssemblyConfig = function (config) {
    const seed = Number(config.random_seed);
    const seconds = Number(config.time_limit_seconds);
    const solutions = Number(config.max_solutions);
    if (!Number.isInteger(seed)) throw new Error('随机种子必须是整数');
    if (!Number.isInteger(seconds) || seconds < 1) throw new Error('时限至少 1 秒');
    if (!Number.isInteger(solutions) || solutions < 1 || solutions > 3) {
      // 上限 3 是规格与 0034 的 CHECK；这里先挡，避免一次注定 422 的请求。
      throw new Error('方案数只能是 1—3');
    }
    return {
      random_seed: seed,
      time_limit_seconds: seconds,
      max_solutions: solutions
    };
  };

  // ---------------------------------------------------------------------------
  // 取数
  // ---------------------------------------------------------------------------

  function loadSupply(state) {
    if (!state.selected) return Promise.resolve();
    return A.api.assemblyCandidateSupply(state.selected.id)
      .then(supply => { state.supply = supply; })
      .catch(() => { state.supply = null; });
  }

  function loadRuns(state) {
    if (!state.selected) return Promise.resolve();
    return A.api.assemblyRuns(state.selected.id, 20)
      .then(runs => {
        state.runs = Array.isArray(runs) ? runs : [];
        // 默认展示最近一次运行：教师打开页面最想知道的是"上次跑成什么样"。
        state.run = state.runs[0] || null;
      })
      .catch(error => { state.runs = []; state.error = errorText(error); });
  }

  A.loadPaperAssembly = function (courseId, { force = false } = {}) {
    const state = S();
    if (!courseId) return;
    const sameCourse = state.courseId === courseId;
    if (!force && sameCourse && (state.loading || state.loaded)) return;
    state.courseId = courseId;
    state.loading = true;
    state.error = null;
    A.render();
    return A.api.courseBlueprints(courseId).then(blueprints => {
      state.blueprints = Array.isArray(blueprints) ? blueprints : [];
      const keep = state.blueprints.find(row => row.id === state.selectedId);
      state.selected = keep || state.blueprints[0] || null;
      state.selectedId = state.selected ? state.selected.id : null;
      return Promise.all([loadSupply(state), loadRuns(state)]);
    }).then(() => {
      state.loading = false;
      state.loaded = true;
      A.render();
    }).catch(error => {
      state.loading = false;
      state.error = errorText(error);
      A.render();
    });
  };

  function run(action, task) {
    const state = S();
    if (state.busy) return Promise.resolve();
    state.busy = action;
    state.error = null;
    state.message = null;
    A.render();
    return Promise.resolve().then(task).then(message => {
      state.busy = null;
      if (message) state.message = message;
      A.render();
    }).catch(error => {
      state.busy = null;
      state.error = errorText(error);
      A.render();
    });
  }

  // ---------------------------------------------------------------------------
  // 渲染
  // ---------------------------------------------------------------------------

  function statusBadge(run) {
    return A.badge(A.paperAssemblyStatus(run), A.paperAssemblyTone(run));
  }

  function blueprintPicker(state) {
    if (!state.blueprints.length) {
      return `<p class="muted">这门课还没有蓝图版本。先到「建卷」页建一份蓝图——
        约束组卷只能在已有蓝图上进行。</p>`;
    }
    const options = state.blueprints.map(row => {
      const mode = row.assembly_mode === 'constraint_assisted' ? '约束组卷' : '手工';
      return `<option value="${escape(row.id)}"${row.id === state.selectedId ? ' selected' : ''}>
        第 ${escape(row.version_no)} 版 · ${escape(row.purpose)} · ${escape(mode)} ·
        ${escape(row.total_score)} 分</option>`;
    }).join('');
    return `<div class="assembly-picker">
      <label for="assembly-blueprint">蓝图版本</label>
      <select id="assembly-blueprint" data-input="assembly-blueprint">${options}</select>
      <small>只有「约束组卷」模式的蓝图可以跑求解；手工蓝图请在「建卷」页挑题。</small>
    </div>`;
  }

  function supplyBlock(state) {
    const supply = state.supply;
    if (!state.selected) return '';
    if (!supply) {
      return `<section class="panel assembly-supply" aria-label="候选供给">
        <h4>候选供给</h4>
        <p class="muted">还没读到候选供给（可能这门课还没有冻结的 Release）。</p></section>`;
    }
    const rows = Object.keys(supply.supply || {}).map(code => {
      const count = supply.supply[code];
      return `<tr data-slot="${escape(code)}"><td>${escape(code)}</td>
        <td>${escape(count)}</td>
        <td>${count > 0 ? '' : '<span class="warn-text">这一槽没有可选题目</span>'}</td></tr>`;
    }).join('');
    const issues = (supply.issues || []).map(issue => `<li data-code="${escape(issue.code)}">
      <b>${escape(A.paperAssemblyIssueText(issue.code))}</b>
      ${escape(issue.message)}${issue.slot_code
        ? ` <span class="muted">（槽位 ${escape(issue.slot_code)}）</span>` : ''}</li>`).join('');
    const issuesBlock = issues
      ? `<div class="assembly-issues" role="alert"><h5>预检发现这些问题</h5><ul>${issues}</ul></div>`
      : '';
    return `<section class="panel assembly-supply" aria-label="候选供给">
      <h4>候选供给<span class="muted small">（共 ${escape((supply.candidates || 0))} 道候选，
        排除 ${escape(((supply.exclusions || []).length))} 道）</span></h4>
      <table class="ops-table"><thead><tr><th>槽位</th><th>可选题数</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table>
      ${issuesBlock}
      <p class="muted small">预检只能证明"一定无解"的情况。预检没发现问题**不保证可解**——
        组合上的冲突只有跑一次求解才知道。</p></section>`;
  }

  function configForm(state) {
    const config = state.config;
    const disabled = state.busy ? ' disabled' : '';
    // 手工蓝图不能跑求解（服务端也会拒）：界面上先说清，而不是让人点了才看到 422。
    if (state.selected && state.selected.assembly_mode === 'manual') {
      return `<p class="assembly-needs-assist">这份蓝图是<strong>手工组卷</strong>：
        它没有约束求解所需的槽位条件，不能跑求解。先到「建卷」页把它另存为
        <strong>约束组卷</strong>蓝图（assembly_mode=constraint_assisted），再回到本页。</p>
        <div class="button-row">${A.link('去建卷页', 'paper-build', state.courseId, 'pen', '', {})}</div>`;
    }
    return `<div class="assembly-form">
      <label for="assembly-seed">随机种子</label>
      <input id="assembly-seed" data-input="assembly-seed" value="${escape(config.random_seed)}">
      <label for="assembly-seconds">时限（秒）</label>
      <input id="assembly-seconds" data-input="assembly-seconds"
        value="${escape(config.time_limit_seconds)}">
      <label for="assembly-solutions">方案数（1—3）</label>
      <input id="assembly-solutions" data-input="assembly-solutions"
        value="${escape(config.max_solutions)}">
      ${A.button('开始求解', 'assembly-start', 'refresh', disabled, 'primary')}
    </div>`;
  }

  function diagnosticsBlock(run) {
    const diagnostics = (run && run.diagnostics) || {};
    const lines = [];
    if (diagnostics.reason) {
      lines.push(`<li data-reason="${escape(diagnostics.reason)}">
        <b>${escape(A.paperAssemblyReasonText(diagnostics.reason))}</b></li>`);
    }
    (diagnostics.issues || []).forEach(issue => {
      lines.push(`<li data-code="${escape(issue.code)}">
        <b>${escape(A.paperAssemblyIssueText(issue.code))}</b> ${escape(issue.message)}
        ${issue.slot_code ? ` <span class="muted">（槽位 ${escape(issue.slot_code)}）</span>` : ''}
      </li>`);
    });
    if (diagnostics.detail && !A.paperAssemblyDegraded(run)) {
      lines.push(`<li>${escape(diagnostics.detail)}</li>`);
    }
    const degraded = A.paperAssemblyDegraded(run);
    const degradedBlock = degraded
      ? `<div class="assembly-degraded" role="alert">
          <h5>这台机器没有装求解器</h5>
          <p>求解需要 OR-Tools CP-SAT。安装后重跑同一次运行即可：
          <code>.venv/Scripts/python.exe -m pip install -r server/requirements-assembly.txt</code></p>
          <p class="muted small">在此之前，系统不会给出任何"看起来可行"的方案。</p></div>`
      : '';
    if (!lines.length && !degraded) {
      return `<section class="panel assembly-diagnostics" aria-label="诊断">
        <h4>诊断</h4><p class="muted">这次运行没有报告问题。</p></section>`;
    }
    const list = lines.length ? `<ul>${lines.join('')}</ul>` : '';
    return `<section class="panel assembly-diagnostics" aria-label="诊断">
      <h4>诊断</h4>${degradedBlock}${list}</section>`;
  }

  function solutionsBlock(run, state) {
    if (!run || !SOLVED[run.status]) {
      return `<section class="panel assembly-solutions" aria-label="方案">
        <h4>方案</h4>
        <p class="muted">这次运行没有方案。没有方案就不会建卷——先按上面的诊断改蓝图，
        然后重跑一次求解。</p></section>`;
    }
    const rows = A.paperAssemblySolutions(run).map(solution => `<tr data-index="${solution.index}">
      <td>${escape(solution.label)}</td>
      <td>${escape(solution.status)}${solution.optimal ? '' : '（未证明最优）'}</td>
      <td>${escape(solution.selections)}</td>
      <td>${escape(solution.objective)}</td>
      <td>${escape(solution.hash)}…</td>
      <td>${A.button('用这个方案建卷', 'assembly-select', 'check',
        `data-index="${solution.index}"${state.busy ? ' disabled' : ''}`, 'small')}</td>
    </tr>`).join('');
    const compareLink = run.solutions.length > 1
      ? `<div class="button-row">${A.button('对照方案 1 与方案 2', 'assembly-compare', 'graph',
          state.busy ? 'disabled' : '', 'small')}</div>`
      : '';
    return `<section class="panel assembly-solutions" aria-label="方案">
      <h4>方案<span class="muted small">（每个方案最多 3 个，分别独立复验）</span></h4>
      <table class="ops-table"><thead><tr><th>方案</th><th>结论</th><th>题数</th>
        <th>目标值</th><th>方案哈希</th><th></th></tr></thead><tbody>${rows}</tbody></table>
      ${compareLink}${comparisonBlock(state)}
      <p class="muted small">建卷后仍是一份草稿：要过整卷校验与双人复核才能提交。</p>
    </section>`;
  }

  /** 两个方案的对照：只讲结构（分值、共用题），不讲"等值"。 */
  function comparisonBlock(state) {
    const comparison = state.comparison;
    if (!comparison) return '';
    const issues = (comparison.issues || []).map(issue => `<li data-code="${escape(issue.code)}">
      ${escape(issue.message)}</li>`).join('');
    const overlap = (comparison.shared_version_ids || []).length;
    return `<div class="assembly-compare" role="status">
      <h5>${escape(comparison.note || '结构对照')}</h5>
      <p>槽位 ${escape((comparison.summary || {}).slots || 0)} 个；
        共用题目 ${escape(overlap)} 道；共用同族 ${escape(
          (comparison.shared_family_ids || []).length)} 道。</p>
      ${issues ? `<ul class="assembly-issues">${issues}</ul>` : ''}
      <p class="muted small">这里只说结构：分值分布是否一致、除锚题外有没有共用题。
        它<strong>不是</strong>难度等值结论——本系统没有等值设计，不会给出这种结论。</p>
    </div>`;
  }

  function runsBlock(state) {
    const runs = state.runs || [];
    if (!runs.length) {
      return `<p class="muted">这份蓝图还没有跑过求解。</p>`;
    }
    const currentId = state.run && state.run.assembly_run_id;
    const rows = runs.map(run => {
      const isCurrent = run.assembly_run_id === currentId;
      // 只有"当前查看的这次运行"和"另一个已解出的运行"才能配对：拿两次无解去比
      // 结构毫无意义，而让教师自己判断哪两个能配，比界面上瞎猜安全。
      const canPair = !isCurrent && currentId
        && (run.solutions || []).length > 0
        && state.run && (state.run.solutions || []).length > 0;
      const actions = [
        isCurrent
          ? '<span class="muted">当前查看</span>'
          : A.button('查看', 'assembly-open', 'search',
            `data-run="${escape(run.assembly_run_id)}" data-input="assembly-open"`, 'small'),
        canPair
          ? A.button('与本页运行配 A/B', 'assembly-pair', 'check',
            `data-peer="${escape(run.assembly_run_id)}"`, 'small')
          : ''
      ].join(' ');
      return `<tr data-run="${escape(run.assembly_run_id)}">
        <td>${statusBadge(run)}</td>
        <td>${escape(String(run.created_at || '').slice(0, 16).replace('T', ' '))}</td>
        <td>${escape((run.solutions || []).length)}</td>
        <td>${escape(run.candidate_manifest_hash || '').slice(0, 12)}…</td>
        <td>${actions}</td>
      </tr>`;
    }).join('');
    return `<table class="ops-table assembly-runs"><thead><tr><th>状态</th><th>时间</th>
      <th>方案数</th><th>候选哈希</th><th></th></tr></thead><tbody>${rows}</tbody></table>
      ${pairBlock(state)}`;
  }

  /** A/B 配对结果：成功与失败都要显示，且都必须带上"不代表统计等值"。 */
  function pairBlock(state) {
    const pair = state.pair;
    if (!pair) return '';
    if (pair.error) {
      const issues = (pair.issues || []).map(issue => `<li data-code="${escape(issue.code)}">
        ${escape(issue.message)}</li>`).join('');
      return `<div class="assembly-pair is-rejected" role="alert">
        <h5>这两次运行不能配成 A/B</h5>
        <p>${escape(pair.error)}</p>
        ${issues ? `<ul class="assembly-issues">${issues}</ul>` : ''}
        <p class="muted small">配对要求：两卷结构一致，且除锚题外不共用题目、不共用同族题。
          这里只说结构，不代表统计等值。</p></div>`;
    }
    return `<div class="assembly-pair" role="status">
      <h5>已配成 A/B：${escape((pair.labels || []).join(' / '))}</h5>
      <p>${escape(pair.note || '结构匹配的 A/B 卷；不代表统计等值')}</p>
      <p class="muted small">${escape(pair.next_step || '')}</p></div>`;
  }

  function createdBlock(state) {
    const created = state.created;
    if (!created) return '';
    return `<div class="assembly-created" role="status">
      <h5>草稿卷已建好</h5>
      <p>卷面哈希 <code>${escape(String(created.manifest_hash || '').slice(0, 16))}…</code>；
        状态 ${escape(created.status || 'draft')}。</p>
      <p class="muted small">${escape(created.next_step || '仍需整卷校验与双人复核。')}</p>
      <div class="button-row">
        ${A.link('进卷面编辑器', 'paper-editor', state.courseId, 'pen', '', { version: created.paper_version_id })}
        ${A.link('进入审核与发布', 'paper-ops', state.courseId, 'check', '', { version: created.paper_version_id })}
      </div></div>`;
  }

  A.paperAssemblyHtml = function (state) {
    const current = state || S();
    if (!current.courseId) {
      return `<p class="muted">先选择一门课程。</p>`;
    }
    const run = current.run;
    const header = run
      ? `<div class="assembly-head">${statusBadge(run)}
          <span class="muted">候选清单 ${escape(String(run.candidate_manifest_hash || '').slice(0, 12))}…
          · 求解器 ${escape(run.solver_name || '')} ${escape(run.solver_version || '')}</span></div>
          <p class="assembly-verdict" role="status">${escape(A.paperAssemblyVerdict(run))}</p>`
      : '<p class="muted">还没有运行记录。</p>';
    const messages = `${current.error
      ? `<div class="alert" role="alert">${escape(current.error)}</div>` : ''}
      ${current.message ? `<div class="notice" role="status">${escape(current.message)}</div>` : ''}`;
    return `<div class="assembly">
      ${messages}
      ${blueprintPicker(current)}
      ${supplyBlock(current)}
      <section class="panel assembly-run" aria-label="求解">
        <h4>求解</h4>${configForm(current)}${header}
        ${run ? diagnosticsBlock(run) : ''}
        ${run ? solutionsBlock(run, current) : ''}
        ${createdBlock(current)}
      </section>
      <section class="panel assembly-history" aria-label="运行历史">
        <h4>运行历史</h4>${runsBlock(current)}
      </section>
    </div>`;
  };

  A.views['paper-assembly'] = function () {
    const state = S();
    const course = A.course;
    const heading = A.sectionTitle(
      'CONSTRAINT ASSEMBLY',
      '约束组卷',
      '用求解器按蓝图的硬约束挑题：先看候选供给与预检，再跑一次求解，最后选一个方案落成草稿卷。',
      `<div class="button-row">${A.button('重新读取', 'assembly-refresh', 'refresh')}</div>`
    );
    if (!course) {
      return heading + `<div class="panel"><p class="muted">先选择一门课程。</p></div>`;
    }
    if (state.courseId !== course.id) {
      // 换课程后旧运行必须清空：否则会把别的课的方案当成这门课的。
      state.courseId = course.id;
      state.blueprints = [];
      state.selectedId = null;
      state.selected = null;
      state.runs = [];
      state.run = null;
      state.supply = null;
      state.created = null;
      state.loaded = false;
      state.message = null;
      state.error = null;
    }
    if (state.loading && !state.blueprints.length) {
      return heading + `<div class="panel"><p class="muted">正在读取蓝图与候选供给…</p></div>`;
    }
    if (state.error && !state.blueprints.length) {
      return heading + `<div class="panel"><h2>读不到组卷数据</h2>
        <p class="muted">${escape(state.error)}</p>
        <div class="button-row">${A.button('重试', 'assembly-refresh', 'refresh')}</div></div>`;
    }
    return heading + `<div class="panel">${A.paperAssemblyHtml(state)}</div>`;
  };

  A.after['paper-assembly'] = function () {
    A.loadPaperAssembly(A.course && A.course.id);
  };

  // ---------------------------------------------------------------------------
  // 输入
  // ---------------------------------------------------------------------------

  A.inputs['assembly-blueprint'] = input => {
    const state = S();
    const row = state.blueprints.find(item => item.id === input.value);
    if (!row) return;
    state.selected = row;
    state.selectedId = row.id;
    state.run = null;
    state.created = null;
    state.comparison = null;
    return run('switch', () => Promise.all([loadSupply(state), loadRuns(state)])
      .then(() => null));
  };

  A.inputs['assembly-seed'] = input => { S().config.random_seed = input.value; };
  A.inputs['assembly-seconds'] = input => { S().config.time_limit_seconds = input.value; };
  A.inputs['assembly-solutions'] = input => { S().config.max_solutions = input.value; };

  A.actions['assembly-open'] = function (button) {
    const state = S();
    const runId = button.dataset.run;
    if (!runId) return;
    return run('open', () => A.api.assemblyRun(runId).then(row => {
      state.run = row;
      return null;
    }));
  };

  // ---------------------------------------------------------------------------
  // 动作
  // ---------------------------------------------------------------------------

  A.actions['assembly-refresh'] = function () {
    S().loaded = false;
    return A.loadPaperAssembly(A.course && A.course.id, { force: true });
  };

  A.actions['assembly-start'] = function () {
    const state = S();
    if (!state.selected) {
      state.error = '先选一份蓝图版本';
      A.render();
      return Promise.resolve();
    }
    let config;
    try {
      config = A.paperAssemblyConfig(state.config);
    } catch (error) {
      state.error = error.message;
      A.render();
      return Promise.resolve();
    }
    return run('start', () => A.api.startAssemblyRun(state.selected.id, config, newKey('run'))
      .then(row => {
        if (row.queued) {
          // 排队路径：这时还没有结论，不能当成"求解完成"来说。
          state.queued = row;
          state.run = null;
          state.created = null;
          return `已排队（job ${String(row.job_id || '—')}）：求解在后台跑，`
            + '完成后刷新本页看结论。';
        }
        state.queued = null;
        state.run = row;
        state.created = null;
        state.runs = [row].concat((state.runs || []).filter(
          item => item.assembly_run_id !== row.assembly_run_id));
        return statusMessage(row);
      }));
  };

  /** 启动后的提示语必须按真实状态写，不能一律"成功"。 */
  function statusMessage(row) {
    if (!row) return '求解已返回。';
    if (row.status === 'optimal') return '求解完成：已证明最优，可以选方案建卷。';
    if (row.status === 'feasible') return '求完成解：有可行方案，但未证明最优（界面会如实标注）。';
    if (row.status === 'infeasible') return '这次运行判定无解：请按诊断逐条修改后重跑。';
    if (row.status === 'timeout') return '运行超时：找到的方案仍可用，但没有最优性结论。';
    if (row.status === 'failed') return '这次运行失败了：诊断里有具体原因。';
    return `运行状态：${A.paperAssemblyStatus(row)}。`;
  }

  A.actions['assembly-select'] = function (button) {
    const state = S();
    const index = Number(button.dataset.index);
    if (!state.run) {
      state.error = '先跑一次求解（或从历史里打开一次运行）';
      A.render();
      return Promise.resolve();
    }
    // A/B 身份：配对成功后再建卷，把分组与字母一起交上去。
    // 只在**已配成对**时才带身份——没配对就贴字母，等于给卷子写一个查不到来历的标签。
    const pair = state.pair && !state.pair.error ? state.pair : null;
    const variantGroup = pair ? (pair.variant_group_id || null) : null;
    const variantLabel = pair && variantGroup
      ? ((pair.labels || [])[index === 0 ? 0 : 1] || 'A')
      : null;
    return run('select', () => A.api.assemblySolutionPaper(
      state.run.assembly_run_id, index, newKey('solution'),
      variantGroup, variantLabel
    ).then(draft => {
      state.created = draft;
      return '草稿卷已建好：还要过整卷校验与双人复核，才能提交审核。';
    }));
  };

  /* 与另一次运行配 A/B：判定在服务端（compare_structure 同一套规则），
   * 这里只负责把"配不上"的原因显示清楚——绝不把拒绝画成一个绿色的成功。 */
  A.actions['assembly-pair'] = function (button) {
    const state = S();
    const peer = button.dataset.peer;
    if (!state.run || !peer) {
      state.error = '先打开一次有方案的运行，再选另一条历史运行来配对';
      A.render();
      return Promise.resolve();
    }
    return run('pair', () => A.api.assemblyPair(state.run.assembly_run_id, peer, 0, 0, '')
      .then(pair => {
        state.pair = pair;
        return `已配成 A/B：${(pair.labels || []).join(' / ')}（仍要分别送审与渲染）。`;
      })
      .catch(error => {
        const detail = (error && error.detail) || {};
        // 409 ab_pair_rejected 是"配不上"，不是系统故障：把它当成一种结果来展示。
        if (detail.code && String(detail.code).startsWith('ab_')) {
          state.pair = {
            error: detail.message || '这两次运行不能配成 A/B',
            issues: detail.issues || []
          };
          return null;
        }
        throw error;
      }));
  };

  /* 对照两个方案：只取结构事实（分值分布、共用题）。界面上必须同时出现
   * "不代表统计等值"——否则教师会把"结构一致"读成"难度一样"。 */
  A.actions['assembly-compare'] = function () {
    const state = S();
    if (!state.run || (state.run.solutions || []).length < 2) {
      state.error = '至少要有两个方案才能对照';
      A.render();
      return Promise.resolve();
    }
    return run('compare', () => A.api.assemblyComparison(
      state.run.assembly_run_id, 0, 1
    ).then(comparison => {
      state.comparison = comparison;
      return null;
    }));
  };
})();
