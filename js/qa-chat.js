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
        // 滚动：整个消息区每次重渲染都会被重建，scrollTop 归零。
    // 记下之前的位置与"是否贴着底部"，渲染后放回去。
    scrollTop: 0,
    stickBottom: true,
    pinBottom: false,     // 刚发出问题：无视当前位置，直接贴底
    focusComposer: false  // 一轮问答结束后把光标放回输入框
  };

  const S = () => A.state.qa;

  function messageBox() {
    return document.getElementById('qa-messages');
  }

  /* 在渲染**之前**记下滚动位置。
   *
   * A.views.qa 被调用时旧 DOM 还在，所以这里能读到真实的 scrollTop；
   * A.render 之后新节点从 0 开始，再由 restoreScroll 放回去。
   * 不挂在 scroll 事件上：事件要节流、还要处理"滚到底又回上来"，
   * 而每次重渲染都要读一次位置，直接读比监听更准。 */
  function captureScroll() {
    const state = S();
    const box = messageBox();
    if (!box) return;
    if (state.pinBottom) { state.stickBottom = true; return; }
    const gap = box.scrollHeight - box.scrollTop - (box.clientHeight || 0);
    state.scrollTop = box.scrollTop;
    // 32px 容差：差一两行也算"在底部"，不要因为像素误差把人钉在中间
    state.stickBottom = gap < 32;
  }

  function restoreScroll() {
    const state = S();
    const box = messageBox();
    if (!box) return;
    if (state.stickBottom) {
      // 新消息在场时贴底：这是聊天窗口的默认行为，也是"别让我手动往下滑"的答案
      box.scrollTop = box.scrollHeight;
    } else if (typeof state.scrollTop === 'number') {
      box.scrollTop = state.scrollTop;
    }
  }

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

  // 引用的位置有两种：教材切片的"第 N 页"，和知识点语料的"知识点：某节"。
  // 知识点没有精确页（只有章节起页），所以显示成知识点路径，而不是编一个页码。
  function citationLabel(citation) {
    const path = (citation.section_path || []).join(' / ');
    if (citation.page === null || citation.page === undefined) {
      return {
        icon: A.icon('graph'),
        text: citation.node_title || path.split(' / ').pop() || '知识点',
        title: path || citation.textbook || '课程知识点'
      };
    }
    return { icon: A.icon('book'), text: `第 ${citation.page} 页`, title: path };
  }

  function citationChips(citations) {
    if (!citations || !citations.length) return '';
    return `<div class="qa-citations">${citations.map(citation => {
      const label = citationLabel(citation);
      return `<span class="qa-cite" title="${e(label.title)}">${label.icon}${e(label.text)}</span>`;
    }).join('')}</div>`;
  }

  function bubble(message) {
    if (message.role === 'user') {
      return `<div class="qa-msg user"><div class="qa-msg-body">${e(message.content)}</div></div>`;
    }
    const tags = [
      message.refused ? A.badge('拒答', 'amber') : '',
      // 没走教材的回答必须一眼看出来。灰色不行——它要能阻止人把结论当真。
      message.grounded === false ? A.badge('通用知识·未依据教材', 'amber') : '',
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
      <p>回答优先依据课程材料（教材片段或知识点正文）并标出出处。材料里找不到依据时，它会改用模型的通用知识回答，但会明确标注「通用知识·未依据教材」——那种回答没有经过教师确认，请自行核对。</p>
      <div class="qa-hints">
        <span>${A.icon('info')}默认是「引导思考」：先给思路，不直接给答案</span>
        <span>${A.icon('info')}「完整解答」要先提交自己的作答才能解锁</span>
        <span>${A.icon('info')}教材标注「仅校内使用」时，原文不会随外传端点发出</span>
      </div>
    </div>`;
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
    captureScroll();
    const heading = A.sectionTitle(
      'COURSE QA',
      'AI 问答',
      '依据课程材料回答，并标出出处（教材页码或知识点）；没有依据时明说没有。',
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
      </section>`;
  };

  A.after.qa = function () {
    // 恢复滚动要在任何提前 return 之前：发送中、出错时同样需要它
    restoreScroll();
    const state = S();
    if (state.focusComposer) {
      state.focusComposer = false;
      // preventScroll：重建后浏览器默认会把焦点元素滚进视口，那会抵消上面的恢复
      document.getElementById('qa-input')?.focus({ preventScroll: true });
    }
    if (S().status !== 'idle') return;
    // 从习题页进来时带 ?exercise=...，顺手把题目上下文接上
    S().exerciseId = A.route.params.get('exercise') || S().exerciseId;
    S().routeCourseId = A.route.params.get('course') || A.course?.id || null;
    loadHealth();
  };

  async function loadHealth() {
    // /health 只当连通性探针：后端挂了要在页面上明说。模型名、数据流向这些
    // 原来显示在右侧「服务状态」栏里，那栏已删——真出问题时看错误横幅就够。
    try {
      await A.api.get('/api/v1/health');
    } catch (error) {
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
    state.pinBottom = true;
    push('user', question);
    A.render();
    state.pinBottom = false;

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
        // 后端没给这个字段时按"有依据"处理：老数据没有 basis，不该被误标成通用知识
        grounded: payload.grounded !== false,
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
