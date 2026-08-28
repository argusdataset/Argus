"use strict";

/**
 * ARGUS Public — a thin, honest client over Module 20's API.
 *
 * Same-origin by design: every path below is relative
 * (`/public/stats`, not `https://something/public/stats`), so this file
 * works unmodified wherever it is served from the same process as
 * services/public_stats — see infra/deploy/asgi.py for how that is wired.
 *
 * Three states this page has to tell apart, because they are different
 * facts and Module 20 is careful to keep them different in its own
 * responses:
 *
 *   1. "ARGUS has this number and it is a real number."
 *   2. "ARGUS has nothing to show here, and here is exactly why"
 *      (an `Unavailable` block, or a 503 with STATISTICS_UNAVAILABLE /
 *      STATISTICS_WITHDRAWN — both are ARGUS *declining* to show
 *      something, not ARGUS being broken).
 *   3. "This page could not reach ARGUS right now" — a genuine failure,
 *      unrelated to what ARGUS has or hasn't published.
 *
 * Conflating (2) and (3) is the one mistake this file is written to
 * avoid: a network hiccup must never read as "ARGUS has no track
 * record," and an honest absence must never read as "something is
 * broken."
 */

const CHARTS = [
  "win_rate",
  "cumulative_performance",
  "regime_breakdown",
  "excursion_distribution",
];

const HONEST_ABSENCE_CODES = new Set(["STATISTICS_UNAVAILABLE", "STATISTICS_WITHDRAWN"]);

document.addEventListener("DOMContentLoaded", () => {
  loadStatus();
  for (const chart of CHARTS) loadChart(chart);
  loadReleases();
});

// ---------------------------------------------------------------------
// Fetching
// ---------------------------------------------------------------------

async function getJson(path) {
  try {
    const res = await fetch(path, { headers: { Accept: "application/json" } });
    let data = null;
    try {
      data = await res.json();
    } catch (_parseError) {
      data = null;
    }
    return { ok: res.ok, status: res.status, data };
  } catch (networkError) {
    return { ok: false, status: 0, data: null, networkError: String(networkError) };
  }
}

// ---------------------------------------------------------------------
// Publication status  —  GET /public/stats
// ---------------------------------------------------------------------

async function loadStatus() {
  const container = document.getElementById("status");
  const { ok, data, networkError } = await getJson("/public/stats");

  if (!ok || !data) {
    renderLoadFailure(container, networkError || "Could not load publication status.");
    return;
  }

  clear(container);
  const badge = h(
    "span",
    { class: "status-badge " + (data.published ? "published" : "unpublished") },
    data.published ? "Published" : "Not yet published"
  );

  const line = h("div", { class: "status-line" }, [badge]);
  container.append(line);
  container.append(h("p", { class: "status-explanation" }, data.explanation));

  const counts = h(
    "div",
    { class: "status-counts" },
    `${data.approved_run_count} approved validation run(s) · ` +
      `${data.approved_window_count} approved live-tracking window(s)`
  );
  container.append(counts);

  if (data.freshness) {
    container.append(freshnessStrip(data.freshness));
  }
}

// ---------------------------------------------------------------------
// One chart  —  GET /public/stats/{chart}
// ---------------------------------------------------------------------

async function loadChart(chart) {
  const panel = document.getElementById("chart-" + chart);
  const body = panel.querySelector(".chart-body");
  const { ok, data, status, networkError } = await getJson("/public/stats/" + chart);

  clear(body);

  if (!ok) {
    const code = data && data.error ? data.error.code : null;
    const message =
      data && data.error
        ? data.error.message
        : networkError || `The server returned HTTP ${status}.`;

    if (code && HONEST_ABSENCE_CODES.has(code)) {
      body.append(
        unavailableBlock({
          reason: code,
          explanation: message,
        })
      );
    } else {
      body.append(errorBlock(message));
    }
    return;
  }

  body.append(h("p", { class: "caption" }, data.caption));

  const renderers = {
    win_rate: renderWinRate,
    cumulative_performance: renderCumulative,
    regime_breakdown: renderRegime,
    excursion_distribution: renderExcursions,
  };
  renderers[chart](body, data);

  if (data.freshness) body.append(freshnessStrip(data.freshness));
  if (data.provenance) body.append(provenanceNote(data.provenance));
}

// ---------------------------------------------------------------------
// Chart 1 — win rate
// ---------------------------------------------------------------------

