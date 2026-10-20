const fs = require('node:fs/promises');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { readRows } = require('../js/xlsx-reader.js');
const { parsePackage, referencedImages } = require('../js/import-core.js');
async function main() {
  const source = process.argv[2];
  if (!source) throw new Error('用法：npm run import:course -- "/完整路径/课程资源包"');
  const project = path.resolve(__dirname, '..');
  const rows = await readRows(await fs.readFile(path.join(source, '知识节点.xlsx')));
  const knowledge = await fs.readFile(path.join(source, '知识点.md'), 'utf8');
  const exercises = await fs.readFile(path.join(source, '习题.md'), 'utf8');
  let metadata = {};
  try { metadata = JSON.parse(await fs.readFile(path.join(source, 'course.json'), 'utf8')); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  const course = parsePackage(rows, knowledge, exercises, metadata);
  const safeId = course.id.replace(/[^a-zA-Z0-9_-]/g, '-');
  const folder = safeId === course.id ? 'course-' + safeId.replace(/^course-/, '') : 'course-' + createHash('sha256').update(course.id).digest('hex').slice(0, 16);
  const variantText = course.exercises.flatMap(exercise => (exercise.variants || [exercise]).flatMap(variant => [variant.prompt, variant.answer]));
  const images = referencedImages(...course.nodes.map(node => node.content), ...variantText);
  for (const image of images) {
    const file = await fs.stat(path.join(source, image));
    if (!file.isFile()) throw new Error(`插图不是文件：${image}`);
  }
  course.resourceBase = `course-packs/${folder}/`;
  const destination = path.join(project, course.resourceBase);
  const packRoot = path.dirname(destination);
  await fs.mkdir(packRoot, { recursive: true });
  const staged = await fs.mkdtemp(path.join(packRoot, '.import-'));
  const backup = `${staged}-backup`;
  let oldMoved = false;
  try {
    for (const file of ['知识节点.xlsx', '知识点.md', '习题.md']) {
      await fs.copyFile(path.join(source, file), path.join(staged, file));
    }
    for (const image of images) {
      const target = path.join(staged, image);
      await fs.mkdir(path.dirname(target), { recursive: true });
      await fs.copyFile(path.join(source, image), target);
    }
    try { await fs.rename(destination, backup); oldMoved = true; }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    try { await fs.rename(staged, destination); }
    catch (error) {
      if (oldMoved) await fs.rename(backup, destination);
      throw error;
    }
    if (oldMoved) await fs.rm(backup, { recursive: true });
  } finally {
    await fs.rm(staged, { recursive: true, force: true });
  }
  course.files = ['知识节点.xlsx', '知识点.md', '习题.md'];
  course.attachments = images;
  if (course.title === '辛几何与数学物理') {
    course.description = '从经典力学走向辛几何，理解运动背后的几何结构。';
    course.subject = '专业课程';
  }
  await fs.mkdir(path.join(project, 'data'), { recursive: true });
  const output = path.join(project, 'data', `${folder}.json`);
  try { const previous = JSON.parse(await fs.readFile(output, 'utf8')); if (!course.textbook) course.textbook = previous.textbook || ''; }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  await fs.writeFile(output, JSON.stringify(course));
  const names = (await fs.readdir(path.join(project, 'data'))).filter(file => file.endsWith('.json') && file.startsWith('course-'));
  const all = await Promise.all(names.map(async name => JSON.parse(await fs.readFile(path.join(project, 'data', name), 'utf8'))));
  await fs.writeFile(path.join(project, 'data', 'courses.js'), `window.COURSE_DATA = ${JSON.stringify(all).replace(/</g, '\\u003c')};\n`);
  console.log(JSON.stringify({ course: course.title, id: course.id, ...course.stats, copiedImages: images.length, copiedPdfs: 0, warnings: course.warnings }, null, 2));
}
main().catch(error => { console.error(error.stack); process.exitCode = 1; });
