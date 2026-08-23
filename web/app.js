/* DueDiligence Direct console.
 *
 * A client of the control plane, not the fleet itself: everything on screen comes
 * from the same HTTP API that any other client would call. No framework, no build
 * step, no CDN — the container serves three static files and the API does the rest.
 */
'use strict';

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const TERMINAL = ['SUCCEEDED', 'FAILED', 'CANCELLED', 'INTERRUPTED'];
const POLL_MS = 2000;

const state = {
  view: 'console',
  jobId: null,
  poll: null,
  files: [],
  company: null,
  announced: {},
  openTab: 'report',
  history: { offset: 0, limit: 25, query: '', status: '', total: 0 },
};

/* ------------------------------------------------------------------ utils */

function esc(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function num(value, digits) {
  if (value === null || value === undefined || value === '') return '-';
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return esc(value);
  return parsed.toLocaleString('en-GB', {
    minimumFractionDigits: digits || 0,
    maximumFractionDigits: digits === undefined ? 0 : digits,
  });
}

function clock(timestamp) {
  if (!timestamp) return '';
  const match = String(timestamp).match(/T(\d{2}:\d{2}:\d{2})/);
  return match ? match[1] : String(timestamp).slice(0, 19);
}

function titleCase(value) {
  return String(value || '').replace(/[_-]/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

function pick(bundle, key) {
  const record = (bundle || {})[key] || {};
  const data = record.data;
  return data && typeof data === 'object' ? data : {};
}

function address(addr) {
  if (!addr || typeof addr !== 'object') return '';
  return ['premises', 'address_line_1', 'address_line_2', 'locality', 'region', 'postal_code', 'country']
    .map((k) => addr[k]).filter(Boolean).join(', ');
}

/* ------------------------------------------------------------- components */

const SEVERITY_COLOUR = { HIGH: 'red', MEDIUM: 'orange', LOW: 'blue', CLEAR: 'green' };
const STATUS_COLOUR = {
  active: 'green', dissolved: 'grey', liquidation: 'red', administration: 'orange',
  receivership: 'orange', 'voluntary-arrangement': 'yellow', 'converted-closed': 'grey',
  SUCCEEDED: 'green', FAILED: 'red', RUNNING: 'blue', QUEUED: 'grey',
  CANCELLED: 'grey', INTERRUPTED: 'yellow',
};

function tag(text, colour) {
  return `<span class="gv-tag gv-tag--${colour || 'grey'}">${esc(text)}</span>`;
}

function severityTag(severity) {
  const key = String(severity || 'unknown').toUpperCase();
  return tag(key, SEVERITY_COLOUR[key]);
}

function statusTag(status) {
  const key = String(status || 'unknown');
  return tag(key.replace(/-/g, ' '), STATUS_COLOUR[key] || STATUS_COLOUR[key.toLowerCase()]);
}

function summaryList(rows) {
  const body = rows.filter(Boolean).map(
    ([key, value]) => `<tr><th>${esc(key)}</th><td>${value === undefined || value === null || value === '' ? '-' : value}</td></tr>`
  ).join('');
  return `<table class="gv-summary">${body}</table>`;
}

function table(caption, headers, rows, options) {
  if (!rows || !rows.length) return '';
  const numeric = (options && options.numeric) || [];
  const head = headers.map((h, i) =>
    `<th scope="col"${numeric.includes(i) ? ' class="numeric"' : ''}>${esc(h)}</th>`).join('');
  const body = rows.map((row) =>
    `<tr>${row.map((cell, i) =>
      `<td class="${numeric.includes(i) ? 'numeric' : 'wrap'}">${cell === null || cell === undefined || cell === '' ? '-' : cell}</td>`
    ).join('')}</tr>`).join('');
  return `<div class="gv-scroll"><table class="gv-table">
    ${caption ? `<caption>${esc(caption)}</caption>` : ''}
    <thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function notice(title, body, isError) {
  return `<div class="gv-notice${isError ? ' gv-notice--error' : ''}">
    <div class="gv-notice__header">${esc(title)}</div>
    <div class="gv-notice__body">${body}</div></div>`;
}

function warning(text) {
  return `<div class="gv-warning"><div class="gv-warning__icon" aria-hidden="true">!</div>
    <div class="gv-warning__text"><span class="gv-sr">Warning</span>${esc(text)}</div></div>`;
}

function muted(text) {
  return `<p class="gv-muted gv-small">${esc(text)}</p>`;
}

function findingCard(item) {
  const severity = String(item.severity || 'UNKNOWN').toUpperCase();
  const quote = item.evidentiary_quote || item.quote || '';
  const verified = item.evidence_verified;
  return `<div class="gv-finding gv-finding--${esc(severity)}">
    <div class="gv-finding__head">
      <span class="gv-finding__category">${esc(item.category || item.issue || 'Finding')}</span>
      ${severityTag(severity)}
      ${verified === false ? tag('unverified citation', 'yellow') : ''}
      ${verified === true ? tag('evidence verified', 'green') : ''}
    </div>
    <div class="gv-finding__body">${esc(item.finding || item.detail || '')}</div>
    ${quote ? `<div class="gv-quote">${esc(quote)}</div>` : ''}
  </div>`;
}

/* --------------------------------------------------------------- transport */

class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

async function api(path, options) {
  const opts = Object.assign({ credentials: 'same-origin' }, options || {});
  opts.headers = Object.assign({ 'content-type': 'application/json' }, opts.headers || {});
  const response = await fetch(path, opts);
  if (response.status === 401) { showLocked(); throw new ApiError('Not signed in', 401); }
  if (response.status === 204) return null;
  const text = await response.text();
  let payload = null;
  try { payload = text ? JSON.parse(text) : null; } catch (err) { payload = { raw: text }; }
  if (!response.ok) {
    throw new ApiError((payload && payload.error) || `Request failed (${response.status})`, response.status);
  }
  return payload;
}

/* -------------------------------------------------------------- navigation */

function switchView(name) {
  state.view = name;
  ['console', 'history', 'registry', 'audit', 'memory', 'locked'].forEach((view) => {
    const section = $(`#view-${view}`);
    if (section) section.hidden = view !== name;
  });
  $$('.gv-nav button').forEach((button) => {
    if (button.dataset.view === name) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  if (name === 'history') loadHistory();
  if (name === 'registry') loadRegistry();
  if (name === 'audit') loadAudit();
}

function showLocked() {
  if (state.poll) { clearInterval(state.poll); state.poll = null; }
  $('.gv-nav').hidden = true;
  switchView('locked');
}

/* ------------------------------------------------------- company lookup */

/* One field resolves both ways of naming a company. The register's search
 * matches company numbers as well as names, so the work here is deciding
 * whether the input was meant as a number and whether a hit is exact enough to
 * choose without asking. */

// Companies House numbers are eight characters: eight digits, or two letters
// then six digits (SC, NI, OC and friends). People routinely drop the leading
// zero, so 3994971 is treated as 03994971 rather than as a failed search.
function companyNumberCandidate(query) {
  const compact = query.replace(/[\s-]/g, '').toUpperCase();
  if (/^\d{1,8}$/.test(compact)) return compact.padStart(8, '0');
  if (/^[A-Z]{2}\d{6}$/.test(compact)) return compact;
  return '';
}

function setSelection(company) {
  state.company = company;
  $('#company-results').innerHTML = '';
  $('#selected-company').innerHTML = company
    ? `<div class="gv-chosen">
         <div class="gv-chosen__name">${esc(company.title)} ${company.company_status ? statusTag(company.company_status) : ''}</div>
         <div class="gv-small gv-muted">Company number ${esc(company.company_number)}</div>
         <p style="margin:8px 0 0">
           <button type="button" class="gv-button gv-button--secondary gv-button--small"
                   id="change-company">Change</button>
         </p>
       </div>`
    : '';
  const change = $('#change-company');
  if (change) {
    change.addEventListener('click', () => {
      setSelection(null);
      $('#company-query').value = '';
      $('#company-query').focus();
    });
  }
  const submit = $('#submit-audit');
  submit.disabled = !company;
  submit.title = company ? '' : 'Find a company first';
}

function renderCandidates(results, query) {
  $('#company-results').innerHTML = `
    <p class="gv-small gv-muted" style="margin-bottom:6px">
      ${results.length} match${results.length === 1 ? '' : 'es'} for &ldquo;${esc(query)}&rdquo;. Choose one:
    </p>
    <ul class="gv-results">${results.map((item, index) => `
      <li>
        <p class="gv-results__name">
          <button type="button" data-index="${index}">${esc(item.title || 'Unnamed company')}</button>
        </p>
        <p class="gv-results__meta">
          ${esc(item.company_number)}
          ${item.company_status ? ' &middot; ' + esc(item.company_status) : ''}
          ${item.date_of_creation ? ' &middot; since ' + esc(item.date_of_creation) : ''}
          ${item.address_snippet ? `<br>${esc(item.address_snippet)}` : ''}
        </p>
      </li>`).join('')}</ul>`;

  $('#company-results').querySelectorAll('button[data-index]').forEach((button) => {
    button.addEventListener('click', () => setSelection(results[Number(button.dataset.index)]));
  });
}

function lookupMessage(html) {
  $('#company-results').innerHTML = html;
}

async function findCompany(event) {
  if (event) event.preventDefault();

  const raw = $('#company-query').value.trim().replace(/\s+/g, ' ');
  if (raw.length < 2) {
    lookupMessage('<p class="gv-error">Enter a company name or number.</p>');
    $('#company-query').focus();
    return;
  }

  setSelection(null);
  const candidate = companyNumberCandidate(raw);
  lookupMessage('<p class="gv-muted gv-small"><span class="gv-spinner"></span> Searching the register&hellip;</p>');

  try {
    const payload = await api(`/companies/search?q=${encodeURIComponent(candidate || raw)}&limit=15`);
    const results = (payload && payload.results) || [];

    // An exact number match is not ambiguous, so do not make them click it.
    const exact = candidate && results.find((item) => item.company_number === candidate);
    if (exact) { setSelection(exact); return; }

    if (results.length) { renderCandidates(results, raw); return; }

    lookupMessage(candidate
      ? `<p class="gv-error">No company on the register has the number ${esc(candidate)}.</p>`
      : `<p class="gv-error">No company matched &ldquo;${esc(raw)}&rdquo;.</p>`);
  } catch (err) {
    if (err.status === 401) return;
    // A search outage should not block an operator who already knows the number.
    lookupMessage(`<p class="gv-error">${esc(err.message)}</p>`
      + (candidate
        ? `<p><button type="button" class="gv-button gv-button--secondary gv-button--small"
             id="use-anyway">Audit ${esc(candidate)} anyway</button></p>`
        : ''));
    const anyway = $('#use-anyway');
    if (anyway) {
      anyway.addEventListener('click', () =>
        setSelection({ company_number: candidate, title: `Company ${candidate}`, company_status: '' }));
    }
  }
}

/* ------------------------------------------------------------------ upload */

function renderFileList() {
  const list = $('#file-list');
  list.innerHTML = state.files.map((file, index) => `
    <li><span>${esc(file.name)} <span class="gv-muted">(${num(Math.round(file.size / 1024))} KB)</span></span>
    <button type="button" data-index="${index}">Remove</button></li>`).join('');
  list.querySelectorAll('button[data-index]').forEach((button) => {
    button.addEventListener('click', () => {
      state.files.splice(Number(button.dataset.index), 1);
      renderFileList();
    });
  });
}

function addFiles(fileList) {
  const allowed = ['.csv', '.md', '.pdf', '.txt'];
  Array.from(fileList).forEach((file) => {
    const ext = file.name.slice(file.name.lastIndexOf('.')).toLowerCase();
    if (allowed.includes(ext) && state.files.length < 25) state.files.push(file);
  });
  renderFileList();
}

function readAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',')[1] || '');
    reader.onerror = () => reject(new Error(`Could not read ${file.name}`));
    reader.readAsDataURL(file);
  });
}

