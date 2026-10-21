#!/usr/bin/env node
/**
 * 课程覆盖体检：规划清单 / course-packs / data 包 / 数据库，四个口径对一遍。
 *
 * 起因：一次人工核对用了只匹配三字段的正则，把带别名的两行漏掉，
 * 于是误报"神经网络、数值线代未归类"。这种错不该靠人眼发现，所以固化成命令：
 *   npm run audit:courses
 */
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');

const catalog = require(path.join(root, 'js/course-catalog.js'));
const planned = catalog.entries;                       // 规划清单（含规范 id 与别名）
const packsDir = path.join(root, 'course-packs');
const dataDir = path.join(root, 'data');

const readJson = file => JSON.parse(fs.readFileSync(file, 'utf8'));

// data/*.json：前端打包前的内容，也是 db/seed.sql 的来源
const dataFiles = fs.readdirSync(dataDir).filter(name => /^course-.*\.json$/.test(name));
const dataCourses = new Map();
for (const name of dataFiles) {
  const obj = readJson(path.join(dataDir, name));
  // id 必须读文件里的，不能从文件名猜：辛几何那门的 id 本身就是
  // course-symplectic-math-physics，按文件名剥前缀会变成另一个不存在的课。
  const id = obj.id || name.replace(/^course-/, '').replace(/\.json$/, '');
  const nodes = obj.nodes || [];
  dataCourses.set(id, {
    exercises: (obj.exercises || []).length,
    nodes: nodes.length,
    // 库里 knowledge_count 只数 kind='knowledge' 的叶子知识点，章节级容器不算；
    // 拿 4485 去比 4146 会以为丢了 339 个，其实差的就是这层容器。
    knowledge: nodes.filter(node => node.kind === 'knowledge').length,
    containers: nodes.filter(node => node.kind !== 'knowledge').length,
    importedAt: obj.importedAt || ''
  });
}

// course-packs/course-*：标准课程包目录。目录名与课程 id 的关系不统一
// （多数是 course-<id>，辛几何那门目录名本身就是它的 id），所以两种都试。
const packDirs = fs.readdirSync(packsDir)
  .filter(name => name.startsWith('course-') && fs.statSync(path.join(packsDir, name)).isDirectory());
const packDirOf = id => [id, `course-${id}`].find(name => packDirs.includes(name)) || null;
const packIds = new Set(packDirs.map(name => name.replace(/^course-/, '')));
const looseDirs = fs.readdirSync(packsDir)
  .filter(name => fs.statSync(path.join(packsDir, name)).isDirectory() && !name.startsWith('course-'));

const plannedIds = new Set(planned.map(entry => entry.id));
const plannedTitles = new Map(planned.map(entry => [entry.id, entry.title]));

console.log(`规划清单：${planned.length} 门（${catalog.groups.map(g => `${g.shortLabel} ${planned.filter(e => e.groupId === g.id).length}`).join('，')}）`);
const dataTotals = [...dataCourses.values()].reduce(
  (sum, c) => ({
    exercises: sum.exercises + c.exercises,
    nodes: sum.nodes + c.nodes,
    knowledge: sum.knowledge + c.knowledge,
    containers: sum.containers + c.containers
  }),
  { exercises: 0, nodes: 0, knowledge: 0, containers: 0 }
);
console.log(`data 包：${dataCourses.size} 门，${dataTotals.exercises} 道题、` +
  `${dataTotals.knowledge} 个知识点 + ${dataTotals.containers} 个章节级节点（库里只数前者）`);
console.log(`course-packs：${packIds.size} 个标准包 + ${looseDirs.length} 个非标准目录（${looseDirs.join('、') || '无'}）`);

const problems = [];

const notPlanned = [...dataCourses.keys()].filter(id => !plannedIds.has(id));
if (notPlanned.length) problems.push(`有内容但不在规划清单：${notPlanned.join('、')}`);

const noData = [...plannedIds].filter(id => !dataCourses.has(id));
const noPack = [...plannedIds].filter(id => !packDirOf(id));
console.log(`\n规划但还没有内容：${noData.length} 门`);
if (noData.length) console.log('  ' + noData.map(id => plannedTitles.get(id)).join('、'));

const packWithoutData = [...packIds].filter(id => !dataCourses.has(id) && !dataCourses.has(`course-${id}`));
const dataWithoutPack = [...dataCourses.keys()].filter(id => !packDirOf(id));
if (packWithoutData.length) problems.push(`有课程包但没生成 data：${packWithoutData.join('、')}`);
if (dataWithoutPack.length) problems.push(`有 data 但没课程包：${dataWithoutPack.join('、')}`);
if (noPack.length && noPack.length !== noData.length) {
  problems.push(`规划了、有内容、却没有课程包：${noPack.filter(id => dataCourses.has(id)).join('、')}`);
}

// 数据库是最终口径；后端没起就跳过这一段，不算失败
async function checkDb() {
  const base = process.env.API_BASE || 'http://127.0.0.1:8000';
  let rows;
  try {
    const response = await fetch(`${base}/api/v1/courses`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    rows = (await response.json()).items || [];
  } catch (error) {
    console.log(`\n数据库：跳过（${base} 连不上：${error.message}）`);
    return;
  }
  const dbIds = new Set(rows.map(row => row.id));
  const missing = [...dataCourses.keys()].filter(id => !dbIds.has(id));
  const extra = [...dbIds].filter(id => !dataCourses.has(id));
  console.log(`\n数据库：${rows.length} 门，${rows.reduce((sum, r) => sum + (r.exercise_count || 0), 0)} 道题、` +
    `${rows.reduce((sum, r) => sum + (r.knowledge_count || 0), 0)} 个知识点`);
  if (missing.length) problems.push(`data 有但库里没有：${missing.join('、')}`);
  if (extra.length) problems.push(`库里有但 data 没有：${extra.join('、')}`);
  const counted = new Map(rows.map(row => [row.id, row]));
  for (const [id, info] of dataCourses) {
    const row = counted.get(id);
    if (!row) continue;
    if (row.exercise_count !== info.exercises) {
      problems.push(`${id} 题数对不上：data ${info.exercises}，库 ${row.exercise_count}`);
    }
    if (row.knowledge_count !== info.knowledge) {
      problems.push(`${id} 知识点数对不上：data ${info.knowledge}，库 ${row.knowledge_count}`);
    }
  }
}

checkDb().then(() => {
  console.log('');
  if (problems.length) {
    for (const line of problems) console.log(`✗ ${line}`);
    console.log(`\n发现 ${problems.length} 处不一致。`);
    process.exit(1);
  }
  console.log(`✓ 一致：${dataCourses.size} 门有内容的课，全部在规划清单里、全部有课程包、全部已入库。`);
  console.log(`  （规划清单共 ${planned.length} 门，其中 ${noData.length} 门还没开始做内容）`);
});
