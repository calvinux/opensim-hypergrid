'use strict';
/* Console web OpenSim v2 : tout le texte dynamique passe par textContent (jamais innerHTML). */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const tr = window.WC_I18N.tr;
const trText = window.WC_I18N.trText;
const trf = window.WC_I18N.trf;
function translateServerError(value) {
  const text = String(value);
  const patterns = [
    [/^extension non acceptée \((.*)\)$/, 'extension non acceptée ({0})'],
    [/^fichier trop gros \(limite (\d+) Mo\)$/, 'fichier trop gros (limite {0} Mo)'],
    [/^erreur d'écriture: ([\s\S]*)$/, "erreur d'écriture: {0}"],
    [/^erreur: ([\s\S]*)$/, 'erreur: {0}'],
  ];
  for (const [pattern, source] of patterns) {
    const match = pattern.exec(text);
    if (match) return trf(source, match[1]);
  }
  return tr(text);
}
const msg = (source, ...args) => ({ __i18nTemplate: source, __i18nArgs: args });
function setLocalizedText(element, source, ...args) {
  element.dataset.i18nSource = source;
  element.dataset.i18nArgs = JSON.stringify(args);
  element.textContent = trf(source, ...args);
}
function setLocalizedAttribute(element, attribute, source, ...args) {
  element.dataset['i18n' + attribute[0].toUpperCase() + attribute.slice(1)] = source;
  if (args.length) element.dataset['i18nArgs' + attribute[0].toUpperCase() + attribute.slice(1)] = JSON.stringify(args);
  element.setAttribute(attribute, trf(source, ...args));
}

function h(tag, props, ...kids) {
  const e = document.createElement(tag);
  if (props) for (const [k, v] of Object.entries(props)) {
    if (k === 'class') e.className = v;
    else if (k === 'text') {
      const source = v && v.__i18nTemplate ? v.__i18nTemplate : String(v);
      const args = v && v.__i18nTemplate ? v.__i18nArgs : [];
      e.dataset.i18nSource = source;
      if (args.length) e.dataset.i18nArgs = JSON.stringify(args);
      e.textContent = trf(source, ...args);
    }
    else if (k.startsWith('on')) e[k] = v;
    else if (v !== false && v != null) {
      if (['placeholder', 'aria-label', 'title'].includes(k)) {
          const source = v && v.__i18nTemplate ? v.__i18nTemplate : String(v);
          const args = v && v.__i18nTemplate ? v.__i18nArgs : [];
          e.setAttribute('data-i18n-' + k, source);
          if (args.length) e.setAttribute('data-i18n-args-' + k, JSON.stringify(args));
          e.setAttribute(k, trf(source, ...args));
      } else e.setAttribute(k, v === true ? '' : v);
    }
  }
  for (const k of kids.flat()) if (k != null && k !== false) {
    if (k.nodeType) e.append(k);
    else if (k && k.__i18nTemplate) {
      const text = document.createTextNode(trf(k.__i18nTemplate, ...k.__i18nArgs));
      text.__i18nSource = k.__i18nTemplate;
      text.__i18nArgs = k.__i18nArgs;
      e.append(text);
    }
    else {
      const text = document.createTextNode(trText(k));
      text.__i18nSource = String(k);
      e.append(text);
    }
  }
  return e;
}
const fmtBytes = n => {
  if (n == null) return '—';
  const u = window.WC_I18N.language === 'fr' ? ['o', 'Ko', 'Mo', 'Go', 'To'] : ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(n < 10 ? 1 : 0) : n) + ' ' + u[i];
};
const fmtDur = s => {
  if (s == null) return '—';
  const d = Math.floor(s / 86400), hh = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  const units = { fr: ['j', 'h', 'min'], en: ['d', 'h', 'min'], de: ['T', 'Std.', 'Min.'], it: ['g', 'h', 'min'] }[window.WC_I18N.language];
  return (d ? d + ' ' + units[0] + ' ' : '') + (hh || d ? hh + ' ' + units[1] + ' ' : '') + m + ' ' + units[2];
};
const fmtDate = ts => ts ? new Date(ts * 1000).toLocaleString({ fr: 'fr-FR', en: 'en-GB', de: 'de-DE', it: 'it-IT' }[window.WC_I18N.language]) : '—';

function toast(msg, kind) {
  const t = h('div', { class: 'toast ' + (kind || ''), role: 'status' }, msg);
  $('#toasts').append(t);
  setTimeout(() => t.remove(), kind === 'bad' ? 7000 : 3500);
}
async function copyText(txt) {
  try { await navigator.clipboard.writeText(txt); toast(tr('Copié'), 'ok'); return; }
  catch (e) {
    const ta = h('textarea', { 'aria-label': 'Texte à copier' });
    ta.value = txt; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.append(ta);
    try {
      ta.select();
      if (!document.execCommand('copy')) throw new Error('copy failed');
      toast(tr('Copié'), 'ok');
    } catch (e2) {
      toast(tr('Copie impossible : sélectionnez et copiez le texte manuellement.'), 'bad');
    } finally { ta.remove(); }
  }
}
async function api(path, { method = 'GET', body } = {}) {
  const o = { method, headers: { 'X-Requested-With': 'webconsole' }, credentials: 'same-origin' };
  if (body !== undefined) { o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(body); }
  const r = await fetch(path, o);
  if (r.status === 401 && !path.startsWith('/api/login')) { showLogin(); throw new Error(tr('Session expirée')); }
  let d = null; try { d = await r.json(); } catch (e) { /* corps vide */ }
  if (!r.ok) throw new Error(translateServerError((d && d.error) || trf('Erreur {0}', r.status)));
  return d;
}

/* ---------- session ---------- */
let me = null, tab = 'dashboard', logTimer = null, statTimer = null;

async function boot() {
  try {
    const c = await (await fetch('/api/config')).json();
    const g = c.grid || 'OpenSim';
    $('#loginTitle').textContent = g; $('#brandName').textContent = g; document.title = g + ' · ' + tr('Console');
    if (c.totp) $('#lcWrap').classList.remove('hidden');
  } catch (e) { /* ignoré */ }
  try { me = await api('/api/me'); startApp(); } catch (e) { showLogin(); }
}
function showLogin() {
  clearInterval(logTimer); clearInterval(statTimer);
  $('#app').classList.add('hidden'); $('#login').classList.remove('hidden'); $('#lu').focus();
}
function startApp() {
  $('#login').classList.add('hidden'); $('#app').classList.remove('hidden');
  const admin = me.role === 'admin';
  document.body.classList.toggle('viewer', !admin);
  $('#meName').textContent = me.user; $('#meRole').textContent = admin ? 'admin' : tr('Lecteur');
  clearInterval(logTimer); clearInterval(statTimer);
  logTimer = setInterval(pollLog, 1500); statTimer = setInterval(() => { if (tab === 'dashboard') loadStatus(); }, 5000);
  pollLog(); show(tab === 'audit' && !admin ? 'dashboard' : tab);
}
$('#loginform').onsubmit = async ev => {
  ev.preventDefault(); $('#lerr').textContent = '';
  try {
    await api('/api/login', { method: 'POST', body: { user: $('#lu').value, pass: $('#lp').value, code: $('#lc').value } });
    $('#lp').value = ''; $('#lc').value = '';
    me = await api('/api/me'); startApp();
  } catch (e) { $('#lerr').textContent = e.message; }
};
$('#logout').onclick = async () => { try { await api('/api/logout', { method: 'POST', body: {} }); } catch (e) { /* ignoré */ } showLogin(); };