/* ------------------------------------------------------------------ submit */

async function submitAudit() {
  const button = $('#submit-audit');
  const errorBox = $('#submit-error');
  errorBox.innerHTML = '';

  if (!state.company) {
    errorBox.innerHTML = '<p class="gv-error">Find a company first.</p>';
    $('#company-query').focus();
    return;
  }
  const crn = state.company.company_number;

  button.disabled = true;
  button.textContent = 'Starting\u2026';
  try {
    let dataRoomPath = '';
    if (state.files.length) {
      button.textContent = 'Uploading documents\u2026';
      const files = await Promise.all(state.files.map(async (file) => ({
        name: file.name, content_base64: await readAsBase64(file),
      })));
      const uploaded = await api('/data-rooms', {
        method: 'POST', body: JSON.stringify({ files, submitted_by: 'console' }),
      });
      dataRoomPath = (uploaded && uploaded.data_room_path) || '';
    }
    const job = await api('/jobs', {
      method: 'POST',
      body: JSON.stringify({ crn, data_room_path: dataRoomPath, submitted_by: 'console' }),
    });
    requestNotificationPermission();
    startWatching(job.job_id);
  } catch (err) {
    errorBox.innerHTML = `<p class="gv-error">${esc(err.message)}</p>`;
  } finally {
    button.disabled = !state.company;
    button.textContent = 'Start audit';
  }
}

/* --------------------------------------------------------------- live job */

function startWatching(jobId) {
  state.jobId = jobId;
  state.openTab = 'report';
  $('#console-placeholder').hidden = true;
  $('#console-job').hidden = false;
  if (state.poll) clearInterval(state.poll);
  refreshJob();
  state.poll = setInterval(refreshJob, POLL_MS);
}

async function refreshJob() {
  if (!state.jobId) return;
  try {
    const job = await api(`/jobs/${encodeURIComponent(state.jobId)}`);
    if (!job) return;
    renderJob(job);
    if (TERMINAL.includes(job.status)) {
      clearInterval(state.poll);
      state.poll = null;
      announce(job);
    }
  } catch (err) {
    if (err.status !== 401) {
      $('#console-job').innerHTML = notice('Could not load the job', esc(err.message), true);
      clearInterval(state.poll);
      state.poll = null;
    }
  }
}

function requestNotificationPermission() {
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission().catch(() => {});
  }
}

function announce(job) {
  if (state.announced[job.job_id] === job.status) return;
  state.announced[job.job_id] = job.status;
  document.title = `${job.status === 'SUCCEEDED' ? 'Complete' : titleCase(job.status)} - DueDiligence Direct`;
  const verdict = ((job.result || {}).red_flag_verdict || {}).recommendation;
  const body = job.status === 'SUCCEEDED'
    ? `${job.crn}: ${verdict || 'audit complete'}`
    : `${job.crn}: audit ${job.status.toLowerCase()}`;
  if ('Notification' in window && Notification.permission === 'granted') {
    try { new Notification('Due diligence audit finished', { body, tag: job.job_id }); } catch (err) { /* ignore */ }
  }
}

function elapsedSeconds(job) {
  if (!job.started_at || !job.finished_at) return null;
  const delta = (new Date(job.finished_at) - new Date(job.started_at)) / 1000;
  return Number.isFinite(delta) ? Math.round(delta) : null;
}

