'use strict';
/* 接入后端模型的问答页。
 *
 * 为什么和「AI 学习助手」分成两个页面：
 *   那个页面是本地检索，断网也能用；这个页面必须连后端、会调用模型、会把
 *   教材片段发出去。两种行为混在一个界面里，学生分不清"这句是教材原文还是
 *   模型生成的"，而且一旦后端没起，整页就废了。分开之后各自的失败边界清楚。
 *
 * 页面要做对的三件事：
 *   1. 回答一律经 A.markdown（KaTeX + 净化），题干来自教材，不信任。
 *   2. 引用必须显示页码——模型的答案如果指不出教材哪一页，就不该被当真。
 *   3. 拒答要显示成"拒答"，不能伪装成正常回答。refused=true 时给灰底说明。
 */
(function () {
  const A = App, e = A.escape;

  A.state.qa = A.state.qa || {
    status: 'idle',        // idle | sending | ready
    error: '',
    messages: [],          // { role, content, citations, refused, demo, mode }
    conversationId: null,
    mode: 'guide',
    question: '',
    courseId: null,
    exerciseId: null,
    courses: [],
    courseNote: '',      // 前端课程 id 与后端课程对不上的时候要说清楚
    resolved: false,
    health: null
  };

  const S = () => A.state.qa;

  function modeSwitch() {
    const fullDisabled = !S().exerciseId;
    return `<div class="qa-mode" role="group" aria-label="回答模式">
      <button class="qa-mode-btn ${S().mode === 'guide' ? 'active' : ''}"
              data-action="qa-mode" data-mode="guide" aria-pressed="${S().mode === 'guide'}">
        引导思考
      </button>
      <button class="qa-mode-btn ${S().mode === 'full' ? 'active' : ''}"
              data-action="qa-mode" data-mode="full" aria-pressed="${S().mode === 'full'}"
              ${fullDisabled ? 'disabled title="完整解答需要先选一道题并提交作答"' : ''}>
        完整解答
      </button>
    </div>`;
  }

  // 没有片段就必然拒答，提前说清楚，比让学生反复试错好
  function courseWithoutTextbook(courses) {
    const current = courses.find(course => course.id === S().courseId);
    return current && (current.chunk_count ?? 0) === 0;
  }

  // 课程 id 匹配统一放在 api-client，问答题与作答提交共用一份规则
  const resolveCourse = (routeCourseId, courses) => A.api.matchCourse(routeCourseId, courses);

  A.qaResolveCourse = resolveCourse;

  function citationChips(citations) {
    if (!citations || !citations.length) return '';
    return `<div class="qa-citations">${citations.map(citation => `
      <span class="qa-cite" title="${e((citation.section_path || []).join(' / '))}">
        ${A.icon('book')}第 ${e(citation.page)} 页
      </span>`).join('')}</div>`;
  }

  function bubble(message) {
    if (message.role === 'user') {
      return `<div class="qa-msg user"><div class="qa-msg-body">${e(message.content)}</div></div>`;
    }
    const tags = [
      message.refused ? A.badge('拒答', 'amber') : '',
      message.demo ? A.badge('演示模式', 'blue') : '',
      message.mode === 'full' ? A.badge('完整解答', 'green') : ''
    ].join(' ');
    return `<div class="qa-msg assistant">
      <span class="qa-avatar">${A.icon('ai')}</span>
      <div class="qa-msg-body">
        ${tags ? `<div class="qa-tags">${tags}</div>` : ''}
        <div class="md ${message.refused ? 'qa-refused' : ''}">${A.markdown(message.content)}</div>
        ${citationChips(message.citations)}
      </div>
    </div>`;
  }

  function welcome() {
    return `<div class="qa-welcome">
      <span class="qa-avatar large">${A.icon('ai')}</span>
      <h2>问一个具体的问题</h2>
      <p>回答只依据课程教材片段，并标出页码。教材没有覆盖的内容，它会说没有依据，而不是编一个。</p>
      <div class="qa-hints">
        <span>${A.icon('info')}默认是「引导思考」：先给思路，不直接给答案</span>
        <span>${A.icon('info')}「完整解答」要先提交自己的作答才能解锁</span>
      </div>
    </div>`;
  }

  function statusAside() {
    const health = S().health;
    const chat = health?.models?.chat;
    // /health 总会返回配置里的默认端点，所以这里不能说"已配置"——
    // 只能说"后端认为它会用的模型是哪个、数据落在哪一类"。
    const scope = chat?.data_scope;
    const scopeLabel = scope === 'internal' ? '校内' : scope === 'approved' ? '已批准出校' : '未批准';
    const lines = [
      ['后端服务', health ? A.badge('已连接', 'green') : A.badge('未连接', 'amber')],
      ['对话模型', chat ? A.badge(e(chat.model), 'blue') : A.badge('未知', 'amber')],
      ['数据流向', A.badge(scopeLabel, scope === 'internal' ? 'green' : 'amber')],
      ['检索范围', A.badge(S().courseId ? '本课程' : '全库', '')],
      ['回答模式', A.badge(S().mode === 'guide' ? '引导思考' : '完整解答', '')]
    ];
    return `<aside class="qa-aside panel">
      <div class="eyebrow">SERVICE</div>
      <h3>服务状态</h3>
      ${lines.map(([label, value]) => `<div class="service-line"><span>${label}</span>${value}</div>`).join('')}
      <div class="divider"></div>
      <h4>回答是怎么来的</h4>
      <ol class="qa-flow">
        <li>按你的问题检索教材片段</li>
        <li>片段有字符上限，只传必要的部分</li>
        <li>模型只依据这些片段作答</li>
        <li>回答与页码一起落库，可回溯</li>
      </ol>
      <p class="muted small">教材若标注为「仅校内使用」，原文不会随外传端点发出。</p>
    </aside>`;
  }

  function errorBanner() {
    const state = S();
    if (!state.error) return '';
    return `<div class="qa-error panel">
      <b>${A.icon('info')}${e(state.error)}</b>
      ${state.errorHint ? `<p class="muted">${e(state.errorHint)}</p>` : ''}
      <div class="api-base-row">
        <label for="api-base-input">后端地址</label>
        <input id="api-base-input" data-input="api-base" value="${e(A.api.base())}"
               placeholder="http://127.0.0.1:8000" autocomplete="off">
        <button class="btn" data-action="qa-reconnect">重连</button>
      </div>
    </div>`;
  }

  A.views.qa = function () {
    const state = S();
    const heading = A.sectionTitle(
      'COURSE QA',
      'AI 问答',
      '依据课程教材回答，并标出页码；没有依据时明说没有。',
      `<div class="button-row">
        ${modeSwitch()}
        ${A.button('换个话题', 'qa-reset', 'refresh', '', 'small text-btn')}
      </div>`
    );
    const courses = state.courses || [];
    const contextLine = courses.length || state.exerciseId || state.courseNote
      ? `<div class="qa-context">
          ${courses.length ? `<label class="qa-course">
            <span>检索范围</span>
            <select data-input="qa-course">
              <option value="">全部课程</option>
              ${courses.map(course => `<option value="${e(course.id)}"${course.id === state.courseId ? ' selected' : ''}>${e(course.title)}（${course.chunk_count ?? 0} 片段）</option>`).join('')}
            </select>
          </label>` : ''}
          ${state.exerciseId ? `<span class="qa-exercise">${A.icon('link')}习题：${e(state.exerciseId)}</span>` : ''}
          ${courseWithoutTextbook(courses) ? `<span class="qa-warn">${A.icon('info')}这门课还没有教材片段，问答会拒答</span>` : ''}
          ${state.courseNote ? `<span class="qa-warn">${A.icon('info')}${e(state.courseNote)}</span>` : ''}
        </div>`
      : '';
    const messages = state.messages.length
      ? state.messages.map(bubble).join('')
      : welcome();
    const thinking = state.status === 'sending'
      ? `<div class="qa-msg assistant"><span class="qa-avatar">${A.icon('ai')}</span>
           <div class="qa-msg-body"><span class="qa-thinking">正在检索教材并组织回答…</span></div></div>`
      : '';
    return heading + errorBanner() + `
      <div class="qa-layout">
        <section class="qa-panel panel">
          ${contextLine}
          <div class="qa-messages" id="qa-messages" aria-live="polite">${messages}${thinking}</div>
          <form class="qa-composer" data-form="qa">
            <textarea id="qa-input" rows="2" placeholder="输入问题，例如：数列极限的 ε-N 定义怎么理解？"
                      aria-label="向课程问答提问">${e(state.question)}</textarea>
            <div class="qa-composer-foot">
              <span class="muted small">${state.mode === 'guide' ? '引导模式：只给思路，不直接给答案' : '完整模式：需要你已提交作答'}</span>
              <button class="btn primary" type="submit" ${state.status === 'sending' ? 'disabled' : ''}>
                ${A.icon('send')}提问
              </button>
            </div>
          </form>
        </section>
        ${statusAside()}
      </div>`;
  };

  A.after.qa = function () {
    if (S().status !== 'idle') return;
    // 从习题页进来时带 ?exercise=...，顺手把题目上下文接上
    S().exerciseId = A.route.params.get('exercise') || S().exerciseId;
    S().routeCourseId = A.route.params.get('course') || A.course?.id || null;
    loadHealth();
  };

  async function loadHealth() {
    try {
      S().health = await A.api.get('/api/v1/health');
    } catch (error) {
      S().health = null;
      if (error.status === 0) {
        S().error = error.message;
        S().errorHint = error.detail;
      }
    }
    try {
      S().courses = await A.api.courseList();
    } catch {
      S().courses = [];
    }
    if (!S().resolved) {
      const resolved = resolveCourse(S().routeCourseId, S().courses);
      S().courseId = resolved.id;
      S().courseNote = resolved.note;
      S().resolved = true;
    }
    S().status = 'ready';
    A.render();
  }

  function push(role, content, extra = {}) {
    S().messages.push({ role, content, ...extra });
  }

  async function send() {
    const state = S();
    const input = document.getElementById('qa-input');
    const question = (input ? input.value : state.question).trim();
    if (!question || state.status === 'sending') return;
    state.question = '';
    state.error = '';
    state.status = 'sending';
    push('user', question);
    A.render();

    try {
      const payload = await A.api.post('/api/v1/qa/ask', {
        question,
        course_id: state.courseId || null,
        exercise_id: state.exerciseId || null,
        mode: state.mode,
        conversation_id: state.conversationId
      });
      state.conversationId = payload.conversation_id;
      push('assistant', payload.answer_md, {
        citations: payload.citations || [],
        refused: Boolean(payload.refused),
        demo: Boolean(payload.demo),
        mode: payload.mode
      });
      state.status = 'ready';
    } catch (error) {
      state.status = 'ready';
      if (error.status === 403) {
        // 未解锁不是故障，是设计：把话说清楚，而不是弹一个红色报错
        push('assistant', error.detail || '先提交你的作答，再解锁完整解答。', { refused: true, mode: state.mode });
      } else if (error.status === 0) {
        state.error = error.message;
        state.errorHint = error.detail;
      } else {
        state.error = error.message;
        state.errorHint = '可以稍后重试；如果持续失败，检查模型端点与密钥配置。';
      }
    }
    A.render();
  }

  A.actions['qa-send'] = send;

  // 换课程等于换检索范围，旧对话里的引用不再成立，所以清空重开
  A.inputs['qa-course'] = function (select) {
    S().courseId = select.value || null;
    S().messages = [];
    S().conversationId = null;
    A.render();
  };

  A.actions['qa-mode'] = function (button) {
    S().mode = button.dataset.mode;
    A.render();
  };

  A.actions['qa-reset'] = function () {
    S().messages = [];
    S().conversationId = null;
    S().error = '';
    A.render();
  };

  A.actions['qa-reconnect'] = function () {
    S().status = 'idle';
    S().error = '';
    loadHealth();
  };

  // 表单提交：app.js 只代理了 chat 表单，这里自己接管
  document.addEventListener('submit', event => {
    if (!event.target.matches('[data-form="qa"]')) return;
    event.preventDefault();
    send();
  });
})();
