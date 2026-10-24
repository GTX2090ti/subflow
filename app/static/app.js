/* SubFlow 前端逻辑 —— 原生 JS，无框架、无构建步骤 */

'use strict';

const $ = (id) => document.getElementById(id);

const S = {
  view: 'dash',
  sites: [],
  overview: null,
  scanSites: new Set(),      // 参与扫描的站点（空集 = 全部）
  cats: { data: null, picked: new Set(), loading: false, savedAt: '' },  // 下载器分类
  jobs: { status: 'all', site: 'all', keyword: '', category: null,
          catOptions: [], page: 1, size: 50, pages: 1 },
  zone: { entries: [], picked: new Set(), site: 'all', loading: false },
  history: { site: 'all', status: 'all', keyword: '', start: '', end: '', page: 1, size: 50 },
  logs: { since: 0, level: 'ALL', source: 'all', keyword: '', count: 0 },
  missing: { items: [], picked: new Set(), loading: false },
  stats: { days: 30 },
  auth: { password_set: false },
  timer: null,
};

/* ---------------------------------------------------------------- 基础工具 */

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data;
  try { data = await res.json(); } catch (e) { throw new Error('HTTP ' + res.status); }
  if (data && data.ok === false) throw new Error(data.error || '请求失败');
  return data;
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtSize(n) {
  n = Number(n) || 0;
  if (n <= 0) return '-';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  if (n < 1024 * 1024 * 1024) return (n / 1048576).toFixed(1) + ' MB';
  return (n / 1073741824).toFixed(2) + ' GB';
}

function fmtDuration(ms) {
  ms = Number(ms) || 0;
  if (!ms) return '-';
  if (ms < 1000) return ms + ' ms';
  return (ms / 1000).toFixed(1) + ' s';
}

function fmtTime(t) {
  if (!t) return '-';
  return String(t).replace('T', ' ').slice(0, 19);
}

// 下载器返回的是 epoch 秒，和 fmtTime 的字符串时间不是一回事
function fmtUnix(sec) {
  const n = Number(sec) || 0;
  if (!n) return '-';
  const d = new Date(n * 1000);
  const p = (x) => String(x).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
    + `${p(d.getHours())}:${p(d.getMinutes())}`;
}

const SOURCE_LABEL = {
  scan: '自动扫描', manual: '手动提交', zone: '字幕区',
  legacy: '旧版导入', missing: '缺字幕补单', schedule: '定时扫描',
};

const STATUS_META = {
  success: ['ok', '成功'],
  exists: ['info', '已存在'],
  running: ['run', '进行中'],
  pending: ['warn', '待处理'],
  failed: ['err', '失败'],
  skipped: ['', '已跳过'],
};

function statusBadge(st) {
  const [cls, label] = STATUS_META[st] || ['', st || '未知'];
  const dot = st === 'running' ? '<i class="dot"></i>' : '';
  return `<span class="badge ${cls}">${dot}${esc(label)}</span>`;
}

// 任务来源分类：手动提交/对不上本地种子时为空；TR 的多标签会显示 "首个 +N"
function catBadge(cat) {
  const s = String(cat || '').trim();
  if (!s) return '<span class="muted">—</span>';
  const parts = s.split(',').filter(Boolean);
  const more = parts.length > 1 ? ` +${parts.length - 1}` : '';
  return `<span class="badge info" title="${esc(s)}">${esc(parts[0])}${more}</span>`;
}

function toast(msg, kind = 'ok') {
  const icon = kind === 'ok' ? 'i-check' : kind === 'err' ? 'i-alert' : 'i-info';
  const div = document.createElement('div');
  div.className = 'toast ' + kind;
  div.innerHTML = `<svg><use href="#${icon}"/></svg><span>${esc(msg)}</span>`;
  $('toasts').appendChild(div);
  setTimeout(() => div.remove(), kind === 'err' ? 6000 : 3200);
}

function fillSelect(sel, items, keepFirst = true) {
  const first = keepFirst ? sel.options[0]?.outerHTML || '' : '';
  sel.innerHTML = first + items.map(
    (it) => `<option value="${esc(it.value)}">${esc(it.label)}</option>`).join('');
}

/* ---------------------------------------------------------------- 视图切换 */

function switchView(name) {
  S.view = name;
  document.querySelectorAll('.tab').forEach(
    (t) => t.classList.toggle('active', t.dataset.view === name));
  document.querySelectorAll('.view').forEach(
    (v) => v.classList.toggle('active', v.id === 'view-' + name));
  if (name === 'dash') { loadOverview(); loadJobs(); }
  if (name === 'zone') renderZone();
  if (name === 'missing') renderMissing();
  if (name === 'stats') loadStats();
  if (name === 'history') loadHistory();
  if (name === 'logs') loadLogs(true);
  if (name === 'settings') loadSettings();
}

/* ---------------------------------------------------------------- 任务台 */

async function loadOverview() {
  try {
    const d = await api('/api/overview');
    S.overview = d;
    $('ver').textContent = 'v' + d.version;
    const st = d.stats || {};
    $('s-total').textContent = st.total ?? 0;
    $('s-total-sub').textContent = `累计 ${fmtSize(st.total_size)}，解压字幕 ${st.total_extracted ?? 0} 个`;
    $('s-ok').textContent = (st.success ?? 0) + (st.exists ?? 0);
    $('s-ok-sub').textContent = `成功 ${st.success ?? 0} · 已存在 ${st.exists ?? 0} · 今日 ${st.today ?? 0}`;
    $('s-pending').textContent = (st.pending ?? 0) + (st.running ?? 0);
    $('s-pending-sub').textContent = `待处理 ${st.pending ?? 0} · 进行中 ${st.running ?? 0} · 队列 ${d.queue}`;
    $('s-fail').textContent = st.failed ?? 0;
    $('s-fail-sub').textContent = `已跳过 ${st.skipped ?? 0}`;
    $('env-queue').textContent = `队列 ${d.queue} / 执行中 ${d.busy}`;
    renderScanState(d.scan || {});
  } catch (e) { /* 静默：状态列会体现 */ }
}

function renderScanState(sc) {
  const running = !!sc.running;
  $('scan-badge').className = 'badge ' + (running ? 'run' : (sc.phase === '失败' ? 'err' : ''));
  $('scan-badge').innerHTML = (running ? '<i class="dot"></i>' : '') + (running ? '扫描中' : (sc.phase || '空闲'));
  $('scan-phase').textContent = sc.running
    ? (sc.phase || '') + (sc.total ? ` ${sc.progress}/${sc.total}` : '')
    : (sc.started_at ? '上次运行 ' + fmtTime(sc.started_at) : '尚未运行');
  $('scan-count').textContent = sc.total ? `${sc.progress}/${sc.total}` : '';
  const bar = $('scan-progress');
  const pct = sc.total ? Math.min(100, (sc.progress / sc.total) * 100) : 0;
  bar.classList.toggle('indet', running && !sc.total);
  bar.querySelector('i').style.width = pct + '%';

  const r = sc.result;
  if (r && !running) {
    if (r.ok === false) {
      $('scan-result').innerHTML = `<span style="color:var(--err)">扫描失败：${esc(r.error || '')}</span>`;
    } else {
      const parts = [
        `扫描 ${r.scanned} 个种子`,
        `命中 ${r.matched}`,
        `含字幕 ${r.with_subs}`,
        `新建任务 ${r.jobs_created}`,
        `错误 ${r.errors}`,
        ((r.elapsed_ms || 0) / 1000).toFixed(1) + 's',
      ];
      const siteBits = Object.values(r.sites || {}).map(
        (v) => `${esc(v.label)} ${v.jobs} 条`).join('、');
      const catBits = (r.categories || []).length
        ? `<br><span class="muted">分类筛选：${r.categories.map(esc).join('、')}`
          + `（分类外跳过 ${r.skipped_category || 0} 个）</span>`
        : '';
      $('scan-result').innerHTML = parts.join(' · ') +
        (siteBits ? `<br><span class="muted">按站点：${siteBits}</span>` : '') +
        catBits +
        (r.dry_run ? '<br><span style="color:var(--warn)">演练模式，未实际建任务</span>' : '');
    }
  }
}