function renderJob(job) {
  const events = job.events || [];
  const stages = events.filter((event) => !(event.attributes || {}).exchange);
  const running = !TERMINAL.includes(job.status);

  let html = `
    <div class="gv-actions" style="justify-content:space-between;margin-bottom:20px">
      <div>
        <h1 style="margin-bottom:6px">Audit ${esc(job.crn)}</h1>
        <p class="gv-small gv-muted" style="margin:0">
          Job ${esc(job.job_id)} &mdash; ${statusTag(job.status)}
          ${job.trace_id ? ` &mdash; trace <span class="gv-mono">${esc(job.trace_id)}</span>` : ''}
        </p>
      </div>
      <div class="gv-actions">
        ${running ? `<button class="gv-button gv-button--warning gv-button--small" type="button" id="cancel-job">Cancel</button>` : ''}
        <button class="gv-button gv-button--secondary gv-button--small" type="button" id="new-audit">New audit</button>
      </div>
    </div>`;

  if (running) {
    html += `<p><span class="gv-spinner"></span> <strong>The fleet is working.</strong>
      <span class="gv-muted">Agents run server-side; you can close this tab and come back.</span></p>`;
  }

  html += `<ul class="gv-steps">${stages.map((event) => `
    <li>
      <span class="gv-steps__time">${esc(clock(event.timestamp))}</span>
      <span class="gv-steps__name">${esc(titleCase(event.stage))}</span>
      <span class="gv-muted gv-small" style="flex:2 1 50%">${esc(event.message)}</span>
    </li>`).join('')}</ul>`;

  if (job.status === 'FAILED') {
    html += notice('The audit failed', esc(job.error || 'No error detail was recorded.'), true);
  } else if (job.status === 'CANCELLED') {
    html += notice('Cancelled', 'An operator cancelled this audit before it finished.');
  } else if (job.status === 'INTERRUPTED') {
    html += notice('Interrupted', 'The runtime restarted while this audit was in flight. Submit it again.');
  }

  if (job.status === 'SUCCEEDED' && job.result) {
    const seconds = elapsedSeconds(job);
    html += renderReport(job.result, seconds);
  } else if (running) {
    html += renderChain(events, true);
  }

  $('#console-job').innerHTML = html;

  const cancel = $('#cancel-job');
  if (cancel) cancel.addEventListener('click', async () => {
    cancel.disabled = true;
    try { await api(`/jobs/${encodeURIComponent(job.job_id)}/cancel`, { method: 'POST' }); refreshJob(); }
    catch (err) { cancel.disabled = false; }
  });
  const fresh = $('#new-audit');
  if (fresh) fresh.addEventListener('click', () => {
    if (state.poll) { clearInterval(state.poll); state.poll = null; }
    state.jobId = null;
    document.title = 'Due diligence audit - DueDiligence Direct';
    $('#console-job').hidden = true;
    $('#console-placeholder').hidden = false;
    loadRecentJobs();
  });

  wireTabs();
  wireDownload(job.job_id);
}

/* ------------------------------------------------------------ chain / tabs */

function renderChain(events, live) {
  const exchanges = (events || []).filter((event) => (event.attributes || {}).exchange);
  if (!exchanges.length) {
    return live ? muted('The agents have not exchanged anything yet.') : '';
  }
  return `<h2>Agent conversation</h2>
    <p class="gv-muted gv-small">Each entry is a typed message one agent sent another, recorded as it happened.</p>
    <ul class="gv-chain">${exchanges.map((event) => {
      const attrs = event.attributes || {};
      const extra = Object.entries(attrs)
        .filter(([key]) => !['exchange', 'sender', 'recipient', 'kind'].includes(key))
        .map(([key, value]) => `${key}=${typeof value === 'object' ? JSON.stringify(value) : value}`)
        .join('  ');
      return `<li>
        <span class="gv-chain__dot" aria-hidden="true"></span>
        <div class="gv-chain__route">${esc(titleCase(attrs.sender || 'agent'))}
          <span class="gv-chain__arrow" aria-label="sends to">&rarr;</span>
          ${esc(titleCase(attrs.recipient || 'agent'))}</div>
        <div class="gv-chain__meta">${esc(clock(event.timestamp))} &middot; ${esc(attrs.kind || 'message')}</div>
        <div class="gv-chain__msg">${esc(event.message)}</div>
        ${extra ? `<div class="gv-chain__attrs">${esc(extra)}</div>` : ''}
      </li>`;
    }).join('')}</ul>`;
}

function chainFromState(chain) {
  if (!chain || !chain.length) return muted('No inter-agent exchanges were recorded.');
  return `<p class="gv-muted gv-small">Each entry is a typed message one agent sent another during this run.</p>
    <ul class="gv-chain">${chain.map((entry) => {
      const extra = Object.entries(entry.attributes || {})
        .map(([key, value]) => `${key}=${typeof value === 'object' ? JSON.stringify(value) : value}`)
        .join('  ');
      return `<li>
        <span class="gv-chain__dot" aria-hidden="true"></span>
        <div class="gv-chain__route">${esc(titleCase(entry.sender))}
          <span class="gv-chain__arrow" aria-label="sends to">&rarr;</span>
          ${esc(titleCase(entry.recipient))}</div>
        <div class="gv-chain__meta">#${esc(entry.seq)} &middot; ${esc(clock(entry.timestamp))} &middot; ${esc(entry.kind)}</div>
        <div class="gv-chain__msg">${esc(entry.message)}</div>
        ${extra ? `<div class="gv-chain__attrs">${esc(extra)}</div>` : ''}
      </li>`;
    }).join('')}</ul>`;
}

function tabs(items) {
  const list = items.map((item) => `<li><button type="button" role="tab" data-tab="${esc(item.id)}"
      aria-selected="${item.id === state.openTab}" aria-controls="panel-${esc(item.id)}">${esc(item.label)}</button></li>`).join('');
  const panels = items.map((item) => `<div class="gv-tabs__panel" id="panel-${esc(item.id)}" role="tabpanel"
      ${item.id === state.openTab ? '' : 'hidden'}>${item.body || muted('Nothing to show.')}</div>`).join('');
  return `<div class="gv-tabs"><ul class="gv-tabs__list" role="tablist">${list}</ul>${panels}</div>`;
}

function wireTabs() {
  $$('.gv-tabs__list button').forEach((button) => {
    button.addEventListener('click', () => {
      const target = button.dataset.tab;
      state.openTab = target;
      $$('.gv-tabs__list button').forEach((other) =>
        other.setAttribute('aria-selected', String(other.dataset.tab === target)));
      $$('.gv-tabs__panel').forEach((panel) => { panel.hidden = panel.id !== `panel-${target}`; });
    });
  });
}

