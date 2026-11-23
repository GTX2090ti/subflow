// 容器等价验证：用本地 HTTP 服务复刻 Index.ets 的 onInterceptRequest 规则
// （rawfile 回填 + 注入标记 + MIME + 本地 404），再用 Edge headless 打开，
// 并注入一个「走 Node 而不是页面 fetch」的 SFB 桥，模拟原生 @ohos.net.http 不受 CORS 约束。
// 这样在没有真机的情况下也能验证：页面在容器形态下能连上后端、走桥、正常渲染。
//
// 前提：本地后端已在 127.0.0.1:8099 运行（SUBFLOW_DB=.local-test/data/subflow.db）。
// 用法：node tools/verify-web.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');
const repoRoot = path.resolve(root, '..', '..');
const RAW = path.join(root, 'entry', 'src', 'main', 'resources', 'rawfile');
const SHOTS = path.join(repoRoot, '.local-test', 'shots');
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const BACKEND = 'http://127.0.0.1:8099';
const PORT = 8788;
const CDP_PORT = 9334;
const ORIGIN = `http://127.0.0.1:${PORT}`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
fs.mkdirSync(SHOTS, { recursive: true });

let pass = 0, fail = 0;
const failures = [];
function check(name, cond, extra) {
  if (cond) { pass++; console.log('PASS ' + name); }
  else {
    fail++; failures.push(name + (extra === undefined ? '' : ' :: ' + JSON.stringify(extra)));
    console.log('FAIL ' + name + (extra === undefined ? '' : ' :: ' + JSON.stringify(extra)));
  }
}
function failFast(msg) {
  console.error('[verify-web] ' + msg);
  process.exit(1);
}

/* ---------- 1. MIME 表直接从 Index.ets 读取，避免复刻实现悄悄漂移 ---------- */
const indexEts = fs.readFileSync(path.join(root, 'entry/src/main/ets/pages/Index.ets'), 'utf8');
const MIME = {};
for (const m of indexEts.matchAll(/case '([^']+)': return '([^']+)';/g)) MIME[m[1]] = m[2];
if (!MIME.html || !MIME.js || !MIME.css) failFast('未能从 Index.ets 解析 MIME 表');

const INJECT = '<script>window.__SUBFLOW_APP__=1;window.__SUBFLOW_DEFAULT_SERVER__='
  + JSON.stringify(BACKEND) + ';</script>';

const requests = [];
const server = http.createServer((req, res) => {
  let rel = decodeURIComponent(new URL(req.url, ORIGIN).pathname);
  while (rel.startsWith('/')) rel = rel.substring(1);
  if (!rel) rel = 'index.html';
  const log = { path: rel, status: 200, mime: '' };
  requests.push(log);

  if (rel.includes('..')) {
    log.status = 403;
    res.writeHead(403, { 'Content-Type': 'text/plain; charset=utf-8' });
    res.end('Forbidden');
    return;
  }
  const file = path.join(RAW, rel);
  if (!file.startsWith(RAW) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
    log.status = 404;
    res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
    res.end('Not Found: ' + rel);
    return;
  }
  const ext = rel.includes('.') ? rel.substring(rel.lastIndexOf('.') + 1).toLowerCase() : '';
  let data = fs.readFileSync(file);
  if (ext === 'html') {
    data = Buffer.from(data.toString('utf8').replace('</head>', INJECT + '</head>'), 'utf8');
  }
  const mime = MIME[ext] || 'application/octet-stream';
  log.mime = mime;
  res.writeHead(200, { 'Content-Type': mime, 'Content-Length': data.length, 'Cache-Control': 'no-store' });
  res.end(data);
});

async function rawStatus(rawPath) {
  return new Promise((resolve) => {
    const req = http.get({ host: '127.0.0.1', port: PORT, path: rawPath }, (res) => {
      res.resume();
      res.on('end', () => resolve(res.statusCode));
    });
    req.on('error', () => resolve(0));
  });
}