function renderScanSites() {
  const box = $('scan-sites');
  const enabled = S.sites.filter((s) => s.enabled);
  if (!enabled.length) {
    box.innerHTML = '<span class="muted">没有启用任何站点，请到「设置」开启</span>';
    return;
  }
  box.innerHTML = enabled.map((s) => {
    const on = S.scanSites.has(s.id) ? ' on' : '';
    const warn = s.cookie_ok === false ? ' style="border-color:rgba(248,81,73,.5)"' : '';
    return `<div class="chip${on}" data-site="${esc(s.id)}"${warn}>
      <i class="cbox"></i>${esc(s.label)}</div>`;
  }).join('');
  box.querySelectorAll('.chip').forEach((c) => {
    c.onclick = () => {
      const id = c.dataset.site;
      if (S.scanSites.has(id)) S.scanSites.delete(id); else S.scanSites.add(id);
      renderScanSites();
    };
  });
}

/* -------------------- 下载器分类：读取分类 → 按分类批量下载字幕 -------------------- */

// 分类的唯一键，形如 `dl1:电影`（分类名为空串表示「未分类」），与后端约定一致
function catKey(did, name) { return `${did}:${name || ''}`; }

function renderCatChips() {
  const box = $('scan-cats');
  const d = S.cats.data;
  if (!d) {
    box.innerHTML = '<span class="muted" style="font-size:12px">'
      + '尚未读取，点上方「读取分类」从下载器拉取分类清单</span>';
    return;
  }
  const groups = (d.downloaders || []).filter((g) => (g.categories || []).length);
  const stale = (d.downloaders || []).filter((g) => g.cached).length;
  const cacheHint = (d.cached || stale)
    ? `<div class="muted" style="font-size:11.5px;margin-bottom:8px">`
      + `上次读取：${esc(d.saved_at || '未知时间')}`
      + `（点「读取分类」刷新，下载器离线时也能按这份结果筛选）</div>`
    : '';
  if (!groups.length) {
    box.innerHTML = cacheHint + '<span class="muted" style="font-size:12px">'
      + esc((d.errors || []).join('；')
        || (d.cached ? '缓存里没有分类，点「读取分类」重新拉取' : '下载器里没有任何分类'))
      + '</span>';
    updateCatStat();
    return;
  }
  box.innerHTML = cacheHint + groups.map((g) => {
    const chips = g.categories.map((c) => {
      const key = catKey(g.id, c.name);
      const on = S.cats.picked.has(key) ? ' on' : '';
      const tip = c.save_path ? `保存路径 ${c.save_path}` : '未设置独立保存路径';
      return `<div class="chip${on}" data-cat="${esc(key)}" title="${esc(tip)}">
        <i class="cbox"></i>${esc(c.label || c.name)}<span class="muted">·${c.count}</span></div>`;
    }).join('');
    const note = g.ok ? '' : ` · ${esc(g.message)}`;
    return `<div style="margin-bottom:10px">
      <div class="muted" style="font-size:11.5px;margin-bottom:5px">
        ${esc(g.name)}（${esc(g.type)}，共 ${g.total} 个种子，已归类 ${g.categorized}${note}）</div>
      <div class="chips">${chips}</div></div>`;
  }).join('');
  box.querySelectorAll('.chip').forEach((el) => {
    el.onclick = () => {
      const k = el.dataset.cat;
      if (S.cats.picked.has(k)) S.cats.picked.delete(k); else S.cats.picked.add(k);
      renderCatChips();
    };
  });
  updateCatStat();
}

function updateCatStat() {
  const el = $('cat-stat');
  const d = S.cats.data;
  if (!d) { el.textContent = '尚未读取'; el.style.color = ''; return; }
  const total = (d.downloaders || []).reduce(
    (a, g) => a + (g.categories || []).length, 0);
  const errs = (d.errors || []).length;
  const stale = (d.downloaders || []).some((g) => g.cached);
  const cached = (d.cached || stale)
    ? ` · 缓存于 ${(d.saved_at || '').slice(5, 16) || '未知'}` : '';
  el.textContent = `${total} 个分类 · 已选 ${S.cats.picked.size} 个`
    + cached + (errs ? ` · ${errs} 个下载器读取失败` : '');
  el.style.color = errs ? 'var(--warn)' : '';
}

// 分类结果同时落库（后端快照），这里统一走同一套回显与勾选校验
function applyCategoryData(d) {
  S.cats.data = d;
  S.cats.savedAt = d.saved_at || '';
  // 丢掉上一次选过、这次已不存在的分类，避免提交了无效筛选
  const valid = new Set();
  (d.downloaders || []).forEach((g) => (g.categories || []).forEach(
    (c) => valid.add(catKey(g.id, c.name))));
  S.cats.picked = new Set([...S.cats.picked].filter((k) => valid.has(k)));
  renderCatChips();
}

async function loadCachedCategories(silent) {
  try {
    const d = await api('/api/categories/cached');
    if (!(d.downloaders || []).length) return null;
    applyCategoryData(d);
    return d;
  } catch (e) {
    if (!silent) toast('读取分类缓存失败：' + e.message, 'err');
    return null;
  }
}

async function loadCategories() {
  if (S.cats.loading) return;
  S.cats.loading = true;
  $('btn-cat-load').disabled = true;
  $('cat-stat').textContent = '读取中（会完整拉取一次种子列表，分类多时稍慢）…';
  try {
    const d = await api('/api/categories');
    applyCategoryData(d);
    const total = (d.downloaders || []).reduce(
      (a, g) => a + (g.categories || []).length, 0);
    const stale = (d.downloaders || []).filter((g) => g.cached).length;
    toast((total ? `读到 ${total} 个分类` : '该下载器没有分类，可先用「未分类」或直接扫描')
      + (stale ? `（${stale} 个下载器离线，用了上次快照）` : ''),
      total && !stale ? 'ok' : 'info');
  } catch (e) {
    const cached = await loadCachedCategories(true);
    toast(e.message + (cached ? '（已显示上次读取的分类）' : ''), 'err');
  } finally {
    S.cats.loading = false;
    $('btn-cat-load').disabled = false;
    renderCatChips();
  }
}

async function loadSites() {
  const d = await api('/api/sites');
  S.sites = d.sites || [];
  renderScanSites();
  fillSelect($('sub-site'), S.sites.filter((s) => s.enabled).map(
    (s) => ({ value: s.id, label: s.label })), false);
  fillSelect($('job-site'), S.sites.map(
    (s) => ({ value: s.id, label: s.label })));
  fillSelect($('h-site'), S.sites.map(
    (s) => ({ value: s.id, label: s.label })));
  renderSitesTable();
  renderCcDomains(d);
}

function renderSitesTable() {
  const body = $('sites-body');
  if (!S.sites.length) { body.innerHTML = '<tr><td colspan="7" class="empty">无站点</td></tr>'; return; }
  body.innerHTML = S.sites.map((s) => {
    const ck = s.cookie_ok === null || s.cookie_ok === undefined
      ? '<span class="badge">未校验</span>'
      : (s.cookie_ok ? '<span class="badge ok">可用</span>' : '<span class="badge err">不可用</span>');
    return `<tr data-site="${esc(s.id)}">
      <td><label class="switch"><input type="checkbox" class="st-en" ${s.enabled ? 'checked' : ''}><span class="track"></span></label></td>
      <td class="nowrap">${esc(s.label)}</td>
      <td class="mono nowrap">${esc(s.id)}</td>
      <td><input type="text" class="st-url" value="${esc(s.base_url)}"></td>
      <td class="mono nowrap">${esc(s.domain)}</td>
      <td class="nowrap">${ck}</td>
      <td class="muted" style="font-size:12px">${esc(s.note || '')}</td>
    </tr>`;
  }).join('');
}

