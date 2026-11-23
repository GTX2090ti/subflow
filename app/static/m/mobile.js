/* SubFlow 手机端 —— 原生 JS，无框架、无构建步骤。
 *
 * 两套运行形态共用这一份代码：
 *   1) 浏览器 / 服务端托管（/m/）：同源 fetch；
 *   2) 鸿蒙 HAP 内 ArkWeb 虚拟域：优先走原生 HTTP 桥 window.SFB，
 *      避免虚拟域到局域网 http 的 CORS / mixed content 限制。
 */

'use strict';

const $ = (id) => document.getElementById(id);
const IS_CONTAINER = window.__SUBFLOW_APP__ === 1;
const BRIDGE = (window.SFB && typeof window.SFB.request === 'function') ? window.SFB : null;

/* 原生桥协议：request(reqJson, reqId) 同步返回 'queued'，HTTP 完成后容器用
 * runJavaScript 调 window.__sfbResolve(reqId, jsonString) 回填结果。 */
let sfbSeq = 0;
const sfbPending = new Map();

window.__sfbResolve = function (id, json) {
  const item = sfbPending.get(id);
  if (!item) return;
  sfbPending.delete(id);
  clearTimeout(item.timer);
  item.resolve(json);
};

function bridgeRequest(payload, timeoutMs) {
  return new Promise((resolve, reject) => {
    const id = 'r' + (++sfbSeq);
    const timer = setTimeout(() => {
      if (sfbPending.has(id)) {
        sfbPending.delete(id);
        reject(new Error('连接超时'));
      }
    }, (timeoutMs || 30000) + 5000);
    sfbPending.set(id, { resolve, reject, timer });
    try {
      window.SFB.request(JSON.stringify(payload), id);
    } catch (e) {
      clearTimeout(timer);
      sfbPending.delete(id);
      reject(e);
    }
  });
}

function probeBridge() {
  if (!BRIDGE) return;
  try {
    console.log('[sfb] ping = ' + String(window.SFB.ping()));
  } catch (e) {
    console.log('[sfb] ping threw: ' + (e && e.message ? e.message : e));
  }
}

/* 局域网自动发现：容器侧扫描本机所在 /24 网段的 8099 端口，
 * 结果同样用 runJavaScript 回调回填，避免长连接挡住原生桥。 */
let sfbDiscSeq = 0;
const sfbDiscPending = new Map();

window.__sfbDiscoverResolve = function (id, json) {
  const item = sfbDiscPending.get(id);
  if (!item) return;
  sfbDiscPending.delete(id);
  clearTimeout(item.timer);
  item.resolve(json);
};

function bridgeDiscover(timeoutMs) {
  return new Promise((resolve, reject) => {
    const id = 'd' + (++sfbDiscSeq);
    const timeout = timeoutMs || 30000;
    const timer = setTimeout(() => {
      if (sfbDiscPending.has(id)) {
        sfbDiscPending.delete(id);
        reject(new Error('扫描超时'));
      }
    }, timeout);
    sfbDiscPending.set(id, { resolve, reject, timer });
    try {
      window.SFB.discover(id);
    } catch (e) {
      clearTimeout(timer);
      sfbDiscPending.delete(id);
      reject(e);
    }
  });
}

function canDiscover() {
  return !!(BRIDGE && typeof window.SFB.discover === 'function');
}

const STORE_KEY = 'subflow.mobile.v1';
const TAB_OF = {
  home: 'home', jobs: 'jobs', missing: 'missing', logs: 'logs',
  more: 'more', stats: 'more', zone: 'more', sites: 'more', schedule: 'more',
};
const SOURCE_LABEL = {
  scan: '自动扫描', manual: '手动提交', zone: '字幕区',
  legacy: '旧版导入', missing: '缺字幕补单', schedule: '定时扫描',
};

const JOB_FILTERS = [
  { k: 'all', label: '全部' },
  { k: 'failed', label: '失败' },
  { k: 'running', label: '进行中' },
  { k: 'pending', label: '待处理' },
  { k: 'success', label: '成功' },
  { k: 'skipped', label: '已跳过' },
];
const LOG_LEVELS = ['ALL', 'INFO', 'WARN', 'ERROR'];
const PAGE_SIZE = 30;

/* ---------------------------------------------------------------- 状态 */

const state = {
  base: '',
  savedBase: '',
  user: '',
  pass: '',
  remember: true,
  view: 'home',
  sites: [],
  sitesAll: [],
  overview: null,
  recentLoaded: false,
  jobs: {
    status: 'all', keyword: '', rows: [], total: 0, offset: 0, loading: false,
    selMode: false, selIds: new Set(),
  },
  missing: {
    items: [], picked: new Set(), loading: false,
    /* 站点复查：运行态、轮询句柄、复查上限、「只看新上」过滤 */
    probe: { running: false, timer: null, probeLimit: 50, newOnly: false, polls: 0 },
    lastStat: '',
  },
  cats: { data: null, picked: new Set(), loading: false },
  logs: { level: 'ALL', keyword: '', since: 0, rows: [], auto: true, loading: false },
  stats: { days: 30, data: null, loading: false },
  zone: {
    site: 'all', pages: 1, keyword: '', onlyMatched: false,
    entries: [], picked: new Set(), loading: false, sites: {},
  },
  sitesView: { data: null, loading: false },
  sched: { data: null, cfg: null, loading: false },
  settingsOpen: false,
  authPrompted: false,
  booted: false,
};

function loadSaved() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(STORE_KEY) || '{}') || {}; } catch (e) { saved = {}; }
  const injected = window.__SUBFLOW_DEFAULT_SERVER__ || '';
  const fallback = IS_CONTAINER ? injected : (location.origin === 'null' ? '' : location.origin);
  state.base = normalizeBase(saved.base || fallback);
  state.user = saved.user || '';
  state.pass = saved.remember === false ? '' : (saved.pass || '');
  state.remember = saved.remember !== false;
  state.savedBase = normalizeBase(saved.base || '');
}

function saveSaved() {
  const payload = {
    base: state.base,
    user: state.user,
    pass: state.remember ? state.pass : '',
    remember: state.remember,
  };
  state.savedBase = state.base;
  try { localStorage.setItem(STORE_KEY, JSON.stringify(payload)); } catch (e) { /* 隐私模式忽略 */ }
}

/* ---------------------------------------------------------------- 基础工具 */

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function normalizeBase(v) {
  v = String(v || '').trim();
  if (!v) return '';
  if (!/^https?:\/\//i.test(v)) v = 'http://' + v;
  return v.replace(/\/+$/, '');
}

function baseLabel() {
  if (!state.base) return '未配置服务器';
  try { return new URL(state.base).host; } catch (e) { return state.base; }
}

function fmtTime(t) {
  if (!t) return '-';
  const s = String(t).replace('T', ' ');
  return s.length >= 16 ? s.slice(5, 16) : s;
}

function fmtDuration(ms) {
  ms = Number(ms) || 0;
  if (!ms) return '-';
  if (ms < 1000) return ms + ' ms';
  if (ms < 60000) return (ms / 1000).toFixed(1) + ' s';
  return (ms / 60000).toFixed(1) + ' min';
}

function fmtSize(n) {
  n = Number(n) || 0;
  if (n <= 0) return '-';
  if (n < 1048576) return (n / 1024).toFixed(0) + ' KB';
  if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
  return (n / 1073741824).toFixed(2) + ' GB';
}

function toast(msg, kind) {
  const root = $('toast-root');
  const el = document.createElement('div');
  el.className = 'toast' + (kind ? ' ' + kind : '');
  el.textContent = msg;
  root.appendChild(el);
  setTimeout(() => {
    el.style.transition = 'opacity .2s';
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 220);
  }, kind === 'err' ? 4200 : 2600);
}

function setConn(kind, text) {
  const dot = $('conn-dot');
  dot.className = 'dot' + (kind ? ' ' + kind : '');
  $('conn-state').textContent = text || baseLabel();
}

function setView(name) {
  if (!TAB_OF[name]) name = 'home';
  state.view = name;
  document.querySelectorAll('.view').forEach((v) => v.classList.toggle('on', v.dataset.view === name));
  const tab = TAB_OF[name];
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('on', t.dataset.view === tab));
  window.scrollTo({ top: 0, behavior: 'instant' });
  if (name !== 'jobs') exitJobSelect();
  if (name === 'home') loadOverview();
  else if (name === 'jobs') { state.jobs.rows.length ? refreshJobsQuiet() : loadJobs(true); }
  else if (name === 'logs') { state.logs.rows.length ? pollLogs() : loadLogsTail(); }
  else if (name === 'stats') loadStats();
  else if (name === 'zone') { if (!state.zone.entries.length) loadZone(); }
  else if (name === 'sites') loadSites();
  else if (name === 'schedule') loadSchedule();
  else if (name === 'more') renderMore();
}

/* 当前视图的刷新动作，供下拉刷新与定时轮询共用。 */
function refreshCurrentView() {
  const v = state.view;
  if (v === 'home') return loadOverview(true);
  if (v === 'jobs') return loadJobs(true);
  // 复查进行中时下拉刷新只提示，不打断正在跑的复查
  if (v === 'missing') {
    if (state.missing.probe.running) return Promise.resolve();
    return state.missing.items.length ? runMissingScan() : Promise.resolve();
  }
  if (v === 'logs') return loadLogsTail();
  if (v === 'stats') return loadStats();
  if (v === 'zone') return loadZone(true);
  if (v === 'sites') return loadSites();
  if (v === 'schedule') return loadSchedule();
  return loadOverview(true);
}

/* ---------------------------------------------------------------- 底部弹层 */

let sheetOnClose = null;

function openSheet(opts) {
  const root = $('sheet-root');
  sheetOnClose = opts.onClose || null;
  const actions = opts.actions || [];
  root.innerHTML = `
    <div class="sheet-backdrop"></div>
    <div class="sheet" role="dialog" aria-modal="true">
      <div class="sheet-head">
        <h3>${esc(opts.title || '')}</h3>
        <button class="icon-btn" data-sheet-close aria-label="关闭"><svg><use href="#i-close"/></svg></button>
      </div>
      <div class="sheet-body">${opts.body || ''}</div>
      ${actions.length ? `<div class="sheet-foot">${actions.map((a, i) =>
        `<button class="btn ${a.kind || ''}" data-si="${i}">${esc(a.label)}</button>`).join('')}</div>` : ''}
    </div>`;
  root.classList.add('on');
  document.body.style.overflow = 'hidden';
  root.querySelector('.sheet-backdrop').onclick = closeSheet;
  root.querySelector('[data-sheet-close]').onclick = closeSheet;
  actions.forEach((a, i) => {
    const btn = root.querySelector(`[data-si="${i}"]`);
    if (!btn) return;
    btn.onclick = async () => {
      if (btn.disabled) return;
      btn.disabled = true;
      let keep = false;
      try { keep = (a.onClick ? await a.onClick(btn) : undefined) === false; }
      catch (e) { toast(e.message || String(e), 'err'); keep = true; }
      finally { btn.disabled = false; }
      if (!keep) closeSheet();
    };
  });
  if (opts.onMount) opts.onMount(root);
  return root;
}

function closeSheet() {
  const root = $('sheet-root');
  if (!root.classList.contains('on')) return;
  root.classList.remove('on');
  root.innerHTML = '';
  document.body.style.overflow = '';
  const cb = sheetOnClose;
  sheetOnClose = null;
  if (cb) cb();
}

function confirmSheet(title, text, okLabel, danger) {
  return new Promise((resolve) => {
    let settled = false;
    openSheet({
      title,
      body: `<p class="sheet-note">${esc(text)}</p>`,
      onClose: () => { if (!settled) { settled = true; resolve(false); } },
      actions: [
        { label: '取消', kind: 'ghost', onClick: () => { settled = true; resolve(false); } },
        { label: okLabel || '确认', kind: danger ? 'danger' : 'primary',
          onClick: () => { settled = true; resolve(true); } },
      ],
    });
  });
}