function renderWinRate(body, data) {
  const { series, summary } = data;

  if (summary.unavailable) {
    body.append(unavailableBlock(summary.unavailable));
  } else {
    body.append(
      statRow([
        stat("Hit rate", formatPercent(summary.hit_rate), signClass(summary.hit_rate)),
        stat("Precision", formatPercent(summary.precision)),
        stat("Resolved", String(summary.resolved)),
      ])
    );
  }

  const maxCount = Math.max(1, ...series.map((row) => row.count));
  const rows = h("div", { class: "bar-rows" });
  for (const row of series) {
    const pct = row.share == null ? null : row.share * 100;
    const widthPct = (row.count / maxCount) * 100;
    rows.append(
      h("div", { class: "bar-row" }, [
        h("div", { class: "bar-label" }, statusLabel(row.status)),
        h("div", { class: "bar-track" }, [svgBarFill(widthPct, "status-" + row.status)]),
        h(
          "div",
          { class: "bar-value" },
          pct == null ? `${row.count}` : `${row.count} (${pct.toFixed(1)}%)`
        ),
      ])
    );
  }
  body.append(rows);
}

/**
 * A bar-track's fill, as an SVG rect rather than a CSS-`width` div.
 *
 * The width has to vary per row, and this page's CSP intentionally does
 * not permit inline `style` attributes (`style-src 'self'`, no
 * `unsafe-inline` — see `infra/deploy/public_web.py` on why this page
 * has its own policy at all). An SVG `width` is a plain XML attribute,
 * not a style, so a `<rect>` scaled by `viewBox` gets a data-driven width
 * without needing one.
 */
function svgBarFill(pct, colorClass) {
  const svg = svgEl("svg", {
    class: "bar-fill-svg",
    viewBox: "0 0 100 18",
    preserveAspectRatio: "none",
  });
  svg.append(svgEl("rect", { x: 0, y: 0, width: Math.max(pct, 0), height: 18, class: colorClass }));
  return svg;
}

function statusLabel(status) {
  const labels = {
    SUCCESS: "Success",
    FAILED: "Failed",
    EXPIRED: "Expired",
    INVALIDATED: "Invalidated",
  };
  return labels[status] || status;
}

// ---------------------------------------------------------------------
// Chart 2 — cumulative performance
// ---------------------------------------------------------------------

function renderCumulative(body, data) {
  const { series, summary } = data;

  if (summary.unavailable || !series || series.length < 2) {
    body.append(unavailableBlock(summary.unavailable || {
      reason: "insufficient_sample",
      explanation: "Not enough concluded, published outcomes yet to draw a line.",
    }));
    return;
  }

  body.append(
    statRow([
      stat(
        "Cumulative return",
        formatSignedPercent(summary.final_cumulative_relative_return),
        signClass(summary.final_cumulative_relative_return)
      ),
      stat("Max drawdown", formatSignedPercent(summary.max_drawdown), "negative"),
    ])
  );

  body.append(lineChart(series));
  body.append(h("p", { class: "provenance-note" }, summary.basis));
}

function lineChart(series) {
  const width = 600;
  const height = 220;
  const padL = 8;
  const padR = 8;
  const padT = 10;
  const padB = 10;

  const values = series.map((p) => p.cumulative_relative_return);
  let min = Math.min(0, ...values);
  let max = Math.max(0, ...values);
  if (min === max) {
    min -= 1;
    max += 1;
  }
  const span = max - min;

  const n = series.length;
  const x = (index) => padL + (index / (n - 1)) * (width - padL - padR);
  const y = (value) => padT + (1 - (value - min) / span) * (height - padT - padB);

  const points = series.map((p, index) => `${x(index)},${y(p.cumulative_relative_return)}`);
  const zeroY = y(0);

  const svg = svgEl("svg", {
    class: "chart-svg",
    viewBox: `0 0 ${width} ${height}`,
    preserveAspectRatio: "none",
  });

  svg.append(
    svgEl("line", {
      x1: padL,
      x2: width - padR,
      y1: zeroY,
      y2: zeroY,
      stroke: "#333c4a",
      "stroke-dasharray": "4 4",
    })
  );
  svg.append(
    svgEl("polyline", {
      points: points.join(" "),
      fill: "none",
      stroke: "#4f8cff",
      "stroke-width": "2",
    })
  );

  const last = series[series.length - 1];
  svg.append(
    svgEl("circle", {
      cx: x(n - 1),
      cy: y(last.cumulative_relative_return),
      r: 3,
      fill: "#4f8cff",
    })
  );

  return svg;
}

// ---------------------------------------------------------------------
// Chart 3 — regime breakdown
// ---------------------------------------------------------------------