function show(t) {
  tab = t;
  $$('.tab').forEach(s => s.classList.toggle('hidden', s.id !== 'tab-' + t));
  $$('.nav').forEach(b => b.classList.toggle('active', b.dataset.tab === t));
  ({ dashboard: loadStatus, users: loadUsers, backups: loadBackups, xfer: loadXfer, regions: loadRegions, 'region-map': loadRegionMap, grid: loadGrid, audit: loadAudit,
     console: () => { renderTail(null, true); $('#cmd').focus(); } })[t]();
}
$$('.nav').forEach(b => b.onclick = () => show(b.dataset.tab));
['#language', '#loginLanguage'].forEach(selector => { $(selector).onchange = e => window.WC_I18N.setLanguage(e.target.value); });
window.addEventListener('webconsole-language-change', () => {
  if (me) $('#meRole').textContent = me.role === 'admin' ? 'admin' : tr('Lecteur');
  if (tab === 'dashboard' && lastStatus) renderStatus(lastStatus);
  if (tab === 'console') renderTail(null, true);
  if (tab === 'backups') loadBackups();
  if (tab === 'regions') loadRegions();
  if (tab === 'region-map') loadRegionMap();
  if (tab === 'xfer') loadXferLists();
  if (tab === 'grid') loadGrid();
  if (tab === 'audit') loadAudit();
});
$$('[data-tab="audit"],[data-tab="xfer"],[data-tab="regions"],[data-tab="region-map"]').forEach(b => b.classList.add('adminonly'));

const ICONS = {
  dash: 'M3 3h7v7H3z M14 3h7v7h-7z M14 14h7v7h-7z M3 14h7v7H3z', term: 'M4 17l6-6-6-6 M12 19h8',
  users: 'M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2 M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z M23 21v-2a4 4 0 0 0-3-3.87 M16 3.13a4 4 0 0 1 0 7.75',
  save: 'M21 8v13H3V8 M1 3h22v5H1z M10 12h4', map: 'M1 6v16l7-4 8 4 7-4V2l-7 4-8-4-7 4z M8 2v16 M16 6v16',
  file: 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z M14 2v6h6 M12 18v-6 M9 15l3 3 3-3',
  net: 'M18 8a3 3 0 1 0 0-6 3 3 0 0 0 0 6z M6 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z M18 22a3 3 0 1 0 0-6 3 3 0 0 0 0 6z M8.6 13.5l6.8 4 M15.4 6.5l-6.8 4',
  shield: 'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z', server: 'M2 3h20v7H2z M2 14h20v7H2z M6 6.5h.01 M6 17.5h.01',
  cpu: 'M4 4h16v16H4z M9 9h6v6H9z M9 1v3 M15 1v3 M9 20v3 M15 20v3 M20 9h3 M20 14h3 M1 9h3 M1 14h3',
  disk: 'M22 12H2 M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z M6 16h.01 M10 16h.01',
  db: 'M12 8c4.97 0 9-1.34 9-3s-4.03-3-9-3-9 1.34-9 3 4.03 3 9 3z M3 5v14c0 1.66 4.03 3 9 3s9-1.34 9-3V5 M3 12c0 1.66 4.03 3 9 3s9-1.34 9-3',
  msg: 'M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z', kick: 'M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2 M8.5 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z M18 8l5 5 M23 8l-5 5',
  pin: 'M21 10c0 7-9 13-9 13S3 17 3 10a9 9 0 0 1 18 0z M12 13a3 3 0 1 0 0-6 3 3 0 0 0 0 6z', clock: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z M12 6v6l4 2',
};
function ico(name) {
  const ns = 'http://www.w3.org/2000/svg', s = document.createElementNS(ns, 'svg');
  s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('class', 'ico'); s.setAttribute('aria-hidden', 'true');
  for (const d of ICONS[name].split(' M').map((x, i) => i ? 'M' + x : x)) {
    const p = document.createElementNS(ns, 'path'); p.setAttribute('d', d); s.append(p);
  }
  return s;
}
const NAVI = { dashboard: 'dash', console: 'term', users: 'users', backups: 'save', regions: 'map', 'region-map': 'map', xfer: 'file', grid: 'net', audit: 'shield' };
$$('.nav').forEach(b => b.prepend(ico(NAVI[b.dataset.tab] || 'dash')));
$('.nav[data-tab="console"]').append(h('span', { class: 'live', text: 'LIVE' }));
$('#language').onchange = e => window.WC_I18N.setLanguage(e.target.value);
window.addEventListener('webconsole-language-change', () => {
  if (tab === 'dashboard' && lastStatus) renderStatus(lastStatus);
  if (tab === 'console') renderTail(null, true);
});

/* ---------- composants ---------- */
const card = (title, ...kids) => h('div', { class: 'card' }, h('h3', { text: title }), ...kids);
const badge = (t, kind) => h('span', { class: 'badge ' + (kind || '') }, t);
function kv(rows) {
  const dl = h('dl', { class: 'kv' });
  for (const [k, v] of rows) dl.append(h('dt', { text: k }), h('dd', null, v == null || v === '' ? '—' : v));
  return dl;
}
function meter(frac, color) {
  const pct = Math.max(0, Math.min(100, Math.round((frac || 0) * 100)));
  const i = h('i', { class: pct > 90 ? 'bad' : pct > 75 ? 'warn' : color ? 'c-' + color : '' });
  i.style.width = pct + '%';
  return h('div', { class: 'bar' }, i);
}
function table(head, rows, rightCols = []) {
  return h('div', { class: 'tablewrap' }, h('table', null,
    h('thead', null, h('tr', null, head.map((t, i) => h('th', { class: rightCols.includes(i) ? 'r' : '', text: t })))),
    h('tbody', null, rows.map(r => h('tr', null, r.map((c, i) => h('td', { class: rightCols.includes(i) ? 'r' : '' }, c)))))));
}
const linkRow = (label, value) => h('div', { class: 'linkrow' }, h('span', { class: 'lbl', text: label }), h('code', { text: value }),
  h('button', { class: 'btn small', onclick: () => copyText(value) }, 'Copier'));

