/* 老资源包（没有 <!-- source-exercise:ID --> 标记）能不能导进来。
 *
 * 二十几本教材是用旧版提示词生成的，题目只写了 "1. …" 没有稳定 ID。
 * 导入器原来直接抛错，整包进不来。现在按题目内容补 ID，这个脚本盯三件事：
 *   1. 有标记的包，ID 一个字都不能变（不能因为改兼容就改掉原始题号）；
 *   2. 没标记的包，补出来的 ID 必须稳定（同一份文件两次解析得到同一批 ID）；
 *   3. 答案里缩进的解题步骤不能被当成新题。
 */
const assert = require('node:assert/strict');
const { parsePackage, headers } = require('../js/import-core.js');

const ref = id => `知识内容：知识点.md#${id}\n习题：习题.md#${id}`;
const row = (levels, id) => [...Array.from({ length: 6 }, (_, i) => levels[i] || ''), '来源', ref(id)];
const rows = [
  headers,
  row(['拓扑学'], 'course-topo'),
  row(['拓扑学', '拓扑空间'], 'chapter-space'),
  row(['拓扑学', '拓扑空间', '拓扑空间的定义'], 'kp-def'),
  row(['拓扑学', '拓扑空间', '开集与闭集'], 'kp-open')
];
const section = (id, title, body = '', level = '###') =>
  `<a id="${id}"></a>\n${level} ${title}\n${body}\n`;
const book =
  section('course-topo', '拓扑学', '', '#') +
  section('chapter-space', '拓扑空间', '', '##') +
  section('kp-def', '拓扑空间的定义', '**图谱路径：** 拓扑学 / 拓扑空间 / 拓扑空间的定义\n\n设 $X$ 是非空集合。') +
  section('kp-open', '开集与闭集', '开集是拓扑的成员。');

const shell =
  section('course-topo', '拓扑学', '', '#') +
  section('chapter-space', '拓扑空间', '', '##');

const q = (text, answer, step = '') =>
  `\n${text}\n\n   **答案：** ${answer}${step}\n\n   **难度：1/5；出处：** 教材 1.1\n`;

const marked =
  shell + section('kp-def', '拓扑空间的定义',
    `<!-- source-exercise:book-p012-1 -->${q('证明空集是开集。', '拓扑公理给出。')}` +
    `<!-- source-exercise:book-p012-2 -->${q('证明全集是开集。', '同上。')}`) +
  section('kp-open', '开集与闭集', `<!-- source-exercise:book-p014-1 -->${q('举例说明离散拓扑。', '任取幂集。')}`);

const unmarked =
  shell + section('kp-def', '拓扑空间的定义',
    q('1. 证明空集是开集。', '拓扑公理给出。') +
    q('2. 证明全集是开集。', '同上。')) +
  section('kp-open', '开集与闭集', q('1. 举例说明离散拓扑。', '任取幂集。'));

const checks = [];
const check = (name, ok) => { checks.push([name, Boolean(ok)]); };

// 1. 有标记：ID 保持原样，一个都不许改写
const withMarks = parsePackage(rows, book, marked);
check('有标记的包仍用原始题号',
  withMarks.exercises.map(ex => ex.id).sort().join(',') === 'book-p012-1,book-p012-2,book-p014-1');
check('有标记的包不报"补 ID"', withMarks.stats.synthesized === 0);
check('有标记的包三题两知识点',
  withMarks.exercises.length === 3 && withMarks.stats.associations === 3);

// 2. 没标记：能导入，且 ID 稳定、按内容区分
const first = parsePackage(rows, book, unmarked);
const second = parsePackage(rows, book, unmarked);
check('没标记的包也能导入', first.exercises.length === 3);
check('补的 ID 都以 q- 开头', first.exercises.every(ex => /^q-[0-9a-f]{16}$/.test(ex.id)));
check('两次解析得到同一批 ID',
  first.exercises.map(ex => ex.id).sort().join(',') === second.exercises.map(ex => ex.id).sort().join(','));
check('题干不同则 ID 不同', new Set(first.exercises.map(ex => ex.id)).size === first.exercises.length);
check('补 ID 的数量有上报', first.stats.synthesized === 3);
check('预览里有解释这条警告',
  (first.warnings || []).some(text => text.includes('source-exercise')));