/* ---------------------------------------------------------------- 网络层 */

function authHeader() {
  if (!state.user && !state.pass) return '';
  const raw = state.user + ':' + state.pass;
  try {
    return 'Basic ' + btoa(unescape(encodeURIComponent(raw)));
  } catch (e) {
    return '';
  }
}

function shortBridgeError(raw) {
  const s = String(raw || '');
  if (s.includes('2300060')) return '证书校验失败（原生桥不支持自签 https，请用 http 或有效证书）';
  if (s.includes('2300007')) return '无法连接服务器（地址或端口不对）';
  if (s.includes('timeout') || s.includes('Timeout')) return '连接超时';
  return s || '容器网络通道异常';
}

async function api(path, opts) {
  opts = opts || {};
  const method = (opts.method || 'GET').toUpperCase();
  const timeout = Number(opts.timeout) || 30000;
  const headers = {};
  const body = opts.body == null ? null : JSON.stringify(opts.body);
  if (body !== null) headers['Content-Type'] = 'application/json';
  const auth = authHeader();
  if (auth) headers.Authorization = auth;

  const url = state.base.replace(/\/+$/, '') + path;
  let status = 0;
  let text = '';

  if (BRIDGE) {
    let res;
    try {
      res = await bridgeRequest({
        method, url, headers,
        body: body === null ? '' : body,
        connectTimeout: 8000,
        readTimeout: timeout,
      }, timeout);
    } catch (e) {
      throw new Error('容器通道异常：' + (e && e.message ? e.message : e));
    }
    let parsed;
    try { parsed = JSON.parse(res); } catch (e) { throw new Error('容器通道返回异常'); }
    status = Number(parsed.status) || 0;
    if (!status) throw new Error(shortBridgeError(parsed.error));
    text = parsed.body || '';
  } else {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeout);
    try {
      const res = await fetch(url, {
        method, headers,
        body: body === null ? undefined : body,
        signal: ctl.signal,
        cache: 'no-store',
      });
      status = res.status;
      text = await res.text();
    } catch (e) {
      const msg = e && e.name === 'AbortError'
        ? '请求超时（' + Math.round(timeout / 1000) + 's）'
        : '网络错误：无法连接 ' + baseLabel();
      throw new Error(msg);
    } finally {
      clearTimeout(timer);
    }
  }

  let data = null;
  try { data = text ? JSON.parse(text) : {}; } catch (e) {
    throw new Error('服务器返回非 JSON（HTTP ' + status + '）');
  }
  if (status === 401) {
    if (!state.settingsOpen && !state.authPrompted) {
      state.authPrompted = true;
      setTimeout(() => openSettings('需要登录：请填写访问控制的账号密码'), 0);
    }
    throw new Error('需要登录（HTTP 401）');
  }
  if (status >= 400 || (data && data.ok === false)) {
    throw new Error((data && data.error) || ('HTTP ' + status));
  }
  return data || {};
}

/* ---------------------------------------------------------------- 概览 */

async function loadOverview(silent) {
  try {
    const d = await api('/api/overview');
    state.overview = d;
    setConn('ok', baseLabel() + ' · 已连接');
    state.authPrompted = false;
    renderOverview(d);
    loadRecent();
  } catch (e) {
    setConn('err', baseLabel() + ' · 连接失败');
    if (!silent) toast(e.message, 'err');
    if (silent) renderOffline();
  }
}

function renderOffline() {
  if (state.overview) return;
  $('home-phase').textContent = '无法连接';
  $('home-meta').textContent = '检查服务器地址与账号';
  $('home-queue').textContent = '';
}

function renderOverview(d) {
  const st = d.stats || {};
  $('st-pending').textContent = st.pending == null ? '-' : st.pending;
  $('st-running').textContent = st.running == null ? '-' : st.running;
  $('st-failed').textContent = st.failed == null ? '-' : st.failed;
  $('st-today').textContent = st.today == null ? '-' : st.today;
  $('home-version').textContent = d.version ? 'v' + d.version : '-';
  $('home-queue').textContent = '队列 ' + (d.queue || 0) + ' · 执行中 ' + (d.busy || 0);

  const scan = d.scan || {};
  const phase = $('home-phase');
  const bar = $('home-progress');
  if (scan.running) {
    phase.textContent = (scan.phase || '扫描中') + (scan.total ? ` ${scan.progress}/${scan.total}` : '');
    const pct = scan.total ? Math.min(100, Math.round(scan.progress / scan.total * 100)) : 12;
    bar.style.width = pct + '%';
    bar.parentElement.classList.add('busy');
    $('home-meta').textContent = '开始于 ' + fmtTime(scan.started_at);
  } else {
    const r = scan.result;
    if (r) {
      phase.textContent = '上次扫描完成';
      bar.style.width = '100%';
      $('home-meta').textContent = `扫描 ${r.scanned || 0} · 命中 ${r.matched || 0} · 新建 ${r.jobs_created || 0} · ${fmtDuration(r.elapsed_ms)}`
        + (r.dry_run ? ' · 演练' : '');
    } else {
      phase.textContent = '空闲';
      bar.style.width = '0%';
      $('home-meta').textContent = '还没有执行过扫描';
    }
    bar.parentElement.classList.remove('busy');
  }
  $('home-tip').textContent = `成功 ${st.success || 0} · 跳过 ${st.skipped || 0} · 累计 ${st.total || 0}`;
  renderTabBadge(st);
  renderMore(d);
  renderJobChips();
  updateQuickButtons();
}

function renderTabBadge(st) {
  const badge = $('tab-badge-jobs');
  if (!badge) return;
  const failed = Number((st || {}).failed) || 0;
  badge.hidden = failed <= 0;
  badge.textContent = failed > 99 ? '99+' : String(failed);
}

function renderMore(d) {
  d = d || state.overview;
  const base = $('more-base');
  if (!base) return;
  base.textContent = state.base ? baseLabel() : '未配置服务器';
  const st = (d && d.stats) || {};
  $('more-summary').textContent = d
    ? `累计 ${st.total || 0} · 成功 ${st.success || 0} · 失败 ${st.failed || 0}`
    : '尚未连接';
  $('more-version').textContent = d && d.version ? 'v' + d.version : '-';
  $('more-channel').textContent = '通道：' + (BRIDGE ? '鸿蒙原生 HTTP 桥' : '浏览器 fetch')
    + (IS_CONTAINER ? ' · 容器内运行' : '') + '。';
}

function updateQuickButtons() {
  const scan = (state.overview && state.overview.scan) || {};
  const running = !!scan.running;
  $('btn-scan').disabled = running;
  $('btn-cat').disabled = running;
}

async function loadRecent() {
  const box = $('home-jobs');
  if (!state.recentLoaded) box.innerHTML = skeletonJobs(3);
  try {
    const d = await api('/api/jobs?limit=5&offset=0');
    const rows = d.jobs || [];
    state.recentLoaded = true;
    box.innerHTML = rows.length
      ? rows.map((j) => jobCard(j, false)).join('')
      : '<div class="empty">暂无任务</div>';
  } catch (e) {
    if (!state.overview) box.innerHTML = '<div class="empty err">' + esc(e.message) + '</div>';
  }
}

/* ---------------------------------------------------------------- 骨架屏 */

function skeletonJobs(n) {
  let out = '';
  for (let i = 0; i < (n || 3); i++) {
    out += `<div class="sk sk-pad"><div class="sk-line w30"></div>
      <div class="sk-line w90"></div><div class="sk-line w60"></div></div>`;
  }
  return out;
}

function skeletonTiles(n) {
  let out = '';
  for (let i = 0; i < (n || 6); i++) out += '<div class="sk sk-tile"></div>';
  return `<div class="sk-tiles">${out}</div>`;
}

/* ---------------------------------------------------------------- 任务 */

function statusBadge(j) {
  const s = j.status || '';
  return `<span class="badge ${esc(s)}">${esc(j.status_label || s)}</span>`;
}

function jobCard(j, actions, selMode) {
  const cat = j.category
    ? `<p class="job-meta">${esc(j.category)}${j.downloader_id ? ' · ' + esc(j.downloader_id) : ''}</p>`
    : '';
  const msg = j.message ? `<p class="job-msg ${j.status === 'failed' ? 'err' : ''}">${esc(j.message)}</p>` : '';
  const acts = actions === false ? '' : `
    <div class="job-acts">
      ${j.status === 'failed' ? '<button data-act="retry">重试</button>' : ''}
      <button data-act="del" class="danger">删除</button>
    </div>`;
  const id = String(j.id);
  const picked = !!(selMode && state.jobs.selIds.has(id));
  const pick = selMode
    ? `<label class="job-pick"><input type="checkbox" data-pickjob="${esc(id)}" ${picked ? 'checked' : ''}></label>`
    : '';
  return `<article class="job${selMode ? ' selecting' : ''}${picked ? ' picked' : ''}" data-id="${esc(id)}">
    ${pick}
    <div class="job-top">${statusBadge(j)}<span class="job-site">${esc(j.site_label || j.site_id || '')}</span>
      <time>${esc(fmtTime(j.created_at))}</time></div>
    <h3 class="job-name">${esc(j.torrent_name || j.torrent_id || '未命名任务')}</h3>
    ${j.sub_title ? `<p class="job-sub">${esc(j.sub_title)}</p>` : ''}
    ${cat}
    ${j.file_size ? `<p class="job-meta">字幕包 ${esc(fmtSize(j.file_size))}</p>` : ''}
    ${msg}
    <div class="job-foot">
      <span>${esc(fmtTime(j.finished_at || j.started_at || ''))}</span>
      ${acts}
    </div>
  </article>`;
}

function renderJobChips() {
  const st = (state.overview && state.overview.stats) || {};
  const counts = {
    all: st.total, failed: st.failed, running: st.running,
    pending: st.pending, success: st.success, skipped: st.skipped,
  };
  const box = $('job-chips');
  box.innerHTML = JOB_FILTERS.map((f) => {
    const n = counts[f.k];
    return `<button class="chip ${state.jobs.status === f.k ? 'on' : ''}" data-filter="${f.k}">
      ${esc(f.label)}${n != null ? `<span class="n">${n}</span>` : ''}</button>`;
  }).join('');
}

async function loadJobs(reset) {
  const st = state.jobs;
  if (st.loading) return;
  st.loading = true;
  if (reset) {
    st.offset = 0;
    st.rows = [];
    $('job-list').innerHTML = skeletonJobs(4);
    $('job-more').hidden = true;
  }
  try {
    const q = new URLSearchParams({
      status: st.status, limit: String(PAGE_SIZE), offset: String(st.offset),
    });
    if (st.keyword) q.set('keyword', st.keyword);
    const d = await api('/api/jobs?' + q.toString());
    const rows = d.jobs || [];
    st.total = Number(d.total) || 0;
    st.rows = reset ? rows : st.rows.concat(rows);
    st.offset = st.rows.length;
    setConn('ok', baseLabel() + ' · 已连接');
    renderJobs();
  } catch (e) {
    if (reset || !st.rows.length) {
      $('job-list').innerHTML = '<div class="empty err">' + esc(e.message) + '</div>';
    }
    if (reset) toast(e.message, 'err');
  } finally {
    st.loading = false;
  }
}

function renderJobs() {
  const st = state.jobs;
  const box = $('job-list');
  box.innerHTML = st.rows.length
    ? st.rows.map((j) => jobCard(j, true, st.selMode)).join('')
    : `<div class="empty">${st.keyword ? '没有匹配的任务' : '这个筛选下没有任务'}</div>`;
  const more = $('job-more');
  more.hidden = st.rows.length >= st.total;
  more.textContent = `加载更多（${st.rows.length}/${st.total}）`;
  updateJobBar();
}

/* ---- 多选：长按进入，勾选框显式可选 ---- */