function renderCcDomains(d) {
  const box = $('cc-domains');
  const list = d.cookiecloud_domains || [];
  if (!d.cookiecloud_ok) {
    box.innerHTML = `<span style="color:var(--err)">CookieCloud 不可用：${esc(d.cookiecloud_message || '')}</span>`;
    return;
  }
  const known = new Set(S.sites.map((s) => s.domain));
  box.innerHTML = '<div style="margin-bottom:8px">已从 CookieCloud 同步的站点：</div>' +
    '<div class="chips">' + list.map((x) => {
      const hit = [...known].some((k) => x.domain === k || x.domain.endsWith('.' + k));
      return `<span class="chip${hit ? ' on' : ''}" title="${hit ? '已注册为可下载站点' : '未注册适配器'}">
        ${esc(x.domain)}<span class="muted">·${x.count}</span></span>`;
    }).join('') + '</div>';
}

// 页码窗口照搬 Stash 用的 MUI Pagination 算法（siblings=1 / boundary=1）：
// 第 1 页是 1 2 3 4 5 … 20，中间页是 1 … 9 10 11 … 20
function jobPageNumbers(page, pages) {
  if (pages <= 0) return [];
  const seq = (a, b) => {
    const out = [];
    for (let i = a; i <= b; i++) out.push(i);
    return out;
  };
  const start = seq(1, Math.min(1, pages));
  const end = seq(Math.max(pages, 2), pages);
  const sibStart = Math.max(Math.min(page - 1, pages - 4), 3);
  const sibEnd = Math.min(Math.max(page + 1, 5), pages - 2);
  const out = [...start];
  if (sibStart > 3) out.push('…');
  else if (2 < pages - 1) out.push(2);
  out.push(...seq(sibStart, sibEnd));
  if (sibEnd < pages - 2) out.push('…');
  else if (pages - 1 > 1) out.push(pages - 1);
  out.push(...end);
  return out;
}

function renderJobsPager(page, pages, total) {
  const size = S.jobs.size;
  const from = total ? (page - 1) * size + 1 : 0;
  const to = Math.min(total, page * size);
  const numsHtml = jobPageNumbers(page, pages).map((p) => (
    p === '…'
      ? '<span class="gap">…</span>'
      : `<button class="btn sm pg${p === page ? ' on' : ''}" data-page="${p}"`
        + `${p === page ? ' aria-current="page"' : ''}>${p}</button>`
  )).join('');
  document.querySelectorAll('[data-pager="jobs"]').forEach((root) => {
    const q = (role) => root.querySelector(`[data-role="${role}"]`);
    q('range').textContent = total ? `${from}–${to} / 共 ${total} 条` : '共 0 条';
    const nums = q('pages');
    nums.innerHTML = numsHtml;
    nums.querySelectorAll('button[data-page]').forEach((b) => {
      b.onclick = () => { S.jobs.page = Number(b.dataset.page); loadJobs(); };
    });
    q('first').disabled = q('prev').disabled = page <= 1;
    q('next').disabled = q('last').disabled = page >= pages;
    q('last').title = `最后一页（第 ${pages} 页）`;
    const sel = q('size');
    if (sel.value !== String(size)) sel.value = String(size);
  });
}

function bindJobsPagers() {
  document.querySelectorAll('[data-pager="jobs"]').forEach((root) => {
    const q = (role) => root.querySelector(`[data-role="${role}"]`);
    q('first').onclick = () => { S.jobs.page = 1; loadJobs(); };
    q('prev').onclick = () => {
      if (S.jobs.page > 1) { S.jobs.page--; loadJobs(); }
    };
    q('next').onclick = () => {
      if (S.jobs.page < S.jobs.pages) { S.jobs.page++; loadJobs(); }
    };
    q('last').onclick = () => {
      S.jobs.page = S.jobs.pages || 1; loadJobs();
    };
    q('size').onchange = (e) => {
      S.jobs.size = Number(e.target.value); S.jobs.page = 1; loadJobs();
    };
  });
}

async function loadJobCategoryOptions() {
  const sel = $('job-category');
  let d;
  try { d = await api('/api/jobs/categories'); } catch (e) { return; }
  const rows = d.categories || [];
  const labelCount = {};
  rows.forEach((r) => { labelCount[r.label] = (labelCount[r.label] || 0) + 1; });
  const keep = S.jobs.category;
  sel.innerHTML = '<option value="">全部分类</option>';
  rows.forEach((r, i) => {
    const opt = document.createElement('option');
    opt.value = String(i);
    // 同名分类出现在多个下载器时补下载器名，避免选项看起来重复
    opt.textContent = r.label + (labelCount[r.label] > 1 ? ` · ${r.downloader}` : '')
      + `（${r.count}）`;
    sel.appendChild(opt);
  });
  S.jobs.catOptions = rows;
  const idx = keep
    ? rows.findIndex((r) => r.downloader_id === keep.downloader_id
      && r.category === keep.category)
    : -1;
  if (keep && idx < 0) S.jobs.category = null;
  sel.value = idx >= 0 ? String(idx) : '';
}

async function loadJobs() {
  const size = S.jobs.size;
  const page = Math.max(1, S.jobs.page);
  const q = new URLSearchParams({
    status: S.jobs.status, site: S.jobs.site, keyword: S.jobs.keyword,
    limit: String(size), offset: String((page - 1) * size),
  });
  if (S.jobs.category) {
    q.set('downloader', S.jobs.category.downloader_id || '');
    q.set('category', S.jobs.category.category || '__uncat__');
  }
  const d = await api('/api/jobs?' + q);
  const total = d.total || 0;
  const pages = Math.max(1, Math.ceil(total / size));
  if (page > pages) {
    // 删到当前页空了：退到最后一个有数据的页
    S.jobs.page = pages;
    return loadJobs();
  }
  S.jobs.page = page;
  S.jobs.pages = pages;
  $('jobs-hint').textContent = `共 ${total} 条`;
  const body = $('jobs-body');
  if (!d.jobs || !d.jobs.length) {
    body.innerHTML = '<tr><td colspan="10" class="empty">暂无任务，先执行一次「扫描种子库」或「按分类批量下载」</td></tr>';
    renderJobsPager(page, pages, total);
    return;
  }
  body.innerHTML = d.jobs.map((j) => `<tr>
    <td class="mono nowrap">#${j.id}</td>
    <td class="nowrap">${statusBadge(j.status)}</td>
    <td class="nowrap">${esc(j.site_label || j.site_id)}</td>
    <td><div class="ellip" title="${esc(j.torrent_name)}">${esc(j.torrent_name || '-')}</div>
        <div class="muted mono" style="font-size:11px">tid=${esc(j.torrent_id)} · subid=${esc(j.subid)}</div></td>
    <td><div class="ellip" title="${esc(j.sub_title)}">${esc(j.sub_title || '-')}</div></td>
    <td class="nowrap">${catBadge(j.category)}</td>
    <td class="nowrap">${fmtSize(j.file_size)}</td>
    <td class="nowrap">${fmtDuration(j.duration_ms)}</td>
    <td class="nowrap muted" style="font-size:12px">${fmtTime(j.finished_at || j.started_at || j.created_at)}</td>
    <td class="nowrap">
      <button class="btn sm" data-act="retry" data-id="${j.id}"><svg><use href="#i-refresh"/></svg>重试</button>
      <button class="btn sm danger" data-act="del" data-id="${j.id}"><svg><use href="#i-trash"/></svg></button>
    </td></tr>`).join('');
  body.querySelectorAll('button[data-act]').forEach((b) => {
    b.onclick = async () => {
      const id = b.dataset.id;
      if (b.dataset.act === 'retry') {
        await api(`/api/jobs/${id}/retry`, { method: 'POST' });
        toast(`任务 #${id} 已重新入队`);
      } else {
        await api(`/api/jobs/${id}`, { method: 'DELETE' });
        toast(`任务 #${id} 已删除`);
      }
      loadJobs(); loadOverview();
    };
  });
  renderJobsPager(page, pages, total);
}