// 3. 内容变了就是新题（同一 ID 不能一格两用）
const edited = parsePackage(rows, book, unmarked.replace('证明空集是开集。', '证明单点集是闭集。'));
check('改过题干的题拿到新 ID',
  !edited.exercises.some(ex => first.exercises.some(old => old.id === ex.id && old.prompt !== ex.prompt)));

// 4. 答案里缩进的解题步骤不算新题
const stepped = shell + section('kp-def', '拓扑空间的定义',
  q('1. 证明空集是开集。', '分两步。', '\n\n   1. 空集不含元素。\n   2. 由公理得证。')) +
  section('kp-open', '开集与闭集', '');
const withSteps = parsePackage(rows, book, stepped);
check('缩进的解题步骤不被当成新题', withSteps.exercises.length === 1);
check('步骤留在解答里', withSteps.exercises[0].answer.includes('空集不含元素'));

// 5. 有答案但无序号的段落：这是真的坏了，必须报错而不是悄悄丢掉
const broken = shell + section('kp-def', '拓扑空间的定义', '\n证明空集是开集。\n\n   **答案：** 略。\n\n   **难度：1/5；出处：** 教材。\n') +
  section('kp-open', '开集与闭集', '');
check('有答案但没题号仍报错', (() => {
  try { parsePackage(rows, book, broken); return false; }
  catch (error) { return /既没有 source-exercise 标记，也找不到题号/.test(error.message); }
})());

// 6. 同一道题出现在两个知识点：一条题两条关联（和原来一致）
const shared = shell +
  section('kp-def', '拓扑空间的定义', q('1. 证明空集是开集。', '略。')) +
  section('kp-open', '开集与闭集', q('1. 证明空集是开集。', '略。'));
const sharedPack = parsePackage(rows, book, shared);
check('同一道题两处出现只算一条', sharedPack.exercises.length === 1);
check('两处都挂了关联', sharedPack.stats.associations === 2);

// 7. 题目缺字段时，报错要说清缺哪一段、是哪道题，并且一次列全
const noAnswer = shell +
  section('kp-def', '拓扑空间的定义',
    '<!-- source-exercise:book-p020-1 -->\n证明紧集是闭集。\n\n   **难度：1/5；出处：** 教材 2.1\n') +
  section('kp-open', '开集与闭集', '');
const noAnswerError = (() => {
  try { parsePackage(rows, book, noAnswer); return ''; } catch (error) { return error.message; }
})();
check('缺答案时点名缺的是答案', /缺少「\*\*答案：\*\*」/.test(noAnswerError));
check('缺答案时带出题面开头', noAnswerError.includes('证明紧集是闭集'));

const swapped = shell +
  section('kp-def', '拓扑空间的定义',
    '<!-- source-exercise:book-p021-1 -->\n证明开集补集是闭集。\n\n   **难度：1/5；出处：** 教材。\n\n   **答案：** 略。\n') +
  section('kp-open', '开集与闭集', '');
const swappedError = (() => {
  try { parsePackage(rows, book, swapped); return ''; } catch (error) { return error.message; }
})();
check('顺序倒了说清要对调', /写在了.*前面/.test(swappedError) && /对调/.test(swappedError));

const twoBad = shell +
  section('kp-def', '拓扑空间的定义',
    '<!-- source-exercise:book-p022-1 -->\n第一道坏题。\n\n   **难度：1/5；出处：** 教材。\n' +
    '<!-- source-exercise:book-p022-2 -->\n第二道坏题。\n\n   **难度：2/5；出处：** 教材。\n') +
  section('kp-open', '开集与闭集', '');
const twoBadError = (() => {
  try { parsePackage(rows, book, twoBad); return ''; } catch (error) { return error.message; }
})();
check('两道坏题一次列全', /有 2 道习题格式不对/.test(twoBadError) &&
  twoBadError.includes('第一道坏题') && twoBadError.includes('第二道坏题'));

let failures = 0;
for (const [name, ok] of checks) {
  if (!ok) failures += 1;
  console.log(`  ${ok ? '✓' : '✗'} ${name}`);
}
if (failures) {
  console.error(`\n资源包兼容性测试失败 ${failures} 项。`);
  process.exit(1);
}
console.log(`\n资源包兼容性测试通过（${checks.length} 项）。`);
assert.ok(checks.length >= 12);