/* ---------- 2. CDP 客户端 ---------- */
class CDP {
  constructor(ws) {
    this.ws = ws; this.seq = 0; this.pending = new Map(); this.handlers = new Map();
    this.exceptions = []; this.consoleErrors = [];
    ws.addEventListener('message', (ev) => {
      let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m.id && this.pending.has(m.id)) {
        const { res, rej } = this.pending.get(m.id); this.pending.delete(m.id);
        if (m.error) rej(new Error(m.error.message)); else res(m.result);
        return;
      }
      if (m.method === 'Runtime.exceptionThrown') {
        this.exceptions.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
      }
      if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
        this.consoleErrors.push(m.params.args.map((a) => a.value || a.description || '').join(' '));
      }
      const list = this.handlers.get(m.method);
      if (list) for (const fn of list) fn(m.params);
    });
  }
  on(method, fn) {
    if (!this.handlers.has(method)) this.handlers.set(method, []);
    this.handlers.get(method).push(fn);
  }
  send(method, params) {
    const id = ++this.seq;
    return new Promise((res, rej) => {
      this.pending.set(id, { res, rej });
      this.ws.send(JSON.stringify({ id, method, params: params || {} }));
      setTimeout(() => { if (this.pending.has(id)) { this.pending.delete(id); rej(new Error('CDP 超时: ' + method)); } }, 15000);
    });
  }
  async eval(expression) {
    const r = await this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) throw new Error('页面内异常: ' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
    return r.result.value;
  }
}

