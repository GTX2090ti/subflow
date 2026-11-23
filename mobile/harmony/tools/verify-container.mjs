// 容器工程静态校验：断言「必须存在的正确写法」，而不是钉死某个具体值。
// 覆盖：虚拟域回填、错误处理语义、外部导航、原生桥注册、页面注入、资源打包一致性。
// 用法：node tools/verify-container.mjs [--hap dist/xxx.hap]
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');
const repoRoot = path.resolve(root, '..', '..');

let pass = 0, fail = 0;
const failures = [];
function check(name, cond, extra) {
  if (cond) { pass++; console.log('PASS ' + name); }
  else {
    fail++; failures.push(name + (extra === undefined ? '' : ' :: ' + JSON.stringify(extra)));
    console.log('FAIL ' + name + (extra === undefined ? '' : ' :: ' + JSON.stringify(extra)));
  }
}

function read(rel) {
  return fs.readFileSync(path.join(root, rel), 'utf8');
}
function sha(buf) {
  return crypto.createHash('sha256').update(buf).digest('hex');
}
function readZip(file) {
  const buf = fs.readFileSync(file);
  let eocd = -1;
  for (let i = buf.length - 22; i >= Math.max(0, buf.length - 65557); i--) {
    if (buf.readUInt32LE(i) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) throw new Error('不是有效的 zip（未找到 EOCD）');
  const count = buf.readUInt16LE(eocd + 10);
  let off = buf.readUInt32LE(eocd + 16);
  const entries = new Map();
  for (let i = 0; i < count; i++) {
    if (buf.readUInt32LE(off) !== 0x02014b50) throw new Error('中央目录损坏');
    const method = buf.readUInt16LE(off + 10);
    const compSize = buf.readUInt32LE(off + 20);
    const nameLen = buf.readUInt16LE(off + 28);
    const extraLen = buf.readUInt16LE(off + 30);
    const commentLen = buf.readUInt16LE(off + 32);
    const localOff = buf.readUInt32LE(off + 42);
    const name = buf.toString('utf8', off + 46, off + 46 + nameLen);
    const lnameLen = buf.readUInt16LE(localOff + 26);
    const lextraLen = buf.readUInt16LE(localOff + 28);
    const start = localOff + 30 + lnameLen + lextraLen;
    const raw = buf.subarray(start, start + compSize);
    entries.set(name, method === 0 ? raw : zlib.inflateRawSync(raw));
    off += 46 + nameLen + extraLen + commentLen;
  }
  return entries;
}

const indexEts = read('entry/src/main/ets/pages/Index.ets');
const bridgeEts = read('entry/src/main/ets/model/Bridge.ets');
const moduleJson = read('entry/src/main/module.json5');
const appJson = read('AppScope/app.json5');
const mobileJs = read('../../app/static/m/mobile.js');
const indexHtml = read('../../app/static/m/index.html');
const mobileCss = read('../../app/static/m/mobile.css');

/* ---------------- 虚拟域与资源映射 ---------------- */
check('虚拟域常量', /const VHOST = 'subflow\.local'/.test(indexEts));
check('入口指向 index.html', /const ENTRY = ORIGIN \+ '\/index\.html'/.test(indexEts));
check('rawfile 前缀为空串（与平铺布局一致）', /const RAWFILE_ROOT = '';/.test(indexEts));

const rawDir = path.join(root, 'entry', 'src', 'main', 'resources', 'rawfile');
const srcDir = path.join(repoRoot, 'app', 'static', 'm');
const rawFiles = fs.readdirSync(rawDir, { withFileTypes: true });
check('rawfile 无子目录', rawFiles.every((e) => e.isFile()));
const rawNames = new Set(rawFiles.filter((e) => e.isFile()).map((e) => e.name));
for (const name of ['index.html', 'mobile.css', 'mobile.js']) {
  check('rawfile 含 ' + name, rawNames.has(name));
}
let syncOk = true;
for (const name of ['index.html', 'mobile.css', 'mobile.js']) {
  if (!rawNames.has(name)) { syncOk = false; continue; }
  if (sha(fs.readFileSync(path.join(srcDir, name))) !== sha(fs.readFileSync(path.join(rawDir, name)))) syncOk = false;
}
check('rawfile 与 app/static/m 内容一致', syncOk);

/* ---------------- 主文档响应：少一项真机就把源码当纯文本显示 ---------------- */
check('响应带 Content-Type 头', /setResponseHeader\(\[\{ headerKey: 'Content-Type'/.test(indexEts));
check('文本内容以字符串回填', /resp\.setResponseData\(text\)/.test(indexEts));
check('显式声明响应编码', indexEts.includes("setResponseEncoding('utf-8')"));
check('MIME 表不带 charset 参数', /case 'html': return 'text\/html';/.test(indexEts)
  && !/text\/html; charset/.test(indexEts));
check('CSS/JS/JSON 也走文本通道', /isTextExt\(ext\)[\s\S]{0,220}?this\.textResponse\(200/.test(indexEts));
check('二进制资源仍回 ArrayBuffer', /setResponseData\(bytes\.buffer as ArrayBuffer\)/.test(indexEts));

/* ---------------- 错误处理语义 ---------------- */
check('缺失资源返回本地 404（不 return null）',
  /catch \(e\)[\s\S]{0,600}?return this\.textResponse\(404/.test(indexEts));
check('onErrorReceive 判定主框架', indexEts.includes('isMainFrame()'));
check('主框架兜底 url !== ENTRY', indexEts.includes('url !== ENTRY'));
check('onPageEnd 清除错误态', /onPageEnd\(\(\) => \{[\s\S]{0,120}?this\.loadError = ''/.test(indexEts));
check('路径穿越被拒', /rel\.indexOf\('\.\.'\) >= 0/.test(indexEts));

/* ---------------- 外部导航 ---------------- */
check('注册 onLoadIntercept', indexEts.includes('.onLoadIntercept('));
check('外部导航交给系统', indexEts.includes('ohos.want.action.viewData') && indexEts.includes('startAbility'));
check('内部 URL 白名单含 about/data/blob',
  indexEts.includes("url.startsWith('about:')") && indexEts.includes("url.startsWith('data:')")
  && indexEts.includes("url.startsWith('blob:')"));

/* ---------------- 原生桥 ---------------- */
check('桥对象持有强引用（防 GC 后 native proxy not found）',
  /private bridge: Bridge = new Bridge\(\)/.test(indexEts));
check('用 javaScriptProxy 声明式注册 SFB',
  /\.javaScriptProxy\(\{[\s\S]{0,240}?name: 'SFB'/.test(indexEts));
check('桥用同步入队 + runJavaScript 回填',
  bridgeEts.includes('runJavaScript(') && bridgeEts.includes('__sfbResolve')
  && /request\(reqJson: string, reqId: string\)/.test(bridgeEts));
check('不用 asyncMethodList（真机异步代理拿不到返回值）',
  !/asyncMethodList\s*:/.test(indexEts) && !/asyncMethodList\s*:/.test(bridgeEts));
check('页面实现 __sfbResolve 回调', mobileJs.includes('window.__sfbResolve'));
check('不再用 registerJavaScriptProxy + refresh（真机丢对象）',
  !indexEts.includes('registerJavaScriptProxy(') && !indexEts.includes('.refresh()'));
check('桥走 @kit.NetworkKit', bridgeEts.includes("from '@kit.NetworkKit'"));
check('桥在 finally 中 destroy', /finally \{[\s\S]{0,80}?client\.destroy\(\)/.test(bridgeEts));
check('桥错误通过返回值传递',
  /catch \(e\)[\s\S]{0,500}?this\.reply\(reqId, \{ status: 0/.test(bridgeEts));

/* ---------------- 局域网自动发现 ---------------- */
check('SFB 代理暴露 discover', /methodList: \[[^\]]*'discover'/.test(indexEts));
check('申请 GET_NETWORK_INFO 权限', moduleJson.includes('ohos.permission.GET_NETWORK_INFO'));
check('桥实现 discover 同步入队 + 回调回填',
  /discover\(reqId: string\)/.test(bridgeEts) && bridgeEts.includes('__sfbDiscoverResolve'));
check('发现读取系统连接属性拿本机 IP',
  bridgeEts.includes('getDefaultNetSync') && bridgeEts.includes('getConnectionPropertiesSync'));
check('发现优先轻量探针 /api/ping，兼容 /api/health',
  bridgeEts.includes("'/api/ping'") && bridgeEts.includes("'/api/health'"));
check('发现按 /24 网段并发扫描 8099',
  /DISCOVER_PORT = 8099/.test(bridgeEts) && /PROBE_CONCURRENCY/.test(bridgeEts)
  && /i <= 254/.test(bridgeEts));
check('页面实现 __sfbDiscoverResolve 回调', mobileJs.includes('window.__sfbDiscoverResolve'));
check('发现入口仅在原生桥存在时可用',
  /function canDiscover\(\)[\s\S]{0,180}?window\.SFB\.discover/.test(mobileJs)
  && mobileJs.includes('id="disc-btn"') && mobileJs.includes('function runDiscovery'));

/* ---------------- 移动端日常功能锚点 ---------------- */
check('底部 5 个标签', mobileCss.includes('grid-template-columns: repeat(5, 1fr)'));
check('失败任务角标', indexHtml.includes('id="tab-badge-jobs"') && mobileJs.includes('function renderTabBadge'));
check('骨架屏（任务/磁贴）', mobileJs.includes('function skeletonJobs') && mobileJs.includes('function skeletonTiles')
  && mobileCss.includes('.sk-tile'));
check('下拉刷新指标与处理', indexHtml.includes('id="ptr"') && indexHtml.includes('id="ptr-text"')
  && mobileJs.includes('function bindPullToRefresh') && mobileJs.includes('function refreshCurrentView'));
check('长按多选 + 批量重试',
  mobileJs.includes('function bindJobLongPress') && mobileJs.includes('function retrySelectedJobs')
  && indexHtml.includes('id="job-bar"') && indexHtml.includes('id="job-retry"'));
check('显式勾选控件（不是仅靠手势）', mobileJs.includes('data-pickjob=') && mobileCss.includes('.job-pick'));
check('统计视图', indexHtml.includes('data-view="stats"') && mobileJs.includes('function renderStats')
  && mobileJs.includes("'/api/stats?days='"));
check('字幕区视图', indexHtml.includes('data-view="zone"') && mobileJs.includes('function submitZone')
  && mobileJs.includes("'/api/zone/submit'"));
check('站点与 Cookie 视图', indexHtml.includes('data-view="sites"') && mobileJs.includes('function renderSites')
  && mobileJs.includes("'/api/cookiecloud/refresh'"));
check('定时扫描视图', indexHtml.includes('data-view="schedule"') && mobileJs.includes('function saveSchedule')
  && mobileJs.includes("patch: { schedule: cfg }"));
check('更多页承载子视图导航', indexHtml.includes('data-view="more"') && mobileJs.includes('const TAB_OF'));
check('下拉刷新不劫持输入框与日志滚动',
  /closest\('input, textarea, select, \.logbox'\)/.test(mobileJs));

/* ---------------- 页面注入与 Web 能力 ---------------- */
check('注入容器标记', indexEts.includes('window.__SUBFLOW_APP__=1'));
check('注入默认服务器', /window\.__SUBFLOW_DEFAULT_SERVER__=.*DEFAULT_SERVER/.test(indexEts));
const defaultServer = (indexEts.match(/const DEFAULT_SERVER = '([^']+)'/) || [])[1] || '';
check('默认服务器是 http(s) 地址', /^https?:\/\/[^\s']+$/.test(defaultServer), defaultServer);
check('开启 domStorageAccess', indexEts.includes('.domStorageAccess(true)'));
check('开启 onlineImageAccess', indexEts.includes('.onlineImageAccess(true)'));
check('mixedMode 允许 http', indexEts.includes('.mixedMode(MixedMode.All)'));
check('不做系统强制反色', indexEts.includes('.forceDarkAccess(false)'));

/* ---------------- 模块配置 ---------------- */
check('申请 INTERNET 权限', moduleJson.includes('ohos.permission.INTERNET'));
check('支持 phone', /"deviceTypes"[\s\S]{0,120}"phone"/.test(moduleJson));
check('入口页面在 main_pages', fs.readFileSync(path.join(root, 'entry/src/main/resources/base/profile/main_pages.json'), 'utf8').includes('pages/Index'));

/* ---------------- HAP 产物 ---------------- */
const hapArg = process.argv.indexOf('--hap');
const hapPath = hapArg >= 0 ? process.argv[hapArg + 1] : (() => {
  const dist = path.join(root, 'dist');
  if (!fs.existsSync(dist)) return '';
  // 同一目录里可能同时有 unsigned / signed，取最新产的那个
  const files = fs.readdirSync(dist)
    .filter((f) => f.endsWith('.hap'))
    .map((f) => ({ f, t: fs.statSync(path.join(dist, f)).mtimeMs }))
    .sort((a, b) => b.t - a.t);
  return files.length ? path.join(dist, files[0].f) : '';
})();

if (!hapPath || !fs.existsSync(hapPath)) {
  check('HAP 产物存在（先运行 tools/build-hap.mjs）', false, hapPath || 'dist/*.hap 不存在');
} else {
  check('HAP 产物存在', true, path.basename(hapPath));
  const entries = readZip(hapPath);
  const moduleEntry = [...entries.keys()].find((n) => n.endsWith('module.json'));
  let app = null, mod = null;
  if (moduleEntry) {
    const parsed = JSON.parse(entries.get(moduleEntry).toString('utf8'));
    app = parsed.app || {};
    mod = parsed.module || {};
  }
  const bundle = (appJson.match(/"bundleName"\s*:\s*"([^"]+)"/) || [])[1];
  check('HAP bundleName 与 AppScope 一致', app && app.bundleName === bundle, app && app.bundleName);
  check('minAPIVersion = 50100018（对齐可安装的 5.1.0(18)）', app && app.minAPIVersion === 50100018,
    app && app.minAPIVersion);
  check('targetAPIVersion 不低于 min', app && app.targetAPIVersion >= app.minAPIVersion);
  check('HAP 内申请 INTERNET 权限',
    mod && Array.isArray(mod.requestPermissions) && mod.requestPermissions.some((p) => p.name === 'ohos.permission.INTERNET'));
  check('HAP 内申请 GET_NETWORK_INFO 权限',
    mod && Array.isArray(mod.requestPermissions)
    && mod.requestPermissions.some((p) => p.name === 'ohos.permission.GET_NETWORK_INFO'));
  let hapWebOk = true;
  for (const name of ['index.html', 'mobile.css', 'mobile.js']) {
    const data = entries.get('resources/rawfile/' + name);
    if (!data || sha(data) !== sha(fs.readFileSync(path.join(srcDir, name)))) hapWebOk = false;
  }
  check('HAP 内 Web 资源与源文件一致', hapWebOk);
  check('HAP 内无多余 rawfile 条目',
    [...entries.keys()].filter((n) => n.startsWith('resources/rawfile/')).length === 3);
}

console.log(`\nRESULT PASS ${pass} / FAIL ${fail}`);
if (failures.length) console.log('FAILURES:\n' + failures.join('\n'));
process.exit(fail ? 1 : 0);