function renderRegime(body, data) {
  const { series, summary } = data;

  body.append(
    statRow([
      stat(
        "Reportable regimes",
        `${summary.reportable_regimes} / ${summary.regimes}`
      ),
      stat("Floor per regime", String(summary.floor)),
    ])
  );

  if (!series.length) {
    body.append(
      unavailableBlock({
        reason: "no_data",
        explanation: "No published outcomes yet.",
      })
    );
    return;
  }

  const rows = h("div", { class: "bar-rows" });
  for (const row of series) {
    if (row.unavailable) {
      rows.append(
        h("div", { class: "bar-row" }, [
          h("div", { class: "bar-label" }, row.regime),
          h(
            "div",
            { class: "bar-value bar-value-wide" },
            `${row.sample_size} outcome(s) — too few to report a rate ` +
              `(needs ${row.unavailable.required})`
          ),
        ])
      );
    } else {
      const pct = row.hit_rate * 100;
      rows.append(
        h("div", { class: "bar-row" }, [
          h("div", { class: "bar-label" }, row.regime),
          h("div", { class: "bar-track" }, [svgBarFill(pct, "status-SUCCESS")]),
          h(
            "div",
            { class: "bar-value" },
            `${pct.toFixed(1)}% · n=${row.sample_size}`
          ),
        ])
      );
    }
  }
  body.append(rows);
}

// ---------------------------------------------------------------------
// Chart 4 — MFE / MAE distribution
// ---------------------------------------------------------------------

function renderExcursions(body, data) {
  const { series, summary } = data;

  if (summary.unavailable) {
    body.append(unavailableBlock(summary.unavailable));
  } else {
    body.append(
      statRow([
        stat("Median MFE", formatPercent(summary.median_mfe), "positive"),
        stat("Median MAE", formatPercent(summary.median_mae), "negative"),
      ])
    );
  }

  const grid = h("div", { class: "excursion-grid" });
  for (const entry of series) {
    const label = entry.measure === "mfe" ? "Max favourable excursion" : "Max adverse excursion";
    const colorClass = entry.measure === "mfe" ? "excursion-mfe" : "excursion-mae";
    grid.append(
      h("div", { class: "excursion-col" }, [
        h("div", { class: "provenance-note" }, label),
        histogram(entry.bins, colorClass),
      ])
    );
  }
  body.append(grid);
}

function histogram(bins, colorClass) {
  const width = 280;
  const height = 140;
  const padB = 18;
  const maxCount = Math.max(1, ...bins.map((b) => b.count));
  const barWidth = width / bins.length;

  const svg = svgEl("svg", {
    class: "chart-svg",
    viewBox: `0 0 ${width} ${height}`,
    preserveAspectRatio: "none",
  });

  bins.forEach((bin, index) => {
    const barHeight = (bin.count / maxCount) * (height - padB - 4);
    svg.append(
      svgEl("rect", {
        x: index * barWidth + 1,
        y: height - padB - barHeight,
        width: Math.max(barWidth - 2, 1),
        height: barHeight,
        class: colorClass + (bin.count === 0 ? " empty-bin" : ""),
      })
    );
  });

  const first = bins[0];
  const last = bins[bins.length - 1];
  const lowLabel = (first.open_low ? "≤ " : "") + formatPercent(first.low, 0);
  const highLabel = (last.open_high ? "≥ " : "") + formatPercent(last.high, 0);

  svg.append(
    svgEl("text", { x: 2, y: height - 4, fill: "#6b7481", "font-size": "10" }, lowLabel)
  );
  svg.append(
    svgEl(
      "text",
      { x: width - 2, y: height - 4, fill: "#6b7481", "font-size": "10", "text-anchor": "end" },
      highLabel
    )
  );

  return svg;
}

// ---------------------------------------------------------------------
// Release windows  —  GET /public/releases
// ---------------------------------------------------------------------

