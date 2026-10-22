#!/usr/bin/env node
/**
 * Excel 读写（真跑 exceljs，不 mock）：写一份模板、再读回来，确认表头与数据一字不差。
 *
 * 这一层必须用真库测——xlsx 的坑几乎都在"写进去的和你读出来的是不是同一件事"上，
 * 拿假对象测等于什么都没测。
 */
const path = require('node:path');
const { Buffer } = require('node:buffer');

const xlsx = require(path.join(__dirname, '..', 'js', 'xlsx-sheet.js'));

const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};

(async () => {
  console.log('写表再读回来：');
  const header = ['账号', '姓名', '角色', '初始密码'];
  const rows = [['2023301001', '张三', '学生', ''], ['teacher02', '李四', '教师', 'InitPass2026']];
  const bytes = await xlsx.buildSheet({
    name: '账号', header, rows,
    notes: ['第一行是表头，别改列名。', '角色可以写 学生 / 教师 / 管理员。']
  });
  check('生成的是 xlsx（zip 头）', bytes[0] === 0x50 && bytes[1] === 0x4b
    && bytes[2] === 0x03 && bytes[3] === 0x04, [...bytes.slice(0, 4)].join(','));
  check('文件不小于 1 KB（有样式与说明页）', bytes.byteLength > 1024, String(bytes.byteLength));

  const grid = await xlsx.readGrid(bytes);
  check('工作表名保留', grid.sheetName === '账号', grid.sheetName);
  check('表头原样读回', JSON.stringify(grid.rows[0]) === JSON.stringify(header),
    JSON.stringify(grid.rows[0]));
  check('第一行数据原样读回', JSON.stringify(grid.rows[1]) === JSON.stringify(rows[0]),
    JSON.stringify(grid.rows[1]));
  check('中文与大小写都没走样', grid.rows[2][1] === '李四' && grid.rows[2][3] === 'InitPass2026',
    JSON.stringify(grid.rows[2]));
  check('说明页不混进数据（只读第一个工作表）', grid.rows.length === 3, String(grid.rows.length));

  console.log('\n边界：');
  const empty = await xlsx.buildSheet({ name: '名单', header: ['学号', '姓名'], rows: [] });
  const emptyGrid = await xlsx.readGrid(empty);
  check('只有表头时读回一行', emptyGrid.rows.length === 1, JSON.stringify(emptyGrid.rows));

  let failed = '';
  try { await xlsx.readGrid(Buffer.from('这不是 xlsx')); } catch (error) { failed = error.message; }
  check('给个假文件会报错而不是静默返回空表', Boolean(failed), failed.slice(0, 40));

  const longName = '一个特别特别长的班级名单工作表名字，长到明显超过三十一个字的上限了';
  const longSheet = await xlsx.buildSheet({ name: longName, header: ['学号'], rows: [['1']] });
  const longGrid = await xlsx.readGrid(longSheet);
  check('工作表名超过 31 字会被截断（否则 Excel 打不开）',
    longGrid.sheetName.length === 31 && longGrid.rows[0][0] === '学号',
    `${longName.length} → ${longGrid.sheetName.length}`);

  const numberCells = await xlsx.buildSheet({
    name: '数字', header: ['学号', '人数'], rows: [[2023301001, 42]]
  });
  const numberGrid = await xlsx.readGrid(numberCells);
  check('数字单元格读成字符串（学号不会被科学计数法改写）',
    numberGrid.rows[1][0] === '2023301001', numberGrid.rows[1][0]);

  const longText = '这是很长的一段说明文字，用来确认读写不会截断。'.repeat(4);
  const withWrap = await xlsx.buildSheet({
    name: '长文本', header: ['说明'], rows: [[longText]], widths: [60]
  });
  const wrapGrid = await xlsx.readGrid(withWrap);
  check('长文本不截断（题目、备注可能很长）', wrapGrid.rows[1][0] === longText,
    `${wrapGrid.rows[1][0].length} / ${longText.length}`);

  console.log();
  if (failures.length) {
    console.log(`失败 ${failures.length} 项：${failures.join('、')}`);
    process.exit(1);
  }
  console.log('Excel 读写测试全部通过。');
})();