function exitJobSelect() {
  const st = state.jobs;
  if (!st.selMode && !st.selIds.size) return;
  st.selMode = false;
  st.selIds.clear();
  const bar = $('job-bar');
  if (bar) bar.hidden = true;
  document.querySelector('.view[data-view="jobs"]').classList.remove('has-bar');
  if (state.view === 'jobs') renderJobs();
}

function enterJobSelect(id) {
  const st = state.jobs;
  st.selMode = true;
  st.selIds.clear();
  if (id) st.selIds.add(String(id));
  $('job-bar').hidden = false;
  document.querySelector('.view[data-view="jobs"]').classList.add('has-bar');
  renderJobs();
}

function toggleJobPick(id, on) {
  const st = state.jobs;
  if (on) st.selIds.add(String(id)); else st.selIds.delete(String(id));
  if (!st.selIds.size) { exitJobSelect(); return; }
  const card = $('job-list').querySelector(`.job[data-id="${id}"]`);
  if (card) card.classList.toggle('picked', on);
  updateJobBar();
}

function updateJobBar() {
  const st = state.jobs;
  const bar = $('job-bar');
  if (!bar) return;
  $('job-picked').textContent = '已选 ' + st.selIds.size;
  $('job-retry').disabled = !st.selIds.size;
  const all = st.rows.length > 0 && st.rows.every((j) => st.selIds.has(String(j.id)));
  $('job-all').checked = all;
}

async function retrySelectedJobs() {
  const st = state.jobs;
  const ids = [...st.selIds];
  if (!ids.length) { toast('请先选择任务', 'err'); return; }
  const yes = await confirmSheet('批量重试', `将把 ${ids.length} 个任务重新入队。`, '重试');
  if (!yes) return;
  const btn = $('job-retry');
  btn.disabled = true;
  let ok = 0;
  let bad = 0;
  for (const id of ids) {
    try {
      await api(`/api/jobs/${id}/retry`, { method: 'POST' });
      ok++;
    } catch (e) {
      bad++;
    }
    btn.textContent = `重试中 ${ok + bad}/${ids.length}`;
  }
  btn.textContent = '批量重试';
  exitJobSelect();
  toast(`已入队 ${ok} 个${bad ? `，失败 ${bad}` : ''}`, bad ? 'err' : 'ok');
  await loadJobs(true);
  loadOverview(true);
}

async function openJobSheet(id) {
  let job;
  try {
    const d = await api('/api/jobs/' + encodeURIComponent(id));
    job = d.job;
  } catch (e) {
    toast(e.message, 'err');
    return;
  }
  if (!job) { toast('任务不存在', 'err'); return; }
  const row = (k, v, err) => (v === '' || v == null
    ? '' : `<div class="kv-row"><span>${esc(k)}</span><b class="${err ? 'err' : ''}">${esc(v)}</b></div>`);
  const body = `
    <div class="kv-row"><span>状态</span><b>${esc(job.status_label || job.status || '')}</b></div>
    ${row('站点', job.site_label || job.site_id)}
    ${row('种子 ID', job.torrent_id)}
    ${row('字幕 ID', job.subid)}
    ${row('字幕标题', job.sub_title)}
    ${row('种子名称', job.torrent_name)}
    ${row('保存目录', job.save_path)}
    ${row('文件', job.file_name)}
    ${row('大小', job.file_size ? fmtSize(job.file_size) : '')}
    ${row('解压数', job.extracted ? String(job.extracted) : '')}
    ${row('来源', SOURCE_LABEL[job.source] || job.source)}
    ${row('分类', job.category)}
    ${row('下载器', job.downloader_id)}
    ${row('创建', job.created_at)}
    ${row('开始', job.started_at)}
    ${row('完成', job.finished_at)}
    ${row('耗时', job.duration_ms ? fmtDuration(job.duration_ms) : '')}
    ${job.message ? `<div class="kv-row"><span>消息</span><b class="${job.status === 'failed' ? 'err' : ''}">${esc(job.message)}</b></div>` : ''}`;
  openSheet({
    title: '任务 #' + job.id,
    body,
    actions: [
      { label: '关闭', kind: 'ghost' },
      ...(job.status === 'failed'
        ? [{ label: '重试', kind: 'primary', onClick: async (btn) => {
          btn.textContent = '提交中…';
          try {
            await api(`/api/jobs/${job.id}/retry`, { method: 'POST' });
            toast('已重新入队', 'ok');
            loadJobs(true);
            loadOverview(true);
          } catch (e) { toast(e.message, 'err'); btn.textContent = '重试'; return false; }
        } }]
        : []),
    ],
  });
}

async function refreshJobsQuiet() {
  const st = state.jobs;
  if (st.loading || st.offset > PAGE_SIZE) return;
  try {
    const q = new URLSearchParams({ status: st.status, limit: String(PAGE_SIZE), offset: '0' });
    if (st.keyword) q.set('keyword', st.keyword);
    const d = await api('/api/jobs?' + q.toString());
    st.rows = d.jobs || [];
    st.total = Number(d.total) || 0;
    st.offset = st.rows.length;
    renderJobs();
  } catch (e) { /* 静默刷新失败不打扰 */ }
}

let suppressJobTap = false;

function onJobClick(ev) {
  const card = ev.target.closest('.job');
  if (!card) return;
  const id = String(card.dataset.id || '');
  if (!id) return;
  if (suppressJobTap) { suppressJobTap = false; return; }

  if (state.jobs.selMode && ev.currentTarget && ev.currentTarget.id === 'job-list') {
    if (ev.target.closest('.job-pick')) return;   // 勾选框自己走 change
    if (ev.target.closest('button[data-act]')) return;
    toggleJobPick(id, !state.jobs.selIds.has(id));
    return;
  }

  const btn = ev.target.closest('button[data-act]');
  if (btn) { onJobAction(btn, id); return; }
  openJobSheet(id);
}

function bindJobLongPress() {
  const list = $('job-list');
  let press = null;
  const cancel = () => {
    if (press) { clearTimeout(press.timer); press = null; }
  };
  list.addEventListener('pointerdown', (ev) => {
    // 上一次长按留下的抑制标记只挡「松开手指时那一次」合成 click；
    // 下一次按下就清掉，避免列表重绘后没有 click 可消费、把后面一整次点击吞掉。
    suppressJobTap = false;
    if (state.jobs.selMode) return;
    const card = ev.target.closest('.job');
    if (!card) return;
    if (ev.target.closest('button, input, label')) return;
    const id = String(card.dataset.id || '');
    if (!id) return;
    const x = ev.clientX;
    const y = ev.clientY;
    press = {
      timer: setTimeout(() => {
        press = null;
        suppressJobTap = true;
        enterJobSelect(id);
        try { if (navigator.vibrate) navigator.vibrate(12); } catch (e) { /* 忽略 */ }
      }, 520),
      x, y,
    };
  });
  list.addEventListener('pointermove', (ev) => {
    if (!press) return;
    if (Math.abs(ev.clientX - press.x) > 10 || Math.abs(ev.clientY - press.y) > 10) cancel();
  });
  ['pointerup', 'pointercancel', 'pointerleave'].forEach((name) => {
    list.addEventListener(name, cancel);
  });
}

async function onJobAction(btn, id) {
  const act = btn.dataset.act;
  if (act === 'retry') {
    btn.disabled = true;
    try {
      await api(`/api/jobs/${id}/retry`, { method: 'POST' });
      toast('已重新入队', 'ok');
      await loadJobs(true);
      loadOverview(true);
    } catch (e) { toast(e.message, 'err'); }
    finally { btn.disabled = false; }
  } else if (act === 'del') {
    const yes = await confirmSheet('删除记录', '只删除任务记录，不会删除已落盘的字幕文件。', '删除', true);
    if (!yes) return;
    try {
      await api(`/api/jobs/${id}`, { method: 'DELETE' });
      toast('已删除');
      await loadJobs(true);
      loadOverview(true);
    } catch (e) { toast(e.message, 'err'); }
  }
}

/* ---------------------------------------------------------------- 统计 */

async function loadStats() {
  const S = state.stats;
  if (S.loading) return;
  S.loading = true;
  const body = $('stats-body');
  body.innerHTML = skeletonTiles(6)
    + '<div class="sk sk-pad"><div class="sk-line w30"></div>'
    + '<div class="sk-line" style="height:94px"></div></div>';
  try {
    const d = await api('/api/stats?days=' + S.days);
    S.data = d;
    renderStats(d);
  } catch (e) {
    body.innerHTML = '<div class="empty err">' + esc(e.message) + '</div>';
  } finally {
    S.loading = false;
  }
}

function pct(n, max) {
  n = Number(n) || 0;
  max = Number(max) || 0;
  return max > 0 ? Math.max(n > 0 ? 2 : 0, Math.round(n / max * 100)) : 0;
}

function renderStats(d) {
  const t = d.totals || {};
  const body = $('stats-body');
  $('stats-at').textContent = d.generated_at ? '更新 ' + fmtTime(d.generated_at) : '';

  const totals = `
    <div class="stats-totals">
      <div class="stat"><span>任务总数</span><b>${t.total || 0}</b></div>
      <div class="stat ok"><span>成功</span><b>${t.ok || 0}</b></div>
      <div class="stat err"><span>失败</span><b>${t.failed || 0}</b></div>
      <div class="stat"><span>跳过</span><b>${t.skipped || 0}</b></div>
      <div class="stat"><span>字幕体积</span><b>${esc(fmtSize(t.size))}</b></div>
      <div class="stat"><span>平均耗时</span><b>${esc(fmtDuration(t.avg_ms))}</b></div>
    </div>`;

  const daily = d.daily || [];
  const max = daily.reduce((m, x) => Math.max(m, Number(x.total) || 0), 0);
  const dense = daily.length > 60;
  const bars = daily.map((x) => {
    const segs = [
      ['ok', x.ok], ['failed', x.failed], ['skipped', x.skipped], ['open', x.open],
    ].map(([cls, n]) => (Number(n) > 0 ? `<i class="seg ${cls}" style="height:${pct(n, max)}%"></i>` : ''))
      .join('');
    const tip = `${x.d} 共 ${x.total || 0} · 成功 ${x.ok || 0} · 失败 ${x.failed || 0}`
      + ` · 跳过 ${x.skipped || 0} · 待处理 ${x.open || 0}`;
    return `<div class="bcol" title="${esc(tip)}">${segs}</div>`;
  }).join('');
  const chart = `
    <div class="card chart-card">
      <div class="chart-head"><h3>每日任务</h3><span>峰值 ${max}</span></div>
      <div class="bars${dense ? ' dense' : ''}">${bars || '<div class="empty">窗口内没有任务</div>'}</div>
      <div class="bars-x"><span>${esc((daily[0] || {}).d || '')}</span>
        <span>${esc((daily[daily.length - 1] || {}).d || '')}</span></div>
      <div class="legend">
        <span><i class="ok"></i>成功</span><span><i class="failed"></i>失败</span>
        <span><i class="skipped"></i>跳过</span><span><i class="open"></i>未完成</span>
      </div>
    </div>`;

  const sites = d.by_site || [];
  const siteMax = sites.reduce((m, x) => Math.max(m, Number(x.total) || 0), 0);
  const siteRows = sites.map((s) => `
    <div class="rank-row">
      <div class="rank-top">
        <span class="rank-label">${esc(s.label || s.site_id || '未知站点')}</span>
        <span class="rank-num">${s.total || 0} · 成功 ${s.ok || 0}${s.failed ? ' · 失败 ' + s.failed : ''}</span>
      </div>
      <div class="rank-bar"><i style="width:${pct(s.total, siteMax)}%"></i></div>
    </div>`).join('');

  const fails = d.failures || [];
  const failRows = fails.map((f) => `
    <div class="fail-row"><span class="why">${esc(f.reason || '未知原因')}</span>
      <span class="n">×${f.n || 0}</span></div>`).join('');

  const sourceChips = (d.by_source || []).map((s) =>
    `<span class="mini-chip">${esc(SOURCE_LABEL[s.source] || s.source || '未知')}`
    + ` <b>${s.total || 0}</b></span>`).join('');
  const catChips = (d.by_category || []).slice(0, 8).map((c) =>
    `<span class="mini-chip">${esc(c.category || '未分类')} <b>${c.total || 0}</b></span>`).join('');

  body.innerHTML = totals + chart
    + `<div class="card"><div class="chart-head"><h3>站点分布</h3><span>${sites.length} 个</span></div>
        <div class="rank">${siteRows || '<p class="hint">暂无数据</p>'}</div></div>`
    + `<div class="card"><div class="chart-head"><h3>失败原因 Top</h3><span>${fails.length} 类</span></div>
        ${failRows || '<p class="hint">窗口内没有失败任务</p>'}</div>`
    + `<div class="card"><div class="chart-head"><h3>来源 / 分类</h3></div>
        <div class="mini-chips">${sourceChips || '<span class="hint">暂无数据</span>'}</div>
        ${catChips ? `<div class="mini-chips" style="margin-top:8px">${catChips}</div>` : ''}</div>`;
}

