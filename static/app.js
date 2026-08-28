/* ZenithCore Console — front-end controller */
'use strict';

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const THEME = {
  background: '#0a0c0e',
  foreground: '#d7dde3',
  cursor: '#58b09c',
  cursorAccent: '#0a0c0e',
  selectionBackground: '#1d3b35',
  black: '#0a0c0e',   brightBlack: '#5b636c',
  red: '#cf6a5a',     brightRed: '#e08877',
  green: '#58b09c',   brightGreen: '#7ccdb8',
  yellow: '#c9a227',  brightYellow: '#ddbb52',
  blue: '#6a9fd4',    brightBlue: '#8bbaea',
  magenta: '#a887c4', brightMagenta: '#c0a3da',
  cyan: '#5fb3b3',    brightCyan: '#7fcfcf',
  white: '#c3cad1',   brightWhite: '#e8edf2',
};

/* ------------------------------------------------------------- helpers */

function toast(message, isError) {
  const box = document.createElement('div');
  box.className = 'toast' + (isError ? ' err' : '');
  box.textContent = message;
  $('#toasts').appendChild(box);
  setTimeout(() => {
    box.style.opacity = '0';
    setTimeout(() => box.remove(), 200);
  }, 3200);
}

function bytes(n) {
  if (!n && n !== 0) return '—';
  const units = ['B', 'K', 'M', 'G', 'T'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n : n.toFixed(n < 10 ? 1 : 0)) + units[i];
}

function duration(sec) {
  if (sec == null) return '—';
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m ${sec % 60}s`;
}

function setBar(el, pct) {
  const value = Math.max(0, Math.min(100, pct || 0));
  el.style.width = value + '%';
  el.className = value > 90 ? 'crit' : value > 70 ? 'hot' : '';
}

async function copyText(text, label) {
  try {
    await navigator.clipboard.writeText(text);
    toast((label || 'Đã copy') + ' vào clipboard');
  } catch (_) {
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
    toast('Đã copy');
  }
}

/* -------------------------------------------------------------- session */

let sessionCounter = 0;
const sessions = new Map();
let activeId = null;

function wsUrl(path) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  return `${proto}://${location.host}${path}`;
}

function renderTabs() {
  const tabs = $('#tabs');
  tabs.innerHTML = '';
  sessions.forEach((s, id) => {
    const tab = document.createElement('div');
    tab.className = 'tab' + (id === activeId ? ' active' : '') +
      (s.state === 'connected' ? ' connected' : s.state === 'dead' ? ' dead' : '');
    tab.innerHTML = `<span class="live"></span><span class="label"></span><span class="x">&times;</span>`;
    tab.querySelector('.label').textContent = s.name;
    tab.addEventListener('mousedown', (e) => {
      if (e.target.classList.contains('x')) { e.stopPropagation(); closeSession(id); return; }
      focusSession(id);
    });
    tabs.appendChild(tab);
  });
  $('#sSessions').textContent = sessions.size + ' phiên';
  $('#termEmpty').classList.toggle('hidden', sessions.size > 0);
}

function focusSession(id) {
  activeId = id;
  sessions.forEach((s, key) => s.pane.classList.toggle('active', key === id));
  renderTabs();
  const s = sessions.get(id);
  if (s) {
    setTimeout(() => { s.fit.fit(); s.term.focus(); syncSize(s); }, 0);
    updateStatus(s.state);
  }
}

function syncSize(s) {
  $('#sSize').textContent = `${s.term.cols}x${s.term.rows}`;
  if (s.socket && s.socket.readyState === WebSocket.OPEN) {
    s.socket.send(JSON.stringify({ type: 'resize', rows: s.term.rows, cols: s.term.cols }));
  }
}

function updateStatus(state) {
  const dot = $('#sDot');
  dot.className = 'dot' + (state === 'connected' ? ' on' : state === 'dead' ? ' err' : '');
  $('#sState').textContent =
    state === 'connected' ? 'connected' : state === 'connecting' ? 'connecting…' : 'disconnected';
}