async function doScan() {
  const payload = {
    site_ids: S.scanSites.size ? [...S.scanSites] : null,
    // 勾了分类就带上，作为扫描的筛选条件（仍受时间窗口限制）
    categories: S.cats.picked.size ? [...S.cats.picked] : null,
    days: Number($('scan-days').value) || 30,
    limit: Number($('scan-limit').value) || null,
    dry_run: $('scan-dry').checked,
    refresh_cookie: $('scan-refresh').checked,
  };
  try {
    await api('/api/scan', { method: 'POST', body: payload });
    toast('扫描已启动，进度见右侧状态面板');
    loadOverview();
  } catch (e) { toast(e.message, 'err'); }
}

// 按分类批量下载：与「开始扫描」同一条链路，区别只在
// days=0（不限时间窗口）——分类全量处理时才不会被 30 天窗口截掉。
async function doScanByCategory() {
  if (!S.cats.picked.size) {
    toast('请先点「读取分类」并勾选要处理的分类', 'err');
    loadCategories();
    return;
  }
  const payload = {
    site_ids: S.scanSites.size ? [...S.scanSites] : null,
    categories: [...S.cats.picked],
    days: 0,
    limit: Number($('scan-limit').value) || null,
    dry_run: $('scan-dry').checked,
    refresh_cookie: $('scan-refresh').checked,
  };
  try {
    await api('/api/scan', { method: 'POST', body: payload });
    toast(`已按 ${S.cats.picked.size} 个分类启动全量扫描`);
    loadOverview();
  } catch (e) { toast(e.message, 'err'); }
}

async function doSubmit() {
  const body = {
    site_id: $('sub-site').value,
    target: $('sub-target').value,
    save_dir: $('sub-dir').value || null,
    refresh_cookie: $('sub-refresh').checked,
    auto_run: true,
  };
  if (!body.site_id) return toast('请选择站点', 'err');
  if (!body.target.trim()) return toast('请填写种子 ID 或链接', 'err');
  try {
    const d = await api('/api/submit', { method: 'POST', body });
    const lines = (d.items || []).map(
      (x) => `${esc(x.torrent_id)} → ${esc(x.name)}：新建 ${x.created} 条${x.note ? '（' + esc(x.note) + '）' : ''}`);
    $('sub-result').innerHTML =
      `解析 ${d.parsed} 条，新建任务 ${d.jobs_created} 个，入队 ${d.jobs_queued} 个`
      + (lines.length ? '<br>' + lines.join('<br>') : '')
      + ((d.errors || []).length
        ? '<br><span style="color:var(--err)">' + d.errors.map(esc).join('<br>') + '</span>' : '');
    toast(`已提交，新建 ${d.jobs_created} 个任务`);
    loadJobs(); loadOverview();
  } catch (e) { toast(e.message, 'err'); }
}

/* ---------------------------------------------------------------- 历史记录 */

async function loadHistory() {
  const h = S.history;
  const q = new URLSearchParams({
    site: h.site, status: h.status, keyword: h.keyword,
    start: h.start, end: h.end,
    limit: String(h.size), offset: String((h.page - 1) * h.size),
  });
  try {
    const d = await api('/api/history?' + q);
    const body = $('history-body');
    if (!d.records || !d.records.length) {
      body.innerHTML = '<tr><td colspan="10" class="empty">没有符合条件的历史记录</td></tr>';
    } else {
      body.innerHTML = d.records.map((r) => `<tr>
        <td class="mono nowrap">#${r.id}</td>
        <td class="nowrap">${fmtTime(r.finished_at || r.created_at)}</td>
        <td class="nowrap">${esc(r.site_label || r.site_id)}</td>
        <td><div class="ellip" title="${esc(r.torrent_name)}">${esc(r.torrent_name || '-')}</div></td>
        <td class="nowrap">${catBadge(r.category)}</td>
        <td><div class="ellip" title="${esc(r.sub_title)}">${esc(r.sub_title || '-')}</div></td>
        <td><div class="ellip mono" style="font-size:12px" title="${esc(r.file_name)}">${esc(r.file_name || '-')}</div></td>
        <td class="nowrap">${fmtSize(r.file_size)}</td>
        <td class="nowrap">${statusBadge(r.status)}</td>
        <td class="muted" style="font-size:12px">${esc(r.message || '')}${r.extracted ? ` · 解压 ${r.extracted}` : ''}</td>
      </tr>`).join('');
    }
    const pages = Math.max(1, Math.ceil(d.total / h.size));
    $('h-pageinfo').textContent = `第 ${h.page} / ${pages} 页 · 共 ${d.total} 条`;
    $('h-prev').disabled = h.page <= 1;
    $('h-next').disabled = h.page >= pages;
  } catch (e) { toast(e.message, 'err'); }
}

/* ---------------------------------------------------------------- 日志 */

async function loadLogs(reset) {
  if (reset) { S.logs.since = 0; $('logbox').innerHTML = ''; S.logs.count = 0; }
  const q = new URLSearchParams({
    since_id: String(S.logs.since), level: S.logs.level,
    source: S.logs.source, keyword: S.logs.keyword, limit: '500',
  });
  try {
    const d = await api('/api/logs?' + q);
    fillSelect($('log-source'),
      (d.sources || []).map((s) => ({ value: s, label: s })));
    if (S.logs.source !== 'all') $('log-source').value = S.logs.source;

    const box = $('logbox');
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
    for (const r of d.logs || []) {
      const div = document.createElement('div');
      div.className = 'ln' + (r.level === 'ERROR' || r.level === 'FATAL' ? ' err-line' : '');
      div.innerHTML = `<span class="ts">${esc(r.ts)}</span>
        <span class="lv ${esc(r.level)}">${esc(r.level)}</span>
        <span class="sc" title="${esc(r.source)}">${esc(r.source)}</span>
        <span class="ms">${esc(r.message)}</span>`;
      box.appendChild(div);
      S.logs.count++;
    }
    S.logs.since = d.last_id || S.logs.since;
    // 上限保护：DOM 里最多保留 3000 行
    while (box.childElementCount > 3000) box.removeChild(box.firstChild);
    if ($('log-follow').checked && (atBottom || reset)) box.scrollTop = box.scrollHeight;
    $('log-stat').textContent = `本次已加载 ${S.logs.count} 条 · 最新 ID ${S.logs.since}`;
  } catch (e) { /* 静默重试 */ }
}

/* ---------------------------------------------------------------- 设置 */