/* ---------- tableau de bord ---------- */
let lastStatus = null;
async function loadStatus() {
  try { lastStatus = await api('/api/status'); renderStatus(lastStatus); } catch (e) { if (e.message !== 'Session expirée') toast(e.message, 'bad'); }
}
function renderStatus(s) {
  if (['rmin', 'rtxt'].includes(document.activeElement && document.activeElement.id)) return;   // ne pas écraser une saisie en cours
  const o = s.opensim, sy = s.system || {}, al = s.alerts || {}, up = !!o.running, admin = me.role === 'admin';
  $('#brandDot').className = 'dot ' + (up && s.console_ok ? 'ok' : 'bad');
  $('#brandHost').textContent = `${s.host}:${s.port}`;
  const gp = $('#gridPill'); gp.className = 'pill ' + (s.grid ? 'ok' : 'bad'); gp.textContent = tr(s.grid ? 'Grille en ligne' : 'Grille injoignable');
  const memUsed = sy.mem_total ? (sy.mem_total - sy.mem_avail) / sy.mem_total : 0;
  const diskUsed = sy.disk_total ? (sy.disk_total - sy.disk_free) / sy.disk_total : 0;
  const dbs = s.dbs || [], dbTotal = dbs.reduce((t, d) => t + d.size, 0);
  const next = s.restart_job ? msg('{0} dans {1} (par {2})', s.restart_job.text, fmtDur(s.restart_job.remaining), s.restart_job.who)
  : (al.daily ? msg('Chaque jour à {0}', al.daily) : 'Aucun');
  const hero = h('div', { class: 'card hero' },
    h('div', { class: 'hl' }, h('span', { class: 'ib' }, ico('server')),
      h('div', null, h('div', { class: 'ht' }, s.grid_name, h('span', { class: 'tag', text: 'PID ' + (o.pid || '—') })),
        h('div', { class: 'muted mono sm', text: msg('Uptime : {0} · {1}:{2}', fmtDur(o.uptime), s.host, s.port) }),
        s.links && s.links.login ? h('div', { class: 'hero-login' },
          h('span', { class: 'muted sm', text: 'URI de connexion' }),
          h('div', { class: 'hero-login-row' }, h('strong', { class: 'hero-login-uri', text: s.links.login }),
            h('button', { class: 'btn small', 'aria-label': 'Copier l’URI de connexion', onclick: () => copyText(s.links.login) }, 'Copier'))) : null)),
    h('div', { class: 'hr' }, h('div', null, h('div', { class: 'muted xs', text: s.restart_job ? 'Redémarrage programmé' : 'Redémarrage automatique' }), h('div', { class: 'mono sm', text: next })),
      admin ? h('button', { class: 'btn warn', onclick: () => { const e = $('#rmin'); if (e) { e.scrollIntoView({ behavior: 'smooth', block: 'center' }); e.focus(); } } }, ico('clock'), 'Programmer un redémarrage') : null));
  const stats = h('div', { class: 'stats' },
    stat('cpu', 'Processus OpenSim', 'dotnet', ['CPU utilisé', o.cpu == null ? '…' : o.cpu + ' %'], (o.cpu || 0) / 100, 'blue',
      [msg('RAM : {0}', fmtBytes(o.rss)), msg(' · console : {0}', s.console_ok ? 'OK' : 'KO')], badge(up ? 'En marche' : 'Arrêté', up ? 'ok' : 'bad')),
    stat('cpu', 'Mémoire du serveur', sy.mem_total ? `${fmtBytes(sy.mem_total - sy.mem_avail)} / ${fmtBytes(sy.mem_total)}` : '—', ['Charge globale RAM', Math.round(memUsed * 100) + ' %'], memUsed, 'violet',
      [msg('Load : {0}', sy.load ? sy.load.join(', ') : '—')]),
    stat('disk', 'Espace disque (/data)', sy.disk_total ? `${fmtBytes(sy.disk_total - sy.disk_free)} / ${fmtBytes(sy.disk_total)}` : '—', ['Utilisation', Math.round(diskUsed * 100) + ' %'], diskUsed, 'green',
      [msg('Libre : {0}', fmtBytes(sy.disk_free))]),
    stat('db', 'Bases SQLite actives', msg(dbs.length === 1 ? '1 fichier DB' : '{0} fichiers DB', dbs.length), ['Taille totale', fmtBytes(dbTotal)], 0, 'amber',
      dbs.slice().sort((a, b) => b.size - a.size).slice(0, 2).map(d => `${d.name} ${fmtBytes(d.size)}`)));
  const regs = s.regions || [];
  const regions = h('div', { class: 'card' }, cardTitle('pin', 'Régions de la grille', null, h('span', { class: 'muted sm', text: msg(regs.length === 1 ? '1 région configurée' : '{0} régions configurées', regs.length) })),
    ...regs.map(r => h('div', { class: 'regrow' }, h('i', { class: 'sd ' + (up ? 'ok' : 'bad') }),
      h('div', { class: 'rg' }, h('b', { text: r.name }), h('div', { class: 'muted mono xs', text: `${r.location} · Port ${r.port}` })),
      h('span', { class: 'sz' + (parseInt(r.size) > 256 ? ' big' : ''), text: String(r.size).replace('x', '×') }))),
    h('div', { class: 'cfoot' }, h('span', { class: 'muted sm', text: 'UDP : un port par région' }),
      admin ? h('a', { class: 'lnk', href: '#', onclick: e => { e.preventDefault(); show('regions'); } }, 'Gérer les régions →') : null));
  const extra = s.stats ? h('div', { class: 'card' }, cardTitle('cpu', 'Statistiques de la région'), kv(Object.entries(s.stats).slice(0, 14))) : null;
  $('#dash').replaceChildren(...[hero, stats, h('div', { class: 'split' }, avatarsCard(s), regions), restartCard(s), extra].filter(Boolean));
}
const cardTitle = (icon, title, count, right) => h('div', { class: 'ct' }, h('span', { class: 'ib sm' }, ico(icon)), h('b', { text: title }),
  count != null ? h('span', { class: 'cnt', text: count }) : null, h('span', { class: 'sp' }), right || null);
