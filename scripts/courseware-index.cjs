const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const JSZip = require('jszip');
const root = path.resolve(__dirname, '..');
const coursewareRoot = path.join(root, 'courseware');
const output = path.join(root, 'data/courseware.js');
const core = ['知识节点.xlsx', '知识点.md', '习题.md'];
function regularFile(base, relative) {
  assert(relative && !path.isAbsolute(relative) && !relative.split('/').includes('..'), `无效的课件路径：${relative}`);
  assert(fs.statSync(path.join(base, relative)).isFile(), `缺少课件文件：${relative}`);
}
async function inspectManifest(file) {
  const base = path.dirname(file), manifest = JSON.parse(fs.readFileSync(file, 'utf8'));
  assert.equal(manifest.schemaVersion, '1.0');
  assert.equal(path.basename(base), manifest.chapterId);
  assert.equal(path.basename(path.dirname(base)), manifest.courseId);
  const dataName = `course-${manifest.courseId.replace(/^course-/, '')}.json`;
  const course = JSON.parse(fs.readFileSync(path.join(root, 'data', dataName), 'utf8'));
  assert(course.nodes.some(node => node.id === manifest.chapterId && node.level === 2), `章节不存在：${manifest.chapterId}`);
  const hash = createHash('sha256');
  for (const name of core) hash.update(fs.readFileSync(path.join(root, course.resourceBase, name)));
  assert.equal(manifest.sourceSha256, hash.digest('hex'), `资源包已变化，请复核课件：${manifest.courseId}/${manifest.chapterId}`);
  assert(manifest.versions.length > 0);
  manifest.versions.forEach((version, index) => {
    assert.equal(version.id, `v${index + 1}`);
    assert.equal(version.number, index + 1);
    assert.equal(version.slideCount, version.files.previews.length);
    [version.files.pptx, version.files.cover, ...version.files.previews, version.files.plan, version.files.feedback, version.files.equations].filter(Boolean).forEach(name => regularFile(base, name));
  });
  for (const version of manifest.versions) {
    const zip = await JSZip.loadAsync(fs.readFileSync(path.join(base, version.files.pptx)));
    const slides = Object.keys(zip.files).filter(name => /^ppt\/slides\/slide\d+\.xml$/.test(name));
    assert.equal(slides.length, version.slideCount, `PPT 页数不一致：${version.id}`);
    const images = Object.keys(zip.files).filter(name => name.startsWith('ppt/media/') && !zip.files[name].dir);
    assert(images.length, `PPT 缺少封面摄影：${version.id}`);
    assert((await zip.files[images[0]].async('nodebuffer')).length > 100000, `PPT 封面摄影损坏：${version.id}`);
    if (version.formulaMode === 'office-math-omml') {
      const xml = await Promise.all(slides.map(name => zip.files[name].async('string')));
      const count = xml.reduce((sum, page) => sum + (page.match(/<a14:m>/g) || []).length, 0);
      assert.equal(count, version.nativeEquationCount, `原生公式数不一致：${version.id}`);
      assert(count > 0 && xml.some(page => page.includes('<mc:Fallback>')), '原生公式缺少兼容预览');
      const plan = JSON.parse(fs.readFileSync(path.join(base, version.files.plan), 'utf8'));
      const formulas = JSON.parse(fs.readFileSync(path.join(base, version.files.equations), 'utf8')).formulas;
      assert.equal(plan.slides.length, version.slideCount);
      assert.equal(plan.slides.reduce((sum, slide) => sum + slide.equations.length, 0), count);
      assert(plan.slides.every(slide => slide.equations.every(id => formulas[id]?.latex)), '公式源式与逐页计划不一致');
    }
  }
  return manifest;
}
async function collect() {
  const manifests = [];
  for (const courseId of fs.readdirSync(coursewareRoot)) {
    const courseDir = path.join(coursewareRoot, courseId);
    if (!fs.statSync(courseDir).isDirectory()) continue;
    for (const chapterId of fs.readdirSync(courseDir)) {
      const file = path.join(courseDir, chapterId, 'manifest.json');
      if (fs.existsSync(file)) manifests.push(await inspectManifest(file));
    }
  }
  return manifests;
}
async function main() {
  const manifests = await collect();
  const content = `window.COURSEWARE_DATA = ${JSON.stringify(manifests).replace(/</g, '\\u003c')};\n`;
  if (process.argv.includes('--check')) assert.equal(fs.readFileSync(output, 'utf8'), content, '课件目录索引未更新');
  else fs.writeFileSync(output, content);
  console.log(JSON.stringify({ chapters: manifests.length, versions: manifests.reduce((sum, item) => sum + item.versions.length, 0), slides: manifests.reduce((sum, item) => sum + item.versions.reduce((n, version) => n + version.slideCount, 0), 0), mode: process.argv.includes('--check') ? 'checked' : 'built' }));
}
if (require.main === module) main().catch(error => { console.error(error.stack); process.exitCode = 1; });
module.exports = { collect };
