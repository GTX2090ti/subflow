// 构建未签名 HAP：同步 Web 资源 -> 建 hvigor 依赖 junction -> 调 hvigor。
// 用法：node tools/build-hap.mjs [--release]
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');
const DEVECO = process.env.DEVECO_HOME || 'D:/DevEco Studio';
const NODE = path.join(DEVECO, 'tools', 'node', 'node.exe');

function fail(msg) {
  console.error('[build] ' + msg);
  process.exit(1);
}

for (const p of [DEVECO, NODE, path.join(DEVECO, 'tools', 'hvigor', 'hvigor', 'bin', 'hvigor.js')]) {
  if (!fs.existsSync(p)) fail('DevEco 路径不存在: ' + p);
}

const sync = spawnSync(NODE, [path.join(root, 'tools', 'sync-web.mjs')], { stdio: 'inherit' });
if (sync.status !== 0) fail('Web 资源同步失败');

// 命令行构建不会补工程 node_modules，插件与其依赖的 hvigor 本体都要指过去。
const modulesDir = path.join(root, 'node_modules', '@ohos');
fs.mkdirSync(modulesDir, { recursive: true });
const links = [
  [path.join(DEVECO, 'tools', 'hvigor', 'hvigor-ohos-plugin'), path.join(modulesDir, 'hvigor-ohos-plugin')],
  [path.join(DEVECO, 'tools', 'hvigor', 'hvigor'), path.join(modulesDir, 'hvigor')],
];
for (const [target, link] of links) {
  if (fs.existsSync(link)) continue;
  try {
    fs.symlinkSync(target, link, 'junction');
    console.log('[build] junction ' + link + ' -> ' + target);
  } catch (e) {
    fail('创建 junction 失败: ' + link + ' (' + e.message + ')');
  }
}

const release = process.argv.includes('--release');
const mode = release ? 'release' : 'debug';
const env = {
  ...process.env,
  DEVECO_SDK_HOME: path.join(DEVECO, 'sdk'),
  HOS_SDK_HOME: path.join(DEVECO, 'sdk'),
  JAVA_HOME: path.join(DEVECO, 'jbr'),
  NODE_HOME: path.join(DEVECO, 'tools', 'node'),
  NODE_PATH: path.join(root, 'node_modules'),
  PATH: path.join(DEVECO, 'jbr', 'bin') + path.delimiter + process.env.PATH,
};

const args = [
  path.join(DEVECO, 'tools', 'hvigor', 'hvigor', 'bin', 'hvigor.js'),
  'assembleHap', '--mode', 'module',
  '-p', 'product=default', '-p', 'buildMode=' + mode,
  '--no-daemon',
];
console.log('[build] hvigor assembleHap (' + mode + ')');
const build = spawnSync(NODE, args, { cwd: root, env, stdio: 'inherit' });
if (build.status !== 0) fail('hvigor 构建失败，退出码 ' + build.status);

const outHap = path.join(root, 'entry', 'build', 'default', 'outputs', 'default', 'entry-default-unsigned.hap');
if (!fs.existsSync(outHap)) fail('未找到构建产物: ' + outHap);

const appJson = fs.readFileSync(path.join(root, 'AppScope', 'app.json5'), 'utf8');
const version = (appJson.match(/"versionName"\s*:\s*"([^"]+)"/) || [])[1] || '0.0.0';
const dist = path.join(root, 'dist');
fs.mkdirSync(dist, { recursive: true });
const named = path.join(dist, 'SubFlow-' + version + '-' + mode + '-unsigned.hap');
fs.copyFileSync(outHap, named);
console.log('[build] 产物: ' + named);
