// 把移动端 Web 资源（app/static/m）单向同步到 HAP 的 rawfile 根目录。
// 必须平铺到 rawfile 根：Index.ets 里 RAWFILE_ROOT 为空串，两边不一致会白屏。
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(here, '..');
const repoRoot = path.resolve(projectRoot, '..', '..');
const src = path.join(repoRoot, 'app', 'static', 'm');
const dest = path.join(projectRoot, 'entry', 'src', 'main', 'resources', 'rawfile');

const REQUIRED = ['index.html', 'mobile.css', 'mobile.js'];

function fail(msg) {
  console.error('[sync-web] ' + msg);
  process.exit(1);
}

if (!fs.existsSync(src)) fail('源目录不存在: ' + src);

const wanted = new Set();
for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
  if (entry.isFile()) wanted.add(entry.name);
}
for (const name of REQUIRED) {
  if (!wanted.has(name)) fail('缺少必需文件: ' + name);
}
for (const name of wanted) {
  if (name.includes('/') || name.includes('\\') || name === '..') fail('非法文件名: ' + name);
}

fs.mkdirSync(dest, { recursive: true });

const copied = [];
for (const name of wanted) {
  fs.copyFileSync(path.join(src, name), path.join(dest, name));
  copied.push(name);
}

// 增量清理：源侧已删除的文件才删，逐个 unlink，避免一次删太多触发安全护栏。
const removed = [];
for (const entry of fs.readdirSync(dest, { withFileTypes: true })) {
  if (!entry.isFile()) continue;
  if (entry.name === '.gitkeep') continue;
  if (wanted.has(entry.name)) continue;
  fs.unlinkSync(path.join(dest, entry.name));
  removed.push(entry.name);
}

console.log('[sync-web] ' + copied.length + ' 个文件 -> ' + path.relative(repoRoot, dest));
for (const name of copied.sort()) console.log('  + ' + name);
for (const name of removed.sort()) console.log('  - ' + name);