function stat(icon, label, big, line, frac, color, foot, right) {
  return h('div', { class: 'card stat' },
    h('div', { class: 'srow' }, h('span', { class: 'muted sm', text: label }), right || h('span', { class: 'ib sm' }, ico(icon))),
    h('div', { class: 'big', text: big }),
    h('div', { class: 'srow sm' }, h('span', { class: 'muted', text: line[0] }), h('b', { class: 'mono', text: line[1] })),
    meter(frac, color), h('div', { class: 'foot mono xs' }, ...foot));
}
$('#refreshDash').onclick = loadStatus;
async function post(path, body, okMsg) {
  try { await api(path, { method: 'POST', body }); if (okMsg) toast(okMsg, 'ok'); loadStatus(); } catch (e) { toast(e.message, 'bad'); }
}
const ibtn = (name, title, onclick, danger) => h('button', { class: 'ibtn' + (danger ? ' danger' : ''), title, 'aria-label': title, onclick }, ico(name));
function avatarsCard(s) {
  const admin = me.role === 'admin', a = s.avatars || [], nowS = Date.now() / 1000;
  const rows = a.map(v => {
    const parts = v.name.split(' '), ava = h('div', { class: 'ava', text: ((parts[0] || '?')[0] + ((parts[1] || '')[0] || '')).toUpperCase() });
    ava.style.background = `hsl(${[...v.name].reduce((x, c) => x + c.charCodeAt(0), 0) % 360} 55% 38%)`;
    return h('div', { class: 'avrow' },
      h('div', { class: 'who' }, ava, h('div', null, h('div', { class: 'wn', text: v.name }), h('div', { class: 'muted xs', text: v.hg ? 'Visiteur Hypergrid' : 'Compte local' }))),
      h('div', null, h('span', { class: 'rpill', text: v.region })),
      h('div', { class: 'mono sm', text: v.since ? `${new Date(v.since * 1000).toLocaleTimeString({ fr: 'fr-FR', en: 'en-GB', de: 'de-DE', it: 'it-IT' }[window.WC_I18N.language])} (${fmtDur(nowS - v.since)})` : '—' }),
      h('div', { class: 'acts' }, admin ? [
        !v.hg ? ibtn('msg', 'Message privé', () => { const t = prompt(trf('Message privé à {0} :', v.name)); if (t) post('/api/avatars/message', { first: v.first, last: v.last, text: t }, 'Message envoyé'); }) : null,
        ibtn('kick', 'Expulser', () => {
          const target = v.hg ? `${v.name} (${v.home || 'Hypergrid'})` : v.name;
          if (!confirm(trf(v.hg ? 'Expulser {0} ? La console ciblera la région {1}.' : 'Expulser {0} ?', target, v.region))) return;
          const text = prompt(tr("Message affiché à l'avatar (facultatif) :"));
          if (text === null) return;
          post(v.hg ? '/api/avatars/kick-hypergrid' : '/api/avatars/kick',
            v.hg ? { id: v.id, text } : { first: v.first, last: v.last, text }, tr('Commande d’expulsion envoyée ; vérifier la console.'));
        }, true),
      ] : v.hg ? h('span', { class: 'muted xs', text: 'visiteur' }) : null));
  });
  return h('div', { class: 'card' }, cardTitle('users', 'Avatars en ligne', a.length),
    a.length ? [h('div', { class: 'avhead' }, h('span', { text: 'Avatar' }), h('span', { text: 'Région actuelle' }), h('span', { text: 'Depuis' }), h('span', { text: '' })), ...rows]
             : h('p', { class: 'muted', text: 'Personne en ligne.' }));
}
function restartCard(s) {
  const j = s.restart_job, al = s.alerts || { channels: [] };
  const body = j
    ? [h('p', null, badge('Programmé', 'warn'), ' ', msg('{0} dans {1} (par {2})', j.text, fmtDur(j.remaining), j.who)), h('button', { class: 'btn', onclick: () => post('/api/restart/cancel', {}, 'Redémarrage annulé') }, 'Annuler')]
    : [h('div', { class: 'formrow' },
        h('input', { id: 'rmin', value: '10', inputmode: 'numeric', 'aria-label': 'Minutes avant le redémarrage', size: '4' }),
        h('input', { id: 'rtxt', placeholder: 'Message aux avatars (facultatif)', 'aria-label': 'Message' }),
        h('button', { class: 'btn primary', onclick: () => post('/api/restart/schedule', { minutes: +$('#rmin').value, text: $('#rtxt').value }, 'Redémarrage programmé') }, 'Programmer (minutes)'),
        h('button', { class: 'btn danger', onclick: () => { if (confirm(tr('Redémarrer la grille maintenant ?\nLes avatars connectés seront déconnectés.'))) post('/api/grid/restart', {}, 'Redémarrage en cours…'); } }, 'Redémarrer maintenant'))];
  return h('div', { class: 'card wide adminonly' }, h('h3', { text: 'Redémarrage et alertes' }), ...body,
    h('p', { class: 'muted' },
      msg('Annonces aux avatars à chaque palier, puis arrêt propre et relance d\'OpenSim. Alertes : {0}',
        al.channels.length ? al.channels.join(', ') : tr('aucun canal configuré')),
      al.daily ? msg(' · redémarrage quotidien à {0}', al.daily) : ''),
    h('button', { class: 'btn small', onclick: () => post('/api/alerts/test', {}, 'Notification de test envoyée') }, 'Tester les alertes'));
}
$('#restartConsole').onclick = async () => {
  if (!confirm(tr("Redémarrer la console web ?\nLa grille continue de tourner ; il faudra te reconnecter."))) return;
  try { await api('/api/console/restart', { method: 'POST', body: {} }); } catch (e) { toast(e.message, 'bad'); return; }
  toast(tr('Redémarrage de la console…'), 'ok');
  const t0 = Date.now();
  const wait = async () => {
    try { const r = await fetch('/api/config', { cache: 'no-store' }); if (r.ok) { location.reload(); return; } } catch (e) { /* encore arrêtée */ }
    if (Date.now() - t0 > 30000) { toast(tr('La console ne répond pas : voir le journal du conteneur.'), 'bad'); return; }
    setTimeout(wait, 1000);
  };
  setTimeout(wait, 1500);
};

/* ---------- console ---------- */
const MAXL = 3000;
let lines = [], partial = '', logPos = null, logCursor = null, hist = [], hi = 0;
try { hist = JSON.parse(sessionStorage.getItem('hist') || '[]'); } catch (e) { hist = []; }
hi = hist.length;

