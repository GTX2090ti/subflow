// 用本机 HoKit 调试证书给 unsigned HAP 签名。
// 密钥口令不落仓库：由环境变量 HOKIT_KEY_PWD 提供。
// 用法：HOKIT_KEY_PWD=*** node tools/sign-hap.mjs [unsigned.hap]
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');
const local = process.env.LOCALAPPDATA || '';
const DEVECO = process.env.DEVECO_HOME || 'D:\\DevEco Studio';
const JAVA = path.join(DEVECO, 'jbr', 'bin', 'java.exe');
const JAR = path.join(local, 'Programs', 'HoKit', 'resources', 'assets', 'tools', 'sign', 'libs', 'hap-sign-tool.jar');
const KEYSTORE = path.join(local, 'Programs', 'HoKit', 'resources', 'assets', 'tools', 'sign', 'store', 'ho-kit.p12');
const SIGN_DIR = path.join(local, 'ho-kit', 'sign');

const appJson = fs.readFileSync(path.join(root, 'AppScope', 'app.json5'), 'utf8');
const bundleName = (appJson.match(/"bundleName"\s*:\s*"([^"]+)"/) || [])[1];
const versionName = (appJson.match(/"versionName"\s*:\s*"([^"]+)"/) || [])[1] || '0.0.0';
if (!bundleName) {
  console.error('无法从 AppScope/app.json5 取到 bundleName');
  process.exit(1);
}

const keyPwd = process.env.HOKIT_KEY_PWD;
if (!keyPwd) {
  console.error('缺少 HOKIT_KEY_PWD（HoKit 调试 keystore 口令），未做任何签名');
  process.exit(1);
}

function newest(dir, re) {
  if (!fs.existsSync(dir)) return '';
  return fs.readdirSync(dir)
    .filter((f) => re.test(f))
    .map((f) => ({ f, t: fs.statSync(path.join(dir, f)).mtimeMs }))
    .sort((a, b) => b.t - a.t)
    .map((x) => path.join(dir, x.f))[0] || '';
}

// profile 按 bundle-name 绑定，必须精确匹配当前包名
const profile = newest(path.join(SIGN_DIR, 'provisions'), new RegExp('^' + bundleName.replace(/\./g, '\\.') + '_.*\\.p7b$'));
const cert = newest(path.join(SIGN_DIR, 'certs'), /\.cer$/);
if (!profile) {
  console.error(`找不到 ${bundleName} 的 p7b（${path.join(SIGN_DIR, 'provisions')}）`);
  process.exit(1);
}

const unsigned = process.argv[2] || path.join(root, 'dist', `SubFlow-${versionName}-debug-unsigned.hap`);
if (!fs.existsSync(unsigned)) {
  console.error('unsigned HAP 不存在：' + unsigned);
  process.exit(1);
}

const signed = unsigned.replace(/-unsigned\.hap$/, '-signed.hap');
const args = [
  '-jar', JAR, 'sign-app',
  '-keyAlias', process.env.HOKIT_KEY_ALIAS || 'hokit',
  '-keyPwd', keyPwd,
  '-keystorePwd', keyPwd,
  '-signCode', '1',
  '-signAlg', 'SHA256withECDSA',
  '-mode', 'localSign',
  '-appCertFile', cert,
  '-profileFile', profile,
  '-keystoreFile', KEYSTORE,
  '-inFile', unsigned,
  '-outFile', signed,
];
const r = spawnSync(JAVA, args, { encoding: 'utf8' });
const out = (r.stdout || '') + (r.stderr || '');
if (r.status !== 0 || !/sign-app success/i.test(out) || !fs.existsSync(signed)) {
  console.error('签名失败 rc=' + r.status);
  console.error(out.slice(0, 2000));
  process.exit(1);
}

console.log('profile = ' + path.basename(profile));
console.log('signed  = ' + signed + ' (' + fs.statSync(signed).size + ' bytes)');
