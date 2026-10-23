/* 知识节点.xlsx 的两条解析路径。
 *
 * 为什么要有兜底：exceljs 对"不完全标准"的工作簿很挑（WPS 另存、程序生成的
 * 带命名空间前缀的 xlsx），抛出的往往是一句 "Cannot read properties of
 * undefined"。文件本身用 Excel 打开却完全正常。兜底直接读 OOXML，只认单元格
 * 文本，够导入器用。
 *
 * 这里用仓库里的真实资源包对拍两条路：行数、表头、前 60 行逐格。
 */
const fs = require('node:fs');
const path = require('node:path');
const { readRows, readRowsStrict, readRowsRaw } = require('../js/xlsx-reader.js');

const checks = [];
const check = (name, ok, detail = '') => checks.push([name, Boolean(ok), detail]);

/* exceljs 给的是数字或富文本对象，兜底给的是文本：先统一成文本再比 */
const text = value => {
  if (value == null) return '';
  if (typeof value === 'object') {
    if (value.richText) return value.richText.map(run => run.text).join('');
    return String(value.text ?? value.result ?? '');
  }
  return String(value);
};

(async () => {
  const file = path.join(__dirname, '..', 'course-packs', 'course-analysis', '知识节点.xlsx');
  check('仓库里有可对拍的资源包', fs.existsSync(file), file);
  if (!fs.existsSync(file)) return finish();
  const bytes = fs.readFileSync(file);

  const strict = await readRowsStrict(bytes);
  const raw = await readRowsRaw(bytes);
  check('两条路行数一致', strict.length === raw.length, `${strict.length} vs ${raw.length}`);
  check('表头一致', strict[0].map(text).join('|') === raw[0].join('|'), raw[0].join('|'));
  check('首行是八列表头', raw[0][0] === '1级节点' && raw[0][7] === '知识点标签');
  check('每一行都补齐八列', raw.every(row => row.length === 8));

  let mismatches = [];
  for (let r = 0; r < Math.min(60, raw.length); r += 1) {
    for (let c = 0; c < 8; c += 1) {
      if (text(strict[r]?.[c]) !== raw[r][c]) mismatches.push(`第${r + 1}行第${c + 1}列`);
    }
  }
  check('前 60 行逐格一致', mismatches.length === 0, mismatches.slice(0, 5).join('、'));

  const merged = await readRows(bytes);
  check('兜底入口正常返回', merged.length === strict.length);
  check('兜底入口内容不变', merged[1].map(text).join('|') === raw[1].join('|'));

  // 带前缀的 XML 是这批文件的常态，必须能剥掉
  check('命名空间前缀被正确处理', raw[1][7].includes('知识点.md#'),
    raw[1][7].slice(0, 40));

  let threw = false;
  try { await readRowsRaw(Uint8Array.from([1, 2, 3, 4])); } catch { threw = true; }
  check('不是 xlsx 就报错，不返回空表', threw);

  // 多工作表是有意拒绝的规则，不能被兜底悄悄改成"只读第一张表"
  const ExcelJS = require('exceljs');
  const book = new ExcelJS.Workbook();
  book.addWorksheet('课程').addRow(['1级节点', '', '', '', '', '', '', '']);
  book.addWorksheet('另一张').addRow(['x']);
  const twoSheets = new Uint8Array(await book.xlsx.writeBuffer());
  let rejected = '';
  try { await readRows(twoSheets); } catch (error) { rejected = error.message; }
  check('多工作表仍被拒绝', rejected.includes('一个课程工作表'), rejected);

  finish();
})().catch(error => {
  console.error(`  ✗ 抛异常：${error.message}`);
  process.exit(1);
});

function finish() {
  for (const [name, ok, detail] of checks) {
    console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? '  — ' + detail : ''}`);
  }
  const failed = checks.filter(([, ok]) => !ok).length;
  if (failed) {
    console.error(`\nxlsx 解析测试失败 ${failed} 项。`);
    process.exit(1);
  }
  console.log(`\nxlsx 解析测试通过（${checks.length} 项）。`);
}
