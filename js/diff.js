'use strict';
/* 文本差异。
 *
 * 为什么自己写：审核界面要显示的是"这次改了什么"，而教材题干的改动往往只是
 * 一个符号、一个下标、一处页码——整段并排显示看不出差别，必须定位到字符。
 *
 * 为什么按字符而不是按行：数学文本经常整段是一行（$...$ 里没有换行），
 * 按行比对会把"改了一个 ε"报成"整行都变了"。
 *
 * 复杂度 O(n·m)。超过上限就退化为"整段替换"——审核员仍然能看到前后两版，
 * 只是没有逐字高亮。宁可慢一点也要给出准确定位，但不能让页面卡死。
 */
(function () {
  const A = App;
  const CELL_LIMIT = 1_200_000;   // 约 1100 × 1100 字符

  A.diffText = function (before, after) {
    const a = String(before == null ? '' : before);
    const b = String(after == null ? '' : after);
    if (a === b) return [];
    if (a.length * b.length > CELL_LIMIT) {
      return [
        { type: 'del', text: a },
        { type: 'add', text: b }
      ];
    }

    const n = a.length, m = b.length;
    // dp[i][j] = a[i..] 与 b[j..] 的最长公共子序列长度
    const dp = new Array(n + 1);
    for (let i = 0; i <= n; i++) dp[i] = new Uint32Array(m + 1);
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        dp[i][j] = a[i] === b[j]
          ? dp[i + 1][j + 1] + 1
          : Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }

    const parts = [];
    const push = (type, char) => {
      const last = parts[parts.length - 1];
      if (last && last.type === type) last.text += char;
      else parts.push({ type, text: char });
    };

    let i = 0, j = 0;
    while (i < n && j < m) {
      if (a[i] === b[j]) {
        push('same', a[i]); i++; j++;
      } else if (dp[i + 1][j] >= dp[i][j + 1]) {
        push('del', a[i]); i++;
      } else {
        push('add', b[j]); j++;
      }
    }
    while (i < n) push('del', a[i++]);
    while (j < m) push('add', b[j++]);
    return parts;
  };

  /* 标量字段（难度、页码）不走字符差异，直接并列。 */
  A.diffField = function (before, after) {
    return { changed: String(before) !== String(after), before, after };
  };

  /* 集合字段（挂载的知识点）看增减。 */
  A.diffSet = function (before, after) {
    const from = before || [], to = after || [];
    return {
      added: to.filter(item => !from.includes(item)),
      removed: from.filter(item => !to.includes(item))
    };
  };
})();