/* ---------------------------------------------------------------- 扫描 / 分类 / 手动提交 */

function ensureSites() {
  if (state.sites.length) return Promise.resolve(state.sites);
  return ensureSiteList().then(() => state.sites);
}

function ensureSiteList() {
  if (state.sitesAll.length) return Promise.resolve(state.sitesAll);
  return api('/api/sites').then((d) => {
    state.sitesAll = d.sites || [];
    state.sites = state.sitesAll.filter((s) => s.enabled);
    return state.sitesAll;
  });
}

async function openScanSheet() {
  let sites = [];
  try { sites = await ensureSites(); } catch (e) { toast(e.message, 'err'); return; }
  const picked = new Set(sites.map((s) => s.id));
  const days = localStorage.getItem('subflow.scanDays') || '30';
  const body = `
    <div class="sheet-note" style="margin-bottom:12px">扫描下载器种子库，反查站点详情页并创建字幕任务。</div>
    <div class="form-row">
      <label class="field"><span>时间窗口（天，0 = 不限）</span>
        <input id="scan-days" type="number" min="0" step="1" value="${esc(days)}"></label>
    </div>
    <div class="group" style="margin-top:14px">
      <div class="group-title"><span>参与站点</span><button class="link" id="scan-all">全选</button></div>
      <div class="chips" id="scan-sites"></div>
    </div>
    <label class="switch-row"><input type="checkbox" id="scan-cookie"><span>强制刷新 CookieCloud 登录态</span></label>
    <p class="hint" id="scan-hint">站点全不选表示全部启用站点。</p>`;
  const root = openSheet({
    title: '开始扫描',
    body,
    actions: [
      { label: '取消', kind: 'ghost' },
      { label: '开始扫描', kind: 'primary', onClick: async (btn) => {
        const n = Number($('scan-days').value);
        if (!Number.isFinite(n) || n < 0) { $('scan-hint').textContent = '时间窗口必须 ≥ 0'; $('scan-hint').className = 'hint err'; return false; }
        const ids = [...picked];
        btn.textContent = '提交中…';
        try {
          const res = await api('/api/scan', {
            method: 'POST',
            body: {
              days: n,
              site_ids: ids.length && ids.length < sites.length ? ids : null,
              refresh_cookie: $('scan-cookie').checked,
            },
          });
          localStorage.setItem('subflow.scanDays', String(n));
          toast(res.message || '扫描已启动', 'ok');
          setView('home');
          loadOverview(true);
        } catch (e) { $('scan-hint').textContent = e.message; $('scan-hint').className = 'hint err'; return false; }
      } },
    ],
  });
  const box = root.querySelector('#scan-sites');
  const render = () => {
    box.innerHTML = sites.map((s) => `<button class="chip ${picked.has(s.id) ? 'on' : ''}" data-site="${esc(s.id)}">${esc(s.label)}</button>`).join('')
      || '<span class="muted">没有启用的站点</span>';
  };
  render();
  box.onclick = (ev) => {
    const chip = ev.target.closest('[data-site]');
    if (!chip) return;
    const id = chip.dataset.site;
    if (picked.has(id)) picked.delete(id); else picked.add(id);
    render();
  };
  root.querySelector('#scan-all').onclick = () => {
    if (picked.size === sites.length) picked.clear();
    else sites.forEach((s) => picked.add(s.id));
    render();
  };
}

async function openCategorySheet() {
  state.cats = { data: null, picked: new Set(), loading: false };
  const root = openSheet({
    title: '按分类批量下载',
    body: `
      <div class="sheet-note" style="margin-bottom:10px">
        对所选分类做<b>全量</b>处理，忽略时间窗口。种子多时耗时会明显变长。
      </div>
      <div class="cat-head" id="cat-stat">读取上次分类快照…</div>
      <div id="cat-body"><div class="empty">加载中…</div></div>`,
    actions: [
      { label: '取消', kind: 'ghost' },
      { label: '全量下载', kind: 'primary', onClick: async (btn) => {
        const keys = [...state.cats.picked];
        if (!keys.length) { toast('请先选择分类', 'err'); return false; }
        btn.textContent = '提交中…';
        try {
          const res = await api('/api/scan', { method: 'POST', body: { categories: keys, days: 0 } });
          toast(res.message || '已启动', 'ok');
          setView('home');
          loadOverview(true);
        } catch (e) { toast(e.message, 'err'); btn.textContent = '全量下载'; return false; }
      } },
    ],
  });

  const stat = root.querySelector('#cat-stat');
  const body = root.querySelector('#cat-body');

  const render = (data) => {
    state.cats.data = data;
    const valid = new Set();
    (data.downloaders || []).forEach((g) => (g.categories || []).forEach((c) => valid.add(`${g.id}:${c.name || ''}`)));
    state.cats.picked = new Set([...state.cats.picked].filter((k) => valid.has(k)));
    const groups = (data.downloaders || []).filter((g) => (g.categories || []).length);
    const total = valid.size;
    const stale = (data.downloaders || []).some((g) => g.cached);
    stat.textContent = `${total} 个分类 · 已选 ${state.cats.picked.size}`
      + (data.saved_at ? ` · 快照 ${String(data.saved_at).slice(5, 16)}` : '')
      + (stale ? ' · 含缓存' : '');
    if (!groups.length) {
      body.innerHTML = '<div class="empty">' + esc((data.errors || []).join('；') || '下载器里没有可用分类，可直接扫描或手动提交') + '</div>';
      return;
    }
    body.innerHTML = groups.map((g) => `
      <div class="cat-group">
        <div class="cat-head">${esc(g.name)} · ${esc(g.type)} · ${g.total || 0} 个种子${g.cached ? '（缓存）' : ''}</div>
        <div class="chips">${(g.categories || []).map((c) => {
          const key = `${g.id}:${c.name || ''}`;
          return `<button class="chip ${state.cats.picked.has(key) ? 'on' : ''}" data-cat="${esc(key)}">
            ${esc(c.label || c.name || '未分类')}<span class="n">${c.count || 0}</span></button>`;
        }).join('')}</div>
      </div>`).join('')
      + ((data.errors || []).length ? `<p class="hint err">${esc(data.errors.join('；'))}</p>` : '');
    body.querySelectorAll('[data-cat]').forEach((el) => {
      el.onclick = () => {
        const k = el.dataset.cat;
        if (state.cats.picked.has(k)) state.cats.picked.delete(k);
        else state.cats.picked.add(k);
        el.classList.toggle('on');
        stat.textContent = `${total} 个分类 · 已选 ${state.cats.picked.size}`
          + (data.saved_at ? ` · 快照 ${String(data.saved_at).slice(5, 16)}` : '');
      };
    });
  };

  try {
    const cached = await api('/api/categories/cached');
    if ((cached.downloaders || []).length) render(cached);
  } catch (e) { /* 没缓存就等实时读取 */ }

  stat.textContent = '正在读取分类（会完整拉取一次种子列表）…';
  try {
    const fresh = await api('/api/categories', { timeout: 180000 });
    render(fresh);
  } catch (e) {
    if (!state.cats.data) body.innerHTML = '<div class="empty err">' + esc(e.message) + '</div>';
    else stat.textContent += ' · 刷新失败，显示缓存';
    toast(e.message, 'err');
  }
}

async function openSubmitSheet() {
  let sites = [];
  try { sites = await ensureSites(); } catch (e) { toast(e.message, 'err'); return; }
  const opts = sites.map((s) => `<option value="${esc(s.id)}">${esc(s.label)}</option>`).join('');
  openSheet({
    title: '手动提交',
    body: `
      <label class="field"><span>站点</span><select id="sub-site">${opts}</select></label>
      <label class="field" style="margin-top:10px"><span>种子 ID / 详情页 / 字幕页 URL</span>
        <textarea id="sub-target" placeholder="例如 12345 或 https://kamept.com/details.php?id=12345"></textarea></label>
      <label class="field" style="margin-top:10px"><span>保存目录（可选）</span>
        <input id="sub-dir" type="text" placeholder="留空使用下载器里的保存路径"></label>
      <p class="hint" id="sub-hint">提交后会立即入队执行。</p>`,
    actions: [
      { label: '取消', kind: 'ghost' },
      { label: '提交', kind: 'primary', onClick: async (btn) => {
        const site = $('sub-site').value;
        const target = $('sub-target').value.trim();
        const dir = $('sub-dir').value.trim();
        if (!target) { $('sub-hint').textContent = '请填写种子 ID 或 URL'; $('sub-hint').className = 'hint err'; return false; }
        btn.textContent = '提交中…';
        try {
          const res = await api('/api/submit', {
            method: 'POST',
            body: { site_id: site, target, save_dir: dir || null, auto_run: true },
          });
          toast(res.message || '已提交', 'ok');
          setView('jobs');
          loadJobs(true);
          loadOverview(true);
        } catch (e) { $('sub-hint').textContent = e.message; $('sub-hint').className = 'hint err'; return false; }
      } },
    ],
  });
}

async function retryFailed() {
  const st = (state.overview && state.overview.stats) || {};
  const yes = await confirmSheet('重试失败任务', `将把 ${st.failed || 0} 个失败任务重新入队（最多 10000 条）。`, '重试');
  if (!yes) return;
  try {
    const d = await api('/api/jobs/retry-failed', { method: 'POST' });
    toast(d.message || `已入队 ${d.count || 0} 个`, 'ok');
    loadOverview(true);
    if (state.view === 'jobs') loadJobs(true);
  } catch (e) { toast(e.message, 'err'); }
}

async function runPending() {
  try {
    const d = await api('/api/jobs/run-pending', { method: 'POST' });
    toast(d.message || `已入队 ${d.count || 0} 个`, 'ok');
    loadOverview(true);
    if (state.view === 'jobs') loadJobs(true);
  } catch (e) { toast(e.message, 'err'); }
}

/* ---------------------------------------------------------------- 缺字幕 */

function missKey(it, idx) { return String(idx); }

function missRepairable(it) {
  return !!(it && it.site_id && it.torrent_id);
}

/* 「只看新上」过滤：返回 [{it, idx}]，idx 始终是 state.missing.items 里的原始下标，
   这样勾选集合 picked 与过滤开关无关，切换过滤不会丢已选。 */
function missVisible() {
  const items = state.missing.items;
  const newOnly = !!(state.missing.probe && state.missing.probe.newOnly);
  const out = [];
  items.forEach((it, idx) => {
    if (newOnly && !(it && it.site_check && it.site_check.state === 'new')) return;
    out.push({ it, idx });
  });
  return out;
}

/* 站点复查状态 → 标签文案与样式（只用现有 .miss-tag / .miss-tag.no，
   失败态用内联色，避免为此新增样式表规则） */