function sev(t) {
  if (/\b(ERROR|FATAL|Exception)\b|\bfailed\b|échec/i.test(t)) return 'err';
  if (/\bWARN/i.test(t)) return 'warn';
  if (/^Region \(/.test(t)) return 'cmd';
  return '';
}
function pass(l) {
  const q = $('#logq').value.trim().toLowerCase(), lv = $('#logl').value;
  if (q && !l.t.toLowerCase().includes(q)) return false;
  if (lv === 'err' && l.s !== 'err') return false;
  if (lv === 'warn' && l.s !== 'err' && l.s !== 'warn') return false;
  return true;
}
const lineEl = l => h('div', { class: l.s, text: l.t || '\u00a0' });
function renderTail(added, full) {
  const box = $('#logbox');
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 24;
  const old = $('#partial'); if (old) old.remove();
  if (full) box.replaceChildren(...lines.filter(pass).map(lineEl));
  else {
    for (const l of added) if (pass(l)) box.append(lineEl(l));
    while (box.children.length > MAXL + 5) box.firstChild.remove();
  }
  if (partial) { const l = { t: partial, s: sev(partial) }; if (pass(l)) { const p = lineEl(l); p.id = 'partial'; box.append(p); } }
  if (atBottom || full) box.scrollTop = box.scrollHeight;
}
async function pollLog() {
  if ($('#logpause').checked) return;
  try {
    const d = await api('/api/log' + (logPos == null ? '' : '?pos=' + logPos + '&cursor=' + encodeURIComponent(logCursor)));
    if (d.reset) { lines = []; partial = ''; }
    logPos = d.pos;
    logCursor = d.cursor;
    if (!d.text && !d.reset) return;
    const parts = (partial + d.text).split('\n'); partial = parts.pop();
    const added = parts.map(t => ({ t, s: sev(t) }));
    lines.push(...added); if (lines.length > MAXL) lines.splice(0, lines.length - MAXL);
    if (tab === 'console') renderTail(added, d.reset);
  } catch (e) { /* ignoré : on réessaie au prochain tour */ }
}
['input', 'change'].forEach(ev => { $('#logq').addEventListener(ev, () => renderTail(null, true)); $('#logl').addEventListener(ev, () => renderTail(null, true)); });
$('#logclear').onclick = () => { lines = []; partial = ''; renderTail(null, true); };
$('#logdl').onclick = () => {
  const a = h('a', { href: URL.createObjectURL(new Blob([lines.map(l => l.t).join('\n')], { type: 'text/plain' })), download: 'opensim-console.log' });
  document.body.append(a); a.click(); a.remove();
};
async function sendCmd(cmd, confirmMsg) {
  cmd = (cmd || '').trim();   // vide = touche Entrée (valeur par défaut d'une question d'OpenSim)
  if (confirmMsg && !confirm(tr(confirmMsg))) return;
  try {
    await api('/api/cmd', { method: 'POST', body: { cmd } });
    if (cmd && hist[hist.length - 1] !== cmd) { hist.push(cmd); if (hist.length > 100) hist.shift(); try { sessionStorage.setItem('hist', JSON.stringify(hist)); } catch (e) { /* ignoré */ } }
    hi = hist.length; setTimeout(pollLog, 500);
  } catch (e) { toast(e.message, 'bad'); }
}
$('#send').onclick = () => { const i = $('#cmd'); sendCmd(i.value); i.value = ''; };
$('#ent').onclick = () => sendCmd('');
$('#cmd').onkeydown = e => {
  if (e.key === 'Enter') $('#send').click();
  else if (e.key === 'ArrowUp') { if (hi > 0) { hi--; e.target.value = hist[hi]; } e.preventDefault(); }
  else if (e.key === 'ArrowDown') { if (hi < hist.length - 1) { hi++; e.target.value = hist[hi]; } else { hi = hist.length; e.target.value = ''; } e.preventDefault(); }
};
const CHIPS = [
  ['Infos', [{ c: 'show regions' }, { c: 'show users' }, { c: 'show connections' }, { c: 'show uptime' }, { c: 'show version' }, { c: 'show info' }, { c: 'show hyperlinks' }, { c: 'show modules' }, { c: 'show threads' }]],
  ['Région', [{ c: 'backup' }, { c: 'alert ', arg: true }, { c: 'login status' }, { c: 'login disable', confirm: 'Désactiver les connexions ?' }, { c: 'login enable' }]],
  ['Aide', [{ c: 'help' }, { c: 'help show' }, { c: 'help create' }]],
  ['Danger', [{ c: 'shutdown', confirm: 'Arrêter la grille ?', danger: true }]],
];
for (const [grp, items] of CHIPS) {
  $('#chips').append(h('span', { class: 'grp', text: grp }));
  for (const it of items) $('#chips').append(h('button', {
    class: 'chip' + (it.danger ? ' danger' : ''), text: it.c.trim(), title: it.arg ? 'À compléter puis Entrée' : 'Envoyer',
    onclick: () => { if (it.arg) { $('#cmd').value = it.c; $('#cmd').focus(); } else sendCmd(it.c, it.confirm); },
  }));
}
$('#chips').classList.add('adminonly'); $('.cmdrow').classList.add('adminonly');

/* ---------- utilisateurs ---------- */
async function loadUsers() {
  const box = $('#usersbox');
  try {
    const d = await api('/api/users'), admin = me.role === 'admin';
    if (!d.available) { box.replaceChildren(h('div', { class: 'card muted', text: 'Base des comptes introuvable (table UserAccounts absente).' })); return; }
    const head = ['Nom complet', 'Statut', 'Dernière connexion', 'Créé'].concat(admin ? ['E-mail', 'Niveau', ''] : ['Niveau']);
    const rows = d.users.map(u => {
      const r = [`${u.first} ${u.last}`, u.online ? badge('en ligne', 'ok') : badge('hors ligne'), u.last_login || '—', u.created || '—'];
      if (admin) {
        const email = h('input', { type: 'email', value: u.email || '', maxlength: '254', 'aria-label': msg('E-mail de {0} {1}', u.first, u.last) });
        const currentLevel = u.level == null ? '' : String(u.level);
        const knownLevels = [
          [-1, '{0} · Bloqué'], [0, '{0} · Utilisateur normal'], [200, '{0} · Admin'], [250, '{0} · Grid admin'],
        ];
        const options = knownLevels.map(([value, label]) => h('option', {
          value: String(value), selected: currentLevel === String(value),
        }, msg(label, value)));
        if (!knownLevels.some(([value]) => currentLevel === String(value))) {
          options.unshift(h('option', { value: currentLevel, selected: true },
            currentLevel ? msg('Niveau actuel ({0})', currentLevel) : 'Non défini (inchangé)'));
        }
        const level = h('select', { 'aria-label': msg('Niveau de {0} {1}', u.first, u.last) }, options);
        const save = h('button', { class: 'btn small primary', onclick: async () => {
          if (!email.reportValidity()) return;
          const body = { id: u.id, email: email.value.trim() };
          if (level.value !== currentLevel) body.level = Number(level.value);
          try {
            await api('/api/users/update', { method: 'POST', body });
            toast(trf('Compte de {0} {1} mis à jour', u.first, u.last), 'ok');
            loadUsers();
          } catch (e) { toast(e.message, 'bad'); }
        } }, 'Enregistrer');
        r.push(email, level, h('span', null, save, ' ',
          h('button', { class: 'btn small', onclick: () => resetPw(u) }, 'Mot de passe…')));
      } else {
        r.push(u.level == null ? '—' : String(u.level));
      }
      return r;
    });
    box.replaceChildren(rows.length ? table(head, rows) : h('div', { class: 'card muted', text: 'Aucun utilisateur.' }));
  } catch (e) { if (e.message !== 'Session expirée') toast(e.message, 'bad'); }
}
async function resetPw(u) {
  const p = prompt(trf('Nouveau mot de passe pour {0} {1} (sans espaces) :', u.first, u.last));
  if (!p) return;
  if (/\s/.test(p) || p.length < 6) { toast('Mot de passe invalide (6 caractères min., sans espaces).', 'bad'); return; }
  try { await api('/api/users/password', { method: 'POST', body: { first: u.first, last: u.last, pass: p } }); toast('Commande envoyée', 'ok'); }
  catch (e) { toast(e.message, 'bad'); }
}
$('#mkuser').onclick = async () => {
  const b = { first: $('#nf').value, last: $('#nl').value, pass: $('#np').value, email: $('#ne').value };
  try {
    await api('/api/users/create', { method: 'POST', body: b });
    toast('Utilisateur en cours de création', 'ok'); $('#np').value = '';
    setTimeout(loadUsers, 2500);
  } catch (e) { toast(e.message, 'bad'); }
};
$('#refreshUsers').onclick = loadUsers;
$('#createuser').classList.add('adminonly');

/* ---------- sauvegardes ---------- */
async function loadBackups() {
  try {
    const d = await api('/api/backups'), admin = me.role === 'admin';
    $('#bkinfo').textContent = trf('Conservation : les {0} sauvegardes datées les plus récentes. ', d.keep) +
      (d.every_h > 0 ? trf('Sauvegarde automatique des bases toutes les {0} h.', d.every_h) : tr('Pas de sauvegarde automatique (variable BACKUP_EVERY_HOURS).'));
    const rows = d.items.map(b => {
      const r = [b.name, fmtDate(b.mtime), fmtBytes(b.size), [b.has_db ? badge('bases', 'ok') : null, ' ', b.oar.length ? badge('OAR') : null]];
      if (admin) r.push(h('span', null,
        h('a', { class: 'btn small', href: '/api/backups/download?name=' + encodeURIComponent(b.name) }, 'Télécharger'), ' ',
        b.has_db ? h('button', { class: 'btn small', onclick: () => restoreBk(b) }, 'Restaurer les bases') : null, ' ',
        b.oar.length ? h('button', { class: 'btn small', onclick: () => loadOar(b) }, tr('Charger les OAR')) : null, ' ',
        h('button', { class: 'btn small danger', onclick: () => delBk(b) }, 'Supprimer')));
      return r;
    });
    $('#bkbox').replaceChildren(rows.length ? table(['Nom', 'Date', 'Taille', 'Contenu'].concat(admin ? [''] : []), rows, [2]) : h('div', { class: 'card muted', text: 'Aucune sauvegarde pour le moment.' }));
  } catch (e) { if (e.message !== 'Session expirée') toast(e.message, 'bad'); }
}
$$('[data-bk]').forEach(b => b.onclick = async () => {
  $$('[data-bk]').forEach(x => x.disabled = true);
  try {
    const r = await api('/api/backups/create', { method: 'POST', body: { kind: b.dataset.bk } });
    toast(trf('Sauvegarde créée : {0}', r.name), 'ok');
    loadBackups(); if (b.dataset.bk !== 'db') setTimeout(loadBackups, 6000);
  } catch (e) { toast(e.message, 'bad'); }
  $$('[data-bk]').forEach(x => x.disabled = false);
});
async function delBk(b) {
  if (!confirm(trf('Supprimer définitivement la sauvegarde {0} ?', b.name))) return;
  try { await api('/api/backups/delete', { method: 'POST', body: { name: b.name } }); toast('Supprimée', 'ok'); loadBackups(); } catch (e) { toast(e.message, 'bad'); }
}
async function restoreBk(b) {
  if (!confirm(trf("Restaurer les bases de {0} ?\nLes bases actuelles seront mises de côté dans le dossier des sauvegardes. La restauration s'applique au prochain démarrage de la grille.", b.name))) return;
  try {
    const r = await api('/api/backups/restore', { method: 'POST', body: { name: b.name } });
    toast(r.message, 'ok');
    if (confirm(tr("Arrêter la grille maintenant pour appliquer la restauration ?\n(Il faudra ensuite la redémarrer depuis Container Manager.)"))) sendCmd('shutdown');
  } catch (e) { toast(e.message, 'bad'); }
}
async function loadOar(b) {
  if (!confirm(trf("Charger les OAR de {0} ? Le contenu des régions correspondantes sera remplacé.", b.name))) return;
  try { await api('/api/backups/loadoar', { method: 'POST', body: { name: b.name } }); toast(tr('Chargement des OAR lancé (voir la console)'), 'ok'); } catch (e) { toast(e.message, 'bad'); }
}
$('#refreshBackups').onclick = loadBackups;
$$('[data-bk]')[0].closest('.card').classList.add('adminonly');

/* ---------- grille & liens ---------- */
async function loadGrid() {
  try {
    const s = await api('/api/status'); lastStatus = s;
    const L = s.links, g = s.grid || {};
    const cards = [
      card('Connexion au viewer', linkRow('URI de connexion', L.login), linkRow('Infos de grille', L.gridinfo), linkRow('Passerelle (gatekeeper)', L.gatekeeper)),
      card('Hypergrid : liens vers vos régions', L.teleports.map(t => linkRow('Téléportation', t)), L.hops.map(t => linkRow('Lien hop://', t))),
      card('Informations publiées par la grille', Object.keys(g).length ? kv(Object.entries(g)) : h('p', { class: 'muted', text: 'La grille ne répond pas en local.' })),
    ];
    $('#gridbox').replaceChildren(...cards);
  } catch (e) { if (e.message !== 'Session expirée') toast(e.message, 'bad'); }
}
$('#refreshGrid').onclick = loadGrid;

/* ---------- journal d'actions ---------- */
async function loadAudit() {
  try { const d = await api('/api/audit'); const b = $('#auditbox'); setLocalizedText(b, d.lines.join('\n') || '(vide)'); b.scrollTop = b.scrollHeight; }
  catch (e) { if (e.message !== 'Session expirée') toast(e.message, 'bad'); }
}
$('#refreshAudit').onclick = loadAudit;

/* ---------- import / export OAR & IAR ---------- */
let xferBuilt = false, xferMaxMb = 2048;
const opt = (id, label) => h('label', { class: 'chk' }, h('input', { type: 'checkbox', id }), ' ' + label);
function uploader(kind) {
  const f = h('input', { type: 'file', accept: kind === 'oar' ? '.oar,.tgz' : '.iar' });
  const st = h('span', { class: 'muted' });
  const b = h('button', { class: 'btn', onclick: () => {
    const file = f.files[0];
    if (!file) { toast(tr('Choisis un fichier.'), 'bad'); return; }
    if (file.size > xferMaxMb * 1048576) { toast(trf('Fichier trop gros (limite {0} Mo, variable MAX_UPLOAD_MB).', xferMaxMb), 'bad'); return; }
    const x = new XMLHttpRequest();
    x.open('POST', `/api/transfer/upload?kind=${kind}&name=${encodeURIComponent(file.name)}`);
    x.setRequestHeader('X-Requested-With', 'webconsole'); x.setRequestHeader('Content-Type', 'application/octet-stream');
    x.upload.onprogress = e => { if (e.lengthComputable) st.textContent = Math.round(e.loaded / e.total * 100) + ' %'; };
    x.onload = () => {
      let d = {}; try { d = JSON.parse(x.responseText); } catch (e) { /* ignoré */ }
      st.textContent = '';
      if (x.status === 200) { toast(trf('Fichier envoyé : {0}', d.name), 'ok'); f.value = ''; loadXferLists(); }
      else toast(translateServerError(d.error || trf('Erreur {0}', x.status)), 'bad');
    };
    x.onerror = () => { st.textContent = ''; toast("Échec de l'envoi", 'bad'); };
    st.textContent = '0 %'; x.send(file);
  } }, 'Envoyer');
  return h('div', { class: 'formrow' }, f, b, st);
}
function xferTable(kind, items, loadFn) {
  if (!items.length) return h('p', { class: 'muted', text: 'Aucun fichier. Exporte depuis la grille ou envoie un fichier depuis ton ordinateur.' });
  return table(['Fichier', 'Taille', 'Date', ''], items.map(i => [i.name, fmtBytes(i.size), fmtDate(i.mtime), h('span', null,
    h('a', { class: 'btn small', href: `/api/transfer/download?kind=${kind}&name=${encodeURIComponent(i.name)}` }, 'Télécharger'), ' ',
    h('button', { class: 'btn small', onclick: () => loadFn(i) }, kind === 'oar' ? 'Charger' : 'Importer'), ' ',
    h('button', { class: 'btn small danger', onclick: () => delXfer(kind, i) }, 'Supprimer'))]), [1]);
}
function buildXfer() {
  const oar = h('div', { class: 'card' }, h('h3', { text: 'Régions (OAR)' }),
    h('p', { class: 'muted', text: "Un OAR contient une région entière (terrain, objets, parcelles). L'export est asynchrone : le fichier apparaît dans la liste quand OpenSim a terminé (voir la console)." }),
    h('div', { class: 'formrow' }, h('select', { id: 'xr', 'aria-label': 'Région' }), h('button', { class: 'btn primary', onclick: exportOar }, 'Exporter la région')),
    h('div', { class: 'opts' }, opt('onoa', 'Export sans les assets (plus léger)'), opt('omerge', 'Import : fusionner avec la région existante'), opt('oskip', "Import : ignorer les assets")),
    h('h3', { text: 'Fichiers OAR' }), h('div', { id: 'oarlist' }),
    h('h3', { text: 'Envoyer un OAR depuis cet ordinateur' }), uploader('oar'));
  const iar = h('div', { class: 'card' }, h('h3', { text: 'Inventaires (IAR)' }),
    h('p', { class: 'muted', text: "Un IAR contient tout ou partie de l'inventaire d'un avatar. Le mot de passe demandé est celui de l'avatar ; il n'est ni enregistré ni affiché dans le journal d'actions." }),
    h('div', { class: 'formrow' }, h('select', { id: 'xu', 'aria-label': 'Avatar' }),
      h('input', { id: 'xinv', value: '/*', placeholder: 'Dossier (ex. /*)', 'aria-label': "Dossier d'inventaire" }),
      h('input', { id: 'xpw', type: 'password', placeholder: "Mot de passe de l'avatar", autocomplete: 'new-password' }),
      h('button', { class: 'btn primary', onclick: exportIar }, "Exporter l'inventaire")),
    h('div', { class: 'opts' }, opt('inoa', 'Export sans les assets'), opt('imerge', 'Import : fusionner avec le contenu existant')),
    h('h3', { text: 'Fichiers IAR' }), h('div', { id: 'iarlist' }),
    h('h3', { text: 'Envoyer un IAR depuis cet ordinateur' }), uploader('iar'));
  $('#xferbox').replaceChildren(oar, iar);
}
async function loadXferLists() {
  try {
    const [o, a] = await Promise.all([api('/api/transfer?kind=oar'), api('/api/transfer?kind=iar')]);
    xferMaxMb = o.max_mb || xferMaxMb;
    $('#oarlist').replaceChildren(xferTable('oar', o.items, loadOarFile));
    $('#iarlist').replaceChildren(xferTable('iar', a.items, importIar));
  } catch (e) { if (e.message !== 'Session expirée') toast(e.message, 'bad'); }
}
async function loadXfer() {
  if (!xferBuilt) { buildXfer(); xferBuilt = true; }
  try {
    const [s, u] = await Promise.all([api('/api/status'), api('/api/users')]);
    const r = $('#xr'), rc = r.value; r.replaceChildren(...(s.regions || []).map(x => h('option', { value: x.name, text: x.name }))); if (rc) r.value = rc;
    const a = $('#xu'), ac = a.value; a.replaceChildren(...(u.users || []).map(x => h('option', { value: x.first + '|' + x.last, text: x.first + ' ' + x.last }))); if (ac) a.value = ac;
  } catch (e) { /* listes déroulantes laissées telles quelles */ }
  loadXferLists();
}
async function exportOar() {
  try {
    const r = await api('/api/oar/export', { method: 'POST', body: { region: $('#xr').value, noassets: $('#onoa').checked } });
    toast(trf('Export lancé : {0}', r.name), 'ok'); [3000, 10000, 30000].forEach(t => setTimeout(loadXferLists, t));
  } catch (e) { toast(e.message, 'bad'); }
}
async function loadOarFile(i) {
  const merge = $('#omerge').checked;
  if (!confirm(trf('Charger {0} dans la région {1} ?\n{2}', i.name, $('#xr').value,
    tr(merge ? 'Le contenu sera fusionné avec l\'existant.' : 'Le contenu actuel de la région sera REMPLACÉ.')))) return;
  try {
    await api('/api/oar/load', { method: 'POST', body: { name: i.name, region: $('#xr').value, merge, skip_assets: $('#oskip').checked } });
    toast('Chargement lancé (suivre la console)', 'ok');
  } catch (e) { toast(e.message, 'bad'); }
}
function iarWho() {
  const v = ($('#xu').value || '|').split('|');
  return { first: v[0], last: v[1], pass: $('#xpw').value, inventory: $('#xinv').value };
}
async function exportIar() {
  const w = iarWho();
  if (!w.first || !w.pass) { toast("Choisis un avatar et saisis son mot de passe.", 'bad'); return; }
  try {
    const r = await api('/api/iar/export', { method: 'POST', body: { ...w, noassets: $('#inoa').checked } });
    $('#xpw').value = ''; toast(trf('Export lancé : {0}', r.name), 'ok'); [3000, 10000, 30000].forEach(t => setTimeout(loadXferLists, t));
  } catch (e) { toast(e.message, 'bad'); }
}
async function importIar(i) {
  const w = iarWho();
  if (!w.first || !w.pass) { toast("Choisis l'avatar destinataire et saisis son mot de passe.", 'bad'); return; }
  if (!confirm(trf("Importer {0} dans l'inventaire de {1} {2} (dossier {3}) ?", i.name, w.first, w.last, w.inventory))) return;
  try {
    await api('/api/iar/load', { method: 'POST', body: { ...w, name: i.name, merge: $('#imerge').checked } });
    $('#xpw').value = ''; toast('Import lancé (suivre la console)', 'ok');
  } catch (e) { toast(e.message, 'bad'); }
}
async function delXfer(kind, i) {
  if (!confirm(trf('Supprimer définitivement {0} ?', i.name))) return;
  try { await api('/api/transfer/delete', { method: 'POST', body: { kind, name: i.name } }); toast('Supprimé', 'ok'); loadXferLists(); } catch (e) { toast(e.message, 'bad'); }
}
$('#refreshXfer').onclick = loadXfer;

/* ---------- régions ---------- */
let regionMapData = null, runningRegionRequest = 0;
const parseRegionLocation = location => {
  const match = /^\s*(-?\d+)\s*,\s*(-?\d+)\s*$/.exec(String(location || ''));
  return match ? { x: Number(match[1]), y: Number(match[2]) } : null;
};
const regionSizeCells = size => Math.max(1, Math.floor(Number.parseInt(String(size || '256'), 10) / 256) || 1);
function renderRegionMap(data = regionMapData) {
  if (!data) return;
  regionMapData = data;
  const regions = data.items.map(r => {
    const position = parseRegionLocation(r.location);
    return position ? { ...r, ...position, cells: regionSizeCells(r.size) } : null;
  }).filter(Boolean);
  const suggestion = parseRegionLocation(data.suggest.location);
  const selected = parseRegionLocation($('#rl').value) || suggestion;
  const selectedSize = regionSizeCells($('#rs').value || '256');
  const bounds = regions.map(r => ({ left: r.x, right: r.x + r.cells, bottom: r.y, top: r.y + r.cells }));
  if (suggestion) bounds.push({ left: suggestion.x, right: suggestion.x + selectedSize, bottom: suggestion.y, top: suggestion.y + selectedSize });
  if (selected) bounds.push({ left: selected.x, right: selected.x + selectedSize, bottom: selected.y, top: selected.y + selectedSize });
  const left = (bounds.length ? Math.min(...bounds.map(b => b.left)) : 0) - 1;
  const right = (bounds.length ? Math.max(...bounds.map(b => b.right)) : 1) + 1;
  const bottom = (bounds.length ? Math.min(...bounds.map(b => b.bottom)) : 0) - 1;
  const top = (bounds.length ? Math.max(...bounds.map(b => b.top)) : 1) + 1;
  const width = right - left, height = top - bottom;
  const svgNS = 'http://www.w3.org/2000/svg';
  const element = (tag, attrs = {}, text = null) => {
    const node = document.createElementNS(svgNS, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    if (text != null) node.textContent = text;
    return node;
  };
  const svg = element('svg', { viewBox: `${left} ${-top} ${width} ${height}`, preserveAspectRatio: 'xMidYMid meet', role: 'img', 'aria-label': tr('Carte des emplacements de régions'), 'data-i18n-aria-label': 'Carte des emplacements de régions' });
  const defs = element('defs');
  const pattern = element('pattern', { id: 'region-map-grid', width: 1, height: 1, x: left, y: -top, patternUnits: 'userSpaceOnUse' });
  pattern.append(element('path', { d: 'M 1 0 L 0 0 0 1', class: 'region-map-gridline' }));
  defs.append(pattern);
  svg.append(defs, element('rect', { x: left, y: -top, width, height, class: 'region-map-background' }),
    element('rect', { x: left, y: -top, width, height, fill: 'url(#region-map-grid)' }));
  for (const region of regions) {
    const rect = element('rect', { x: region.x, y: -region.y - region.cells, width: region.cells, height: region.cells,
      class: 'region-map-region' + (region.active ? '' : ' is-inactive') });
    rect.append(element('title', {}, trf(region.active ? '{0} — {1},{2} ({3}) · activée au prochain démarrage' :
      '{0} — {1},{2} ({3}) · désactivée au prochain démarrage', region.name, region.x, region.y, region.size)));
    svg.append(rect);
    if (region.cells >= 2) svg.append(element('text', { x: region.x + region.cells / 2, y: -region.y - region.cells / 2, class: 'region-map-label' }, region.name));
  }
  if (selected) {
    const overlaps = regions.some(r => selected.x < r.x + r.cells && r.x < selected.x + selectedSize &&
      selected.y < r.y + r.cells && r.y < selected.y + selectedSize);
    svg.append(element('rect', { x: selected.x, y: -selected.y - selectedSize, width: selectedSize, height: selectedSize,
      class: 'region-map-selection' + (overlaps ? ' is-overlapping' : '') }));
    setLocalizedText($('#regionMapInfo'), 'Position sélectionnée : {0},{1} · {2} × {3}{4}',
      selected.x, selected.y, selectedSize * 256, selectedSize * 256, overlaps ? tr(' · emplacement déjà occupé') : '');
    } else {
      setLocalizedText($('#regionMapInfo'), 'Aucune position de région valide à afficher.');
  }
  svg.onclick = event => {
    const point = svg.createSVGPoint();
    point.x = event.clientX; point.y = event.clientY;
    const location = point.matrixTransform(svg.getScreenCTM().inverse());
    const x = Math.floor(location.x), y = Math.floor(-location.y);
    if (x < 0 || y < 0 || x > 99999 || y > 99999) {
      toast(tr('La position doit être comprise entre 0,0 et 99999,99999.'), 'bad');
      return;
    }
    const size = regionSizeCells($('#rs').value || '256');
    const occupied = regions.some(r => x < r.x + r.cells && r.x < x + size && y < r.y + r.cells && r.y < y + size);
    if (occupied) { toast(tr('Cet emplacement chevauche une région configurée.'), 'bad'); return; }
    $('#rl').value = `${x},${y}`;
    renderRegionMap(data);
  };
  $('#regionMap').replaceChildren(svg);
}
async function loadRegions() {
  const request = ++runningRegionRequest;
  const regionSelect = $('#regionSelect'), selectedRegion = regionSelect.value;
  regionSelect.replaceChildren();
  regionSelect.disabled = true;
  $('#changeRegion').disabled = true;
  setLocalizedText($('#runningRegionInfo'), 'Vérification des régions démarrées…');
  try {
    const d = await api('/api/regions?size=' + ($('#rs').value || 256));
    $('#regbox').replaceChildren(table(['Nom', 'Position', 'Taille', 'Port', 'UUID', 'État au prochain démarrage', ''],
      d.items.map(r => [r.name, r.location, r.size, r.port, r.uuid, r.active ? 'Activée' : 'Désactivée',
        h('div', { class: 'region-actions' },
          h('button', { class: 'btn small', onclick: () => toggleRegion(r) }, r.active ? 'Désactiver' : 'Activer'),
          h('button', { class: 'btn small danger', onclick: () => delRegion(r) }, 'Retirer'))])));
    setLocalizedAttribute($('#rl'), 'placeholder', 'Position x,y (proposée : {0})', d.suggest.location);
    setLocalizedAttribute($('#rp'), 'placeholder', 'Port (proposé : {0})', d.suggest.port);
    renderRegionMap(d);
    const running = await api('/api/regions/running');
    if (request !== runningRegionRequest) return;
    regionSelect.replaceChildren(...running.items.map(name => h('option', { value: name, text: name })));
    if (running.items.includes(selectedRegion)) regionSelect.value = selectedRegion;
    regionSelect.disabled = running.items.length === 0;
    $('#changeRegion').disabled = running.items.length === 0;
    setLocalizedText($('#runningRegionInfo'), running.items.length ? 'Seules les régions actuellement démarrées sont proposées.' : 'Aucune région démarrée.');
  } catch (e) {
    if (request !== runningRegionRequest) return;
    setLocalizedText($('#runningRegionInfo'), 'Impossible de vérifier les régions démarrées. Réessayez avec Actualiser.');
    if (e.message !== 'Session expirée') toast(e.message, 'bad');
  }
}
async function loadRegionMap() {
  try {
    const d = await api('/api/regions?size=' + ($('#rs').value || 256));
    renderRegionMap(d);
  } catch (e) { if (e.message !== 'Session expirée') toast(e.message, 'bad'); }
}
$('#rs').onchange = loadRegions;
$('#rl').oninput = () => renderRegionMap();
$('#changeRegion').onclick = async () => {
  const region = $('#regionSelect').value;
  if (!region) return;
  try {
    await api('/api/regions/change', { method: 'POST', body: { region } });
    toast(trf('La console cible maintenant la région {0}.', region), 'ok');
  } catch (e) { toast(e.message, 'bad'); }
};
$('#rmk').onclick = async () => {
  try {
    const r = await api('/api/regions/create', { method: 'POST', body: { name: $('#rn').value, location: $('#rl').value, size: +$('#rs').value, port: +$('#rp').value || 0 } });
    setLocalizedText($('#regnote'), "Région « {0} » créée (position {1}, port {2}). Pour l'activer : 1) ouvrir {2} en TCP et UDP sur la Livebox et dans le pare-feu DSM ; 2) redémarrer la grille.", r.name, r.location, r.port);
    ['#rn', '#rl', '#rp'].forEach(i => { $(i).value = ''; });
    toast(tr('Région créée (redémarrage requis)'), 'ok'); loadRegions();
  } catch (e) { toast(e.message, 'bad'); }
};
async function delRegion(r) {
  const exp = r.active && confirm(trf("Exporter « {0} » en OAR avant de la retirer ?\nOK = exporter d'abord, Annuler = ne pas exporter.", r.name));
  if (!confirm(trf("Retirer la région {0} ?\nSon fichier est mis de côté dans data/regions-removed ; la grille doit être redémarrée pour que ça prenne effet.", r.name))) return;
  try { await api('/api/regions/delete', { method: 'POST', body: { name: r.name, export: exp } }); toast(tr('Région retirée (redémarrage requis)'), 'ok'); loadRegions(); }
  catch (e) { toast(e.message, 'bad'); }
}
async function toggleRegion(r) {
  try {
    await api('/api/regions/active', { method: 'POST', body: { file: r.file, active: !r.active } });
    setLocalizedText($('#regnote'), "Le changement pour « {0} » sera effectif au prochain démarrage de la grille.", r.name);
    toast(tr('Changement enregistré (redémarrage requis)'), 'ok');
    await loadRegions();
  } catch (e) { toast(e.message, 'bad'); }
}
$('#refreshRegions').onclick = loadRegions;
$('#refreshRegionMap').onclick = loadRegionMap;
$('#showRegionMap').onclick = () => show('region-map');
$('#mapToRegions').onclick = () => show('regions');

boot();
