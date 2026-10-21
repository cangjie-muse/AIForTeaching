#!/usr/bin/env node
/**
 * AI 问答页的渲染冒烟测试（node:vm，不启浏览器）。
 *
 * 这一页比审核页更容易"看起来正常、实际骗人"，所以断言集中在几处：
 *   - 拒答必须显示成拒答，不能伪装成正常回答
 *   - 引用必须带页码，指不出页码的答案不该被当真
 *   - 未解锁（403）要说"先提交作答"，而不是报"服务出错"
 *   - 后端不可达时给引导和地址输入框，不白屏
 *   - 回答内容经过 App.markdown（教材原文不可当 HTML 信任）
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');

const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  URLSearchParams,
  location: { hash: '#/qa' },
  fetch: () => Promise.reject(new Error('测试中不应发起真实请求')),
  localStorage: {
    store: new Map(),
    getItem(key) { return this.store.has(key) ? this.store.get(key) : null; },
    setItem(key, value) { this.store.set(key, String(value)); }
  },
  document: {
    getElementById: () => null,
    addEventListener() {},
    body: { classList: { add() {}, remove() {} } }
  }
};
sandbox.window = sandbox;
vm.createContext(sandbox);

const bundle = ['js/core.js', 'js/visual-identity.js', 'js/api-client.js', 'js/qa-chat.js']
  .map(file => fs.readFileSync(path.join(root, file), 'utf8'))
  .join('\n;\n');
vm.runInContext(bundle, sandbox, { filename: 'bundle.js' });

const App = sandbox.window.App;
assert.ok(App.views.qa, '视图未注册：App.views.qa');

const markdownCalls = [];
App.markdown = (source) => { markdownCalls.push(source); return '<div class="prose">[md]</div>'; };

const failures = [];
const check = (name, condition, detail = '') => {
  console.log(`  ${condition ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!condition) failures.push(name);
};

function reset(overrides) {
  App.state.qa = {
    status: 'ready', error: '', errorHint: '', messages: [], conversationId: null,
    mode: 'guide', question: '', courseId: null, exerciseId: null, courses: [], health: null,
    ...overrides
  };
}

// 1. 空页面
reset();
let html = App.views.qa();
check('空状态给出提问引导', html.includes('问一个具体的问题'));
check('空状态说明了两种模式的区别', html.includes('引导思考') && html.includes('完整解答'));
check('有输入框与提交按钮', html.includes('id="qa-input"') && html.includes('提问'));

// 2. 正常回答带引用
reset({
  health: { status: 'ok', models: { chat: { name: 'deepseek', model: 'deepseek-chat', data_scope: 'approved' } } },
  messages: [
    { role: 'user', content: '数列极限怎么定义' },
    {
      role: 'assistant',
      content: '先看 ε 的几何意义。',
      citations: [
        { ord: 1, page: 18, section_path: ['第一章 极限', '数列极限'], excerpt: '定义 1.1' },
        { ord: 2, page: 22, section_path: ['第一章 极限', '函数极限'], excerpt: '区别在于' }
      ],
      refused: false, mode: 'guide'
    }
  ]
});
html = App.views.qa();
check('用户消息出现', html.includes('数列极限怎么定义'));
check('回答经过 App.markdown', markdownCalls.some(text => String(text).includes('ε 的几何意义')));
check('引用显示页码', html.includes('第 18 页') && html.includes('第 22 页'));
check('引用带章节路径以便核对', html.includes('第一章 极限 / 数列极限'));
check('显示后端连接状态', html.includes('已连接'));
check('显示模型与数据流向', html.includes('deepseek-chat') && html.includes('已批准出校'));

// 3. 拒答必须看得出来
reset({
  messages: [{ role: 'assistant', content: '教材里没有可以引用的依据。', refused: true, citations: [] }]
});
html = App.views.qa();
check('拒答有明确标记', html.includes('拒答') && html.includes('qa-refused'));

// 4. 演示模式要自曝身份
reset({ messages: [{ role: 'assistant', content: '演示', demo: true, citations: [] }] });
html = App.views.qa();
check('演示模式标注出来', html.includes('演示模式'));

// 5. 完整解答：没有题目时不能点
reset();
html = App.views.qa();
check('未选题时完整解答被禁用', /data-mode="full"[^>]*disabled/.test(html));

reset({
  exerciseId: 'ex-1',
  courseId: 'analysis',
  courses: [{ id: 'analysis', title: '数学分析 I', chunk_count: 120 },
            { id: 'analysis-ii', title: '数学分析 II', chunk_count: 88 }]
});
html = App.views.qa();
check('有课程列表时可按课程缩小检索', html.includes('data-input="qa-course"') && html.includes('数学分析 II（88 片段）'));
check('当前课程被选中', /<option value="analysis" selected>/.test(html));
check('有题目上下文后可切完整解答', !/data-mode="full"[^>]*disabled/.test(html));
check('显示当前习题', html.includes('ex-1'));

// 6. 403 不是故障，是设计
markdownCalls.length = 0;
reset({
  messages: [
    { role: 'user', content: '给我完整答案' },
    { role: 'assistant', content: '先提交你的作答，再解锁完整解答。', refused: true, mode: 'full' }
  ]
});
html = App.views.qa();
// 正文经 App.markdown 渲染，探针里只剩 [md]，所以查探针收到的原文
check('未解锁提示保留原文', markdownCalls.some(text => String(text).includes('先提交你的作答')));
check('未解锁仍标注为拒答', html.includes('qa-refused'));

// 7. 后端不可达
reset({ error: '无法连接后端（http://127.0.0.1:8000）：Failed to fetch', errorHint: '请确认后端已启动' });
html = App.views.qa();
check('后端不可达时显示引导', html.includes('无法连接后端'));
check('提供后端地址输入框', html.includes('data-input="api-base"') && html.includes('data-action="qa-reconnect"'));

// 8. 发送中
reset({ status: 'sending', messages: [{ role: 'user', content: '问' }] });
html = App.views.qa();
check('发送中显示等待提示', html.includes('正在检索教材并组织回答'));
check('发送中输入按钮禁用', /type="submit"[^>]*disabled/.test(html));

// 9. 教材原文里的 HTML 不能当标签
reset({
  messages: [{ role: 'assistant', content: '正常', citations: [
    { ord: 1, page: 3, section_path: ['<img src=x onerror=alert(1)>'], excerpt: 'x' }
  ] }]
});
html = App.views.qa();
check('引用里的 HTML 被转义', html.includes('&lt;img src=x') && !html.includes('<img src=x'));

// 10. 前端课程 id 与后端课程 id 对不上：要么对上，要么明说
const backend = [{ id: 'demo-analysis-2', title: '数学分析（II）' }];
const matched = App.qaResolveCourse('数学分析II', backend);
check('标题归一化后能对上后端课程', matched.id === 'demo-analysis-2', JSON.stringify(matched));

const unmatched = App.qaResolveCourse('高等代数', backend);
check('对不上时不乱猜课程', unmatched.id === null);
check('对不上时说明检索范围', unmatched.note.includes('高等代数') && unmatched.note.includes('全部课程'));

reset({ courseNote: unmatched.note });
html = App.views.qa();
check('课程对不上时页面有提示', html.includes('后端没有与「高等代数」对应的课程'));

// 11. 习题页的入口指向真正的问答页
const learning = fs.readFileSync(path.join(root, 'js/learning.js'), 'utf8');
check('习题页有「问这道题」入口', /A\.link\('问这道题', 'qa'/.test(learning));
check('入口带上习题 id', /问这道题', 'qa'[^)]*\{ exercise: ex\.id \}/.test(learning));

console.log();
if (failures.length) {
  console.log(`失败 ${failures.length} 项：${failures.join(', ')}`);
  process.exit(1);
}
console.log('问答页渲染测试全部通过。');