async function loadReleases() {
  const container = document.getElementById("releases-body");
  const { ok, data, networkError, status } = await getJson("/public/releases");

  clear(container);

  if (!ok) {
    container.append(errorBlock(networkError || `The server returned HTTP ${status}.`));
    return;
  }

  if (!data.length) {
    container.append(
      unavailableBlock({
        reason: "no_approved_windows",
        explanation: "No live-tracking release window has been approved for publication yet.",
      })
    );
    return;
  }

  const table = h("table", { class: "releases-table" });
  table.append(
    h("thead", {}, [
      h("tr", {}, [
        h("th", {}, "Period"),
        h("th", {}, "Seq"),
        h("th", {}, "Status"),
        h("th", {}, "Assigned"),
        h("th", {}, "By"),
        h("th", {}, "Note"),
      ]),
    ])
  );

  const tbody = h("tbody");
  for (const window of data) {
    tbody.append(
      h("tr", {}, [
        h("td", {}, `${window.period_start} → ${window.period_end}`),
        h("td", {}, String(window.sequence_number)),
        h("td", {}, h("span", { class: "release-status " + window.status }, window.status)),
        h("td", {}, formatDateTime(window.assigned_at)),
        h("td", {}, window.by_system ? "System" : "Reviewer"),
        h("td", {}, window.note || "—"),
      ])
    );
  }
  table.append(tbody);
  container.append(table);
}

// ---------------------------------------------------------------------
// Shared UI fragments
// ---------------------------------------------------------------------

function unavailableBlock(unavailable) {
  const children = [
    h("span", { class: "reason" }, unavailable.reason),
    h("div", {}, unavailable.explanation),
  ];
  if (unavailable.observed != null && unavailable.required != null) {
    children.push(
      h(
        "div",
        { class: "count" },
        `${unavailable.observed} of ${unavailable.required} required.`
      )
    );
  }
  return h("div", { class: "unavailable-block" }, children);
}

function errorBlock(message) {
  return h("div", { class: "error-block" }, [
    h("span", { class: "reason" }, "Could not load"),
    h(
      "div",
      {},
      "This is a connectivity or server problem, not a statement about ARGUS's track " +
        "record. " +
        message
    ),
  ]);
}

function statRow(stats) {
  return h("div", { class: "stat-row" }, stats);
}

function stat(label, value, valueClass) {
  return h("div", { class: "stat-tile" }, [
    h("span", { class: "label" }, label),
    h("span", { class: "value" + (valueClass ? " " + valueClass : "") }, value),
  ]);
}

function signClass(value) {
  if (value == null) return "";
  return value >= 0 ? "positive" : "negative";
}

function freshnessStrip(freshness) {
  const parts = [
    `as of ${formatDateTime(freshness.as_of)}`,
    `computed ${formatDateTime(freshness.computed_at)}`,
    `(${formatAge(freshness.age_seconds)} ago)`,
  ];
  const strip = h("div", { class: "meta-strip" }, parts.map((text) => h("span", {}, text)));
  if (freshness.stale) {
    const flag = h(
      "span",
      { class: "stale-flag" },
      "STALE — " + (freshness.staleness_reason || "older than expected")
    );
    strip.append(flag);
  }
  return strip;
}

function provenanceNote(provenance) {
  const runCount = (provenance.approved_runs || []).length;
  const windowCount = (provenance.approved_live_windows || []).length;
  return h(
    "div",
    { class: "provenance-note" },
    `Computed from ${runCount} approved validation run(s) and ${windowCount} approved ` +
      `live-tracking window(s). ${provenance.note || ""}`
  );
}

function renderLoadFailure(container, message) {
  clear(container);
  container.append(errorBlock(message));
}

// ---------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------

function formatPercent(value, digits = 1) {
  if (value == null) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

function formatSignedPercent(value, digits = 1) {
  if (value == null) return "—";
  const pct = value * 100;
  const sign = pct > 0 ? "+" : "";
  return `${sign}${pct.toFixed(digits)}%`;
}

function formatDateTime(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatAge(seconds) {
  if (seconds < 3600) return `${Math.round(seconds / 60)} minute(s)`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} hour(s)`;
  return `${Math.round(seconds / 86400)} day(s)`;
}

// ---------------------------------------------------------------------
// DOM helpers — build elements with textContent, never innerHTML, so
// nothing the API returns (a reviewer's free-text note, an explanation
// string) can be interpreted as markup.
// ---------------------------------------------------------------------

function h(tag, attrs = {}, children = []) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") el.className = value;
    else if (key === "style") el.setAttribute("style", value);
    else el.setAttribute(key, value);
  }
  appendChildren(el, children);
  return el;
}

function svgEl(tag, attrs = {}, text) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) {
    el.setAttribute(key, value);
  }
  if (text != null) el.textContent = text;
  return el;
}

function appendChildren(el, children) {
  const list = Array.isArray(children) ? children : [children];
  for (const child of list) {
    if (child == null) continue;
    if (typeof child === "string" || typeof child === "number") {
      el.append(document.createTextNode(String(child)));
    } else {
      el.append(child);
    }
  }
}

function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
}