const MISS_SITE_TAGS = {
  new: { label: '站点新上', cls: '', style: '' },
  avail: { label: '可补', cls: '', style: '' },
  known: { label: '已有', cls: 'no', style: '' },
  none: { label: '站点暂无', cls: 'no', style: '' },
  error: {
    label: '复查失败', cls: 'no',
    style: 'color:var(--err);background:rgba(248,81,73,.08);border-color:rgba(248,81,73,.3)',
  },
};

function missSiteTag(it) {
  const sc = it && it.site_check;
  if (!sc || !sc.state) {
    return '<span class="miss-tag no">未复查</span>';
  }
  const meta = MISS_SITE_TAGS[sc.state] || { label: '未复查', cls: 'no', style: '' };
  let n = 0;
  if (sc.state === 'new') n = Number(sc.new_count || 0) || 0;
  else if (sc.state === 'avail' || sc.state === 'known') n = Number(sc.sub_count || 0) || 0;
  const text = n > 0 ? `${meta.label} ${n}` : meta.label;
  const title = sc.message || (sc.checked_at ? '复查于 ' + fmtTime(sc.checked_at) : '');
  return `<span class="miss-tag ${meta.cls}"${meta.style ? ` style="${meta.style}"` : ''}`
    + `${title ? ` title="${esc(title)}"` : ''}>${esc(text)}</span>`;
}

function missProbeCounts(items) {
  const c = { new: 0, avail: 0, none: 0, known: 0, error: 0, unchecked: 0 };
  (items || []).forEach((it) => {
    const st = it && it.site_check && it.site_check.state;
    if (st && c[st] != null) c[st] += 1; else c.unchecked += 1;
  });
  return c;
}

async function runMissingScan() {
  if (state.missing.loading) return;
  // 重新检查会生成不带 site_check 的新清单，旧复查状态作废
  stopMissProbePoll();
  setMissProbeRunning(false);
  setMissProbeStat('');
  // 新清单没有 site_check，若仍开着「只看新上」会得到空列表，这里复位开关
  if (state.missing.probe.newOnly) {
    state.missing.probe.newOnly = false;
    const cbNew = $('miss-new-only');
    if (cbNew) cbNew.checked = false;
  }
  state.missing.loading = true;
  state.missing.picked = new Set();
  const btn = $('miss-run');
  btn.disabled = true;
  btn.textContent = '检查中…';
  $('miss-stat').className = 'hint';
  $('miss-stat').textContent = '正在扫描本地视频目录…';
  $('miss-list').innerHTML = '<div class="empty">检查中，目录较大时需要一会儿…</div>';
  try {
    const q = new URLSearchParams({
      days: $('miss-days').value || '30',
      limit: '200',
      keyword: $('miss-keyword').value.trim(),
      only_site_matched: $('miss-matched').checked ? 'true' : 'false',
    });
    const d = await api('/api/missing?' + q.toString(), { timeout: 180000 });
    state.missing.items = d.items || [];
    state.missing.picked = new Set();
    $('miss-stat').textContent =
      `检查 ${d.scanned_torrents || 0} 个种子 / ${d.checked_videos || 0} 个视频 · `
      + `缺字幕视频 ${d.missing_videos || 0} 个 · 列出 ${state.missing.items.length} 个种子`
      + (d.skipped_dirs ? ` · 目录缺失 ${d.skipped_dirs}` : '')
      + (d.truncated ? ' · 已截断' : '') + ` · 耗时 ${fmtDuration(d.elapsed_ms)}`;
    state.missing.lastStat = $('miss-stat').textContent;
    if ((d.errors || []).length) {
      $('miss-stat').className = 'hint err';
      $('miss-stat').textContent += ' · ' + d.errors.join('；');
    }
    renderMissing();
  } catch (e) {
    state.missing.lastStat = '';
    $('miss-stat').className = 'hint err';
    $('miss-stat').textContent = e.message;
    $('miss-list').innerHTML = '<div class="empty err">' + esc(e.message) + '</div>';
    state.missing.items = [];
    renderMissing();
    toast(e.message, 'err');
  } finally {
    state.missing.loading = false;
    btn.disabled = false;
    btn.textContent = '检查';
  }
}

function renderMissing() {
  const items = state.missing.items;
  const visible = missVisible();
  const box = $('miss-list');
  const bar = $('miss-bar');
  const repairable = visible.filter((r) => missRepairable(r.it)).length;
  const newOnly = !!state.missing.probe.newOnly;
  $('miss-count').textContent = visible.length
    ? `${visible.length} 项 · 可补 ${repairable}`
      + (newOnly ? ` · 只看新上（共 ${items.length}）` : '')
    : (items.length ? `0 项符合「只看新上」（共 ${items.length}）` : '');
  bar.hidden = !visible.length;
  updateMissBar();
  if (!visible.length) {
    box.innerHTML = items.length
      ? '<div class="empty">没有「新上字幕」的条目，可关闭「只看新上」查看全部</div>'
      : '<div class="empty">没有发现缺字幕的视频</div>';
    return;
  }
  box.innerHTML = visible.map(({ it, idx }) => {
    const ok = missRepairable(it);
    const picked = state.missing.picked.has(idx);
    const files = (it.missing || []).slice(0, 3).map((m) =>
      `<div>· ${esc(m.file)}</div>`).join('');
    const more = (it.missing_count || 0) > 3 ? `<div>… 共 ${it.missing_count} 个</div>` : '';
    return `<article class="miss ${ok ? '' : 'disabled'}" data-idx="${idx}">
      <label class="pick"><input type="checkbox" data-pick="${idx}" ${picked ? 'checked' : ''} ${ok ? '' : 'disabled'}></label>
      <div class="miss-main">
        <p class="miss-title">${esc(it.name || '未命名种子')}</p>
        <p class="miss-meta">${esc(it.site_label || it.site_id || '无法反查站点')} · 缺 ${it.missing_count || 0} 个视频${it.category ? ' · ' + esc(it.category) : ''}</p>
        <div class="miss-files">${files}${more}</div>
        <span class="miss-tag ${ok ? '' : 'no'}">${ok ? '可补字幕' : '需手动提交'}</span> ${missSiteTag(it)}
      </div>
    </article>`;
  }).join('');
}

function updateMissBar() {
  const items = state.missing.items;
  const visible = missVisible();
  const repairable = visible.filter((r) => missRepairable(r.it));
  // 只统计可见项，保证「已选 N」「全选」「补字幕」三者口径一致
  const visibleIdx = new Set(visible.map((r) => r.idx));
  const picked = [...state.missing.picked]
    .filter((i) => visibleIdx.has(i) && missRepairable(items[i]));
  $('miss-picked').textContent = '已选 ' + picked.length;
  $('miss-repair').disabled = !picked.length;
  const all = repairable.length > 0 && picked.length === repairable.length;
  $('miss-all').checked = all;
}