async function loadSettings() {
  try {
    const d = await api('/api/config');
    const c = d.config || {};
    const qb = c.qbittorrent || {}, cc = c.cookiecloud || {}, dl = c.download || {}, lg = c.log || {};
    const au = c.auth || {}, sc = c.schedule || {};
    $('cfg-qb-url').value = qb.url || '';
    $('cfg-qb-key').value = '';
    $('cfg-qb-key').placeholder = qb.api_key_set ? '已设置（' + (qb.api_key || '') + '），留空不修改' : 'qbt_xxx';
    $('cfg-qb-conf').value = qb.conf_path || '';
    $('cfg-qb-map').value = qb.save_map || '';
    $('cfg-cc-url').value = cc.url || '';
    $('cfg-cc-uuid').value = cc.uuid || '';
    $('cfg-cc-pass').value = '';
    $('cfg-cc-pass').placeholder = cc.password_set ? '已设置（' + (cc.password || '') + '），留空不修改' : '••••••';
    $('cfg-cc-ttl').value = cc.cache_ttl ?? 300;
    $('cfg-scan-days').value = dl.scan_days ?? 30;
    $('cfg-interval').value = dl.req_interval ?? 1.5;
    $('cfg-workers').value = dl.workers ?? 3;
    $('cfg-log-days').value = lg.retain_days ?? 30;
    $('cfg-manual-dir').value = dl.manual_save_dir || '';
    $('cfg-extract').checked = dl.extract_zip !== false;
    $('cfg-keep').checked = dl.keep_archive !== false;

    $('cfg-auth-en').checked = !!au.enabled;
    S.auth.password_set = !!au.password_set;
    $('cfg-auth-user').value = au.username || '';
    $('cfg-auth-pass').value = '';
    $('cfg-auth-pass').placeholder = au.password_set
      ? '已设置（' + (au.password || '') + '），留空不修改' : '••••••';
    $('cfg-auth-realm').value = au.realm || 'SubFlow';
    $('cfg-auth-local').checked = au.allow_localhost !== false;

    $('cfg-sched-en').checked = !!sc.enabled;
    $('cfg-sched-hours').value = sc.interval_hours ?? 12;
    $('cfg-sched-days').value = sc.days ?? 3;
    $('cfg-sched-limit').value = sc.limit ?? 0;
    $('cfg-sched-refresh').checked = !!sc.refresh_cookie;
  } catch (e) { toast(e.message, 'err'); }
  await refreshHealth();
  await refreshSchedule();
}

async function refreshHealth() {
  try {
    const d = await api('/api/health');
    setPill('pill-qb', d.qbittorrent?.ok, d.qbittorrent?.message || '');
    setPill('pill-cc', d.cookiecloud?.ok, d.cookiecloud?.message || '');
    $('env-qb').textContent = d.qbittorrent?.message || '-';
    $('env-cc').textContent = d.cookiecloud?.message || '-';
    if (d.sites) {
      const map = {};
      d.sites.forEach((s) => { map[s.id] = s; });
      S.sites = S.sites.map((s) => ({ ...s, ...(map[s.id] || {}) }));
      renderSitesTable();
    }
  } catch (e) {
    setPill('pill-qb', false, e.message);
    setPill('pill-cc', false, e.message);
  }
}

function setPill(id, ok, title) {
  const el = $(id);
  el.className = 'pill ' + (ok ? 'ok' : 'bad');
  if (title) el.title = title;
}

async function saveConfig(patch, label) {
  try {
    await api('/api/config', { method: 'POST', body: { patch } });
    toast(label + ' 已保存');
    await loadSettings(); await loadSites();
  } catch (e) { toast(e.message, 'err'); }
}

/* ---------------------------------------------------------------- 事件绑定 */

function bindEvents() {
  $('tabs').addEventListener('click', (e) => {
    const t = e.target.closest('.tab');
    if (t) switchView(t.dataset.view);
  });

  $('btn-scan').onclick = doScan;
  $('btn-scan-top').onclick = () => { switchView('dash'); doScan(); };
  $('btn-scan-cat').onclick = doScanByCategory;
  $('btn-cat-load').onclick = loadCategories;
  $('btn-cat-clear').onclick = () => {
    if (!S.cats.picked.size) return;
    S.cats.picked = new Set();
    renderCatChips();
  };
  $('btn-run-pending').onclick = async () => {
    const d = await api('/api/jobs/run-pending', { method: 'POST' });
    toast(d.message || '已入队'); loadOverview();
  };
  $('btn-retry-failed').onclick = async () => {
    const d = await api('/api/jobs/retry-failed', { method: 'POST' });
    toast(d.message || '已入队'); loadJobs(); loadOverview();
  };
  $('btn-submit').onclick = doSubmit;

  // 筛选条件变化后回到第一页，否则会停在一个可能已不存在的页码上
  $('job-status').onchange = (e) => {
    S.jobs.status = e.target.value; S.jobs.page = 1; loadJobs();
  };
  $('job-site').onchange = (e) => {
    S.jobs.site = e.target.value; S.jobs.page = 1; loadJobs();
  };
  $('job-category').onchange = (e) => {
    S.jobs.category = e.target.value === ''
      ? null : S.jobs.catOptions[Number(e.target.value)];
    S.jobs.page = 1; loadJobs();
  };
  let kwTimer = null;
  $('job-keyword').oninput = (e) => {
    clearTimeout(kwTimer);
    kwTimer = setTimeout(() => {
      S.jobs.keyword = e.target.value.trim(); S.jobs.page = 1; loadJobs();
    }, 350);
  };
  bindJobsPagers();
  $('btn-job-refresh').onclick = () => {
    loadJobCategoryOptions(); loadJobs(); loadOverview();
  };

  $('btn-h-search').onclick = () => {
    S.history = {
      ...S.history, page: 1,
      site: $('h-site').value, status: $('h-status').value,
      keyword: $('h-keyword').value.trim(),
      start: $('h-start').value, end: $('h-end').value,
    };
    loadHistory();
  };
  $('btn-h-reset').onclick = () => {
    $('h-site').value = 'all'; $('h-status').value = 'all';
    $('h-keyword').value = ''; $('h-start').value = ''; $('h-end').value = '';
    S.history = { ...S.history, page: 1, site: 'all', status: 'all', keyword: '', start: '', end: '' };
    loadHistory();
  };
  $('h-keyword').onkeydown = (e) => { if (e.key === 'Enter') $('btn-h-search').click(); };
  $('h-prev').onclick = () => { if (S.history.page > 1) { S.history.page--; loadHistory(); } };
  $('h-next').onclick = () => { S.history.page++; loadHistory(); };
  $('h-pagesize').onchange = (e) => {
    S.history.size = Number(e.target.value); S.history.page = 1; loadHistory();
  };
  $('btn-h-export').onclick = () => {
    const h = S.history;
    const q = new URLSearchParams({
      site: h.site, status: h.status, keyword: h.keyword, start: h.start, end: h.end,
    });
    window.location.href = '/api/history/export.csv?' + q;
    toast('已开始导出 CSV');
  };

  $('btn-log-apply').onclick = () => {
    S.logs.level = $('log-level').value;
    S.logs.source = $('log-source').value;
    S.logs.keyword = $('log-keyword').value.trim();
    loadLogs(true);
  };
  $('log-keyword').onkeydown = (e) => { if (e.key === 'Enter') $('btn-log-apply').click(); };
  $('btn-log-download').onclick = () => { window.location.href = '/api/logs/download'; };
  $('btn-log-clear').onclick = async () => {
    if (!confirm('确认清空全部日志？该操作不可撤销。')) return;
    await api('/api/logs', { method: 'DELETE' });
    loadLogs(true); toast('日志已清空');
  };

  $('btn-save-qb').onclick = () => saveConfig({
    qbittorrent: {
      url: $('cfg-qb-url').value.trim(),
      api_key: $('cfg-qb-key').value.trim(),
      conf_path: $('cfg-qb-conf').value.trim(),
      save_map: $('cfg-qb-map').value.trim(),
    },
  }, 'qBittorrent 配置');

  $('btn-save-cc').onclick = () => saveConfig({
    cookiecloud: {
      url: $('cfg-cc-url').value.trim(),
      uuid: $('cfg-cc-uuid').value.trim(),
      password: $('cfg-cc-pass').value,
      cache_ttl: Number($('cfg-cc-ttl').value) || 300,
    },
  }, 'CookieCloud 配置');

  $('btn-save-dl').onclick = () => saveConfig({
    download: {
      scan_days: Number($('cfg-scan-days').value) || 30,
      req_interval: Number($('cfg-interval').value) || 1.5,
      workers: Number($('cfg-workers').value) || 3,
      manual_save_dir: $('cfg-manual-dir').value.trim(),
      extract_zip: $('cfg-extract').checked,
      keep_archive: $('cfg-keep').checked,
    },
    log: { retain_days: Number($('cfg-log-days').value) || 30 },
  }, '下载与日志配置');

  $('btn-cc-refresh').onclick = async () => {
    try {
      const d = await api('/api/cookiecloud/refresh', { method: 'POST' });
      toast(`已刷新：${d.meta?.domains ?? 0} 个域名 / ${d.meta?.cookies ?? 0} 条 Cookie`);
      await loadSites(); await refreshHealth();
    } catch (e) { toast(e.message, 'err'); }
  };

  $('btn-save-sites').onclick = async () => {
    const overrides = {};
    $('sites-body').querySelectorAll('tr[data-site]').forEach((tr) => {
      const id = tr.dataset.site;
      overrides[id] = {
        enabled: tr.querySelector('.st-en').checked,
        base_url: tr.querySelector('.st-url').value.trim(),
      };
    });
    await saveConfig({ sites: { enabled: true, overrides } }, '站点配置');
    $('sites-hint').textContent = '已保存，下一轮扫描生效';
  };

  $('btn-zone-load').onclick = loadZone;
  $('btn-zone-submit').onclick = submitZone;
  $('zone-keyword').onkeydown = (e) => { if (e.key === 'Enter') loadZone(); };
  $('zone-all').onchange = (e) => {
    S.zone.picked = e.target.checked
      ? new Set((S.zone.entries || []).map(zoneKey)) : new Set();
    renderZoneTable();
  };

  $('btn-save-auth').onclick = () => {
    // 密码为空时后端会保留旧密码；两者都没有则认证根本不生效，这里明确提示
    const wantOn = $('cfg-auth-en').checked;
    const pass = $('cfg-auth-pass').value;
    if (wantOn && !pass && !S.auth.password_set) {
      toast('请先设置密码，否则认证不会生效', 'err');
      $('cfg-auth-pass').focus();
      return;
    }
    saveConfig({
      auth: {
        enabled: wantOn,
        username: $('cfg-auth-user').value.trim() || 'admin',
        password: pass,
        realm: $('cfg-auth-realm').value.trim() || 'SubFlow',
        allow_localhost: $('cfg-auth-local').checked,
      },
    }, '访问控制');
  };

  $('btn-save-sched').onclick = async () => {
    await saveConfig({
      schedule: {
        enabled: $('cfg-sched-en').checked,
        interval_hours: Number($('cfg-sched-hours').value) || 12,
        days: Number($('cfg-sched-days').value) || 3,
        limit: Number($('cfg-sched-limit').value) || 0,
        refresh_cookie: $('cfg-sched-refresh').checked,
      },
    }, '定时扫描');
    await refreshSchedule();
  };

  $('btn-miss-scan').onclick = loadMissing;
  $('miss-keyword').onkeydown = (e) => { if (e.key === 'Enter') loadMissing(); };
  $('miss-all').onchange = (e) => {
    S.missing.picked = e.target.checked
      ? new Set(missSelectable().map(missKey)) : new Set();
    renderMissing();
  };
  $('btn-miss-repair').onclick = repairMissing;

  $('btn-stats-refresh').onclick = loadStats;
  $('stats-days').onchange = loadStats;

  $('auto-refresh').onchange = (e) => { if (e.target.checked) startPolling(); else stopPolling(); };
}

