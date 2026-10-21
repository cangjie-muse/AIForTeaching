#!/usr/bin/env node
/**
 * 文本差异的单元测试。
 *
 * 差异算错比没有差异更糟：审核员会照着错的高亮下判断。
 * 所以这里断言的不只是"能跑"，而是几种必须正确的情形：
 *   - 改一个字符，只高亮那一个，其余保持"未变"
 *   - 插入与删除分开标，不混成一团
 *   - 前后相同时返回空数组（调用方据此显示"无变化"）
 *   - 超长文本退化为整段替换，而不是卡死
 *   - 数学符号（ε、下标、反斜杠）逐字保留，不被转义或吞掉
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');

const sandbox = { console };
sandbox.window = sandbox;
vm.createContext(sandbox);
for (const file of ['js/core.js', 'js/diff.js']) {
  vm.runInContext(fs.readFileSync(path.join(root, file), 'utf8'), sandbox, { filename: file });
}
const App = sandbox.window.App;
assert.ok(App.diffText, '未注册：App.diffText');

const failures = [];
const check = (name, condition, detail = '') => {
  console.log(`  ${condition ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!condition) failures.push(name);
};

const flat = parts => parts.map(p => `${p.type}:${p.text}`).join('|');
const same = parts => parts.filter(p => p.type === 'same').map(p => p.text).join('');
const added = parts => parts.filter(p => p.type === 'add').map(p => p.text).join('');

check('完全相同返回空', App.diffText('求极限', '求极限').length === 0);

// 单字符修改
const one = App.diffText('求 $\\varepsilon$-N 极限', '求 $\\delta$-N 极限');
// 字符级差异：\\varepsilon → \\delta 会被拆成若干 del/add，这是对的（定位到字符）
check('改一个符号能定位到那一段',
  !same(one).includes('varepsilon') && !same(one).includes('delta')
  && one.some(part => part.type === 'del') && one.some(part => part.type === 'add'),
  flat(one));

// 纯插入
const insert = App.diffText('求极限', '求数列极限');
check('插入只标记新增', flat(insert) === 'same:求|add:数列|same:极限', flat(insert));

// 纯删除
const remove = App.diffText('求数列极限', '求极限');
check('删除只标记移除', flat(remove) === 'same:求|del:数列|same:极限', flat(remove));

// 重排（顺序不同）不应算作未变
const reorder = App.diffText('abc', 'acb');
check('顺序变化会被发现', reorder.some(p => p.type !== 'same'), flat(reorder));

// 前导空白与换行的变化不能吞掉
const spacing = App.diffText('设 $a_n$ 收敛', '设  $a_n$ 收敛');
check('多余空格也算变化', spacing.some(p => p.type === 'add' && p.text === ' '), flat(spacing));

// LaTeX 里的反斜杠与花括号必须原样保留
const latex = App.diffText('\\frac{1}{n}', '\\frac{1}{n^2}');
check('LaTeX 原样保留', same(latex) === '\\frac{1}{n}' && added(latex) === '^2', flat(latex));

// 超长文本退化为整段替换
const long = 'x'.repeat(1200), other = 'y'.repeat(1200);
const fallback = App.diffText(long, other);
check('超长文本退化为整段替换', fallback.length === 2 && fallback[0].type === 'del' && fallback[1].type === 'add');

// 中文与标点的位置要准
const chinese = App.diffText('若数列收敛，则其极限唯一。', '若数列收敛，则其极限唯一且相等。');
check('中文追加被准确定位',
  same(chinese) === '若数列收敛，则其极限唯一。' && added(chinese) === '且相等', flat(chinese));

// 标量比较
const field = App.diffField(2, 3);
check('标量变化被识别', field.changed === true);
check('标量相同不报变化', App.diffField('18', '18').changed === false);

// 集合比较
const set = App.diffSet(['n-1', 'n-2'], ['n-2', 'n-3']);
check('集合识别新增', set.added.length === 1 && set.added[0] === 'n-3', JSON.stringify(set));
check('集合识别移除', set.removed.length === 1 && set.removed[0] === 'n-1', JSON.stringify(set));

console.log();
if (failures.length) {
  console.log(`失败 ${failures.length} 项：${failures.join(', ')}`);
  process.exit(1);
}
console.log('差异测试全部通过。');