async function repairMissing() {
  const items = state.missing.items;
  // 与「已选 N」计数口径一致：只提交当前可见的已选条目
  const visibleIdx = new Set(missVisible().map((r) => r.idx));
  const picked = [...state.missing.picked]
    .filter((i) => visibleIdx.has(i))
    .map((i) => items[i]).filter(missRepairable);
  if (!picked.length) { toast('请先勾选可补字幕的条目', 'err'); return; }
  const btn = $('miss-repair');
  btn.disabled = true;
  try {
    const d = await api('/api/missing/repair', {
      method: 'POST',
      body: {
        items: picked.map((it) => ({
          site_id: it.site_id, torrent_id: it.torrent_id,
          save_dir: it.save_path || '', name: it.name || '',
        })),
        auto_run: true,
      },
      timeout: 60000,
    });
    toast(`新建任务 ${d.created || 0} 个，入队 ${d.queued || 0} 个`
      + ((d.errors || []).length ? `，${d.errors.length} 条失败` : ''), 'ok');
    if ((d.errors || []).length) $('miss-stat').textContent = d.errors.join('；');
    state.missing.picked = new Set();
    renderMissing();
    loadOverview(true);
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

/* ---------------------------------------------------------------- 站点复查 */

const MISS_PROBE_POLL_MS = 1500;
/* 50 个种子的复查通常 <2 分钟；给足余量后放弃轮询，避免定时器无限跑 */
const MISS_PROBE_MAX_POLLS = 200;

function missProbeBtn() { return $('miss-probe-run'); }

function setMissProbeStat(text, kind) {
  const el = $('miss-probe-stat');
  if (!el) return;
  if (!text) { el.hidden = true; el.textContent = ''; el.className = 'hint'; return; }
  el.hidden = false;
  el.className = 'hint' + (kind ? ' ' + kind : '');
  el.textContent = text;
}

function setMissProbeRunning(on) {
  const P = state.missing.probe;
  P.running = !!on;
  if (!on) P.polls = 0;
  const btn = missProbeBtn();
  if (btn) {
    btn.disabled = !!on;
    btn.textContent = on ? '复查中…' : '复查站点';
  }
}

/* 停轮询：完成/失败/离开视图/重进时都要清掉，避免句柄泄漏与重复定时器 */
function stopMissProbePoll() {
  const P = state.missing.probe;
  if (P.timer) { clearTimeout(P.timer); P.timer = null; }
}

/* 页面卸载时兜底清理，避免定时器在视图切换后仍持有闭包 */
function abortMissProbe(reason) {
  const P = state.missing.probe;
  stopMissProbePoll();
  if (P.running) {
    setMissProbeRunning(false);
    if (reason) setMissProbeStat(reason, 'err');
  }
}

function missProbeSummaryText(res) {
  const s = (res && res.summary) || {};
  return `复查 ${s.probed || 0} 个种子 · 站点新上 ${s.new_on_site || 0}`
    + ` · 可补 ${s.available || 0} · 已有 ${s.known || 0} · 暂无 ${s.none || 0}`
    + (s.errors ? ` · 失败 ${s.errors}` : '')
    + (res && res.probe_truncated ? ' · 已达复查上限' : '')
    + (res ? ` · 耗时 ${fmtDuration(res.elapsed_ms)}` : '');
}

async function runMissProbe() {
  const P = state.missing.probe;
  if (P.running) return;
  if (!state.missing.items.length) {
    toast('请先点「检查」生成缺失清单', 'err');
    return;
  }
  P.running = true;
  setMissProbeRunning(true);
  setMissProbeStat('正在提交站点复查…');
  try {
    const d = await api('/api/missing/probe', {
      method: 'POST',
      body: {
        days: Number($('miss-days').value || 30),
        downloader: '',
        category: '',
        keyword: $('miss-keyword').value.trim(),
        limit: 200,
        only_site_matched: !!$('miss-matched').checked,
        probe_limit: P.probeLimit,
        refresh_cookie: false,
      },
      timeout: 60000,
    });
    setMissProbeStat(d.message || '复查任务已启动…');
    scheduleMissProbePoll(0);
  } catch (e) {
    // 后端可能因「已有复查在跑」而拒绝启动；此时改为接管现有任务继续轮询
    let attached = false;
    try {
      const st = await api('/api/missing/probe/state', { timeout: 15000 });
      if (st && st.running) {
        attached = true;
        setMissProbeStat((st.phase || '已有复查在进行') + '…');
        scheduleMissProbePoll(MISS_PROBE_POLL_MS);
      }
    } catch (e2) { /* 读状态也失败，按启动失败处理 */ }
    if (!attached) {
      stopMissProbePoll();
      setMissProbeRunning(false);
      setMissProbeStat(e.message, 'err');
      toast(e.message, 'err');
    }
  }
}

function scheduleMissProbePoll(delay) {
  const P = state.missing.probe;
  stopMissProbePoll();
  P.timer = setTimeout(pollMissProbe, delay == null ? MISS_PROBE_POLL_MS : delay);
}

async function pollMissProbe() {
  const P = state.missing.probe;
  P.timer = null;
  if (!P.running) return;
  if (P.polls >= MISS_PROBE_MAX_POLLS) {
    // 超时放弃：清定时器、恢复按钮，但不覆盖已拿到的清单
    stopMissProbePoll();
    setMissProbeRunning(false);
    setMissProbeStat('复查耗时过长，已停止等待；可稍后重试或查看日志', 'err');
    toast('复查等待超时', 'err');
    return;
  }
  P.polls += 1;
  let d;
  try {
    d = await api('/api/missing/probe/state', { timeout: 30000 });
  } catch (e) {
    stopMissProbePoll();
    setMissProbeRunning(false);
    setMissProbeStat('复查状态读取失败：' + e.message, 'err');
    toast(e.message, 'err');
    return;
  }
  if (d && d.running) {
    const total = Number(d.total || 0);
    const prog = Number(d.progress || 0);
    setMissProbeStat((d.phase || '复查中') + (total ? ` ${prog}/${total}` : ''));
    // 仍在运行：继续轮询（句柄已置空，这里重建）
    scheduleMissProbePoll();
    return;
  }
  // 已结束：无论成败都先停轮询并恢复按钮
  stopMissProbePoll();
  setMissProbeRunning(false);
  const res = d && d.result;
  if (!res || res.ok === false) {
    const msg = (res && (res.error || res.message)) || '站点复查失败';
    setMissProbeStat(msg, 'err');
    toast(msg, 'err');
    return;
  }
  applyMissProbeResult(res);
}

function applyMissProbeResult(res) {
  if (Array.isArray(res.items)) state.missing.items = res.items;
  state.missing.picked = new Set();
  const s = res.summary || {};
  const counts = missProbeCounts(state.missing.items);
  setMissProbeStat(missProbeSummaryText(res)
    + (counts.unchecked ? ` · 未复查 ${counts.unchecked}` : '')
    + ((res.errors || []).length ? ' · ' + res.errors.slice(0, 2).join('；') : ''), 'ok');
  // #miss-stat 保留原「检查」结论，复查统计追加在后面
  const stat = $('miss-stat');
  if (stat) {
    stat.className = 'hint';
    const base = state.missing.lastStat || '缺字幕清单';
    stat.textContent = base + ' · 站点复查：'
      + `新上 ${s.new_on_site != null ? s.new_on_site : counts.new}`
      + ` · 可补 ${s.available != null ? s.available : counts.avail}`
      + ` · 暂无 ${s.none != null ? s.none : counts.none}`
      + ` · 已有 ${s.known != null ? s.known : counts.known}`
      + (counts.error ? ` · 失败 ${counts.error}` : '');
  }
  renderMissing();
  toast(`复查完成：新上 ${s.new_on_site != null ? s.new_on_site : counts.new} 个`, 'ok');
}


function zoneKey(e) {
  return `${e.site_id || ''}:${e.torrent_id || ''}:${e.subid || ''}`;
}

function zoneSites() {
  return state.sitesAll.filter((s) => s.enabled && s.supports_subtitle_zone);
}

function renderZoneSites() {
  const sel = $('zone-site');
  if (!sel) return;
  const list = zoneSites();
  sel.innerHTML = '<option value="all">全部站点</option>'
    + list.map((s) => `<option value="${esc(s.id)}">${esc(s.label)}</option>`).join('');
  if (state.zone.site !== 'all' && !list.some((s) => s.id === state.zone.site)) {
    state.zone.site = 'all';
  }
  sel.value = state.zone.site;
}

function visibleZoneEntries() {
  const Z = state.zone;
  return Z.onlyMatched ? Z.entries.filter((e) => e.matched) : Z.entries;
}

async function loadZone(keepPicked) {
  const Z = state.zone;
  if (Z.loading) return;
  try {
    await ensureSiteList();
  } catch (e) {
    $('zone-list').innerHTML = '<div class="empty err">' + esc(e.message) + '</div>';
    return;
  }
  renderZoneSites();
  if (!zoneSites().length) {
    $('zone-stat').textContent = '没有启用且支持字幕区的站点，请先在服务端配置站点。';
    $('zone-list').innerHTML = '<div class="empty">当前站点都不支持字幕区</div>';
    return;
  }

  Z.site = $('zone-site').value || Z.site;
  Z.pages = Number($('zone-pages').value || 1);
  Z.keyword = $('zone-keyword').value.trim();

  Z.loading = true;
  $('zone-stat').className = 'hint';
  $('zone-stat').textContent = '正在读取字幕区…';
  $('zone-list').innerHTML = skeletonJobs(4);
  try {
    const d = await api('/api/zone/list', {
      method: 'POST',
      body: { site_id: Z.site, pages: Z.pages, keyword: Z.keyword, refresh_cookie: false },
      timeout: 180000,
    });
    Z.entries = d.entries || [];
    Z.sites = d.sites || {};
    const valid = new Set(Z.entries.map(zoneKey));
    Z.picked = keepPicked ? new Set([...Z.picked].filter((k) => valid.has(k))) : new Set();
    const siteStat = Object.values(Z.sites);
    const matched = siteStat.reduce((n, s) => n + (Number(s.matched) || 0), 0);
    const fetched = siteStat.reduce((n, s) => n + (Number(s.fetched) || 0), 0);
    $('zone-stat').textContent = `${Z.entries.length} 条 · 本地命中 ${matched} · 拉取 ${fetched} 条 · `
      + `${siteStat.length} 个站点 · 耗时 ${fmtDuration(d.elapsed_ms)}`;
    if ((d.errors || []).length) {
      $('zone-stat').className = 'hint err';
      $('zone-stat').textContent += ' · ' + d.errors.join('；');
    }
    renderZone();
  } catch (e) {
    $('zone-stat').className = 'hint err';
    $('zone-stat').textContent = e.message;
    $('zone-list').innerHTML = '<div class="empty err">' + esc(e.message) + '</div>';
  } finally {
    Z.loading = false;
  }
}

function renderZone() {
  const Z = state.zone;
  const box = $('zone-list');
  const rows = visibleZoneEntries();
  const bar = $('zone-bar');
  const section = document.querySelector('.view[data-view="zone"]');
  const showBar = Z.entries.length > 0;
  bar.hidden = !showBar;
  section.classList.toggle('has-bar', showBar);
  if (!rows.length) {
    box.innerHTML = `<div class="empty">${Z.entries.length ? '没有命中本地的条目' : '字幕区没有返回条目'}</div>`;
    updateZoneBar();
    return;
  }
  box.innerHTML = rows.map((e) => {
    const key = zoneKey(e);
    const picked = Z.picked.has(key);
    const local = e.local || {};
    const jobTag = e.job_status
      ? `<span class="tag info">任务 #${esc(e.job_id)} · ${esc(e.job_status)}</span>` : '';
    return `<article class="zone-row${picked ? ' picked' : ''}" data-zkey="${esc(key)}">
      <label class="pick"><input type="checkbox" data-zpick="${esc(key)}" ${picked ? 'checked' : ''}></label>
      <div class="zone-main">
        <p class="zone-title">${esc(e.title || '未命名字幕')}</p>
        <p class="zone-meta">${esc(e.site_label || e.site_id || '')} · 种子 ${esc(e.torrent_id || '')}`
          + `${e.lang ? ' · ' + esc(e.lang) : ''}${e.size ? ' · ' + esc(e.size) : ''}`
          + `${e.added ? ' · ' + esc(e.added) : ''}</p>
        <div class="zone-tags">
          ${e.matched ? '<span class="tag ok">本地命中</span>' : '<span class="tag">无本地种子</span>'}
          ${local.name ? `<span class="tag">${esc(local.name)}</span>` : ''}
          ${jobTag}
        </div>
      </div>
    </article>`;
  }).join('');
  updateZoneBar();
}

function updateZoneBar() {
  const Z = state.zone;
  const rows = visibleZoneEntries();
  const bar = $('zone-bar');
  if (!bar) return;
  const picked = rows.filter((e) => Z.picked.has(zoneKey(e))).length;
  $('zone-picked').textContent = '已选 ' + picked;
  $('zone-submit').disabled = picked === 0;
  $('zone-all').checked = rows.length > 0 && picked === rows.length;
}

function toggleZonePick(key, on) {
  const Z = state.zone;
  if (on) Z.picked.add(key); else Z.picked.delete(key);
  const row = $('zone-list').querySelector(`[data-zkey="${key.replace(/"/g, '\\"')}"]`);
  if (row) row.classList.toggle('picked', on);
  updateZoneBar();
}

function submitZone() {
  const Z = state.zone;
  const items = Z.entries.filter((e) => Z.picked.has(zoneKey(e)));
  if (!items.length) { toast('请先选择字幕', 'err'); return; }
  const groups = new Map();
  items.forEach((e) => {
    const sid = e.site_id || '';
    if (!groups.has(sid)) groups.set(sid, []);
    groups.get(sid).push({ torrent_id: e.torrent_id, subid: e.subid, title: e.title || '' });
  });
  openSheet({
    title: '下载字幕',
    body: `
      <p class="sheet-note">已选 ${items.length} 条字幕，来自 ${groups.size} 个站点。
        能对上本地种子的会落盘到视频目录，其余的用下面的目录。</p>
      <label class="field" style="margin-top:12px"><span>保存目录（可选）</span>
        <input id="zone-dir" type="text" placeholder="留空则用下载器的保存路径"></label>
      <p class="hint" id="zone-hint">提交后按站点分批入队执行。</p>`,
    actions: [
      { label: '取消', kind: 'ghost' },
      { label: '提交', kind: 'primary', onClick: async (btn) => {
        const dir = $('zone-dir').value.trim();
        btn.textContent = '提交中…';
        let created = 0;
        let queued = 0;
        const errors = [];
        for (const [siteId, list] of groups) {
          try {
            const d = await api('/api/zone/submit', {
              method: 'POST',
              body: { site_id: siteId, items: list, save_dir: dir || null, auto_run: true },
              timeout: 60000,
            });
            created += Number(d.created) || 0;
            queued += Number(d.queued) || 0;
            errors.push(...(d.errors || []));
          } catch (e) {
            errors.push(e.message);
          }
        }
        if (!created && !queued && errors.length) {
          $('zone-hint').textContent = errors.join('；');
          $('zone-hint').className = 'hint err';
          btn.textContent = '提交';
          return false;
        }
        toast(`新建 ${created} 个，入队 ${queued} 个`
          + (errors.length ? `，${errors.length} 条失败` : ''), 'ok');
        Z.picked.clear();
        renderZone();
        loadOverview(true);
        if (created) setView('jobs');
      } },
    ],
  });
}

/* ---------------------------------------------------------------- 站点与 Cookie */

async function loadSites() {
  const S = state.sitesView;
  if (S.loading) return;
  S.loading = true;
  $('cc-box').innerHTML = '<div class="sk sk-pad"><div class="sk-line w45"></div><div class="sk-line w90"></div></div>';
  $('site-list').innerHTML = skeletonJobs(3);
  try {
    const d = await api('/api/sites');
    S.data = d;
    state.sitesAll = d.sites || [];
    state.sites = state.sitesAll.filter((s) => s.enabled);
    renderSites(d);
  } catch (e) {
    $('cc-box').innerHTML = '<div class="empty err">' + esc(e.message) + '</div>';
    $('site-list').innerHTML = '<div class="empty err">' + esc(e.message) + '</div>';
  } finally {
    S.loading = false;
  }
}

function renderSites(d) {
  const meta = d.meta || {};
  const domains = d.cookiecloud_domains || [];
  const ok = d.cookiecloud_ok !== false;
  $('cc-box').innerHTML = `
    <div class="cc-top">
      <span class="dot ${ok ? 'ok' : 'err'}"></span>
      <h3>CookieCloud</h3>
      <button class="btn sm" id="cc-refresh">刷新</button>
    </div>
    <div class="cc-meta">
      <span class="mini-chip">域名 <b>${meta.domains != null ? meta.domains : domains.length}</b></span>
      <span class="mini-chip">Cookie <b>${meta.cookies || 0}</b></span>
      <span class="mini-chip">拉取 <b>${esc(meta.fetched_at || '-')}</b></span>
      ${meta.update_time ? `<span class="mini-chip">同步 <b>${esc(meta.update_time)}</b></span>` : ''}
    </div>
    <p class="hint ${ok ? '' : 'err'}">${esc(ok
      ? (domains.length ? domains.join('、') : '尚未拉到任何域名')
      : (d.cookiecloud_message || 'CookieCloud 不可用'))}</p>`;
  $('cc-refresh').onclick = refreshCookieCloud;

  const rows = (d.sites || []).map((s) => {
    const cookie = s.cookie_ok;
    return `<article class="site-row">
      <div class="site-top">
        <span class="dot ${cookie === true ? 'ok' : cookie === false ? 'err' : ''}"></span>
        <span class="site-name">${esc(s.label || s.id)}</span>
        <span class="site-dom">${esc(s.domain || '')}</span>
      </div>
      <div class="site-tags">
        ${s.enabled ? '' : '<span class="tag">已禁用</span>'}
        ${s.supports_scan ? '<span class="tag info">扫描</span>' : ''}
        ${s.supports_subtitle_zone ? '<span class="tag info">字幕区</span>' : ''}
        ${cookie === true ? '<span class="tag ok">Cookie 正常</span>'
          : cookie === false ? '<span class="tag warn">Cookie 异常</span>' : ''}
      </div>
      ${s.cookie_message
        ? `<p class="site-msg ${cookie === false ? 'err' : 'muted'}">${esc(s.cookie_message)}</p>` : ''}
    </article>`;
  }).join('');
  $('site-list').innerHTML = rows || '<div class="empty">没有配置站点</div>';
}

async function refreshCookieCloud() {
  const btn = $('cc-refresh');
  if (btn) { btn.disabled = true; btn.textContent = '刷新中…'; }
  try {
    const d = await api('/api/cookiecloud/refresh', { method: 'POST', timeout: 60000 });
    const m = d.meta || {};
    toast(`CookieCloud 已刷新：${m.domains || 0} 个域名 / ${m.cookies || 0} 条 Cookie`, 'ok');
    state.sites = [];
    await loadSites();
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '刷新'; }
  }
}

/* ---------------------------------------------------------------- 定时扫描 */

async function loadSchedule() {
  const S = state.sched;
  if (S.loading) return;
  S.loading = true;
  $('sched-body').innerHTML = '<div class="sk sk-pad"><div class="sk-line w45"></div>'
    + '<div class="sk-line" style="height:130px"></div></div>';
  try {
    const [s, c] = await Promise.all([api('/api/schedule'), api('/api/config')]);
    S.data = s;
    S.cfg = (c.config && c.config.schedule) || {};
    renderSchedule(s, S.cfg);
  } catch (e) {
    $('sched-body').innerHTML = '<div class="empty err">' + esc(e.message) + '</div>';
  } finally {
    S.loading = false;
  }
}

function renderSchedule(s, cfg) {
  const interval = s.interval_hours != null ? s.interval_hours : (cfg.interval_hours || 12);
  const last = s.last_result || null;
  const lastText = last
    ? `扫描 ${last.scanned || 0} · 命中 ${last.matched || 0} · 新建 ${last.jobs_created || 0}`
      + ` · ${fmtDuration(last.elapsed_ms)}${last.finished_at ? ' · ' + fmtTime(last.finished_at) : ''}`
    : '还没有执行记录';
  $('sched-body').innerHTML = `
    <div class="card">
      <label class="switch-row" style="width:100%;justify-content:space-between">
        <span>启用定时扫描</span>
        <input type="checkbox" id="sched-enabled" ${s.enabled ? 'checked' : ''}>
      </label>
      <div class="sched-grid" style="margin-top:10px">
        <label class="field"><span>间隔（小时）</span>
          <input id="sched-interval" type="number" min="0.25" step="0.5" value="${esc(interval)}"></label>
        <label class="field"><span>时间窗口（天）</span>
          <input id="sched-days" type="number" min="1" step="1" value="${esc(s.days || cfg.days || 3)}"></label>
        <label class="field"><span>单轮上限（0 = 不限）</span>
          <input id="sched-limit" type="number" min="0" step="1" value="${esc(s.limit || 0)}"></label>
        <label class="field"><span>Cookie</span>
          <label class="switch-row" style="min-height:40px">
            <input type="checkbox" id="sched-cookie" ${s.refresh_cookie || cfg.refresh_cookie ? 'checked' : ''}>
            <span>每轮强制刷新</span></label></label>
      </div>
      <div class="sched-actions">
        <button class="btn" id="sched-run">立即扫一轮</button>
        <button class="btn primary" id="sched-save">保存</button>
      </div>
      <p class="hint" id="sched-hint">保存后立即热加载，无需重启服务。</p>
    </div>
    <div class="card">
      <div class="kv-row"><span>上次执行</span><b>${esc(s.last_run || '从未')}</b></div>
      <div class="kv-row"><span>下次执行</span><b>${esc(s.next_run || (s.enabled ? '待计算' : '未启用'))}</b></div>
      <div class="kv-row"><span>调度线程</span><b>${s.thread_alive ? '运行中' : '未运行'}</b></div>
      <div class="kv-row"><span>最近结果</span><b>${esc(lastText)}</b></div>
    </div>`;
  $('sched-save').onclick = saveSchedule;
  $('sched-run').onclick = runScheduledScan;
}

function scheduleForm() {
  return {
    enabled: $('sched-enabled').checked,
    interval_hours: Number($('sched-interval').value) || 12,
    days: Number($('sched-days').value) || 3,
    limit: Number($('sched-limit').value) || 0,
    refresh_cookie: $('sched-cookie').checked,
  };
}

async function saveSchedule() {
  const cfg = scheduleForm();
  if (cfg.interval_hours < 0.25) {
    $('sched-hint').textContent = '间隔不能小于 0.25 小时';
    $('sched-hint').className = 'hint err';
    return;
  }
  const btn = $('sched-save');
  btn.disabled = true;
  btn.textContent = '保存中…';
  try {
    await api('/api/config', { method: 'POST', body: { patch: { schedule: cfg } } });
    toast('定时扫描已保存', 'ok');
    await loadSchedule();
  } catch (e) {
    $('sched-hint').textContent = e.message;
    $('sched-hint').className = 'hint err';
    btn.disabled = false;
    btn.textContent = '保存';
  }
}

async function runScheduledScan() {
  const cfg = scheduleForm();
  const btn = $('sched-run');
  btn.disabled = true;
  btn.textContent = '提交中…';
  try {
    const d = await api('/api/scan', {
      method: 'POST',
      body: { days: cfg.days, limit: cfg.limit || null, refresh_cookie: cfg.refresh_cookie },
    });
    toast(d.message || '扫描已启动', 'ok');
    setView('home');
    loadOverview(true);
  } catch (e) {
    toast(e.message, 'err');
    btn.disabled = false;
    btn.textContent = '立即扫一轮';
  }
}

/* ---------------------------------------------------------------- 日志 */

async function loadLogsTail() {
  const L = state.logs;
  if (L.loading) return;
  L.loading = true;
  $('log-list').innerHTML = '<div class="empty">加载中…</div>';
  try {
    const q = new URLSearchParams({ tail: 'true', level: L.level, limit: '200' });
    if (L.keyword) q.set('keyword', L.keyword);
    const d = await api('/api/logs?' + q.toString());
    L.rows = d.logs || [];
    L.since = Number(d.last_id) || 0;
    renderLogs(true);
  } catch (e) {
    $('log-list').innerHTML = '<div class="empty err">' + esc(e.message) + '</div>';
  } finally {
    L.loading = false;
  }
}

async function pollLogs() {
  const L = state.logs;
  if (L.loading) return;
  L.loading = true;
  try {
    if (!L.rows.length && !L.since) { L.loading = false; await loadLogsTail(); return; }
    const q = new URLSearchParams({
      since_id: String(L.since), level: L.level, limit: '200',
    });
    if (L.keyword) q.set('keyword', L.keyword);
    const d = await api('/api/logs?' + q.toString(), { timeout: 20000 });
    const rows = d.logs || [];
    if (rows.length) {
      L.rows = L.rows.concat(rows).slice(-800);
      L.since = Number(d.last_id) || L.since;
      renderLogs(false);
    }
  } catch (e) { /* 轮询失败静默 */ }
  finally { L.loading = false; }
}

function renderLogs(scrollBottom) {
  const box = $('log-list');
  const nearBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 48;
  const rows = state.logs.rows;
  box.innerHTML = rows.length
    ? rows.map((r) => `<div class="logline">
        <span class="t" title="${esc(r.ts)}">${esc(String(r.ts || '').slice(11))}</span>
        <span class="lv ${esc(r.level)}">${esc(r.level)}</span>
        <span class="m">${esc(r.message)}</span>
      </div>`).join('')
    : '<div class="empty">暂无日志</div>';
  if (scrollBottom || nearBottom) box.scrollTop = box.scrollHeight;
}

function renderLogChips() {
  $('log-levels').innerHTML = LOG_LEVELS.map((lv) =>
    `<button class="chip ${state.logs.level === lv ? 'on' : ''}" data-level="${lv}">${lv}</button>`).join('');
}

/* ---------------------------------------------------------------- 连接设置 */

function openSettings(message) {
  state.settingsOpen = true;
  const body = `
    ${message ? `<p class="hint err" style="margin:0 0 12px">${esc(message)}</p>` : ''}
    <label class="field"><span>服务器地址</span>
      <input id="set-base" type="text" inputmode="url" placeholder="http://192.168.2.180:8099" value="${esc(state.base)}"></label>
    <div class="form-row" style="margin-top:10px">
      <label class="field"><span>用户名（可留空）</span>
        <input id="set-user" type="text" autocomplete="username" value="${esc(state.user)}"></label>
      <label class="field"><span>密码</span>
        <input id="set-pass" type="password" autocomplete="current-password" value="${esc(state.pass)}"></label>
    </div>
    <label class="switch-row" style="margin-top:8px">
      <input type="checkbox" id="set-remember" ${state.remember ? 'checked' : ''}><span>记住密码</span></label>
    <div class="group" style="margin-top:14px">
      <div class="group-title"><span>局域网自动发现</span>
        <button class="link" id="disc-btn" ${canDiscover() ? '' : 'disabled'} style="${canDiscover() ? '' : 'opacity:.45'}">扫描</button>
      </div>
      <div class="chips" id="disc-list"><span class="muted">${canDiscover()
        ? '扫描同一网段内 8099 端口的 SubFlow'
        : '仅 App 内可用；浏览器请直接填写地址'}</span></div>
    </div>
    <p class="hint" id="set-hint">通道：${BRIDGE ? '鸿蒙原生 HTTP 桥' : '浏览器 fetch'}${
      IS_CONTAINER ? ' · 容器内运行' : ''}。留空账号密码表示没有开启访问控制。</p>`;
  const root = openSheet({
    title: '连接设置',
    body,
    onClose: () => { state.settingsOpen = false; },
    actions: [
      { label: '取消', kind: 'ghost' },
      { label: '保存并测试', kind: 'primary', onClick: async (btn) => {
        const base = normalizeBase($('set-base').value);
        if (!base) { $('set-hint').textContent = '请填写服务器地址'; $('set-hint').className = 'hint err'; return false; }
        state.base = base;
        state.user = $('set-user').value.trim();
        state.pass = $('set-pass').value;
        state.remember = $('set-remember').checked;
        saveSaved();
        setConn('busy', '正在测试连接…');
        btn.textContent = '测试中…';
        try {
          const d = await api('/api/overview', { timeout: 15000 });
          state.overview = d;
          state.authPrompted = false;
          setConn('ok', baseLabel() + ' · 已连接');
          toast('连接成功' + (d.version ? ' · v' + d.version : ''), 'ok');
          state.sites = [];
          state.sitesAll = [];
          renderOverview(d);
          loadRecent();
          if (state.view === 'jobs' && state.jobs.rows.length) loadJobs(true);
          if (state.view === 'logs' && state.logs.rows.length) loadLogsTail();
          if (state.view === 'stats') loadStats();
          if (state.view === 'sites') loadSites();
          if (state.view === 'schedule') loadSchedule();
          if (state.view === 'zone') { state.zone.entries = []; loadZone(); }
        } catch (e) {
          setConn('err', baseLabel() + ' · 连接失败');
          $('set-hint').textContent = e.message;
          $('set-hint').className = 'hint err';
          btn.textContent = '保存并测试';
          return false;
        }
      } },
    ],
  });
  const discBtn = root.querySelector('#disc-btn');
  if (discBtn && canDiscover()) discBtn.onclick = () => runDiscovery(root);
  return root;
}

async function runDiscovery(root) {
  const btn = root.querySelector('#disc-btn');
  const list = root.querySelector('#disc-list');
  btn.disabled = true;
  btn.textContent = '扫描中…';
  list.innerHTML = '<span class="muted">正在扫描本机网段…</span>';
  try {
    const raw = await bridgeDiscover(30000);
    let d;
    try { d = JSON.parse(raw); } catch (e) { throw new Error('扫描返回异常'); }
    if (!d || d.ok === false) throw new Error((d && d.error) || '扫描失败');
    const servers = d.servers || [];
    if (!servers.length) {
      list.innerHTML = '<span class="muted">没有发现服务器（本机 '
        + esc((d.local || []).join(', ') || '未知') + '）</span>';
      toast('未发现服务器', 'err');
      return;
    }
    list.innerHTML = servers.map((s) => {
      const label = String(s.url || '').replace(/^https?:\/\//, '');
      return `<button class="chip" data-url="${esc(s.url || '')}">${esc(label)}${
        s.version ? ' · v' + esc(s.version) : ''}</button>`;
    }).join('');
    list.querySelectorAll('[data-url]').forEach((el) => {
      el.onclick = () => {
        root.querySelector('#set-base').value = el.dataset.url;
        list.querySelectorAll('.chip').forEach((c) => c.classList.toggle('on', c === el));
        toast('已填入地址，点「保存并测试」验证', 'ok');
      };
    });
  } catch (e) {
    list.innerHTML = '<span class="muted">' + esc(e.message) + '</span>';
    toast(e.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = '扫描';
  }
}

/* ---------------------------------------------------------------- 启动 */

function bindEvents() {
  $('btn-settings').onclick = () => openSettings();
  $('more-settings').onclick = () => openSettings();

  document.querySelectorAll('.tab').forEach((t) => {
    t.onclick = () => setView(t.dataset.view);
  });
  document.querySelectorAll('[data-goto]').forEach((b) => {
    b.onclick = () => setView(b.dataset.goto);
  });
  document.querySelectorAll('[data-back]').forEach((b) => {
    b.onclick = () => setView('more');
  });

  $('job-chips').onclick = (ev) => {
    const chip = ev.target.closest('[data-filter]');
    if (!chip) return;
    state.jobs.status = chip.dataset.filter;
    renderJobChips();
    loadJobs(true);
  };
  $('job-list').onclick = onJobClick;
  $('home-jobs').onclick = onJobClick;
  $('job-list').addEventListener('change', (ev) => {
    const cb = ev.target.closest('input[data-pickjob]');
    if (!cb) return;
    toggleJobPick(cb.dataset.pickjob, cb.checked);
  });
  $('job-all').onchange = () => {
    const st = state.jobs;
    if ($('job-all').checked) st.rows.forEach((j) => st.selIds.add(String(j.id)));
    else st.selIds.clear();
    if (!st.selIds.size) { exitJobSelect(); return; }
    renderJobs();
  };
  $('job-cancel').onclick = exitJobSelect;
  $('job-retry').onclick = retrySelectedJobs;
  bindJobLongPress();

  $('stats-range').onclick = (ev) => {
    const chip = ev.target.closest('[data-days]');
    if (!chip) return;
    state.stats.days = Number(chip.dataset.days) || 30;
    document.querySelectorAll('#stats-range .chip')
      .forEach((c) => c.classList.toggle('on', c === chip));
    loadStats();
  };

  $('zone-load').onclick = () => loadZone(false);
  $('zone-keyword').onkeydown = (ev) => { if (ev.key === 'Enter') loadZone(false); };
  $('zone-site').onchange = () => { state.zone.site = $('zone-site').value; loadZone(false); };
  $('zone-pages').onchange = () => { state.zone.pages = Number($('zone-pages').value || 1); loadZone(false); };
  $('zone-filters').onclick = (ev) => {
    const chip = ev.target.closest('[data-zf]');
    if (!chip) return;
    state.zone.onlyMatched = chip.dataset.zf === 'matched';
    document.querySelectorAll('#zone-filters .chip')
      .forEach((c) => c.classList.toggle('on', c === chip));
    renderZone();
  };
  $('zone-list').addEventListener('change', (ev) => {
    const cb = ev.target.closest('input[data-zpick]');
    if (!cb) return;
    toggleZonePick(cb.dataset.zpick, cb.checked);
  });
  $('zone-all').onchange = () => {
    const Z = state.zone;
    const rows = visibleZoneEntries();
    if ($('zone-all').checked) rows.forEach((e) => Z.picked.add(zoneKey(e)));
    else Z.picked.clear();
    renderZone();
  };
  $('zone-submit').onclick = submitZone;

  $('sites-refresh').onclick = () => loadSites();

  let kwTimer = null;
  $('job-keyword').oninput = () => {
    clearTimeout(kwTimer);
    kwTimer = setTimeout(() => {
      state.jobs.keyword = $('job-keyword').value.trim();
      loadJobs(true);
    }, 350);
  };
  $('job-refresh').onclick = () => loadJobs(true);
  $('job-more').onclick = () => { state.jobs.offset = state.jobs.rows.length; loadJobs(false); };

  $('btn-scan').onclick = openScanSheet;
  $('btn-cat').onclick = openCategorySheet;
  $('btn-submit').onclick = openSubmitSheet;
  $('btn-retry').onclick = retryFailed;
  $('btn-run').onclick = runPending;

  $('miss-run').onclick = runMissingScan;
  $('miss-keyword').onkeydown = (ev) => { if (ev.key === 'Enter') runMissingScan(); };
  $('miss-repair').onclick = repairMissing;
  $('miss-probe-run').onclick = runMissProbe;
  $('miss-new-only').onchange = () => {
    state.missing.probe.newOnly = !!$('miss-new-only').checked;
    renderMissing();
  };
  $('miss-all').onchange = () => {
    if ($('miss-all').checked) {
      // 只勾选当前可见且可补的条目（「只看新上」开启时不会勾到隐藏项）
      missVisible().forEach(({ it, idx }) => {
        if (missRepairable(it)) state.missing.picked.add(idx);
      });
    } else {
      state.missing.picked.clear();
    }
    renderMissing();
  };
  $('miss-list').onchange = (ev) => {
    const cb = ev.target.closest('input[data-pick]');
    if (!cb) return;
    const idx = Number(cb.dataset.pick);
    if (cb.checked) state.missing.picked.add(idx); else state.missing.picked.delete(idx);
    updateMissBar();
  };

  $('log-levels').onclick = (ev) => {
    const chip = ev.target.closest('[data-level]');
    if (!chip) return;
    state.logs.level = chip.dataset.level;
    state.logs.since = 0;
    state.logs.rows = [];
    renderLogChips();
    loadLogsTail();
  };
  $('log-auto').onchange = () => {
    state.logs.auto = $('log-auto').checked;
    if (state.logs.auto) pollLogs();
  };
  let logKw = null;
  $('log-keyword').oninput = () => {
    clearTimeout(logKw);
    logKw = setTimeout(() => {
      state.logs.keyword = $('log-keyword').value.trim();
      state.logs.since = 0;
      state.logs.rows = [];
      loadLogsTail();
    }, 400);
  };
  $('log-refresh').onclick = () => loadLogsTail();

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && state.view === 'home') loadOverview(true);
  });
  // 页面卸载时清掉复查轮询定时器
  window.addEventListener('pagehide', () => abortMissProbe());
  window.addEventListener('beforeunload', () => abortMissProbe());
}