/* ---------------------------------------------------------------- 字幕区（站点级） */

function zoneKey(e) {
  return `${e.site_id}:${e.torrent_id}:${e.subid}`;
}

function zoneSites() {
  return S.sites.filter((s) => s.supports_subtitle_zone && s.enabled);
}

function renderZone() {
  const sel = $('zone-site');
  if (!sel.options.length) {
    const list = zoneSites();
    sel.innerHTML = '<option value="all">全部支持站点</option>'
      + list.map((s) => `<option value="${esc(s.id)}">${esc(s.label)}</option>`).join('');
    if (!list.length) {
      $('zone-stat').textContent = '当前没有启用且支持字幕区的站点，请到「设置」里开启';
    }
  }
  renderZoneTable();
}

function renderZoneStat(d) {
  const sites = Object.values(d.sites || {});
  const fetched = sites.reduce((a, s) => a + (s.fetched || 0), 0);
  const matched = sites.reduce((a, s) => a + (s.matched || 0), 0);
  const pages = sites.reduce((a, s) => a + (s.pages || 0), 0);
  $('zone-badge').className = 'badge ok';
  $('zone-badge').textContent = `${d.entries.length} 条`;
  $('zone-stat').textContent =
    `${sites.length} 个站点 / 读取 ${pages} 页 / 抓到 ${fetched} 条 / 命中本地种子 ${matched} 条`;
  $('zone-elapsed').textContent = fmtDuration(d.elapsed_ms);
  $('zone-hint').textContent = '勾选后点「下载选中」，命中本地种子的条目直接落到该视频目录';
  $('zone-errors').innerHTML = (d.errors || []).length
    ? (d.errors || []).map((x) => `· ${esc(x)}`).join('<br>')
    : '';
}

function renderZoneTable() {
  const body = $('zone-body');
  const rows = S.zone.entries || [];
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="9" class="empty">'
      + (S.zone.loading ? '读取中…' : '尚无条目，点击上方「读取字幕区」') + '</td></tr>';
    updateZonePicked();
    return;
  }
  body.innerHTML = rows.map((e) => {
    const key = zoneKey(e);
    const on = S.zone.picked.has(key);
    let st;
    if (e.job_id) st = statusBadge(e.job_status || 'pending');
    else if (e.matched) st = '<span class="badge info">待下载</span>';
    else st = '<span class="badge warn">未匹配本地</span>';
    const local = e.matched
      ? `<span class="mono" title="${esc(e.local.name || '')}">${esc((e.local.name || '').slice(0, 42))}</span>`
      : '<span class="muted">—</span>';
    return `<tr>
      <td><input type="checkbox" data-zone-pick="${esc(key)}" ${on ? 'checked' : ''}></td>
      <td class="nowrap">${esc(e.site_label || e.site_id || '')}</td>
      <td class="nowrap">${e.lang ? esc(e.lang) : '<span class="muted">—</span>'}</td>
      <td title="${esc(e.title || '')}">${esc((e.title || '').slice(0, 80)) || '<span class="muted">(无标题)</span>'}</td>
      <td>${local}</td>
      <td class="nowrap">${esc(e.size || '-')}</td>
      <td class="nowrap">${esc(fmtTime(e.added))}</td>
      <td class="nowrap">${st}</td>
      <td class="nowrap mono">${esc(e.torrent_id)} / ${esc(e.subid)}</td>
    </tr>`;
  }).join('');
  body.querySelectorAll('input[data-zone-pick]').forEach((cb) => {
    cb.onchange = () => {
      const k = cb.dataset.zonePick;
      if (cb.checked) S.zone.picked.add(k); else S.zone.picked.delete(k);
      updateZonePicked();
    };
  });
  updateZonePicked();
}

function updateZonePicked() {
  $('zone-picked').textContent = `已选 ${S.zone.picked.size} 条`;
  $('zone-all').checked = S.zone.entries.length > 0
    && S.zone.picked.size === S.zone.entries.length;
}