function newSession(name) {
  const id = ++sessionCounter;
  const pane = document.createElement('div');
  pane.className = 'term-pane';
  $('#termHost').appendChild(pane);

  const term = new Terminal({
    cursorBlink: true,
    cursorStyle: 'block',
    fontSize: 13,
    lineHeight: 1.25,
    letterSpacing: 0,
    fontFamily: '"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
    scrollback: 20000,
    allowProposedApi: true,
    macOptionIsMeta: true,
    theme: THEME,
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  try { term.loadAddon(new WebLinksAddon.WebLinksAddon()); } catch (_) {}
  term.open(pane);

  const s = { id, name: name || `bash ${id}`, term, fit, pane, socket: null, state: 'idle' };
  sessions.set(id, s);

  term.onData((data) => {
    if (s.socket && s.socket.readyState === WebSocket.OPEN) {
      s.socket.send(JSON.stringify({ type: 'input', data }));
    }
  });
  term.onResize(() => syncSize(s));

  focusSession(id);
  fit.fit();
  connectSession(s);
  return s;
}

function connectSession(s) {
  if (s.socket) { try { s.socket.close(); } catch (_) {} }
  s.state = 'connecting';
  renderTabs();
  if (s.id === activeId) updateStatus('connecting');

  const socket = new WebSocket(wsUrl('/ws/terminal'));
  s.socket = socket;

  socket.onopen = () => {
    s.fit.fit();
    socket.send(JSON.stringify({ type: 'auth', rows: s.term.rows, cols: s.term.cols }));
  };

  socket.onmessage = (event) => {
    let msg;
    try { msg = JSON.parse(event.data); } catch (_) { return; }
    if (msg.type === 'ready') {
      s.state = 'connected';
      renderTabs();
      if (s.id === activeId) { updateStatus('connected'); s.term.focus(); }
      if (msg.message) s.term.write(msg.message);
    } else if (msg.type === 'output') {
      s.term.write(msg.data || '');
    }
  };

  socket.onclose = (event) => {
    s.state = 'dead';
    renderTabs();
    if (s.id === activeId) updateStatus('dead');
    if (event.code === 1008) {
      showGate('Phiên hết hạn. Nhập lại token.');
    } else {
      s.term.write('\r\n\x1b[38;5;131m[phiên đã đóng]\x1b[0m\r\n');
    }
  };

  socket.onerror = () => { if (s.id === activeId) updateStatus('dead'); };
}

function closeSession(id) {
  const s = sessions.get(id);
  if (!s) return;
  try { s.socket && s.socket.close(); } catch (_) {}
  s.term.dispose();
  s.pane.remove();
  sessions.delete(id);
  if (activeId === id) {
    const next = sessions.keys().next();
    activeId = next.done ? null : next.value;
    if (activeId) focusSession(activeId); else { renderTabs(); updateStatus('idle'); }
  } else {
    renderTabs();
  }
}

window.addEventListener('resize', () => {
  const s = sessions.get(activeId);
  if (s) { s.fit.fit(); syncSize(s); }
});

/* ----------------------------------------------------------- navigation */

function switchView(name) {
  $$('.rail button[data-view]').forEach((b) => b.classList.toggle('active', b.dataset.view === name));
  $$('.view').forEach((v) => v.classList.toggle('active', v.dataset.view === name));
  if (name === 'terminal') {
    const s = sessions.get(activeId);
    if (s) setTimeout(() => { s.fit.fit(); s.term.focus(); }, 0);
  }
  if (name === 'ports') loadPorts();
  if (name === 'system') loadSystem();
  if (name === 'apps') loadApps();
  if (name === 'proxy') loadProxy();
  if (name === 'desktop') loadTunnel();
  if (name === 'jobs') { loadJobs(); loadProcesses(); }
}

/* ------------------------------------------------------------- api glue */

/** Wrapper around fetch that funnels 401s back to the auth gate and
 *  surfaces the server's error message instead of a generic failure. */
async function api(path, options) {
  const res = await fetch(path, options);
  if (res.status === 401) {
    showGate('Cần đăng nhập lại.');
    throw new Error('unauthenticated');
  }
  let data = {};
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) throw new Error(data.error || `Lỗi ${res.status}`);
  return data;
}

function postJson(path, body) {
  return api(path, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
}

/** Every mutating endpoint answers with a background job; show it and
 *  start following its log so the user sees progress immediately. */
function trackJob(data, note) {
  if (data && data.job) {
    toast(`${note || 'Đã khởi chạy'} — job #${data.job.id}`);
    openJobLog(data.job.id);
    loadJobs();
  } else {
    toast(note || 'Đã gửi lệnh');
  }
}

/* ---------------------------------------------------------------- ports */

let portTimer = null;

async function loadPorts() {
  try {
    const res = await fetch('/api/ports');
    if (res.status === 401) return showGate('Cần đăng nhập lại.');
    const data = await res.json();
    renderPorts(data.ports || []);
  } catch (_) {
    renderPorts([]);
  }
}

function renderPorts(ports) {
  const body = $('#portRows');
  if (!ports.length) {
    body.innerHTML = '<tr><td colspan="5" class="empty-row">Không có service nào đang lắng nghe</td></tr>';
    return;
  }
  body.innerHTML = '';
  ports.forEach((p) => {
    const tr = document.createElement('tr');
    const badge = p.self
      ? '<span class="pill self">console</span>'
      : '<span class="pill ok">listening</span>';
    tr.innerHTML = `
      <td class="mono" style="color:var(--fg)">${p.port}</td>
      <td class="mono">${escapeHtml(p.process || 'unknown')} ${badge}</td>
      <td class="mono num">${p.pid || '—'}</td>
      <td class="mono">${escapeHtml(p.address || '')}</td>
      <td class="act"></td>`;
    const cell = tr.querySelector('.act');

    if (!p.self) {
      const open = document.createElement('button');
      open.className = 'btn primary';
      open.textContent = 'Mở UI';
      open.onclick = () => window.open(`/p/${p.port}/`, '_blank');
      cell.appendChild(open);

      const copy = document.createElement('button');
      copy.className = 'btn ghost';
      copy.textContent = 'Link';
      copy.style.marginLeft = '6px';
      copy.onclick = () => copyText(`${location.origin}/p/${p.port}/`, 'Link');
      cell.appendChild(copy);

      const probe = document.createElement('button');
      probe.className = 'btn ghost';
      probe.textContent = 'Kiểm tra';
      probe.style.marginLeft = '6px';
      probe.onclick = async () => {
        probe.disabled = true;
        try {
          const r = await api(`/api/probe?port=${p.port}`);
          if (!r.open) toast(`Port ${p.port} không mở`, true);
          else if (!r.http) toast(`Port ${p.port} mở nhưng không phải HTTP`);
          else toast(`HTTP ${r.status} — ${r.title || r.server || 'không có tiêu đề'}`);
        } catch (err) {
          toast(err.message, true);
        } finally {
          probe.disabled = false;
        }
      };
      cell.appendChild(probe);

      const bridge = document.createElement('button');
      bridge.className = 'btn ghost';
      bridge.textContent = 'Bridge';
      bridge.style.marginLeft = '6px';
      bridge.onclick = () => {
        $('#bridgePort').value = p.port;
        $('#localPort').value = p.port;
        updateBridge();
        switchView('proxy');
      };
      cell.appendChild(bridge);
    }
    body.appendChild(tr);
  });
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* --------------------------------------------------------------- system */

async function loadSystem() {
  try {
    const [sysRes, stRes] = await Promise.all([fetch('/api/system'), fetch('/api/status')]);
    if (sysRes.status === 401) return showGate('Cần đăng nhập lại.');
    const sys = await sysRes.json();
    const st = await stRes.json();

    const cpu = sys.cpu_percent;
    $('#mCpu').innerHTML = cpu == null ? '—' : `${cpu}<small>%</small>`;
    setBar($('#bCpu'), cpu || 0);

    const memPct = sys.mem_total ? (sys.mem_used / sys.mem_total) * 100 : 0;
    $('#mMem').innerHTML = `${bytes(sys.mem_used)}<small>/ ${bytes(sys.mem_total)}</small>`;
    setBar($('#bMem'), memPct);

    const d = sys.disk || {};
    const diskPct = d.total ? (d.used / d.total) * 100 : 0;
    $('#mDisk').innerHTML = `${bytes(d.used)}<small>/ ${bytes(d.total)}</small>`;
    setBar($('#bDisk'), diskPct);

    $('#mUp').textContent = duration(sys.uptime);

    const rows = [
      ['Hostname', st.hostname],
      ['Debian', st.debian],
      ['Kernel', st.kernel],
      ['Kiến trúc', st.arch],
      ['CPU cores', sys.cpu_count],
      ['Load average', (sys.load || []).join('  ')],
      ['Shell', st.shell],
      ['systemctl', st.systemctl ? 'có' : 'không'],
      ['Cổng public', st.public_port],
    ];
    $('#sysRows').innerHTML = rows.map(([k, v]) =>
      `<tr><td style="width:190px;color:var(--fg-faint)">${k}</td><td class="mono">${escapeHtml(v ?? '—')}</td></tr>`
    ).join('');
  } catch (_) {}
}

/* ----------------------------------------------------------------- apps */

async function loadApps() {
  let data;
  try { data = await api('/api/apps'); } catch (_) { return; }

  const warn = $('#appsNoZenith');
  if (warn) warn.classList.toggle('hidden', data.zenith !== false);

  const host = $('#appCards');
  if (!host) return;
  host.innerHTML = '';

  (data.apps || []).forEach((app) => {
    const card = document.createElement('div');
    card.className = 'panel';

    const state = app.running
      ? '<span class="pill ok">đang chạy</span>'
      : app.installed
        ? '<span class="pill">đã cài</span>'
        : '<span class="pill">chưa cài</span>';

    const extra = app.extra || {};
    const creds = [];
    if (extra.user) creds.push(`user: ${escapeHtml(extra.user)}`);
    if (extra.password) creds.push(`mật khẩu: ${escapeHtml(extra.password)}`);

    card.innerHTML = `
      <div class="panel-body">
        <div class="field" style="margin-bottom:8px">
          <label style="font-size:12px;color:var(--fg)">
            ${escapeHtml(app.name)} <span class="pill">${escapeHtml(app.tag || '')}</span> ${state}
          </label>
        </div>
        <div class="hint" style="margin-bottom:10px">${escapeHtml(app.desc || '')}</div>
        <div class="row" style="margin-bottom:10px">
          <div class="field">
            <label>Port</label>
            <input type="number" class="app-port" min="1" max="65535" value="${app.port}" />
          </div>
          <div class="field">
            <label>Mật khẩu (tuỳ chọn)</label>
            <input type="password" class="app-pass" placeholder="tự sinh nếu bỏ trống" />
          </div>
        </div>
        ${creds.length ? `<div class="hint" style="margin-bottom:10px">${creds.join(' · ')}</div>` : ''}
        <div class="hint" style="margin-bottom:10px">Dung lượng ${escapeHtml(app.size || '—')}</div>
        <div class="row act-row"></div>
      </div>`;

    const row = card.querySelector('.act-row');
    const portOf = () => parseInt(card.querySelector('.app-port').value, 10) || app.port;
    const passOf = () => card.querySelector('.app-pass').value.trim();

    const act = (label, action, cls) => {
      const b = document.createElement('button');
      b.className = 'btn ' + (cls || '');
      b.textContent = label;
      b.onclick = async () => {
        b.disabled = true;
        try {
          const body = { id: app.id, port: portOf() };
          const pass = passOf();
          if (pass) body.password = pass;
          trackJob(await postJson(`/api/apps/${action}`, body), `${label} ${app.name}`);
        } catch (err) {
          toast(err.message, true);
        } finally {
          b.disabled = false;
          setTimeout(loadApps, 1500);
        }
      };
      row.appendChild(b);
    };

    if (app.running && app.web) {
      const open = document.createElement('button');
      open.className = 'btn primary';
      open.textContent = 'Mở';
      open.onclick = () => window.open(app.url, '_blank');
      row.appendChild(open);
    }
    act('Chạy', 'start', app.running ? '' : 'primary');
    if (!app.installed) act('Cài', 'install');
    if (app.running) act('Dừng', 'stop');
    if (app.installed) act('Gỡ', 'remove', 'danger');

    host.appendChild(card);
  });

  if (!host.children.length) {
    host.innerHTML = '<div class="panel"><div class="panel-body hint">Danh mục trống.</div></div>';
  }
}

async function installPackages() {
  const input = $('#pkgNames');
  const raw = input.value.trim();
  if (!raw) return toast('Nhập tên gói trước', true);
  const btn = $('#pkgInstall');
  btn.disabled = true;
  try {
    trackJob(await postJson('/api/pkg', { packages: raw }), 'Cài gói');
    input.value = '';
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
  }
}

/* ---------------------------------------------------------- proxy state */

async function loadProxy() {
  let data;
  try { data = await api('/api/proxy'); } catch (_) { return; }

  const body = $('#proxyRows');
  if (body) {
    body.innerHTML = '';
    (data.proxies || []).forEach((p) => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${escapeHtml(p.name)}</td>
        <td><input type="number" class="px-port" min="1" max="65535" value="${p.port}" /></td>
        <td>${p.running ? '<span class="pill ok">đang chạy</span>' : '<span class="pill">tắt</span>'}</td>
        <td class="act"></td>`;
      const cell = tr.querySelector('.act');

      const start = document.createElement('button');
      start.className = 'btn primary';
      start.textContent = p.running ? 'Khởi động lại' : 'Bật';
      start.onclick = async () => {
        start.disabled = true;
        try {
          const payload = {
            kind: p.kind,
            port: parseInt(tr.querySelector('.px-port').value, 10) || p.port,
          };
          const u = $('#pxUser').value.trim();
          const pw = $('#pxPass').value.trim();
          if (u || pw) { payload.user = u; payload.password = pw; }
          trackJob(await postJson('/api/proxy/start', payload), `Bật ${p.name}`);
        } catch (err) {
          toast(err.message, true);
        } finally {
          start.disabled = false;
          setTimeout(loadProxy, 2000);
        }
      };
      cell.appendChild(start);

      const stop = document.createElement('button');
      stop.className = 'btn danger';
      stop.style.marginLeft = '6px';
      stop.textContent = 'Dừng';
      stop.onclick = async () => {
        stop.disabled = true;
        try {
          trackJob(await postJson('/api/proxy/stop', { kind: p.kind }), `Dừng ${p.name}`);
        } catch (err) {
          toast(err.message, true);
        } finally {
          stop.disabled = false;
          setTimeout(loadProxy, 2000);
        }
      };
      cell.appendChild(stop);

      body.appendChild(tr);
    });
    if (!body.children.length) {
      body.innerHTML = '<tr><td colspan="4" class="empty-row">Không có loại proxy nào</td></tr>';
    }
  }

  const out = $('#outCurrent');
  if (out) {
    const url = (data.outbound && (data.outbound.url || data.outbound.proxy)) || '';
    out.textContent = url ? `Đang dùng: ${url}` : 'Chưa cấu hình.';
  }
}

async function setOutbound(action) {
  const input = $('#outUrl');
  const payload = action === 'clear'
    ? { action: 'clear' }
    : { action: 'set', url: input.value.trim() };
  if (action !== 'clear' && !payload.url) return toast('Nhập URL proxy trước', true);
  try {
    trackJob(await postJson('/api/proxy/outbound', payload),
      action === 'clear' ? 'Xoá proxy đi ra' : 'Đặt proxy đi ra');
    if (action === 'clear') input.value = '';
  } catch (err) {
    toast(err.message, true);
  } finally {
    setTimeout(loadProxy, 1500);
  }
}

/* --------------------------------------------------------------- tunnel */

async function loadTunnel() {
  let data;
  try { data = await api('/api/tunnel'); } catch (_) { return; }

  const noCf = $('#tunnelNoCf');
  if (noCf) noCf.classList.toggle('hidden', data.available !== false);

  const state = $('#tunnelState');
  if (state) {
    state.textContent = data.running ? `đang chạy (${data.mode || '?'})` : 'tắt';
    state.className = 'pill' + (data.running ? ' ok' : '');
  }

  const wrap = $('#tunnelUrlWrap');
  const urlEl = $('#tunnelUrl');
  if (wrap && urlEl) {
    if (data.running && data.url) {
      urlEl.textContent = data.url;
      wrap.hidden = false;
    } else {
      wrap.hidden = true;
    }
  }

  const startBtn = $('#tunnelStart');
  const stopBtn = $('#tunnelStop');
  if (startBtn) startBtn.disabled = data.available === false;
  if (stopBtn) stopBtn.disabled = !data.running;
}

async function startTunnel() {
  const btn = $('#tunnelStart');
  btn.disabled = true;
  try {
    const target = $('#tunnelTarget').value.trim();
    trackJob(await postJson('/api/tunnel/start', target ? { target } : {}), 'Khởi động tunnel');
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
    setTimeout(loadTunnel, 2500);
  }
}

async function stopTunnel() {
  const btn = $('#tunnelStop');
  btn.disabled = true;
  try {
    trackJob(await postJson('/api/tunnel/stop', {}), 'Dừng tunnel');
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
    setTimeout(loadTunnel, 2000);
  }
}

/* ----------------------------------------------------------------- jobs */

let followJobId = null;
let followOffset = 0;

async function loadJobs() {
  let data;
  try { data = await api('/api/jobs'); } catch (_) { return; }

  const body = $('#jobRows');
  if (!body) return;
  const jobs = data.jobs || [];
  if (!jobs.length) {
    body.innerHTML = '<tr><td colspan="5" class="empty-row">Chưa có job nào</td></tr>';
    return;
  }
  body.innerHTML = '';
  jobs.slice().reverse().forEach((j) => {
    const tr = document.createElement('tr');
    const running = j.status === 'running';
    const pill = running
      ? '<span class="pill ok">đang chạy</span>'
      : j.exit_code === 0
        ? '<span class="pill">xong</span>'
        : `<span class="pill self">${escapeHtml(j.status || 'lỗi')}</span>`;
    tr.innerHTML = `
      <td class="mono num">${j.id}</td>
      <td class="mono">${escapeHtml(j.label || '')}</td>
      <td>${pill}</td>
      <td class="mono num">${j.exit_code == null ? '—' : j.exit_code}</td>
      <td class="act"></td>`;
    const cell = tr.querySelector('.act');

    const log = document.createElement('button');
    log.className = 'btn';
    log.textContent = 'Log';
    log.onclick = () => openJobLog(j.id);
    cell.appendChild(log);

    if (running) {
      const stop = document.createElement('button');
      stop.className = 'btn danger';
      stop.style.marginLeft = '6px';
      stop.textContent = 'Dừng';
      stop.onclick = () => stopJob(j.id);
      cell.appendChild(stop);
    }
    body.appendChild(tr);
  });
}

function openJobLog(id) {
  followJobId = id;
  followOffset = 0;
  const panel = $('#jobLogPanel');
  if (!panel) return;
  panel.classList.remove('hidden');
  $('#jobLogTitle').textContent = `Log job #${id}`;
  $('#jobLog').textContent = '';
  pollJobLog();
}

async function pollJobLog() {
  if (followJobId == null) return;
  const id = followJobId;
  let data;
  try { data = await api(`/api/jobs/${id}?offset=${followOffset}`); } catch (_) { return; }
  if (followJobId !== id) return;

  const pre = $('#jobLog');
  const lines = data.log || [];
  if (lines.length) {
    pre.textContent += lines.join('\n') + '\n';
    pre.scrollTop = pre.scrollHeight;
  }
  followOffset = data.offset || followOffset;

  const stopBtn = $('#jobLogStop');
  if (stopBtn) stopBtn.disabled = data.status !== 'running';

  if (data.status === 'running') {
    setTimeout(pollJobLog, 1200);
  } else {
    pre.textContent += `\n[kết thúc — ${data.status}, mã ${data.exit_code == null ? '?' : data.exit_code}]\n`;
    pre.scrollTop = pre.scrollHeight;
    loadJobs();
  }
}

async function stopJob(id) {
  try {
    await postJson(`/api/jobs/${id}/stop`, {});
    toast(`Đã yêu cầu dừng job #${id}`);
  } catch (err) {
    toast(err.message, true);
  } finally {
    loadJobs();
  }
}

async function runExec() {
  const input = $('#execCmd');
  const command = input.value.trim();
  if (!command) return toast('Nhập lệnh trước', true);
  const btn = $('#execRun');
  btn.disabled = true;
  try {
    trackJob(await postJson('/api/exec', { command }), 'Chạy lệnh');
    input.value = '';
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
  }
}

/* ------------------------------------------------------------ processes */

async function loadProcesses() {
  let data;
  try { data = await api('/api/processes'); } catch (_) { return; }

  const body = $('#procRows');
  if (!body) return;
  const rows = (data.processes || []).slice(0, 40);
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="5" class="empty-row">Không đọc được /proc</td></tr>';
    return;
  }
  body.innerHTML = '';
  rows.forEach((p) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="mono num">${p.pid}</td>
      <td class="mono">${escapeHtml(p.name || '')}</td>
      <td class="mono" style="max-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
          title="${escapeHtml(p.cmd || '')}">${escapeHtml(p.cmd || '')}</td>
      <td class="mono num">${bytes(p.rss)}</td>
      <td class="act"></td>`;
    const cell = tr.querySelector('.act');

    const term = document.createElement('button');
    term.className = 'btn';
    term.textContent = 'Kết thúc';
    term.onclick = () => killProcess(p.pid, false);
    cell.appendChild(term);

    const force = document.createElement('button');
    force.className = 'btn danger';
    force.style.marginLeft = '6px';
    force.textContent = 'Buộc';
    force.onclick = () => killProcess(p.pid, true);
    cell.appendChild(force);

    body.appendChild(tr);
  });
}

async function killProcess(pid, force) {
  try {
    await postJson('/api/kill', { pid, force });
    toast(`Đã gửi tín hiệu tới PID ${pid}`);
  } catch (err) {
    toast(err.message, true);
  } finally {
    setTimeout(loadProcesses, 800);
  }
}

/* --------------------------------------------------------------- bridge */

function updateBridge() {
  const remote = parseInt($('#bridgePort').value, 10) || 1080;
  const local = parseInt($('#localPort').value, 10) || remote;
  const url = `${location.origin.replace(/^http/, 'ws')}/ws/tcp?port=${remote}`;

  $('#bridgeCmd').innerHTML =
    `curl -O ${location.origin}/static/zenith-bridge.js\n` +
    `<b>node zenith-bridge.js ${local} "${url}" "&lt;CONSOLE_TOKEN&gt;"</b>`;

  $('#pyBridge').innerHTML =
    `curl -o zenith-bridge.py ${location.origin}/static/zenith-bridge.py\n` +
    `pip install websockets\n` +
    `<b>python3 zenith-bridge.py --local ${local} --url ${url} --token &lt;CONSOLE_TOKEN&gt;</b>`;

  $('#proxyTarget').textContent = `socks5://127.0.0.1:${local}`;
  $('#verifyCmd').textContent = `curl -x socks5h://127.0.0.1:${local} https://ifconfig.me`;
}

/* ----------------------------------------------------------------- gate */

function showGate(message) {
  $('#gate').classList.remove('hidden');
  $('#shell').classList.add('hidden');
  $('#gateErr').textContent = message || '';
  setTimeout(() => $('#tokenInput').focus(), 30);
}

function enterShell() {
  $('#gate').classList.add('hidden');
  $('#shell').classList.remove('hidden');
  if (!sessions.size) newSession();
  loadStatusBar();
  loadPorts();
  loadSystem();
  updateBridge();
  if (!portTimer) {
    portTimer = setInterval(() => {
      const isActive = (v) => {
        const el = $(`.view[data-view=${v}]`);
        return el && el.classList.contains('active');
      };
      if ($('#autoPorts') && $('#autoPorts').checked && isActive('ports')) loadPorts();
      if (isActive('system')) loadSystem();
      if ($('#autoJobs') && $('#autoJobs').checked && isActive('jobs')) {
        loadJobs();
        loadProcesses();
      }
    }, 5000);
  }
}

async function loadStatusBar() {
  try {
    const st = await (await fetch('/api/status')).json();
    $('#sHost').textContent = st.hostname || '—';
    $('#sDebian').textContent = 'debian ' + (st.debian || '?');
  } catch (_) {}
}

/* ----------------------------------------------------------------- wire */

document.addEventListener('DOMContentLoaded', () => {
  $$('.rail button[data-view]').forEach((b) =>
    b.addEventListener('click', () => switchView(b.dataset.view)));

  $('#newTabBtn').onclick = () => { switchView('terminal'); newSession(); };
  $('#emptyNewTab').onclick = () => newSession();
  $('#clearBtn').onclick = () => { const s = sessions.get(activeId); if (s) { s.term.clear(); s.term.focus(); } };
  $('#reconnectBtn').onclick = () => { const s = sessions.get(activeId); if (s) connectSession(s); };

  $('#logoutBtn').onclick = async () => {
    await fetch('/api/logout', { method: 'POST' });
    sessions.forEach((_, id) => closeSession(id));
    showGate('Đã đăng xuất.');
  };

  $('#refreshPorts').onclick = loadPorts;
  $('#refreshSys').onclick = loadSystem;

  /* apps */
  $('#refreshApps').onclick = loadApps;
  $('#pkgInstall').onclick = installPackages;
  $('#pkgNames').addEventListener('keydown', (e) => { if (e.key === 'Enter') installPackages(); });

  /* proxy manager */
  $('#refreshProxy').onclick = loadProxy;
  $('#pxStopAll').onclick = async () => {
    try {
      trackJob(await postJson('/api/proxy/stop', { kind: 'all' }), 'Dừng toàn bộ proxy');
    } catch (err) {
      toast(err.message, true);
    } finally {
      setTimeout(loadProxy, 2000);
    }
  };
  $('#outSet').onclick = () => setOutbound('set');
  $('#outClear').onclick = () => setOutbound('clear');
  $('#outUrl').addEventListener('keydown', (e) => { if (e.key === 'Enter') setOutbound('set'); });

  /* jobs */
  $('#refreshJobs').onclick = () => { loadJobs(); loadProcesses(); };
  $('#execRun').onclick = runExec;
  $('#execCmd').addEventListener('keydown', (e) => { if (e.key === 'Enter') runExec(); });
  $('#jobLogClose').onclick = () => {
    followJobId = null;
    $('#jobLogPanel').classList.add('hidden');
  };
  $('#jobLogStop').onclick = () => { if (followJobId != null) stopJob(followJobId); };

  $('#openManual').onclick = () => {
    const p = parseInt($('#manualPort').value, 10);
    if (!p || p < 1 || p > 65535) return toast('Port không hợp lệ', true);
    window.open(`/p/${p}/`, '_blank');
  };
  $('#copyManual').onclick = () => {
    const p = parseInt($('#manualPort').value, 10);
    if (!p) return toast('Nhập port trước', true);
    copyText(`${location.origin}/p/${p}/`, 'Link');
  };

  $('#bridgePort').addEventListener('input', updateBridge);
  $('#localPort').addEventListener('input', updateBridge);

  $('#openDesktop').onclick = () => window.open('/p/6080/vnc.html?autoconnect=1&resize=remote', '_blank');
  $('#runDesktop').onclick = async () => {
    const btn = $('#runDesktop');
    btn.disabled = true;
    try {
      // Start it as a tracked background job so the install log is visible
      // in the Jobs view instead of only inside a terminal session.
      trackJob(await postJson('/api/apps/start', { id: 'desktop' }), 'Khởi động desktop');
      switchView('jobs');
    } catch (err) {
      toast(err.message, true);
    } finally {
      btn.disabled = false;
    }
  };

  /* public tunnel */
  $('#refreshTunnel').onclick = loadTunnel;
  $('#tunnelStart').onclick = startTunnel;
  $('#tunnelStop').onclick = stopTunnel;
  $('#tunnelTarget').addEventListener('keydown', (e) => { if (e.key === 'Enter') startTunnel(); });

  document.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-copy]');
    if (!btn) return;
    const el = document.getElementById(btn.dataset.copy);
    if (el) copyText(el.innerText.trim());
  });

  document.addEventListener('keydown', (e) => {
    if (e.ctrlKey && e.shiftKey && e.code === 'KeyT') { e.preventDefault(); switchView('terminal'); newSession(); }
    if (e.ctrlKey && e.shiftKey && e.code === 'KeyW') { e.preventDefault(); if (activeId) closeSession(activeId); }
  });

  $('#gateForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = $('#gateBtn');
    btn.disabled = true;
    $('#gateErr').textContent = '';
    try {
      const res = await fetch('/api/login', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ token: $('#tokenInput').value }),
      });
      if (!res.ok) {
        $('#gateErr').textContent = 'Token không đúng.';
      } else {
        $('#tokenInput').value = '';
        enterShell();
      }
    } catch (_) {
      $('#gateErr').textContent = 'Không kết nối được tới server.';
    } finally {
      btn.disabled = false;
    }
  });

  fetch('/api/status')
    .then((r) => r.json())
    .then((st) => { if (st.authenticated) enterShell(); else showGate(''); })
    .catch(() => showGate(''));
});
