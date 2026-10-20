import fs from 'node:fs/promises';
import path from 'node:path';
const root = path.resolve(import.meta.dirname, '..');
const components = JSON.parse(await fs.readFile(path.join(root, 'config/vendor-assets.json'), 'utf8'));
const files = components.flatMap(item => item.files.map(([source, target]) => [`${item.package}/${source}`, target]));
for (const [source, target] of files) {
  const dest = path.join(root, 'vendor', target);
  await fs.mkdir(path.dirname(dest), { recursive: true });
  await fs.cp(path.join(root, 'node_modules', source), dest, { recursive: true });
}
console.log('已准备离线 Markdown、数学公式和 Excel 读取组件。');