async function loadZone() {
  if (S.zone.loading) return;
  const sid = $('zone-site').value;
  const pages = Number($('zone-pages').value) || 0;
  S.zone.site = sid;
  S.zone.loading = true;
  $('zone-badge').className = 'badge run';
  $('zone-badge').textContent = '读取中';
  $('zone-stat').textContent = `${sid === 'all' ? '全部站点' : sid} 读取中，请稍候（站点有请求间隔，全量翻页可能较慢）…`;
  $('zone-elapsed').textContent = '';
  $('zone-errors').innerHTML = '';
  renderZoneTable();
  try {
    const d = await api('/api/zone/list', {
      method: 'POST',
      body: {
        site_id: sid, pages,
        keyword: $('zone-keyword').value.trim(),
        refresh_cookie: $('zone-refresh').checked,
      },
    });
    S.zone.entries = d.entries || [];
    S.zone.picked = new Set();
    renderZoneStat(d);
  } catch (e) {
    $('zone-badge').className = 'badge err';
    $('zone-badge').textContent = '读取失败';
    $('zone-stat').textContent = e.message;
    S.zone.entries = [];
    toast(e.message, 'err');
  } finally {
    S.zone.loading = false;
    renderZoneTable();
  }
}

async function submitZone() {
  const sid = $('zone-site').value;
  if (sid === 'all') { toast('请先选择具体站点再下载', 'err'); return; }
  const picked = (S.zone.entries || []).filter((e) => S.zone.picked.has(zoneKey(e)));
  if (!picked.length) { toast('请先勾选要下载的字幕', 'err'); return; }
  const btn = $('btn-zone-submit');
  btn.disabled = true;
  try {
    const d = await api('/api/zone/submit', {
      method: 'POST',
      body: {
        site_id: sid,
        items: picked.map((e) => ({
          torrent_id: e.torrent_id, subid: e.subid, title: e.title || '',
        })),
        save_dir: $('zone-dir').value.trim(),
        auto_run: true,
      },
    });
    toast(`新建 ${d.created}，已存在 ${d.skipped}，入队 ${d.queued}`
      + (d.errors && d.errors.length ? `，${d.errors.length} 条未入队` : ''));
    if (d.errors && d.errors.length) {
      $('zone-errors').innerHTML = d.errors.map((x) => `· ${esc(x)}`).join('<br>');
    }
    S.zone.picked = new Set();
    await loadZone();
    loadOverview();
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

/* ---------------------------------------------------------------- 缺字幕 */

function missKey(it) {
  return `${it.downloader_id || ''}:${it.hash || it.name || ''}`;
}

function missRepairable(it) {
  return !!(it.site_id && it.torrent_id);
}

function missSelectable() {
  return (S.missing.items || []).filter(missRepairable);
}

function renderMissing() {
  const body = $('miss-body');
  const rows = S.missing.items || [];
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="7" class="empty">'
      + (S.missing.loading ? '检查中…' : '没有缺字幕的种子（或尚未检查）') + '</td></tr>';
    updateMissPicked();
    return;
  }
  body.innerHTML = rows.map((it) => {
    const key = missKey(it);
    const can = missRepairable(it);
    const on = S.missing.picked.has(key);
    const files = (it.missing || []).map((m) => m.file).join('\n');
    const first = (it.missing || [])[0]?.file || '';
    return `<tr class="${on ? 'sel' : ''}">
      <td><input type="checkbox" data-miss-pick="${esc(key)}"
          ${can ? '' : 'disabled'} ${on ? 'checked' : ''}></td>
      <td><div class="ellip" title="${esc(it.name)}">${esc(it.name || '-')}</div>
          <div class="muted mono" style="font-size:11px">${esc((it.hash || '').slice(0, 16))}</div></td>
      <td class="nowrap">${esc(it.downloader_name || it.downloader_id || '-')}
          ${it.category ? `<div class="muted" style="font-size:11px">${esc(it.category)}</div>` : ''}</td>
      <td class="nowrap">
        <span class="badge warn" title="${esc(files)}">${it.missing_count}/${it.video_count}</span>
        <div class="ellip muted mono" style="font-size:11px;max-width:220px"
             title="${esc(files)}">${esc(first)}</div></td>
      <td class="nowrap muted" style="font-size:12px">${fmtUnix(it.added_on)}</td>
      <td class="nowrap">${can
        ? `<span class="badge info">${esc(it.site_label || it.site_id)}</span>
           <div class="muted mono" style="font-size:11px">tid=${esc(it.torrent_id)}</div>`
        : '<span class="badge">未匹配站点</span>'}</td>
      <td><div class="ellip mono" style="font-size:11px"
               title="${esc(it.root || it.save_path)}">${esc(it.root || it.save_path || '-')}</div></td>
    </tr>`;
  }).join('');
  body.querySelectorAll('input[data-miss-pick]').forEach((cb) => {
    cb.onchange = () => {
      const k = cb.dataset.missPick;
      if (cb.checked) S.missing.picked.add(k); else S.missing.picked.delete(k);
      cb.closest('tr').classList.toggle('sel', cb.checked);
      updateMissPicked();
    };
  });
  updateMissPicked();
}

function updateMissPicked() {
  const all = missSelectable();
  $('miss-picked').textContent = `已选 ${S.missing.picked.size} 个种子`;
  $('miss-all').checked = all.length > 0
    && all.every((it) => S.missing.picked.has(missKey(it)));
}

async function loadMissing() {
  if (S.missing.loading) return;
  const btn = $('btn-miss-scan');
  S.missing.loading = true;
  btn.disabled = true;
  $('miss-badge').className = 'badge run';
  $('miss-badge').textContent = '检查中';
  $('miss-stat').textContent = '正在遍历下载器种子与本地目录，目录较大时较慢…';
  $('miss-errors').innerHTML = '';
  $('miss-elapsed').textContent = '';
  try {
    const q = new URLSearchParams({
      days: $('miss-days').value || '30',
      limit: $('miss-limit').value || '200',
      keyword: $('miss-keyword').value.trim(),
      only_site_matched: $('miss-matched').checked ? 'true' : 'false',
    });
    const d = await api('/api/missing?' + q);
    S.missing.items = d.items || [];
    S.missing.picked = new Set();
    $('miss-badge').className = 'badge ' + (S.missing.items.length ? 'warn' : 'ok');
    $('miss-badge').textContent = `${S.missing.items.length} 个种子`;
    $('miss-stat').textContent =
      `检查 ${d.scanned_torrents} 个种子 / ${d.checked_videos} 个视频 · `
      + `缺字幕视频 ${d.missing_videos} 个 · 列出 ${S.missing.items.length} 个种子`
      + (d.skipped_dirs ? ` · 目录不存在 ${d.skipped_dirs} 个` : '')
      + (d.truncated ? ' · 结果已截断' : '');
    $('miss-elapsed').textContent = fmtDuration(d.elapsed_ms);
    $('miss-errors').innerHTML = (d.errors || []).map((x) => `· ${esc(x)}`).join('<br>');
    $('miss-hint').textContent =
      `${d.days > 0 ? '窗口 ' + d.days + ' 天' : '不限时间'} · 目录深度 ${d.max_depth} · `
      + `单目录上限 ${d.max_files}`;
  } catch (e) {
    $('miss-badge').className = 'badge err';
    $('miss-badge').textContent = '检查失败';
    $('miss-stat').textContent = e.message;
    S.missing.items = [];
    toast(e.message, 'err');
  } finally {
    S.missing.loading = false;
    btn.disabled = false;
    renderMissing();
  }
}

async function repairMissing() {
  const picked = (S.missing.items || []).filter(
    (it) => S.missing.picked.has(missKey(it)) && missRepairable(it));
  if (!picked.length) { toast('请先勾选可补字幕的种子', 'err'); return; }
  const btn = $('btn-miss-repair');
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
    });
    toast(`新建任务 ${d.created} 个，入队 ${d.queued} 个`
      + ((d.errors || []).length ? `，${d.errors.length} 条失败` : ''));
    $('miss-errors').innerHTML = (d.errors || []).map((x) => `· ${esc(x)}`).join('<br>');
    S.missing.picked = new Set();
    renderMissing();
    loadOverview(); loadJobs();
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

