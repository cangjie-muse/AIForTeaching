/* 资源包体检：上传之前先在本机看一遍，哪个包会在哪一步被拒。
 *
 *   node scripts/check-pack.cjs "D:\\教材资源包"          # 查这个目录下所有资源包
 *   node scripts/check-pack.cjs "D:\\教材\\拓扑学"       # 只查这一个
 *
 * 为什么要有它：平台里的报错是一条一条来的——第一个包卡住，改完再传第二个，
 * 二十几本要来回十几趟。这里把同样的解析与校验在本机跑一遍，一次看全。
 *
 * 判定用的就是浏览器里那套代码（js/import-core.js + js/xlsx-reader.js），
 * 所以"这里通过、上传被拒"只可能是服务端的事（课程 id 冲突、权限、库约束）。
 */
const fs = require('node:fs');
const path = require('node:path');
const CourseImport = require('../js/import-core.js');
const CourseXlsx = require('../js/xlsx-reader.js');

const FILES = { nodes: '知识节点.xlsx', knowledge: '知识点.md', exercises: '习题.md' };
const checks = [];

function say(text) { console.log(text); }

/* 一个目录算不算资源包：三个文件里有任意一个就算，缺的会报出来 */
function looksLikePack(dir) {
  return Object.values(FILES).some(name => fs.existsSync(path.join(dir, name)));
}

function collectPacks(target) {
  if (!fs.existsSync(target)) throw new Error(`找不到路径：${target}`);
  if (fs.statSync(target).isFile()) return [path.dirname(target)];
  if (looksLikePack(target)) return [target];
  return fs.readdirSync(target, { withFileTypes: true })
    .filter(entry => entry.isDirectory())
    .map(entry => path.join(target, entry.name))
    .filter(looksLikePack);
}

async function checkPack(dir) {
  const report = { dir: path.basename(dir), problems: [], notes: [] };
  const missing = Object.entries(FILES)
    .filter(([, name]) => !fs.existsSync(path.join(dir, name)))
    .map(([, name]) => name);
  if (missing.length) report.problems.push(`缺文件：${missing.join('、')}`);
  if (missing.length) return report;

  const bytes = fs.readFileSync(path.join(dir, FILES.nodes));
  let rows;
  try {
    rows = await CourseXlsx.readRows(bytes);
  } catch (error) {
    report.problems.push(`知识节点.xlsx 读不出来：${error.message}`);
    return report;
  }
  report.rows = rows.length;

  const knowledge = fs.readFileSync(path.join(dir, FILES.knowledge), 'utf8');
  const exercises = fs.readFileSync(path.join(dir, FILES.exercises), 'utf8');

  /* 可疑点：这些不会立刻报错，但通常是"导进去发现少东西"的原因 */
  const anchorsInKnowledge = new Set([...knowledge.matchAll(/<a id="([^"]+)"><\/a>/g)].map(m => m[1]));
  const anchorsInExercises = new Set([...exercises.matchAll(/<a id="([^"]+)"><\/a>/g)].map(m => m[1]));
  const markers = (exercises.match(/source-exercise:/g) || []).length;
  const oddNumbering = (exercises.match(/^\s*\d+[、．)）]/gm) || []).length;
  const answers = (exercises.match(/\*\*答案：\*\*/g) || []).length;
  const meta = (exercises.match(/\*\*难度：/g) || []).length;

  if (!anchorsInKnowledge.size) report.notes.push('知识点.md 里没有 <a id="…"> 锚点');
  if (!markers) report.notes.push('习题.md 没有 source-exercise 标记（会按题号补稳定 ID）');
  if (oddNumbering) report.notes.push(`有 ${oddNumbering} 处题号不是"1."格式（是"1、"或"1) "），可能拆不开题`);
  if (answers !== meta) report.notes.push(`答案 ${answers} 处、难度行 ${meta} 处，数量不一致`);

  try {
    const pkg = CourseImport.parsePackage(rows, knowledge, exercises, {});
    report.nodes = pkg.nodes.length;
    report.exercises = pkg.exercises.length;
    report.id = pkg.id;
    report.warnings = pkg.warnings || [];
    const orphanKnowledge = [...anchorsInKnowledge]
      .filter(anchor => !pkg.nodes.some(node => node.id === anchor));
    if (orphanKnowledge.length) {
      report.notes.push(`${orphanKnowledge.length} 个锚点在表里没有对应行（不会导入）`);
    }
  } catch (error) {
    report.problems.push(error.message);
  }
  return report;
}

function printReport(report) {
  const head = `\n${report.dir}`;
  if (!report.problems.length) {
    say(`${head}  ✓ ${report.nodes} 个知识点 / ${report.exercises} 道题  （课程 id：${report.id}）`);
  } else {
    say(`${head}  ✗ ${report.problems.join('；')}`);
  }
  if (report.rows !== undefined) say(`    表格 ${report.rows} 行`);
  for (const note of report.notes || []) say(`    · ${note}`);
  for (const warning of (report.warnings || []).slice(0, 3)) say(`    · ${warning}`);
  if ((report.warnings || []).length > 3) say(`    · …还有 ${report.warnings.length - 3} 条提示`);
}

(async () => {
  const targets = process.argv.slice(2);
  if (!targets.length) {
    console.error('用法：node scripts/check-pack.cjs <资源包目录或它们的上级目录> [...]');
    process.exit(2);
  }
  let failed = 0;
  let total = 0;
  for (const target of targets) {
    let dirs;
    try {
      dirs = collectPacks(target);
    } catch (error) {
      console.error(`✗ ${error.message}`);
      failed += 1;
      continue;
    }
    if (!dirs.length) {
      console.error(`✗ ${target} 下没有找到资源包（要有 知识节点.xlsx / 知识点.md / 习题.md）`);
      failed += 1;
      continue;
    }
    for (const dir of dirs) {
      total += 1;
      const report = await checkPack(dir);
      if (report.problems.length) failed += 1;
      printReport(report);
      checks.push(report);
    }
  }
  say(`\n合计 ${total} 个包：${total - failed} 通过，${failed} 失败。`);
  if (failed) {
    say('失败的原因只来自解析与校验；如果这里通过但平台仍拒绝，问题在服务端（课程 id 冲突、权限、库约束）。');
    process.exit(1);
  }
})();
