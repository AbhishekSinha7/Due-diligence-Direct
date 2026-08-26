/* Charts for the console, as inline SVG.
 *
 * Hand-drawn rather than pulled from a library: the page runs under
 * script-src 'self', these are three specific pictures rather than a general
 * plotting need, and a vendored chart library would outweigh the whole console.
 *
 * Colour follows the job, not decoration:
 *   - the timeline needs no colour identity at all, because each row is labelled;
 *     one hue, and the eye reads position and length instead
 *   - expected-versus-filed is two series, so it gets two validated hues, a
 *     legend, and a direct label on every mark
 *   - net assets over two periods is not a chart at all. Two numbers and a
 *     change are a stat tile; a two-bar chart would be decoration.
 */
'use strict';

(function (global) {
  const INK = '#0b0c0c';
  const MUTED = '#505a5f';
  const RULE = '#b1b4b6';
  const ACCENT = '#1d70b8';   // computed / expected
  const ALERT = '#d4351c';    // what the filing actually says, when it disagrees
  const SURFACE = '#ffffff';

  function esc(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function signed(value, digits) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '-';
    return n.toLocaleString('en-GB', {
      minimumFractionDigits: digits || 0, maximumFractionDigits: digits || 0,
    });
  }

  function seconds(ms) {
    const s = ms / 1000;
    if (s < 1) return `${Math.round(ms)}ms`;
    return s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
  }

  /* ---------------------------------------------------------- run timeline */

  /* What the fleet did, when. Stage events are points in time, so each row spans
   * from a stage's first event to its last — which is what makes the concurrency
   * visible: the legal and financial agents occupy the same seconds. */
  function runTimeline(events, options) {
    const stages = new Map();
    (events || []).forEach((event) => {
      if ((event.attributes || {}).exchange) return;
      const name = event.stage;
      const at = Date.parse(event.timestamp);
      if (!name || !Number.isFinite(at)) return;
      const row = stages.get(name) || { name, start: at, end: at, count: 0 };
      row.start = Math.min(row.start, at);
      row.end = Math.max(row.end, at);
      row.count += 1;
      stages.set(name, row);
    });

    const rows = Array.from(stages.values());
    if (rows.length < 2) return '';

    const first = Math.min(...rows.map((r) => r.start));
    const last = Math.max(...rows.map((r) => r.end));
    const span = Math.max(last - first, 1);

    const rowHeight = 26;
    const barHeight = 12;
    const labelWidth = 150;
    const padRight = 74;
    const top = 26;
    const width = 720;
    const plot = width - labelWidth - padRight;
    const height = top + rows.length * rowHeight + 26;

    const x = (t) => labelWidth + ((t - first) / span) * plot;

    // Ticks at whole seconds, as many as fit without crowding.
    const totalSeconds = span / 1000;
    const step = totalSeconds <= 12 ? 2 : totalSeconds <= 40 ? 10 : totalSeconds <= 120 ? 30 : 60;
    const ticks = [];
    for (let t = 0; t <= totalSeconds; t += step) ticks.push(t);

    const grid = ticks.map((t) => {
      const px = x(first + t * 1000);
      return `<line x1="${px}" y1="${top - 8}" x2="${px}" y2="${height - 26}"
                stroke="${RULE}" stroke-width="1" opacity="0.5"/>
              <text x="${px}" y="${top - 14}" font-size="11" fill="${MUTED}"
                text-anchor="middle">${t}s</text>`;
    }).join('');

    const bars = rows.map((row, index) => {
      const y = top + index * rowHeight;
      const left = x(row.start);
      // An instantaneous stage still needs to be visible.
      const barWidth = Math.max(x(row.end) - left, 3);
      const elapsed = row.end - row.start;
      return `
        <text x="${labelWidth - 10}" y="${y + barHeight - 1}" font-size="12" fill="${INK}"
          text-anchor="end">${esc(row.name)}</text>
        <rect x="${left}" y="${y}" width="${barWidth}" height="${barHeight}" rx="3"
          fill="${ACCENT}">
          <title>${esc(row.name)} — ${seconds(elapsed)}, ${row.count} event${row.count === 1 ? '' : 's'}</title>
        </rect>
        <text x="${left + barWidth + 8}" y="${y + barHeight - 1}" font-size="11" fill="${MUTED}">
          ${elapsed > 400 ? seconds(elapsed) : ''}</text>`;
    }).join('');

    const overlapping = rows.some((a) => rows.some((b) => a !== b && a.start < b.end && b.start < a.end));
    const caption = (options && options.caption) !== undefined
      ? options.caption
      : (overlapping
        ? 'Bars that overlap ran at the same time — the fleet is concurrent, not a queue.'
        : 'Elapsed time from the first stage event to the last.');

    return `<figure class="gv-figure">
      <figcaption class="gv-figure__caption">Stage timeline</figcaption>
      <div class="gv-scroll">
        <svg viewBox="0 0 ${width} ${height}" width="100%" style="max-width:${width}px"
             role="img" aria-label="Timeline of each stage of the run">
          ${grid}
          <line x1="${labelWidth}" y1="${height - 26}" x2="${width - padRight}" y2="${height - 26}"
            stroke="${RULE}" stroke-width="1"/>
          ${bars}
        </svg>
      </div>
      <p class="gv-figure__note">${esc(caption)}</p>
    </figure>`;
  }

  /* ------------------------------------------------- reconciliation checks */

  /* Before-and-after per item is a dumbbell: two marks on one scale with the gap
   * between them carrying the meaning. Here the gap IS the finding — the distance
   * between what the arithmetic gives and what the company filed. */
  function reconciliation(period) {
    const checks = (period && period.reconciliation) || [];
    const usable = checks.filter((c) =>
      Number.isFinite(Number(c.expected)) && Number.isFinite(Number(c.reported)));
    if (!usable.length) return '';

    const values = usable.flatMap((c) => [Number(c.expected), Number(c.reported)]);
    let low = Math.min(...values, 0);
    let high = Math.max(...values, 0);
    const pad = (high - low) * 0.12 || 1;
    low -= pad;
    high += pad;

    const rowHeight = 46;
    const labelWidth = 210;
    const padRight = 20;
    const width = 720;
    const top = 34;
    const plot = width - labelWidth - padRight;
    const height = top + usable.length * rowHeight + 10;
    const x = (v) => labelWidth + ((v - low) / (high - low)) * plot;
    const zero = x(0);

    const rows = usable.map((check, index) => {
      const y = top + index * rowHeight;
      const expected = Number(check.expected);
      const reported = Number(check.reported);
      const ex = x(expected);
      const rx = x(reported);
      const mid = y + 10;
      const agrees = Boolean(check.consistent);
      const filedFill = agrees ? ACCENT : ALERT;

      // When the two coincide, one mark is drawn over the other; a surface ring
      // keeps the top one readable rather than letting them merge into a blob.
      return `
        <text x="${labelWidth - 12}" y="${mid + 4}" font-size="12" fill="${INK}"
          text-anchor="end">${esc(String(check.identity || '').replace(/_/g, ' '))}</text>
        <line x1="${Math.min(ex, rx)}" y1="${mid}" x2="${Math.max(ex, rx)}" y2="${mid}"
          stroke="${agrees ? RULE : ALERT}" stroke-width="2" opacity="${agrees ? 0.6 : 0.35}"/>
        <circle cx="${ex}" cy="${mid}" r="12" fill="transparent">
          <title>${esc(check.identity)} — expected ${signed(expected)}</title>
        </circle>
        <circle cx="${ex}" cy="${mid}" r="5" fill="${ACCENT}" stroke="${SURFACE}" stroke-width="2"
          pointer-events="none"/>
        <circle cx="${rx}" cy="${mid}" r="12" fill="transparent">
          <title>${esc(check.identity)} — filed ${signed(reported)}</title>
        </circle>
        <circle cx="${rx}" cy="${mid}" r="5" fill="${filedFill}" stroke="${SURFACE}" stroke-width="2"
          pointer-events="none"/>
        <text x="${labelWidth - 12}" y="${mid + 19}" font-size="10.5" fill="${MUTED}"
          text-anchor="end">${agrees ? 'reconciles' : `out by ${signed(Math.abs(expected - reported))}`}</text>
        ${agrees ? '' : `
          <text x="${ex}" y="${mid - 11}" font-size="10.5" fill="${ACCENT}" text-anchor="middle">${signed(expected)}</text>
          <text x="${rx}" y="${mid - 11}" font-size="10.5" fill="${ALERT}" text-anchor="middle">${signed(reported)}</text>`}`;
    }).join('');

    return `<figure class="gv-figure">
      <figcaption class="gv-figure__caption">Balance sheet identities</figcaption>
      <div class="gv-legend">
        <span><i style="background:${ACCENT}"></i>What the arithmetic gives</span>
        <span><i style="background:${ALERT}"></i>What the company filed, where it differs</span>
      </div>
      <div class="gv-scroll">
        <svg viewBox="0 0 ${width} ${height}" width="100%" style="max-width:${width}px"
             role="img" aria-label="Expected versus filed value for each balance sheet identity">
          <line x1="${zero}" y1="${top - 12}" x2="${zero}" y2="${height - 6}"
            stroke="${RULE}" stroke-width="1"/>
          <text x="${zero}" y="${top - 18}" font-size="11" fill="${MUTED}" text-anchor="middle">0</text>
          ${rows}
        </svg>
      </div>
      <p class="gv-figure__note">Every value is parsed from the company's own filed accounts.
        A gap means the filing disagrees with itself, and any ratio depending on it was suppressed.</p>
    </figure>`;
  }

  /* ------------------------------------------------------------ stat tile */

  /* Two periods and a change is not a chart. It is one number, its direction,
   * and what it came from. */
  function netAssets(periods) {
    const withValue = (periods || [])
      .filter((p) => Number.isFinite(Number((p.metrics || {}).net_assets)))
      .sort((a, b) => String(a.period_end).localeCompare(String(b.period_end)));
    if (!withValue.length) return '';

    const latest = withValue[withValue.length - 1];
    const previous = withValue.length > 1 ? withValue[withValue.length - 2] : null;
    const now = Number(latest.metrics.net_assets);
    const before = previous ? Number(previous.metrics.net_assets) : null;

    let delta = '';
    if (before !== null && before !== 0) {
      const change = ((now - before) / Math.abs(before)) * 100;
      const falling = change < 0;
      delta = `<div class="gv-figure__delta" style="color:${falling ? ALERT : '#00703c'}">
          ${falling ? '&#9660;' : '&#9650;'} ${signed(Math.abs(change), 1)}%
          <span class="gv-muted gv-small" style="color:${MUTED}">
            from ${signed(before)} at ${esc(previous.period_end)}</span>
        </div>`;
    }

    return `<div class="gv-figure gv-figure--tile">
      <div class="gv-figure__caption">Net assets at ${esc(latest.period_end)}</div>
      <div class="gv-figure__value">${signed(now)}</div>
      ${delta}
      <p class="gv-figure__note">Parsed from the filed iXBRL document, not computed by a model.</p>
    </div>`;
  }

  /* ------------------------------------------------------- board over time */

  /* Who sat on the board, and when. Appointments and resignations are dates, so
   * the honest form is an interval per person on one time axis.
   *
   * Emphasis rather than categorical colour: serving officers are the subject and
   * carry the accent, past officers recede to gray. A date where more than one
   * thing happened gets a rule through it — simultaneous board changes are how a
   * change of control looks in the register. */
  function boardTenure(officers, options) {
    const DAY = 86400000;
    const today = (options && options.today) || Date.now();

    const rows = (officers || []).map((officer) => {
      const from = Date.parse(officer.appointed_on);
      if (!Number.isFinite(from)) return null;
      const resignedAt = Date.parse(officer.resigned_on);
      const to = Number.isFinite(resignedAt) ? resignedAt : today;
      return {
        name: String(officer.name || 'Unnamed'),
        role: String(officer.officer_role || '').replace(/-/g, ' '),
        from,
        to,
        serving: !Number.isFinite(resignedAt),
      };
    }).filter(Boolean).sort((a, b) => a.from - b.from);

    if (rows.length < 2) return '';

    const first = Math.min(...rows.map((r) => r.from));
    const span = Math.max(today - first, DAY);

    const rowHeight = 30;
    const barHeight = 13;
    const labelWidth = 200;
    const padRight = 16;
    const width = 720;
    const top = 30;
    const plot = width - labelWidth - padRight;
    const height = top + rows.length * rowHeight + 26;
    const x = (t) => labelWidth + ((t - first) / span) * plot;

    // Year ticks, thinned so they never collide on a long-lived company.
    const startYear = new Date(first).getUTCFullYear();
    const endYear = new Date(today).getUTCFullYear();
    const years = [];
    for (let y = startYear; y <= endYear; y += 1) years.push(y);
    const every = Math.ceil(years.length / 9) || 1;
    const ticks = years.filter((_, i) => i % every === 0).map((year) => ({
      year, at: Date.parse(`${year}-01-01`),
    })).filter((tick) => tick.at >= first - DAY);

    const grid = ticks.map((tick) => `
      <line x1="${x(tick.at)}" y1="${top - 10}" x2="${x(tick.at)}" y2="${height - 26}"
        stroke="${RULE}" stroke-width="1" opacity="0.45"/>
      <text x="${x(tick.at)}" y="${top - 16}" font-size="11" fill="${MUTED}"
        text-anchor="middle">${tick.year}</text>`).join('');

    // Dates where more than one appointment or resignation happened at once.
    const eventDays = new Map();
    rows.forEach((row) => {
      [row.from, row.serving ? null : row.to].forEach((moment) => {
        if (moment === null) return;
        const day = new Date(moment).toISOString().slice(0, 10);
        eventDays.set(day, (eventDays.get(day) || 0) + 1);
      });
    });
    const clustered = [...eventDays.entries()]
      .filter(([, count]) => count > 1)
      .map(([day]) => day)
      .filter((day) => Date.parse(day) > first);

    const markers = clustered.map((day) => {
      const px = x(Date.parse(day));
      return `<line x1="${px}" y1="${top - 6}" x2="${px}" y2="${height - 26}"
                stroke="${ALERT}" stroke-width="1.5" opacity="0.55"/>
              <text x="${px}" y="${height - 12}" font-size="10.5" fill="${ALERT}"
                text-anchor="middle">${esc(day)}</text>`;
    }).join('');

    const bars = rows.map((row, index) => {
      const y = top + index * rowHeight;
      const left = x(row.from);
      const barWidth = Math.max(x(row.to) - left, 3);
      const name = row.name.length > 26 ? `${row.name.slice(0, 25)}\u2026` : row.name;
      return `
        <text x="${labelWidth - 10}" y="${y + barHeight - 2}" font-size="12" fill="${INK}"
          text-anchor="end">${esc(name)}</text>
        <text x="${labelWidth - 10}" y="${y + barHeight + 11}" font-size="10" fill="${MUTED}"
          text-anchor="end">${esc(row.role)}</text>
        <rect x="${left}" y="${y}" width="${barWidth}" height="${barHeight}" rx="3"
          fill="${row.serving ? ACCENT : RULE}">
          <title>${esc(row.name)} — appointed ${new Date(row.from).toISOString().slice(0, 10)}${
            row.serving ? ', still serving' : `, resigned ${new Date(row.to).toISOString().slice(0, 10)}`}</title>
        </rect>`;
    }).join('');

    const note = clustered.length
      ? `More than one board change landed on ${clustered.map(esc).join(' and ')}. Simultaneous
         appointments and resignations are what a change of control looks like on the register.`
      : 'Bars run from appointment to resignation; open bars are still serving.';

    return `<figure class="gv-figure">
      <figcaption class="gv-figure__caption">Board over time</figcaption>
      <div class="gv-legend">
        <span><i style="background:${ACCENT}"></i>Currently serving</span>
        <span><i style="background:${RULE}"></i>Resigned</span>
      </div>
      <div class="gv-scroll">
        <svg viewBox="0 0 ${width} ${height}" width="100%" style="max-width:${width}px"
             role="img" aria-label="Each officer's period of appointment">
          ${grid}${markers}${bars}
          <line x1="${labelWidth}" y1="${height - 26}" x2="${width - padRight}" y2="${height - 26}"
            stroke="${RULE}" stroke-width="1"/>
        </svg>
      </div>
      <p class="gv-figure__note">${note}</p>
    </figure>`;
  }

  /* ------------------------------------------------------- filing cadence */

  /* Filings are events on a time axis, so they get a strip rather than a chart.
   * What the reader is looking for is rhythm and its absence: a company that
   * files every year and then does not is saying something. */
  function filingCadence(filings, options) {
    const events = (filings || []).map((filing) => ({
      at: Date.parse(filing.date),
      label: String(filing.category || filing.type || 'filing').replace(/-/g, ' '),
      description: String(filing.description || '').replace(/-/g, ' '),
    })).filter((event) => Number.isFinite(event.at)).sort((a, b) => a.at - b.at);

    if (events.length < 3) return '';

    const first = events[0].at;
    const last = (options && options.today) || Date.now();
    const span = Math.max(last - first, 86400000);

    const width = 720;
    const height = 108;
    const padLeft = 12;
    const padRight = 12;
    const plot = width - padLeft - padRight;
    const axisY = 62;
    const x = (t) => padLeft + ((t - first) / span) * plot;

    // The widest silence between consecutive filings, which is the thing worth seeing.
    let gap = { months: 0 };
    for (let i = 1; i < events.length; i += 1) {
      const months = (events[i].at - events[i - 1].at) / (30.44 * 86400000);
      if (months > gap.months) gap = { months, from: events[i - 1].at, to: events[i].at };
    }

    const startYear = new Date(first).getUTCFullYear();
    const endYear = new Date(last).getUTCFullYear();
    const years = [];
    for (let y = startYear; y <= endYear; y += 1) years.push(y);
    const every = Math.ceil(years.length / 10) || 1;
    const ticks = years.filter((_, i) => i % every === 0);

    const grid = ticks.map((year) => {
      const px = x(Date.parse(`${year}-01-01`));
      if (px < padLeft || px > width - padRight) return '';
      return `<text x="${px}" y="${axisY + 26}" font-size="11" fill="${MUTED}"
                text-anchor="middle">${year}</text>`;
    }).join('');

    const marks = events.map((event) => `
      <circle cx="${x(event.at)}" cy="${axisY}" r="11" fill="transparent">
        <title>${esc(new Date(event.at).toISOString().slice(0, 10))} — ${esc(event.description || event.label)}</title>
      </circle>
      <circle cx="${x(event.at)}" cy="${axisY}" r="4.5" fill="${ACCENT}" stroke="${SURFACE}"
        stroke-width="2" pointer-events="none"/>`).join('');

    const gapBand = gap.months >= 15 ? `
      <rect x="${x(gap.from)}" y="${axisY - 20}" width="${Math.max(x(gap.to) - x(gap.from), 2)}" height="40"
        fill="${ALERT}" opacity="0.10"/>
      <text x="${(x(gap.from) + x(gap.to)) / 2}" y="${axisY - 26}" font-size="10.5" fill="${ALERT}"
        text-anchor="middle">${Math.round(gap.months)} months</text>` : '';

    return `<figure class="gv-figure">
      <figcaption class="gv-figure__caption">Filing cadence</figcaption>
      <div class="gv-scroll">
        <svg viewBox="0 0 ${width} ${height}" width="100%" style="max-width:${width}px"
             role="img" aria-label="When each document was filed">
          ${gapBand}
          <line x1="${padLeft}" y1="${axisY}" x2="${width - padRight}" y2="${axisY}"
            stroke="${RULE}" stroke-width="1"/>
          ${grid}${marks}
        </svg>
      </div>
      <p class="gv-figure__note">${events.length} filings retrieved${
        gap.months >= 15
          ? `. The widest gap is ${Math.round(gap.months)} months — a break in an otherwise annual rhythm is worth asking about.`
          : '. Dates come from the filing history; hover any mark for the document.'}</p>
    </figure>`;
  }

  /* --------------------------------------------------------- model spend */

  /* Comparing magnitude across a handful of calls: bars, one hue, no legend —
   * a single series is named by the title. The prompt and output split lives in
   * the table underneath, where a reader who wants it can read exact figures. */
  function tokensPerCall(byCall) {
    const calls = (byCall || []).map((call) => ({
      label: String(call.schema || call.model || 'call'),
      tokens: Number(call.prompt_tokens || 0) + Number(call.output_tokens || 0),
      prompt: Number(call.prompt_tokens || 0),
      output: Number(call.output_tokens || 0),
      latency: Number(call.latency_ms || 0),
    })).filter((call) => call.tokens > 0);

    if (calls.length < 2) return '';

    const most = Math.max(...calls.map((c) => c.tokens));
    const rowHeight = 30;
    const barHeight = 14;
    const labelWidth = 168;
    const padRight = 78;
    const width = 720;
    const top = 10;
    const plot = width - labelWidth - padRight;
    const height = top + calls.length * rowHeight + 8;

    const bars = calls.map((call, index) => {
      const y = top + index * rowHeight;
      const barWidth = Math.max((call.tokens / most) * plot, 2);
      return `
        <text x="${labelWidth - 10}" y="${y + barHeight - 2}" font-size="12" fill="${INK}"
          text-anchor="end">${esc(call.label)}</text>
        <rect x="${labelWidth}" y="${y}" width="${barWidth}" height="${barHeight}" rx="3"
          fill="${ACCENT}">
          <title>${esc(call.label)} — ${signed(call.prompt)} prompt + ${signed(call.output)} output, ${signed(call.latency)}ms</title>
        </rect>
        <text x="${labelWidth + barWidth + 8}" y="${y + barHeight - 2}" font-size="11" fill="${MUTED}">
          ${signed(call.tokens)}</text>`;
    }).join('');

    const total = calls.reduce((sum, call) => sum + call.tokens, 0);
    return `<figure class="gv-figure">
      <figcaption class="gv-figure__caption">Tokens per model call</figcaption>
      <div class="gv-scroll">
        <svg viewBox="0 0 ${width} ${height}" width="100%" style="max-width:${width}px"
             role="img" aria-label="Total tokens used by each model call">${bars}</svg>
      </div>
      <p class="gv-figure__note">${signed(total)} tokens across ${calls.length} calls.
        Hover a bar for its prompt and output split, or read the table below.</p>
    </figure>`;
  }

  global.charts = { runTimeline, reconciliation, netAssets, boardTenure, filingCadence, tokensPerCall };
})(window);