/* ---------------------------------------------------------------- 统计 */

async function loadStats() {
  const days = Number($('stats-days').value) || S.stats.days || 30;
  S.stats.days = days;
  try {
    const d = await api('/api/stats?days=' + days);
    renderStats(d);
  } catch (e) { toast(e.message, 'err'); }
}

function renderStats(d) {
  const t = d.totals || {};
  const total = t.total || 0;
  const ok = t.ok || 0, failed = t.failed || 0, skipped = t.skipped || 0;
  const open = t.open || 0;
  const done = ok + failed + skipped;
  $('st-ok').textContent = ok;
  $('st-ok-sub').textContent = `共 ${total} 条 · 跳过 ${skipped}`;
  $('st-fail').textContent = failed;
  $('st-fail-sub').textContent = done
    ? `失败率 ${(failed / done * 100).toFixed(1)}% · 待处理/进行中 ${open}`
    : `待处理/进行中 ${open}`;
  $('st-size').textContent = t.size ? fmtSize(t.size) : '0 B';
  $('st-size-sub').textContent = `解压字幕 ${t.extracted || 0} 个`;
  $('st-avg').textContent = fmtDuration(t.avg_ms);
  $('st-avg-sub').textContent = `自 ${String(d.since || '').slice(0, 10)} 起`;
  $('stats-range').textContent =
    `${String(d.since || '').slice(0, 10)} → ${String(d.generated_at || '').slice(0, 10)}`;

  renderDaily(d.daily || []);
  renderSiteStats(d.by_site || []);
  renderFailures(d.failures || []);
  renderSimpleStat('stats-cats', d.by_category || [],
    (r) => r.category || '未分类');
  renderSimpleStat('stats-sources', d.by_source || [],
    (r) => SOURCE_LABEL[r.source] || r.source || '未知');
}

function renderDaily(rows) {
  const box = $('stats-daily');
  if (!rows.length) { box.innerHTML = '<div class="empty">没有数据</div>'; return; }
  const max = Math.max(...rows.map((r) => r.total || 0), 1);
  const H = 110;
  const step = Math.max(1, Math.ceil(rows.length / 12));
  box.innerHTML = rows.map((r, i) => {
    const h = (v) => Math.round((v || 0) / max * H);
    const label = i % step === 0 || i === rows.length - 1
      ? String(r.d || '').slice(5) : '';
    const tip = `${r.d} · 共 ${r.total || 0}（成功 ${r.ok || 0} / 失败 ${r.failed || 0}`
      + ` / 跳过 ${r.skipped || 0} / 未完成 ${r.open || 0}）`;
    return `<div class="col" title="${esc(tip)}">
      <div class="bars">
        <span class="seg ok" style="height:${h(r.ok)}px"></span>
        <span class="seg err" style="height:${h(r.failed)}px"></span>
        <span class="seg skip" style="height:${h(r.skipped)}px"></span>
      </div>
      <span class="x">${esc(label)}</span>
    </div>`;
  }).join('');
  const peak = rows.reduce((a, r) => Math.max(a, r.total || 0), 0);
  if (!peak) {
    box.insertAdjacentHTML('beforeend',
      '<div class="chart-empty">窗口内没有任务</div>');
  }
  $('stats-daily-hint').textContent =
    `峰值 ${peak} 条/天 · 柱高按每日总量归一 · 悬停查看明细`;
}

function renderSiteStats(rows) {
  const box = $('stats-sites');
  if (!rows.length) { box.innerHTML = '<div class="empty">窗口内没有任务</div>'; return; }
  const max = Math.max(...rows.map((r) => r.total || 0), 1);
  box.innerHTML = rows.map((r) => `<div class="srow">
    <span class="sname ellip" title="${esc(r.site_id)}">${esc(r.label || r.site_id || '未知')}</span>
    <span class="sbar"><i style="width:${Math.round((r.total || 0) / max * 100)}%"></i></span>
    <span class="sval mono">${r.ok || 0}/${r.total || 0}</span>
    <span class="sextra">${r.failed ? `<span class="badge err">失败 ${r.failed}</span>` : ''}
      <span class="muted mono" style="font-size:11px">${fmtSize(r.size)}</span></span>
  </div>`).join('');
}

function renderFailures(rows) {
  const box = $('stats-failures');
  if (!rows.length) { box.innerHTML = '<div class="empty">窗口内没有失败任务</div>'; return; }
  const max = Math.max(...rows.map((r) => r.n || 0), 1);
  box.innerHTML = rows.map((r) => `<div class="srow">
    <span class="sname ellip" title="${esc(r.reason || '')}">${esc(r.reason || '(空说明)')}</span>
    <span class="sbar err"><i style="width:${Math.round((r.n || 0) / max * 100)}%"></i></span>
    <span class="sval mono">${r.n || 0}</span>
    <span class="sextra"></span>
  </div>`).join('');
}

function renderSimpleStat(id, rows, labelFn) {
  const box = $(id);
  if (!rows.length) { box.innerHTML = '<div class="empty">窗口内没有任务</div>'; return; }
  const max = Math.max(...rows.map((r) => r.total || 0), 1);
  box.innerHTML = rows.map((r) => `<div class="srow">
    <span class="sname ellip" title="${esc(labelFn(r))}">${esc(labelFn(r))}</span>
    <span class="sbar"><i style="width:${Math.round((r.total || 0) / max * 100)}%"></i></span>
    <span class="sval mono">${r.ok || 0}/${r.total || 0}</span>
    <span class="sextra"></span>
  </div>`).join('');
}

async function refreshSchedule() {
  try {
    const d = await api('/api/schedule');
    $('sched-last').textContent = d.last_run || '尚未运行';
    $('sched-next').textContent = d.enabled ? (d.next_run || '等待基准') : '未启用';
    $('sched-thread').textContent = !d.enabled
      ? '未启用' : (d.thread_alive ? '运行中' : '未启动');
    const r = d.last_result;
    $('sched-result').textContent = !r
      ? '-'
      : `${r.ok === false ? '失败' : '完成'} · 扫描 ${r.scanned ?? 0} / 命中 ${r.matched ?? 0}`
        + ` / 新建 ${r.jobs_created ?? 0}`
        + (r.error ? ` · ${r.error}` : '');
  } catch (e) { /* 静默：不影响设置页其它项 */ }
}

/* ---------------------------------------------------------------- 轮询 */

function startPolling() {
  stopPolling();
  S.timer = setInterval(() => {
    if (document.hidden) return;
    if (S.view === 'dash' && $('auto-refresh').checked) { loadOverview(); loadJobs(); }
    if (S.view === 'logs') loadLogs(false);
  }, 2500);
}

function stopPolling() { if (S.timer) clearInterval(S.timer); S.timer = null; }

/* ---------------------------------------------------------------- 启动 */

async function boot() {
  bindEvents();
  renderCatChips();      // 先渲染占位说明，再用上次落库的快照回显
  await loadCachedCategories(true);
  try {
    await loadSites();
    await loadOverview();
    await loadJobCategoryOptions();
    await loadJobs();
    await refreshHealth();
  } catch (e) { toast('初始化失败：' + e.message, 'err'); }
  // 默认扫描天数从配置读取
  try {
    const d = await api('/api/config');
    $('scan-days').value = d.config?.download?.scan_days ?? 30;
    $('sub-dir').value = d.config?.download?.manual_save_dir || '';
  } catch (e) { /* ignore */ }
  startPolling();
}

document.addEventListener('DOMContentLoaded', boot);
