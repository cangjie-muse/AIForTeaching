const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const zlib = require('node:zlib');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const manifest = require('../config/vendor-assets.json');
const lock = require('../package-lock.json');
const directory = process.argv[2];
assert(directory, '请提供从 npm pack 获取的 .tgz 文件所在目录。');
const digest = data => crypto.createHash('sha256').update(data).digest('hex');
function unpack(buffer) {
  const tar = zlib.gunzipSync(buffer, { maxOutputLength: 100 * 1024 * 1024 });
  const files = new Map();
  let offset = 0;
  while (offset + 512 <= tar.length) {
    const header = tar.subarray(offset, offset + 512);
    if (header.every(byte => byte === 0)) break;
    const name = header.subarray(0, 100).toString().split('\0')[0];
    const prefix = header.subarray(345, 500).toString().split('\0')[0];
    const size = parseInt(header.subarray(124, 136).toString().replace(/\0/g, '').trim(), 8);
    assert(Number.isFinite(size) && size >= 0 && offset + 512 + size <= tar.length, '无效 tar 长度');
    const type = header[156];
    if (type === 0 || type === 48) files.set(prefix ? `${prefix}/${name}` : name, tar.subarray(offset + 512, offset + 512 + size));
    offset += 512 + Math.ceil(size / 512) * 512;
  }
  return files;
}
function verifyMappings(component, archive) {
  return component.files.flatMap(([source, target]) => {
    const archivePath = `package/${source}`;
    const matches = [...archive.keys()].filter(name => name === archivePath || name.startsWith(`${archivePath}/`));
    assert(matches.length, `${component.package} 发布包缺少 ${source}`);
    return matches.map(name => {
      const suffix = name.slice(archivePath.length);
      const localPath = `vendor/${target}${suffix}`;
      const expected = digest(archive.get(name));
      assert.equal(digest(fs.readFileSync(path.join(root, localPath))), expected, `发布包与本地文件不一致：${localPath}`);
      return { path: localPath, upstreamPath: name, sha256: expected };
    });
  });
}
const components = manifest.map(component => {
  const pin = lock.packages[`node_modules/${component.package}`];
  assert.equal(new URL(pin.resolved).hostname, 'registry.npmjs.org');
  const bytes = fs.readFileSync(path.join(directory, `${component.package}-${pin.version}.tgz`));
  const integrity = `sha512-${crypto.createHash('sha512').update(bytes).digest('base64')}`;
  assert.equal(integrity, pin.integrity, `${component.package} 发布包完整性校验失败`);
  const archive = unpack(bytes);
  const metadata = JSON.parse(archive.get('package/package.json').toString());
  assert.equal(metadata.name, component.package);
  assert.equal(metadata.version, pin.version);
  return { package: component.package, version: pin.version, license: metadata.license, source: pin.resolved, integrity, files: verifyMappings(component, archive) };
});
const output = { verifiedAt: new Date().toISOString(), method: 'npm published tarball SHA-512 against lockfile, then distributed-file SHA-256 equality', components };
fs.writeFileSync(path.join(root, 'reports/dependency-origin.json'), JSON.stringify(output, null, 2) + '\n');
console.log(JSON.stringify({ packages: components.length, matchedFiles: components.reduce((sum, item) => sum + item.files.length, 0), status: '发布包与本地组件逐文件一致' }, null, 2));