function wireDownload(jobId) {
  const button = $('#download-pdf');
  if (!button) return;
  button.addEventListener('click', async () => {
    button.disabled = true;
    const original = button.textContent;
    button.textContent = 'Preparing…';
    try {
      const response = await fetch(`/jobs/${encodeURIComponent(jobId)}/report.pdf`, { credentials: 'same-origin' });
      if (!response.ok) throw new ApiError(`Export failed (${response.status})`, response.status);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `red-flag-report-${jobId}.pdf`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
    } catch (err) {
      button.insertAdjacentHTML('afterend', `<span class="gv-error">${esc(err.message)}</span>`);
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  });
}

/* ------------------------------------------------------------------ report */

const VERDICT_TAG = {
  'GREEN LIGHT': 'green',
  'PROCEED WITH CAUTION': 'orange',
  'RED FLAG DEAL BREAKER': 'red',
};

const PANEL_CLASS = {
  'GREEN LIGHT': 'gv-panel--green',
  'PROCEED WITH CAUTION': 'gv-panel--amber',
  'RED FLAG DEAL BREAKER': 'gv-panel--red',
};

function renderReport(result, seconds) {
  const verdict = result.red_flag_verdict || {};
  const governance = result.governance || {};
  const counts = governance.severity_counts || {};
  const profile = pick(result.raw_statutory_data, 'profile');
  const recommendation = verdict.recommendation || 'UNKNOWN';

  let html = `<div class="gv-panel ${PANEL_CLASS[recommendation] || 'gv-panel--grey'}">
    <h2 class="gv-panel__title" style="margin:0">${esc(recommendation)}</h2>
    <div class="gv-panel__body">${esc(profile.company_name || result.crn)}<br>
      <span class="gv-small">Company number ${esc(result.crn)}${seconds ? ` &middot; audited in ${seconds}s` : ''}</span>
    </div></div>`;

  html += `<div class="gv-stats">
    ${['HIGH', 'MEDIUM', 'LOW', 'CLEAR'].map((key) => `
      <div class="gv-stat"><div class="gv-stat__label">${key}</div>
      <div class="gv-stat__value">${num(counts[key] || 0)}</div></div>`).join('')}
    <div class="gv-stat"><div class="gv-stat__label">Docs quarantined</div>
      <div class="gv-stat__value">${num(governance.documents_quarantined || 0)}</div></div>
    <div class="gv-stat"><div class="gv-stat__label">Unverified citations</div>
      <div class="gv-stat__value">${num(governance.unverified_citations || 0)}</div></div>
  </div>`;

  html += `<div class="gv-actions" style="margin-bottom:25px">
    <button class="gv-button gv-button--small" type="button" id="download-pdf">Download the Red Flag Report (PDF)</button>
  </div>`;

  html += tabs([
    { id: 'report', label: 'Report', body: reportTab(verdict, result) },
    { id: 'company', label: 'Company', body: companyTab(result.raw_statutory_data) },
    { id: 'conversation', label: 'Agent conversation', body: chainFromState(result.reasoning_chain) },
    { id: 'accounts', label: 'Filed accounts', body: accountsTab(result.accounts) },
    { id: 'legal', label: 'Legal', body: agentTab(result.legal_risks, 'risks', 'overall_legal_status') },
    { id: 'financial', label: 'Financial', body: agentTab(result.financial_analysis, 'findings', 'overall_financial_status') },
    { id: 'debate', label: 'Debate', body: debateTab(result.debate_transcript) },
    { id: 'documents', label: 'Data room', body: dataRoomTab(result.data_room) },
    { id: 'governance', label: 'Governance', body: governanceTab(governance) },
    { id: 'memory', label: 'Memory', body: memoryTab(result.memory) },
  ]);

  return html;
}

function reportTab(verdict, result) {
  const risks = verdict.top_risks || [];
  const review = verdict.required_human_review || [];
  let html = '';
  if (verdict.executive_summary) {
    html += `<h3 style="margin-top:0">Executive summary</h3><p>${esc(verdict.executive_summary)}</p>`;
  }
  if (risks.length) {
    html += `<h3>Top risks</h3><ul>${risks.map((risk) => `<li>${esc(risk)}</li>`).join('')}</ul>`;
  }
  if (review.length) {
    html += `<h3>Requires human review</h3><ul>${review.map((item) => `<li>${esc(item)}</li>`).join('')}</ul>`;
  }
  const armor = (result.governance || {}).armor_verdict;
  if (armor && armor !== 'CLEAN') {
    html += warning(`Model Armor screened this report: ${armor}.`);
    const violations = (result.governance || {}).armor_violations || [];
    if (violations.length) html += muted(`Actions taken: ${violations.join(', ')}`);
  }
  if (verdict.reliance_disclaimer) {
    html += `<div class="gv-inset"><p class="gv-small" style="margin:0">${esc(verdict.reliance_disclaimer)}</p></div>`;
  }
  return html || muted('The synthesizer returned no report.');
}

function agentTab(agent, itemsKey, statusKey) {
  if (!agent) return muted('This agent did not run.');
  const items = agent[itemsKey] || [];
  let html = '';
  if (agent[statusKey]) {
    html += `<div class="gv-inset"><strong>Overall position</strong><br>${esc(agent[statusKey])}</div>`;
  }
  if (agent.accounts_status) html += `<p>${esc(agent.accounts_status)}</p>`;
  html += items.map(findingCard).join('') || muted('No findings recorded.');
  const limits = agent.limitations || [];
  if (limits.length) {
    html += `<details class="gv-details"><summary>What this agent could not check (${limits.length})</summary>
      <ul>${limits.map((item) => `<li>${esc(item)}</li>`).join('')}</ul></details>`;
  }
  html += muted(`Model ${agent.model_used || 'unknown'} · ${num(agent.model_latency_ms)}ms · `
    + `${num(agent.prompt_tokens)} prompt + ${num(agent.output_tokens)} output tokens`);
  return html;
}

function debateTab(debate) {
  if (!debate) return muted('The debate agent did not run.');
  const points = debate.points || [];
  let html = '';
  if (debate.risk_reward_summary) {
    html += `<div class="gv-inset"><strong>Risk and reward</strong><br>${esc(debate.risk_reward_summary)}</div>`;
  }
  html += points.map((point) => `
    <div class="gv-finding gv-finding--${esc(String(point.severity || 'LOW').toUpperCase())}">
      <div class="gv-finding__head">
        <span class="gv-finding__category">${esc(point.issue || 'Contested point')}</span>
        ${severityTag(point.severity)}
      </div>
      ${summaryList([
        ['Legal agent', esc(point.legal_view)],
        ['Financial agent', esc(point.financial_view)],
        ['Resolved position', `<strong>${esc(point.resolved_position)}</strong>`],
      ])}
    </div>`).join('') || muted('The agents found nothing to contest.');
  html += muted(`Model ${debate.model_used || 'unknown'} · ${num(debate.model_latency_ms)}ms · `
    + `${num(debate.prompt_tokens)} prompt + ${num(debate.output_tokens)} output tokens`);
  return html;
}

/* -------------------------------------------------------------- company tab */

function companyTab(bundle) {
  if (!bundle) return muted('No statutory records were retrieved for this run.');
  const profile = pick(bundle, 'profile');
  const accounts = profile.accounts || {};
  const confirmation = profile.confirmation_statement || {};

  const flags = [
    profile.has_charges ? tag('has charges', 'orange') : '',
    profile.has_insolvency_history ? tag('insolvency history', 'red') : '',
    profile.has_been_liquidated ? tag('has been liquidated', 'red') : '',
  ].filter(Boolean).join(' ') || tag('none', 'green');

  let html = `<h3 style="margin-top:0">${esc(profile.company_name || 'Unknown company')}
    ${statusTag(profile.company_status)}</h3>
    <p class="gv-muted gv-small" style="margin-top:-8px">Company number ${esc(profile.company_number || '-')}</p>
    <div class="gv-two">
      <div>${summaryList([
        ['Registered office address', esc(address(profile.registered_office_address)) || 'Not recorded'],
        ['Company type', esc(String(profile.type || '-').toUpperCase())],
        ['Incorporated on', esc(profile.date_of_creation)],
        ['Jurisdiction', esc(profile.jurisdiction)],
        ['Nature of business (SIC)', esc((profile.sic_codes || []).join(', ')) || 'Not recorded'],
      ])}</div>
      <div>${summaryList([
        ['Next accounts due', esc(accounts.next_due)],
        ['Accounts overdue', accounts.overdue ? tag('overdue', 'red') : tag('no', 'green')],
        ['Last accounts made up to', esc((accounts.last_accounts || {}).made_up_to)],
        ['Next confirmation statement due', esc(confirmation.next_due)],
        ['Register flags', flags],
      ])}</div>
    </div>`;

  if (profile.registered_office_is_in_dispute) {
    html += warning('The registered office address is recorded as in dispute.');
  }

  const previous = profile.previous_company_names || [];
  html += `<h3>Previous company names</h3>`;
  html += previous.length
    ? table('', ['Name', 'Effective from', 'Ceased on'],
        previous.map((entry) => [esc(entry.name), esc(entry.effective_from), esc(entry.ceased_on)]))
    : muted('The company has never traded under a different registered name.');

  const officers = pick(bundle, 'officers');
  const officerItems = officers.items || [];
  html += `<h3>Officers <span class="gv-muted gv-small">(${num(officers.active_count || 0)} active,
    ${num(officers.resigned_count || 0)} resigned)</span></h3>`;
  html += officerItems.length
    ? table('', ['Name', 'Role', 'Status', 'Appointed', 'Resigned', 'Nationality', 'Occupation', 'Born'],
        officerItems.map((item) => [
          esc(item.name), esc(titleCase(item.officer_role)),
          item.resigned_on ? tag('resigned', 'grey') : tag('active', 'green'),
          esc(item.appointed_on), esc(item.resigned_on), esc(item.nationality), esc(item.occupation),
          esc(item.date_of_birth ? `${(item.date_of_birth || {}).month || ''}/${(item.date_of_birth || {}).year || ''}` : ''),
        ]))
    : muted(`No officer records returned (endpoint status: ${(bundle.officers || {}).status}).`);

  const pscs = pick(bundle, 'pscs');
  const pscItems = pscs.items || [];
  html += `<h3>Persons with significant control <span class="gv-muted gv-small">(${pscItems.length})</span></h3>`;
  html += pscItems.length
    ? table('', ['Name', 'Kind', 'Notified on', 'Ceased on', 'Nature of control', 'Nationality'],
        pscItems.map((item) => [
          esc(item.name), esc(titleCase(item.kind)), esc(item.notified_on), esc(item.ceased_on),
          esc((item.natures_of_control || []).map(titleCase).join('; ')), esc(item.nationality),
        ]))
    : `${muted(`No PSC records returned (endpoint status: ${(bundle.pscs || {}).status}).`)}
       ${warning('A company with no identified person with significant control is itself a KYB question.')}`;

  const charges = pick(bundle, 'charges');
  const chargeItems = charges.items || [];
  html += `<h3>Charges and mortgages <span class="gv-muted gv-small">(${num(charges.total_count || chargeItems.length)})</span></h3>`;
  html += chargeItems.length
    ? table('', ['Code', 'Status', 'Created', 'Delivered', 'Satisfied', 'Classification', 'Persons entitled'],
        chargeItems.map((item) => [
          esc(item.charge_code || item.id),
          statusTag(String(item.status || '').includes('satisfied') ? 'dissolved' : 'active'),
          esc(item.created_on), esc(item.delivered_on), esc(item.satisfied_on),
          esc((item.classification || {}).description),
          esc((item.persons_entitled || []).map((entry) => entry.name).join('; ')),
        ]))
    : muted('No registered charges, debentures or mortgages.');

  const insolvency = pick(bundle, 'insolvency');
  const cases = insolvency.cases || [];
  html += `<h3>Insolvency <span class="gv-muted gv-small">(${cases.length} case${cases.length === 1 ? '' : 's'})</span></h3>`;
  html += cases.length
    ? table('', ['Type', 'Number', 'Dates', 'Practitioners'],
        cases.map((item) => [
          esc(titleCase(item.type)), esc(item.number),
          esc((item.dates || []).map((d) => `${d.type}=${d.date}`).join('; ')),
          esc((item.practitioners || []).map((p) => p.name).join('; ')),
        ]))
    : muted('No insolvency cases on the register.');

  const filings = pick(bundle, 'filings');
  const filingItems = filings.items || [];
  if (filingItems.length) {
    html += `<h3>Recent filings <span class="gv-muted gv-small">(${filingItems.length})</span></h3>`;
    html += table('', ['Date', 'Type', 'Category', 'Description'],
      filingItems.map((item) => [
        esc(item.date), esc(item.type), esc(titleCase(item.category)),
        esc(titleCase(item.description)),
      ]));
  }

  html += `<details class="gv-details"><summary>Raw Companies House payload (every endpoint)</summary>
    <div class="gv-quote" style="max-height:420px;overflow:auto">${esc(JSON.stringify(bundle, null, 2))}</div></details>`;
  return html;
}

/* ------------------------------------------------------------- accounts tab */

const METRIC_LABELS = {
  current_assets: 'Current assets',
  creditors_within_one_year: 'Creditors within one year',
  net_current_assets: 'Net current assets',
  total_assets_less_current_liabilities: 'Total assets less current liabilities',
  net_assets: 'Net assets',
  equity: 'Equity',
  fixed_assets: 'Fixed assets',
  creditors_after_one_year: 'Creditors after one year',
  cash: 'Cash at bank and in hand',
  employees: 'Average employees',
  current_ratio: 'Current ratio',
  working_capital: 'Working capital',
};

function accountsTab(accounts) {
  if (!accounts || accounts.status !== 'success') {
    return notice('No filed accounts were parsed',
      esc((accounts || {}).message || 'Companies House returned no machine-readable accounts for this company.'), true);
  }
  const latest = accounts.latest || {};
  const analysis = latest.analysis || {};
  const periods = analysis.periods || [];

  let html = `<div class="gv-inset"><p style="margin:0">
    Every figure below was parsed from the iXBRL document the company itself filed at
    Companies House &mdash; not from a model, and not from sample data.</p></div>`;

  html += summaryList([
    ['Entity name', esc(analysis.entity_name)],
    ['Balance sheet date', esc(analysis.balance_sheet_date)],
    ['Filing description', esc(titleCase(latest.description))],
    ['Filed on', esc(latest.filing_date)],
    ['Made up to', esc(latest.made_up_date)],
    ['Filings examined', num(accounts.filings_examined)],
    ['Facts extracted', num(analysis.fact_count)],
    ['Source document', latest.document_url
      ? `<a href="${esc(latest.document_url)}" rel="noreferrer noopener" target="_blank">Companies House document API</a>
         <span class="gv-muted gv-small">(${num(latest.document_bytes)} bytes, ${num(latest.pages)} pages)</span>`
      : '-'],
  ]);

  periods.forEach((period) => {
    const metrics = period.metrics || {};
    const evidence = period.evidence || {};
    html += `<h3>Period ending ${esc(period.period_end)}</h3>`;
    html += table('Balance sheet', ['Metric', 'Value', 'iXBRL fact'],
      Object.keys(metrics).map((key) => [
        esc(METRIC_LABELS[key] || titleCase(key)),
        num(metrics[key], key === 'current_ratio' ? 3 : 0),
        `<span class="gv-mono gv-small">${esc(evidence[key] || 'derived')}</span>`,
      ]), { numeric: [1] });

    const checks = period.reconciliation || [];
    if (checks.length) {
      const failed = checks.filter((check) => !check.consistent);
      html += table('Balance sheet reconciliation', ['Identity', 'Formula', 'Expected', 'Filed', 'Difference', 'Result'],
        checks.map((check) => [
          esc(titleCase(check.identity)),
          `<span class="gv-mono gv-small">${esc(check.formula)}</span>`,
          num(check.expected), num(check.reported), num(check.difference),
          check.consistent ? tag('consistent', 'green') : tag('fails', 'red'),
        ]), { numeric: [2, 3, 4] });
      if (failed.length) {
        html += warning(`${failed.length} balance sheet identit${failed.length === 1 ? 'y does' : 'ies do'} not reconcile in the filed accounts. Ratios that depend on them are suppressed rather than reported.`);
      }
    }

    const signals = period.signals || analysis.signals || [];
    if (signals.length) {
      html += `<h4>Derived signals</h4><ul>${signals.map((signal) =>
        `<li>${esc(typeof signal === 'string' ? signal : signal.message || JSON.stringify(signal))}</li>`).join('')}</ul>`;
    }
  });

  const derived = analysis.derived || {};
  if (Object.keys(derived).length) {
    html += table('Year-on-year movement', ['Measure', 'Value'],
      Object.entries(derived).map(([key, value]) => [
        esc(METRIC_LABELS[key] || titleCase(key)),
        typeof value === 'number' ? num(value, 2) : esc(String(value)),
      ]), { numeric: [1] });
  }

  const errors = analysis.parse_errors || [];
  if (errors.length) {
    html += `<details class="gv-details"><summary>Parser notes (${errors.length})</summary>
      <ul>${errors.map((item) => `<li class="gv-small">${esc(item)}</li>`).join('')}</ul></details>`;
  }
  return html;
}

/* ----------------------------------------------------------- data room tab */

function dataRoomTab(room) {
  if (!room || room.status === 'not_provided') {
    return `${muted((room || {}).message || 'No deal documents were supplied; the audit ran on statutory records alone.')}
      <p>Upload contracts on the next run to add clause-level analysis to the report.</p>`;
  }
  const documents = room.documents || [];
  let html = summaryList([
    ['Status', statusTag(room.status)],
    ['Documents ingested', num(documents.length)],
    ['Quarantined', num((room.quarantined || []).length)],
  ]);

  if (room.triage) {
    html += `<h3>Document triage</h3>`;
    html += muted(`Classified by ${room.triage.model || 'deterministic fallback'}.`);
    const classified = room.triage.documents || room.triage.results || [];
    if (classified.length) {
      html += table('', ['Document', 'Type', 'Confidence'],
        classified.map((item) => [
          esc(item.file_name || item.name), esc(titleCase(item.document_type || item.label)),
          item.confidence !== undefined ? num(item.confidence, 2) : '-',
        ]), { numeric: [2] });
    }
  }

  if (room.semantic_clauses) {
    const matches = room.semantic_clauses.matches || [];
    html += `<h3>Clause detection</h3>`;
    html += muted(`Embedding scan by ${room.semantic_clauses.model || 'deterministic fallback'}.`);
    html += matches.length
      ? table('', ['Clause', 'Document', 'Similarity', 'Extract'],
          matches.map((item) => [
            esc(titleCase(item.clause || item.label)), esc(item.file_name),
            num(item.score, 3), `<span class="gv-small">${esc(item.segment || item.text)}</span>`,
          ]), { numeric: [2] })
      : muted('No clauses passed the similarity and margin thresholds.');
  }

  if (documents.length) {
    html += `<h3>Documents</h3>`;
    html += table('', ['File', 'Characters', 'Notes'],
      documents.map((item) => [
        esc(item.file_name || item.name), num(item.characters || (item.text || '').length),
        esc(item.note || ''),
      ]), { numeric: [1] });
  }

  const errors = room.errors || [];
  if (errors.length) {
    html += `<details class="gv-details"><summary>Ingestion errors (${errors.length})</summary>
      <ul>${errors.map((item) => `<li class="gv-small">${esc(typeof item === 'string' ? item : JSON.stringify(item))}</li>`).join('')}</ul></details>`;
  }
  return html;
}

/* ---------------------------------------------------------- governance tab */

function governanceTab(governance) {
  if (!governance) return muted('No governance record.');
  const usage = governance.token_usage || {};
  const tiers = governance.model_tiers || {};
  const byCall = usage.by_call || [];

  let html = summaryList([
    ['Trace id', `<span class="gv-mono">${esc(governance.trace_id)}</span>`],
    ['Analysis mode', governance.analysis_mode === 'model'
      ? tag('model reasoning', 'green') : tag(governance.analysis_mode || 'unknown', 'yellow')],
    ['Models used', esc((governance.models_used || []).join(', '))],
    ['Agents on deterministic fallback',
      (governance.agents_on_deterministic_fallback || []).length
        ? tag((governance.agents_on_deterministic_fallback || []).join(', '), 'yellow')
        : tag('none', 'green')],
    ['Model Armor verdict', governance.armor_verdict === 'CLEAN'
      ? tag('clean', 'green') : tag(governance.armor_verdict || 'unknown', 'orange')],
    ['Memory written', governance.memory_written ? tag('yes', 'green') : tag('no', 'grey')],
  ]);

  html += `<h3>Model tiers</h3>`;
  html += summaryList([
    ['Reasoning', esc((tiers.reasoning || []).join(', ')) || '-'],
    ['Document triage', esc(tiers.document_triage) || 'not used this run'],
    ['Clause detection', esc(tiers.clause_detection) || 'not used this run'],
  ]);

  html += `<h3>Token usage</h3>`;
  html += `<div class="gv-stats">
    <div class="gv-stat"><div class="gv-stat__label">Model calls</div><div class="gv-stat__value">${num(usage.calls)}</div></div>
    <div class="gv-stat"><div class="gv-stat__label">Prompt tokens</div><div class="gv-stat__value">${num(usage.prompt_tokens)}</div></div>
    <div class="gv-stat"><div class="gv-stat__label">Output tokens</div><div class="gv-stat__value">${num(usage.output_tokens)}</div></div>
    <div class="gv-stat"><div class="gv-stat__label">Total tokens</div><div class="gv-stat__value">${num(usage.total_tokens)}</div></div>
    <div class="gv-stat"><div class="gv-stat__label">Model latency</div><div class="gv-stat__value">${num(usage.total_model_latency_ms)}<span class="gv-small">ms</span></div></div>
  </div>`;

  if (byCall.length) {
    html += table('Every model call in this run', ['#', 'Schema', 'Model', 'Prompt', 'Output', 'Total', 'Latency'],
      byCall.map((call, index) => [
        index + 1, esc(call.schema), esc(call.model),
        num(call.prompt_tokens), num(call.output_tokens),
        num((call.prompt_tokens || 0) + (call.output_tokens || 0)),
        `${num(call.latency_ms)}ms`,
      ]), { numeric: [0, 3, 4, 5, 6] });
  }

  const errors = governance.model_errors || [];
  if (errors.length) {
    html += `<h3>Model errors</h3><ul>${errors.map((item) =>
      `<li class="gv-small">${esc(typeof item === 'string' ? item : JSON.stringify(item))}</li>`).join('')}</ul>`;
  }

  const versions = governance.registry_versions || {};
  html += `<h3>Agent versions in this run</h3>`;
  html += table('', ['Agent', 'Version'],
    Object.entries(versions).map(([agent, version]) => [esc(titleCase(agent)), `<span class="gv-mono">${esc(version)}</span>`]));
  return html;
}

/* -------------------------------------------------------------- memory tab */

function memoryTab(memory) {
  if (!memory) return muted('No memory record for this company.');
  let html = summaryList([
    ['First audit', memory.is_first_audit ? tag('yes', 'blue') : tag('no', 'grey')],
    ['Prior audits held', num((memory.prior_audits || []).length)],
    ['Operator notes', num((memory.operator_notes || []).length)],
  ]);

  const changes = memory.changes_since_last_audit || [];
  html += `<h3>Changes since the last audit</h3>`;
  html += changes.length
    ? table('', ['Fact', 'Previously', 'Now'],
        changes.map((item) => [
          esc(titleCase(item.fact || item.key)), esc(String(item.previous ?? item.before ?? '')),
          `<strong>${esc(String(item.current ?? item.after ?? ''))}</strong>`,
        ]))
    : muted('Nothing tracked has changed since the fleet last looked at this company.');

  const facts = memory.current_facts || {};
  if (Object.keys(facts).length) {
    html += `<h3>Tracked facts</h3>`;
    html += table('', ['Fact', 'Value'],
      Object.entries(facts).map(([key, value]) => [
        esc(titleCase(key)),
        typeof value === 'boolean' ? (value ? tag('yes', 'orange') : tag('no', 'green'))
          : typeof value === 'number' ? num(value) : esc(String(value)),
      ]));
  }

  const priors = memory.prior_audits || [];
  if (priors.length) {
    html += `<h3>Prior audits</h3>`;
    html += table('', ['Run', 'Recommendation', 'Recorded at'],
      priors.map((item) => [
        `<span class="gv-mono gv-small">${esc(item.run_id || item.job_id || '')}</span>`,
        item.recommendation ? tag(item.recommendation, PANEL_CLASS[item.recommendation] === 'gv-panel--green' ? 'green'
          : PANEL_CLASS[item.recommendation] === 'gv-panel--red' ? 'red' : 'orange') : '-',
        esc(item.recorded_at || item.timestamp || ''),
      ]));
  }

  const notes = memory.operator_notes || [];
  if (notes.length) {
    html += `<h3>Operator notes</h3><ul>${notes.map((note) =>
      `<li>${esc(note.note)} <span class="gv-muted gv-small">&mdash; ${esc(note.author)}, ${esc(note.recorded_at || note.timestamp || '')}</span></li>`).join('')}</ul>`;
  }
  return html;
}


/* ------------------------------------------------------------ audit history */

function duration(job) {
  if (!job.started_at || !job.finished_at) return '';
  const seconds = (new Date(job.finished_at) - new Date(job.started_at)) / 1000;
  if (!Number.isFinite(seconds) || seconds < 0) return '';
  return seconds < 60
    ? `${Math.round(seconds)}s`
    : `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
}

function severityChips(counts) {
  const chips = ['HIGH', 'MEDIUM', 'LOW']
    .filter((key) => (counts || {})[key])
    .map((key) => tag(`${counts[key]} ${key.toLowerCase()}`, SEVERITY_COLOUR[key]));
  return chips.join(' ') || '<span class="gv-muted">&ndash;</span>';
}

async function loadHistory() {
  const body = $('#history-body');
  const pager = $('#history-pager');
  const { offset, limit, query, status } = state.history;

  body.innerHTML = '<p class="gv-muted"><span class="gv-spinner"></span> Loading audits&hellip;</p>';
  pager.innerHTML = '';

  // include_result=false: a list needs a summary, not thirty full reports.
  const params = new URLSearchParams({
    limit: String(limit), offset: String(offset), include_result: 'false',
  });
  // One term, matched against the company name or the number, because an
  // operator looking through a history knows one or the other.
  if (query) params.set('q', query);
  if (status) params.set('status', status);

  try {
    const payload = await api(`/jobs?${params.toString()}`);
    const jobs = (payload && payload.jobs) || [];
    state.history.total = (payload && payload.total) || 0;
    renderHistoryStats();

    if (!jobs.length) {
      body.innerHTML = (query || status)
        ? '<p>No audits match those filters. <button type="button" '
          + 'class="gv-button gv-button--secondary gv-button--small" '
          + 'id="history-empty-clear">Clear filters</button></p>'
        : muted('No audits have been run yet.');
      const clear = $('#history-empty-clear');
      if (clear) clear.addEventListener('click', clearHistoryFilters);
      return;
    }

    body.innerHTML = table('',
      ['Company', 'Status', 'Verdict', 'Findings', 'Tokens', 'Submitted by', 'Started', 'Took', ''],
      jobs.map((job) => {
        const summary = job.summary || {};
        return [
          `<strong>${esc(summary.company_name || job.company_name || '')}</strong><br>`
            + `<span class="gv-mono gv-small">${esc(job.crn)}</span>`,
          statusTag(job.status),
          summary.recommendation
            ? tag(summary.recommendation, VERDICT_TAG[summary.recommendation] || 'grey')
            : '<span class="gv-muted">&ndash;</span>',
          severityChips(summary.severity_counts),
          summary.total_tokens ? num(summary.total_tokens) : '<span class="gv-muted">&ndash;</span>',
          `<span class="gv-small">${esc(job.submitted_by || '')}</span>`,
          `<span class="gv-small">${esc(String(job.created_at || '').slice(0, 16).replace('T', ' '))}</span>`,
          `<span class="gv-small">${esc(duration(job))}</span>`,
          `<button class="gv-button gv-button--secondary gv-button--small" type="button"
             data-job="${esc(job.job_id)}">Open</button>`,
        ];
      }), { numeric: [4] });

    body.querySelectorAll('button[data-job]').forEach((button) => {
      button.addEventListener('click', () => {
        switchView('console');
        startWatching(button.dataset.job);
      });
    });

    renderPager(jobs.length);
  } catch (err) {
    if (err.status !== 401) body.innerHTML = `<p class="gv-error">${esc(err.message)}</p>`;
  }
}

function renderHistoryStats() {
  const { total, query, status } = state.history;
  const filtered = Boolean(query || status);
  $('#history-stats').innerHTML = `<div class="gv-stats">
    <div class="gv-stat">
      <div class="gv-stat__label">${filtered ? 'Matching audits' : 'Total audits'}</div>
      <div class="gv-stat__value">${num(total)}</div>
    </div>
  </div>`;
}

function renderPager(shown) {
  const { offset, limit, total } = state.history;
  const first = total === 0 ? 0 : offset + 1;
  const last = offset + shown;
  const hasPrevious = offset > 0;
  const hasNext = last < total;

  $('#history-pager').innerHTML = `
    <span class="gv-small gv-muted">Showing ${num(first)} to ${num(last)} of ${num(total)}</span>
    <span class="gv-actions">
      <button class="gv-button gv-button--secondary gv-button--small" type="button"
              id="history-prev" ${hasPrevious ? '' : 'disabled'}>Previous</button>
      <button class="gv-button gv-button--secondary gv-button--small" type="button"
              id="history-next" ${hasNext ? '' : 'disabled'}>Next</button>
    </span>`;

  const previous = $('#history-prev');
  const next = $('#history-next');
  if (previous && hasPrevious) {
    previous.addEventListener('click', () => {
      state.history.offset = Math.max(0, offset - limit);
      loadHistory();
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });
  }
  if (next && hasNext) {
    next.addEventListener('click', () => {
      state.history.offset = offset + limit;
      loadHistory();
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });
  }
}

function clearHistoryFilters() {
  state.history = { ...state.history, offset: 0, query: '', status: '' };
  $('#history-query').value = '';
  $('#history-status').value = '';
  loadHistory();
}

/* --------------------------------------------------------- recent job list */

async function loadRecentJobs() {
  const container = $('#recent-jobs');
  try {
    const payload = await api('/jobs?limit=10');
    const jobs = (payload && payload.jobs) || [];
    if (!jobs.length) {
      container.innerHTML = muted('No audits have been run yet. Start one from the panel on the right.');
      return;
    }
    container.innerHTML = table('', ['Company', 'Status', 'Submitted', 'Verdict', ''],
      jobs.map((job) => [
        `<span class="gv-mono">${esc(job.crn)}</span>`,
        statusTag(job.status),
        esc(String(job.created_at || '').slice(0, 19).replace('T', ' ')),
        esc(((job.result || {}).red_flag_verdict || {}).recommendation || ''),
        `<button class="gv-button gv-button--secondary gv-button--small" type="button"
          data-job="${esc(job.job_id)}">Open</button>`,
      ]));
    container.querySelectorAll('button[data-job]').forEach((button) => {
      button.addEventListener('click', () => startWatching(button.dataset.job));
    });
  } catch (err) {
    if (err.status !== 401) container.innerHTML = `<p class="gv-error">${esc(err.message)}</p>`;
  }
}

/* ------------------------------------------------------------ other views */

async function loadRegistry() {
  const container = $('#registry-body');
  try {
    const data = await api('/fleet');
    const agents = data.agents || [];
    const identities = data.identities || [];
    const tools = data.tools || [];
    const runtimeStats = data.runtime || {};

    let html = `<div class="gv-stats">
      <div class="gv-stat"><div class="gv-stat__label">Agents</div><div class="gv-stat__value">${num(agents.length)}</div></div>
      <div class="gv-stat"><div class="gv-stat__label">Registered tools</div><div class="gv-stat__value">${num(tools.length)}</div></div>
      ${Object.entries(runtimeStats).filter(([, value]) => typeof value === 'number').map(([key, value]) =>
        `<div class="gv-stat"><div class="gv-stat__label">${esc(titleCase(key))}</div><div class="gv-stat__value">${num(value)}</div></div>`).join('')}
    </div>`;

    html += agents.map((card) => `<div class="gv-card">
      <h3 style="margin-top:0">${esc(titleCase(card.agent_id))}
        ${tag(`v${card.version}`, 'blue')}
        ${card.status ? statusTag(card.status) : ''}</h3>
      <p>${esc(card.description || card.purpose || '')}</p>
      ${summaryList([
        ['Owner', esc(card.owner)],
        ['Model', esc(card.model)],
        ['Permitted tools', (card.tools || card.permitted_tools || []).map((t) => tag(t, 'grey')).join(' ') || '-'],
        ['Scopes', (card.scopes || []).map((s) => tag(s, 'turquoise')).join(' ') || '-'],
        ['Must not', (card.prohibitions || card.must_not || []).length
          ? `<ul style="margin:0;padding-left:18px">${(card.prohibitions || card.must_not || []).map((p) => `<li>${esc(p)}</li>`).join('')}</ul>`
          : '-'],
      ])}</div>`).join('');

    if (identities.length) {
      html += `<h2>Agent identities</h2>`;
      html += table('', ['Principal', 'Version', 'Scopes'],
        identities.map((item) => [
          esc(item.agent_id || item.principal), esc(item.version),
          (item.scopes || []).map((s) => tag(s, 'turquoise')).join(' '),
        ]));
    }

    if (tools.length) {
      html += `<h2>Gateway tool policies</h2>`;
      html += table('', ['Tool', 'Required scope', 'Allowed callers'],
        tools.map((item) => [
          `<span class="gv-mono">${esc(item.name || item.tool)}</span>`,
          esc(item.required_scope || item.scope),
          (item.allowed_callers || item.callers || []).join(', ') || 'any registered agent',
        ]));
    }

    const hosts = data.allowed_egress_hosts || [];
    if (hosts.length) {
      html += `<h2>Egress allowlist</h2>
        <p class="gv-muted gv-small">Any outbound request to a host outside this list is refused at the gateway.</p>
        <p>${hosts.map((host) => tag(host, 'grey')).join(' ')}</p>`;
    }
    container.innerHTML = html;
  } catch (err) {
    if (err.status !== 401) container.innerHTML = `<p class="gv-error">${esc(err.message)}</p>`;
  }
}

async function loadAudit() {
  const container = $('#audit-body');
  try {
    const payload = await api('/audit?limit=200');
    const records = (payload && payload.records) || [];
    if (!records.length) { container.innerHTML = muted('The audit log is empty.'); return; }
    container.innerHTML = table('', ['Time', 'Event', 'Actor', 'Resource', 'Decision', 'Severity'],
      records.slice().reverse().map((record) => [
        esc(String(record.timestamp || '').slice(0, 19).replace('T', ' ')),
        `<span class="gv-mono gv-small">${esc(record.event)}</span>`,
        esc(record.actor),
        `<span class="gv-small">${esc(record.resource)}</span>`,
        record.decision === 'allow' ? tag('allow', 'green') : tag(record.decision || '-', 'red'),
        record.severity && record.severity !== 'INFO' ? tag(record.severity, 'orange') : tag('info', 'grey'),
      ]));
  } catch (err) {
    if (err.status !== 401) container.innerHTML = `<p class="gv-error">${esc(err.message)}</p>`;
  }
}

async function verifyChain() {
  const result = $('#chain-result');
  result.innerHTML = '<span class="gv-spinner"></span>';
  try {
    const data = await api('/audit/verify');
    result.innerHTML = data.valid
      ? `${tag('chain intact', 'green')} <span class="gv-small gv-muted">${num(data.records ?? data.record_count)} records verified</span>`
      : `${tag('chain broken', 'red')} <span class="gv-small">${esc(data.error || `first bad record: ${data.first_invalid_index}`)}</span>`;
  } catch (err) {
    result.innerHTML = `<span class="gv-error">${esc(err.message)}</span>`;
  }
}

async function loadMemory() {
  const container = $('#memory-body');
  const crn = $('#memory-crn').value.trim().toUpperCase();
  if (!crn) { container.innerHTML = `<p class="gv-error">Enter a company number.</p>`; return; }
  container.innerHTML = '<p class="gv-muted"><span class="gv-spinner"></span> Recalling&hellip;</p>';
  try {
    container.innerHTML = memoryTab(await api(`/memory/${encodeURIComponent(crn)}`));
  } catch (err) {
    if (err.status !== 401) container.innerHTML = `<p class="gv-error">${esc(err.message)}</p>`;
  }
}

/* -------------------------------------------------------------- session */

async function unlock(event) {
  event.preventDefault();
  const box = $('#locked-error');
  box.innerHTML = '';
  try {
    await api('/api/session', {
      method: 'POST', body: JSON.stringify({ code: $('#access-code').value }),
    });
    $('.gv-nav').hidden = false;
    switchView('console');
    boot();
  } catch (err) {
    box.innerHTML = `<p class="gv-error">${err.status === 401 ? 'That access code was not recognised.' : esc(err.message)}</p>`;
  }
}

async function boot() {
  try {
    const info = await fetch('/api', { credentials: 'same-origin' }).then((r) => r.json());
    $('#env-meta').textContent = `${info.environment || 'local'} · ${info.region || ''} · v${info.version || ''}`;
    $('#backend-meta').textContent = `${info.agents_registered || 0} agents registered · model ${info.model || 'unknown'}`;
  } catch (err) { /* the banner is decoration; never block on it */ }
  loadRecentJobs();
}

/* ----------------------------------------------------------------- wiring */

function init() {
  $$('.gv-nav button').forEach((button) =>
    button.addEventListener('click', () => switchView(button.dataset.view)));

  $('#lookup-form').addEventListener('submit', findCompany);
  setSelection(null);

  const dropzone = $('#dropzone');
  const fileInput = $('#file-input');
  dropzone.addEventListener('click', () => fileInput.click());
  dropzone.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); fileInput.click(); }
  });
  fileInput.addEventListener('change', () => { addFiles(fileInput.files); fileInput.value = ''; });
  ['dragenter', 'dragover'].forEach((name) => dropzone.addEventListener(name, (event) => {
    event.preventDefault(); dropzone.classList.add('gv-drop--over');
  }));
  ['dragleave', 'drop'].forEach((name) => dropzone.addEventListener(name, (event) => {
    event.preventDefault(); dropzone.classList.remove('gv-drop--over');
  }));
  dropzone.addEventListener('drop', (event) => addFiles(event.dataTransfer.files));

  $('#history-filters').addEventListener('submit', (event) => {
    event.preventDefault();
    state.history.offset = 0;
    state.history.query = $('#history-query').value.trim();
    state.history.status = $('#history-status').value;
    loadHistory();
  });
  $('#history-clear').addEventListener('click', clearHistoryFilters);
  // Buttons outside the nav that jump to a view. Excluding nav buttons matters:
  // they are already bound above, and binding twice fires two loads per click.
  $$('button[data-view]:not(.gv-nav button)').forEach((button) =>
    button.addEventListener('click', () => switchView(button.dataset.view)));

  $('#submit-audit').addEventListener('click', submitAudit);
  $('#locked-form').addEventListener('submit', unlock);
  $('#verify-chain').addEventListener('click', verifyChain);
  $('#memory-load').addEventListener('click', loadMemory);
  $('#memory-crn').addEventListener('keydown', (event) => { if (event.key === 'Enter') loadMemory(); });

  boot();
}

document.addEventListener('DOMContentLoaded', init);
