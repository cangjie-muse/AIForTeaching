#!/usr/bin/env node
/**
 * 把生成流水线产出的「知识节点.json」转成「知识节点.xlsx」。
 *
 * 为什么不在生成时直接写 xlsx：Python 侧的运行环境里没有 openpyxl（服务器上也不一定装得上），
 * 而导入器要的是 xlsx。生成流水线写 JSON（结构清楚、能进 git、能 diff），
 * 转表格这一步交给已经有 exceljs 的 Node 侧，各用各的长处。
 *
 * 用法：
 *   node scripts/pack_to_xlsx.cjs course-packs/analysis-ii/generated/job-12
 */
const fs = require('node:fs');
const path = require('node:path');
const ExcelJS = require('exceljs');

async function main() {
  const dir = process.argv[2];
  if (!dir) {
    console.error('用法：node scripts/pack_to_xlsx.cjs <资源包目录>');
    process.exit(1);
  }
  const source = path.join(dir, '知识节点.json');
  if (!fs.existsSync(source)) {
    console.error(`找不到 ${source}`);
    process.exit(1);
  }
  const { header, rows } = JSON.parse(fs.readFileSync(source, 'utf8'));

  const workbook = new ExcelJS.Workbook();
  const sheet = workbook.addWorksheet('知识节点');
  sheet.addRow(header);
  rows.forEach(row => sheet.addRow(row));
  sheet.getRow(1).font = { bold: true };
  sheet.columns = header.map((title, index) => ({
    width: index === header.length - 2 ? 40 : 18
  }));

  const target = path.join(dir, '知识节点.xlsx');
  await workbook.xlsx.writeFile(target);
  console.log(`已写入 ${target}（${rows.length} 行）`);
}

main().catch(error => {
  console.error(error.message);
  process.exit(1);
});