async function bootstrap() {
  setConn('busy', '正在连接…');
  renderJobChips();
  renderLogChips();
  renderMore();
  try {
    const sites = await api('/api/sites');
    state.sitesAll = sites.sites || [];
    state.sites = state.sitesAll.filter((s) => s.enabled);
  } catch (e) {
    if (!e.message.includes('登录')) toast(e.message, 'err');
  }
  loadOverview();
  if (state.view === 'logs') loadLogsTail();
}

/* ---------------------------------------------------------------- 下拉刷新 */

function bindPullToRefresh() {
  const ptr = $('ptr');
  const txt = $('ptr-text');
  let start = null;
  let dy = 0;
  let active = false;
  let busy = false;

  const hide = () => {
    ptr.classList.remove('loading', 'release');
    ptr.style.transition = '';
    ptr.style.transform = '';
    ptr.style.opacity = '';
    txt.textContent = '下拉刷新';
  };

  document.addEventListener('touchstart', (ev) => {
    if (busy || active || ev.touches.length !== 1) return;
    if (window.scrollY > 0) return;
    if ($('sheet-root').classList.contains('on')) return;
    const t = ev.target;
    if (t && t.closest('input, textarea, select, .logbox')) return;
    start = ev.touches[0].clientY;
    dy = 0;
    active = true;
  }, { passive: true });

  document.addEventListener('touchmove', (ev) => {
    if (!active || busy) return;
    dy = ev.touches[0].clientY - (start || 0);
    if (dy <= 0) return;
    if (window.scrollY > 0) { active = false; hide(); return; }
    if (dy > 8 && ev.cancelable) ev.preventDefault();
    const pull = Math.min(dy * 0.55, 80);
    ptr.style.transition = 'none';
    ptr.style.transform = `translateY(${pull - 58}px)`;
    ptr.style.opacity = String(Math.min(1, pull / 40));
    ptr.classList.toggle('release', pull >= 58);
    txt.textContent = pull >= 58 ? '松开刷新' : '下拉刷新';
  }, { passive: false });

  const finish = async () => {
    if (!active) return;
    active = false;
    const trigger = dy * 0.55 >= 58;
    if (!trigger) { hide(); return; }
    busy = true;
    ptr.classList.add('loading');
    ptr.classList.remove('release');
    ptr.style.transition = '';
    ptr.style.transform = '';
    ptr.style.opacity = '';
    txt.textContent = '正在刷新…';
    try {
      await refreshCurrentView();
    } catch (e) {
      toast(e.message, 'err');
    } finally {
      busy = false;
      hide();
    }
  };

  document.addEventListener('touchend', finish, { passive: true });
  document.addEventListener('touchcancel', () => {
    if (!active) return;
    active = false;
    hide();
  }, { passive: true });
}

function init() {
  loadSaved();
  bindEvents();
  bindPullToRefresh();
  setConn('busy', '正在连接…');
  probeBridge();

  if (!state.base) {
    openSettings('还没有配置服务器地址');
  } else {
    if (!state.savedBase && IS_CONTAINER) {
      const root = openSettings();
      const hint = root && root.querySelector('#set-hint');
      if (hint) {
        hint.textContent = '首次使用：确认服务器地址并填写访问控制的账号密码（未开启访问控制可留空）。'
          + '通道：' + (BRIDGE ? '鸿蒙原生 HTTP 桥' : '浏览器 fetch') + (IS_CONTAINER ? ' · 容器内运行' : '') + '。';
      }
    }
    bootstrap();
  }

  setInterval(() => {
    if (document.hidden || !state.base) return;
    if (state.view === 'home' || state.view === 'more') loadOverview(true);
    else if (state.view === 'jobs') refreshJobsQuiet();
    else if (state.view === 'logs' && state.logs.auto) pollLogs();
  }, 6000);
}

window.addEventListener('DOMContentLoaded', init);