async function connectEdge() {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'subflow-container-'));
  const edge = spawn(EDGE, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--hide-scrollbars', '--force-device-scale-factor=1',
    '--remote-debugging-port=' + CDP_PORT, '--user-data-dir=' + profile,
    '--window-size=414,896', 'about:blank',
  ], { stdio: 'ignore' });
  let target = null;
  for (let i = 0; i < 80 && !target; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`)).json();
      target = list.find((t) => t.type === 'page' && t.webSocketDebuggerUrl);
    } catch (e) { /* 未就绪 */ }
    if (!target) await sleep(400);
  }
  if (!target) throw new Error('无法连接调试端口 ' + CDP_PORT);
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    ws.addEventListener('open', res);
    ws.addEventListener('error', () => rej(new Error('WS 失败')));
  });
  const cdp = new CDP(ws);
  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');
  return { cdp, edge, profile };
}

async function waitFor(cdp, expr, label, timeoutMs) {
  const limit = Date.now() + (timeoutMs || 20000);
  let last = null;
  while (Date.now() < limit) {
    try { last = await cdp.eval(expr); if (last) return last; } catch (e) { last = e.message; }
    await sleep(300);
  }
  throw new Error(`等待超时（${label}），最后结果：${JSON.stringify(last)}`);
}

async function shot(cdp, name) {
  await sleep(420);
  const r = await cdp.send('Page.captureScreenshot', { format: 'png' });
  const file = path.join(SHOTS, name + '.png');
  fs.writeFileSync(file, Buffer.from(r.data, 'base64'));
  return file;
}

/* ---------- 3. 页面侧桥（同步入队 + Node 侧回填 __sfbResolve） ---------- */
const BRIDGE_SRC = `(() => {
  window.SFB = {
    ping() { return JSON.stringify({ mode: 'native' }); },
    request(payload, id) {
      window.__sfbSend(JSON.stringify({ id: String(id), payload: payload }));
      return 'queued';
    },
    discover(id) {
      window.__sfbSend(JSON.stringify({ id: String(id), discover: true }));
      return 'queued';
    },
  };
})();`;

const bridgeCalls = [];
let discoveryCalls = 0;

async function main() {
  try {
    const probe = await fetch(BACKEND + '/api/overview');
    if (!probe.ok) failFast('本地后端不可用: ' + BACKEND);
  } catch (e) {
    failFast('本地后端不可用: ' + BACKEND + '（' + e.message + '）');
  }

  await new Promise((res) => server.listen(PORT, '127.0.0.1', res));

  // 服务侧规则本身也要验证：注入、MIME、404、路径穿越。
  const html = await (await fetch(ORIGIN + '/index.html')).text();
  check('index.html 注入容器标记', html.includes('window.__SUBFLOW_APP__=1'));
  check('index.html 注入默认服务器', html.includes('window.__SUBFLOW_DEFAULT_SERVER__="' + BACKEND + '"'));
  const cssRes = await fetch(ORIGIN + '/mobile.css');
  check('CSS MIME 正确', cssRes.headers.get('content-type') === MIME.css, cssRes.headers.get('content-type'));
  const jsRes = await fetch(ORIGIN + '/mobile.js?v=1');
  check('JS 带查询串仍命中且 MIME 正确',
    jsRes.status === 200 && jsRes.headers.get('content-type') === MIME.js, jsRes.headers.get('content-type'));
  const missRes = await fetch(ORIGIN + '/nope.js');
  check('缺失资源返回本地 404', missRes.status === 404 && (await missRes.text()).startsWith('Not Found:'));
  check('路径穿越被拒', (await rawStatus('/%2e%2e%5capp/static/m/index.html')) === 403);

  const { cdp, edge, profile } = await connectEdge();
  try {
    await cdp.send('Emulation.setDeviceMetricsOverride', { width: 414, height: 896, deviceScaleFactor: 2, mobile: true });
    await cdp.send('Runtime.addBinding', { name: '__sfbSend' });
    await cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: BRIDGE_SRC });

    cdp.on('Runtime.bindingCalled', async (p) => {
      if (p.name !== '__sfbSend') return;
      let id = '', req = null, isDiscover = false;
      try {
        const outer = JSON.parse(p.payload);
        id = String(outer.id);
        if (outer.discover) isDiscover = true;
        else req = JSON.parse(outer.payload);
      } catch (e) {
        return;
      }
      if (isDiscover) {
        discoveryCalls++;
        const out = JSON.stringify({
          ok: true,
          servers: [{ url: BACKEND, version: 'test' }],
          local: ['127.0.0.1'], scanned: 254, elapsed_ms: 12,
        });
        try {
          await cdp.send('Runtime.evaluate', {
            expression: `window.__sfbDiscoverResolve(${JSON.stringify(id)}, ${JSON.stringify(out)})`,
          });
        } catch (e) { /* 页面已导航，忽略 */ }
        return;
      }
      bridgeCalls.push({ method: req.method, url: req.url });
      let out;
      try {
        const res = await fetch(req.url, {
          method: req.method || 'GET',
          headers: req.headers || {},
          body: req.body ? req.body : undefined,
          signal: AbortSignal.timeout(Number(req.readTimeout) || 30000),
        });
        out = JSON.stringify({ status: res.status, body: await res.text() });
      } catch (e) {
        out = JSON.stringify({ status: 0, body: '', error: 'node-bridge: ' + e.message });
      }
      try {
        await cdp.send('Runtime.evaluate', {
          expression: `window.__sfbResolve(${JSON.stringify(id)}, ${JSON.stringify(out)})`,
        });
      } catch (e) { /* 页面已导航，忽略 */ }
    });

    await cdp.send('Page.navigate', { url: ORIGIN + '/index.html' });
    const runtimeStart = requests.length;   // 之前的 404 探针不算页面运行时请求
    await waitFor(cdp, `document.readyState==='complete' && !!document.querySelector('#st-today')`, '首屏 DOM', 25000);
    await waitFor(cdp, `document.querySelector('#st-today').textContent.trim()==='3'`, '概览经原生桥加载', 20000);

    // 容器首次启动：没有已保存地址，应自动弹出连接设置并预填注入的服务器。
    await waitFor(cdp, `!!document.querySelector('#set-base')`, '首次启动连接设置', 8000);
    check('首次启动预填注入地址', (await cdp.eval(`document.querySelector('#set-base').value`)) === BACKEND,
      await cdp.eval(`document.querySelector('#set-base').value`));
    check('首次启动提示原生桥', (await cdp.eval(`document.querySelector('#set-hint').textContent`)).includes('鸿蒙原生 HTTP 桥'),
      await cdp.eval(`document.querySelector('#set-hint').textContent`));
    await cdp.eval(`document.querySelector('[data-sheet-close]').click()`);
    await waitFor(cdp, `!document.querySelector('.sheet')`, '关闭首次启动设置', 8000);

    check('容器标记生效', (await cdp.eval('window.__SUBFLOW_APP__ === 1')) === true);
    check('页面识别容器形态', (await cdp.eval('IS_CONTAINER === true')) === true);
    check('页面选择原生桥而不是 fetch', (await cdp.eval('BRIDGE === window.SFB')) === true);
    check('默认服务器来自注入', (await cdp.eval('state.base')) === BACKEND, await cdp.eval('state.base'));
    check('桥收到概览请求', bridgeCalls.some((c) => c.url === BACKEND + '/api/overview'), bridgeCalls.slice(0, 3));

    check('待处理/失败统计', (await cdp.eval(`document.querySelector('#st-failed').textContent.trim()`)) === '2');
    check('最近任务 5 条', (await cdp.eval(`document.querySelectorAll('#home-jobs .job').length`)) === 5);
    check('移动端无横向溢出', await cdp.eval(`document.documentElement.scrollWidth <= window.innerWidth + 1`));
    check('底栏固定在视口底部', await cdp.eval(
      `(()=>{const r=document.querySelector('.tabbar').getBoundingClientRect();return Math.abs(r.bottom-window.innerHeight)<1.5})()`));

    const homeShot = await shot(cdp, 'container-home');

    await cdp.eval(`document.querySelector('.tab[data-view="jobs"]').click()`);
    await waitFor(cdp, `document.querySelectorAll('#job-list .job').length===7`, '任务列表经桥加载', 15000);
    check('任务列表 7 条（桥）', true);
    await cdp.eval(`document.querySelector('#job-keyword').value='Movie';document.querySelector('#job-keyword').dispatchEvent(new Event('input',{bubbles:true}))`);
    await waitFor(cdp, `document.querySelectorAll('#job-list .job').length===3`, '关键词过滤（桥）', 12000);
    check('关键词过滤（桥）', await cdp.eval(
      `[...document.querySelectorAll('#job-list .job-name')].every(e=>e.textContent.includes('Movie'))`),
      await cdp.eval(`[...document.querySelectorAll('#job-list .job-name')].map(e=>e.textContent)`));
    await cdp.eval(`document.querySelector('#job-keyword').value='';document.querySelector('#job-keyword').dispatchEvent(new Event('input',{bubbles:true}))`);
    await waitFor(cdp, `document.querySelectorAll('#job-list .job').length===7`, '清空关键词恢复', 12000);

    check('底部任务标签显示失败角标',
      (await cdp.eval(`document.querySelector('#tab-badge-jobs').textContent`)) === '2'
      && (await cdp.eval(`document.querySelector('#tab-badge-jobs').hidden`)) === false,
      await cdp.eval(`document.querySelector('#tab-badge-jobs').textContent`));

    // 长按 760ms 进入多选：勾选框必须是显式控件，点选累加，完成后清理。
    const cardPt = await cdp.eval(`(()=>{const r=document.querySelector('#job-list .job').getBoundingClientRect();
      return {x: Math.round(r.left+18), y: Math.round(r.top+Math.min(56, r.height-14))}})()`);
    await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: cardPt.x, y: cardPt.y, button: 'left', clickCount: 1 });
    await sleep(760);
    await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: cardPt.x, y: cardPt.y, button: 'left', clickCount: 1 });
    await waitFor(cdp, `!document.querySelector('#job-bar').hidden`, '长按进入多选', 6000);
    check('长按进入多选', true);
    check('每行提供显式勾选框',
      (await cdp.eval(`document.querySelectorAll('#job-list .job-pick input[data-pickjob]').length`)) === 7);
    check('长按行已选中',
      (await cdp.eval(`document.querySelectorAll('#job-list .job.picked').length`)) === 1);
    const selShot = await shot(cdp, 'container-jobs-select');
    check('批量重试按钮随选中启用', (await cdp.eval(`document.querySelector('#job-retry').disabled`)) === false);

    const card2 = await cdp.eval(`(()=>{const r=document.querySelectorAll('#job-list .job')[1].getBoundingClientRect();
      return {x: Math.round(r.left+18), y: Math.round(r.top+Math.min(56, r.height-14))}})()`);
    await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: card2.x, y: card2.y, button: 'left', clickCount: 1 });
    await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: card2.x, y: card2.y, button: 'left', clickCount: 1 });
    await waitFor(cdp, `document.querySelectorAll('#job-list .job.picked').length===2`, '点选第二行', 5000);
    check('多选累加', (await cdp.eval(`document.querySelector('#job-picked').textContent`)) === '已选 2',
      await cdp.eval(`document.querySelector('#job-picked').textContent`));
    await cdp.eval(`document.querySelector('#job-cancel').click()`);
    await waitFor(cdp, `document.querySelector('#job-bar').hidden && !document.querySelector('.job-pick')`, '退出多选', 6000);
    check('退出多选后清理勾选框', true);

    await cdp.eval(`document.querySelector('.tab[data-view="more"]').click()`);
    await waitFor(cdp, `document.querySelectorAll('.menu-row').length===5`, '更多页', 8000);
    check('更多页 5 个入口', true);

    await cdp.eval(`document.querySelector('[data-goto="stats"]').click()`);
    await waitFor(cdp, `document.querySelectorAll('#stats-body .stats-totals .stat').length===6`, '统计页', 15000);
    check('统计总计 6 项', true);
    check('统计 30 天柱状图', (await cdp.eval(`document.querySelectorAll('#stats-body .bars .bcol').length`)) === 30);
    await cdp.eval(`document.querySelector('#stats-range [data-days="7"]').click()`);
    await waitFor(cdp, `document.querySelectorAll('#stats-body .bars .bcol').length===7`, '统计切 7 天', 15000);
    check('统计范围切换生效', true);
    const statsShot = await shot(cdp, 'container-stats');

    await cdp.eval(`document.querySelector('.view[data-view="stats"] [data-back]').click()`);
    await waitFor(cdp, `document.querySelector('.view[data-view="more"]').classList.contains('on')`, '返回更多', 5000);
    await cdp.eval(`document.querySelector('[data-goto="sites"]').click()`);
    await waitFor(cdp, `!!document.querySelector('#cc-box .cc-top')`, '站点页', 12000);
    check('CookieCloud 卡片渲染',
      (await cdp.eval(`document.querySelector('#cc-box').textContent`)).includes('CookieCloud'));
    check('站点行非空', (await cdp.eval(`document.querySelectorAll('#site-list .site-row').length`)) >= 2,
      await cdp.eval(`document.querySelectorAll('#site-list .site-row').length`));

    await cdp.eval(`document.querySelector('.view[data-view="sites"] [data-back]').click()`);
    await cdp.eval(`document.querySelector('[data-goto="schedule"]').click()`);
    await waitFor(cdp, `!!document.querySelector('#sched-enabled')`, '定时页', 12000);
    check('定时页表单齐全', await cdp.eval(
      `!!document.querySelector('#sched-interval') && !!document.querySelector('#sched-days') && !!document.querySelector('#sched-save')`));
    check('定时页显示下次执行',
      (await cdp.eval(`document.querySelector('#sched-body').textContent`)).includes('下次执行'));

    await cdp.eval(`document.querySelector('.view[data-view="schedule"] [data-back]').click()`);
    await cdp.eval(`document.querySelector('#more-settings').click()`);
    await waitFor(cdp, `!!document.querySelector('#disc-btn')`, '设置弹层', 8000);
    check('设置里有局域网发现入口', (await cdp.eval(`document.querySelector('#disc-btn').disabled`)) === false);
    await cdp.eval(`document.querySelector('#disc-btn').click()`);
    await waitFor(cdp, `document.querySelectorAll('#disc-list .chip[data-url]').length===1`, '发现结果回填', 8000);
    check('发现结果走原生桥', (await cdp.eval(`document.querySelector('#disc-list .chip[data-url]').dataset.url`)) === BACKEND);
    await cdp.eval(`document.querySelector('#disc-list .chip[data-url]').click()`);
    check('点选发现结果回填地址', (await cdp.eval(`document.querySelector('#set-base').value`)) === BACKEND);
    await cdp.eval(`document.querySelector('[data-sheet-close]').click()`);
    check('发现调用次数 1', discoveryCalls === 1, discoveryCalls);

    await cdp.eval(`document.querySelector('.tab[data-view="logs"]').click()`);
    await waitFor(cdp, `document.querySelectorAll('#log-list .logline').length>=1`, '日志经桥加载', 15000);
    check('日志首屏非空（桥）', true);

    await cdp.eval(`document.querySelector('#btn-settings').click()`);
    await waitFor(cdp, `!!document.querySelector('#set-base')`, '设置弹层', 8000);
    check('设置回显注入的服务器地址', (await cdp.eval(`document.querySelector('#set-base').value`)) === BACKEND);
    check('设置显示原生桥通道', (await cdp.eval(`document.querySelector('#set-hint').textContent`)).includes('鸿蒙原生 HTTP 桥'),
      await cdp.eval(`document.querySelector('#set-hint').textContent`));
    const setShot = await shot(cdp, 'container-settings');
    await cdp.eval(`document.querySelector('[data-sheet-close]').click()`);

    const lsOk = await cdp.eval(`(()=>{try{localStorage.setItem('subflow.test','1');const v=localStorage.getItem('subflow.test');localStorage.removeItem('subflow.test');return v==='1'}catch(e){return false}})()`);
    check('localStorage 可用', lsOk === true);

    // 下拉刷新：合成 touch 序列，验证当前视图重新拉取数据。
    await cdp.eval(`document.querySelector('.tab[data-view="home"]').click()`);
    await waitFor(cdp, `document.querySelector('.view[data-view="home"]').classList.contains('on')`, '回到概览', 5000);
    const ovBefore = bridgeCalls.filter((c) => c.url === BACKEND + '/api/overview').length;
    await cdp.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 1 });
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: 200, y: 140 }] });
    for (const y of [200, 270, 340]) {
      await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: 200, y }] });
      await sleep(50);
    }
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    let ovAfter = ovBefore;
    const ptrLimit = Date.now() + 8000;
    while (Date.now() < ptrLimit && ovAfter <= ovBefore) {
      ovAfter = bridgeCalls.filter((c) => c.url === BACKEND + '/api/overview').length;
      if (ovAfter <= ovBefore) await sleep(150);
    }
    check('下拉刷新重新拉取概览', ovAfter > ovBefore, { before: ovBefore, after: ovAfter });

    check('无页面 JS 异常', cdp.exceptions.length === 0, cdp.exceptions);
    const realErrors = cdp.consoleErrors.filter((x) => !/favicon/i.test(x));
    check('无控制台错误', realErrors.length === 0, realErrors);

    const asset404 = requests.slice(runtimeStart).filter((r) => r.status === 404 && !/favicon/i.test(r.path));
    check('页面运行时无资源 404', asset404.length === 0, asset404);
    const badMime = requests.filter((r) => r.status === 200 && r.path.endsWith('.js') && !r.mime.startsWith('text/javascript'));
    check('JS 均以正确 MIME 返回', badMime.length === 0, badMime);

    console.log('\nSHOTS ' + [homeShot, setShot, selShot, statsShot].join(' '));
    console.log(`\nBRIDGE CALLS ${bridgeCalls.length}`);
    console.log(`\nRESULT PASS ${pass} / FAIL ${fail}`);
    if (failures.length) console.log('FAILURES:\n' + failures.join('\n'));
  } finally {
    try { edge.kill(); } catch (e) { /* ignore */ }
    await sleep(600);
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) { /* ignore */ }
  }
}

main()
  .catch((e) => {
    fail++;
    console.log('EXCEPTION ' + e.message);
    console.log(`\nRESULT PASS ${pass} / FAIL ${fail}`);
  })
  .finally(() => server.close());
