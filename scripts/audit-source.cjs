const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const sha256 = file => crypto.createHash('sha256').update(fs.readFileSync(path.join(root, file))).digest('hex');
function filesIn(folder) {
  return fs.readdirSync(path.join(root, folder), { recursive: true, withFileTypes: true })
    .filter(entry => entry.isFile() && entry.name !== '.DS_Store' && !entry.parentPath.includes('__pycache__'))
    .map(entry => path.relative(root, path.join(entry.parentPath, entry.name)).split(path.sep).join('/'));
}
const runtimeFiles = ['index.html', ...filesIn('js'), ...filesIn('styles'), ...filesIn('assets').filter(file => file.endsWith('.svg'))];
const marker = /yuketang|xuetang|雨课堂|荷塘雨课堂|rain\s*classroom/i;
// 唯一允许发起网络请求的前端文件：模型接口配置页。
// 它配置的是服务器上的模型端点，不发请求就没有意义。
// 放行条件是"必须仍在清单里"，而不是"关闭检查"——新增第二个联网文件仍然会失败，
// 促使我们回来想清楚：它是否真的需要联网。
const NETWORK_ALLOWED = new Set(['js/api-config.js']);
const markerHits = runtimeFiles.filter(file => marker.test(fs.readFileSync(path.join(root, file), 'utf8')));
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const entryAssets = [...html.matchAll(/<(?:script|link)\b[^>]*(?:src|href)="([^"#]+)"/g)].map(match => match[1]);
const remoteEntryAssets = entryAssets.filter(value => /^(?:https?:)?\/\//.test(value));
const allNetworkCodeHits = runtimeFiles.filter(file => /\bfetch\s*\(|XMLHttpRequest|new\s+WebSocket|sendBeacon\s*\(/.test(fs.readFileSync(path.join(root, file), 'utf8')));
const networkCodeHits = allNetworkCodeHits.filter(file => !NETWORK_ALLOWED.has(file));
const networkCodeAllowed = allNetworkCodeHits.filter(file => NETWORK_ALLOWED.has(file));
const stylesheetRemoteLoads = filesIn('styles').filter(file => /(?:@import|url\()\s*["']?(?:https?:)?\/\//.test(fs.readFileSync(path.join(root, file), 'utf8')));
const dataScript = fs.readFileSync(path.join(root, 'data/courses.js'), 'utf8');
const wrapper = /^window\.COURSE_DATA = ([\s\S]*);\s*$/.exec(dataScript);
assert(wrapper, '课程数据脚本必须是单一字面量赋值。');
const embeddedCourses = JSON.parse(wrapper[1]);
const canonicalCourses = filesIn('data').filter(file => /\/course-.*\.json$/.test(file)).map(file => JSON.parse(fs.readFileSync(path.join(root, file), 'utf8')));
const ordered = courses => [...courses].sort((a, b) => a.id.localeCompare(b.id));
// 先做廉价计数比对：直接深比较 8MB 结构在不一致时会生成巨型 diff 并耗尽内存。
assert.equal(embeddedCourses.length, canonicalCourses.length, `课程脚本含 ${embeddedCourses.length} 门，标准化 JSON 含 ${canonicalCourses.length} 门。`);
assert.deepEqual(ordered(embeddedCourses), ordered(canonicalCourses), '课程脚本与标准化 JSON 数据不一致。');
const origin = require('../reports/dependency-origin.json');
const pinned = require('../package-lock.json').packages;
const expectedVendor = new Set();
origin.components.forEach(component => {
  const pin = pinned[`node_modules/${component.package}`];
  assert.equal(component.version, pin.version);
  assert.equal(component.integrity, pin.integrity);
  component.files.forEach(file => { assert.equal(sha256(file.path), file.sha256, `开源文件已改变：${file.path}`); expectedVendor.add(file.path); });
});
const extraVendorFiles = filesIn('vendor').filter(file => !expectedVendor.has(file));
const codeFiles = [...runtimeFiles, ...filesIn('scripts'), 'eslint.config.mjs'].filter(file => !file.includes('__pycache__'));
const report = {
  checkedAt: new Date().toISOString(),
  scope: 'Project source/provenance audit; no private competitor source or internet-wide similarity database comparison',
  checks: { runtimeFiles: runtimeFiles.length, generatedCourseScriptIsDataOnly: true, competitorMarkerHits: markerHits, remoteEntryAssets, networkCodeHits, networkCodeAllowed, allowedReason: 'js/api-config.js 是服务器模型配置页，必须调用后端 API；其余前端页面保持离线可用。', stylesheetRemoteLoads, verifiedVendorFiles: expectedVendor.size, unregisteredVendorFiles: extraVendorFiles },
  projectSourceInventory: codeFiles.map(file => ({ path: file, sha256: sha256(file) })),
  openSource: origin.components.map(({ package: name, version, license, source, files }) => ({ package: name, version, license, source, verifiedFiles: files.length })),
  campusPhoto: { path: 'assets/whu-library.jpg', author: 'Howchou', license: 'CC-BY-SA-3.0', source: 'https://commons.wikimedia.org/wiki/File:Wuhan_University_Old_Library.jpg', sha256: sha256('assets/whu-library.jpg') },
  suppliedCourseFiles: filesIn('course-packs').map(file => ({ path: file, sha256: sha256(file) })),
  generatedData: filesIn('data').map(file => ({ path: file, sha256: sha256(file) })),
  conclusion: 'Source origins are separated and inventoried. These checks are not a claim that all dependencies, textbooks, photographs or generic interface patterns are original.'
};
fs.writeFileSync(path.join(root, 'reports/source-provenance.json'), JSON.stringify(report, null, 2) + '\n');
assert.equal(markerHits.length + remoteEntryAssets.length + networkCodeHits.length + stylesheetRemoteLoads.length + extraVendorFiles.length, 0, '来源检查出现待复核项目，见 reports/source-provenance.json');
console.log(JSON.stringify(report.checks, null, 2));
