/* Power rankings front end. Reads JSON from data/ and renders hash routes:
 *   #/rankings[/week]  #/odds  #/awards[/week]  #/stats[/week]  #/team/<roster_id>
 *   #/future[/<year>|/dynasty]  #/future/team/<roster_id>  #/trades[/<transaction_id>]
 */
(() => {
  "use strict";

  const app = document.getElementById("app");
  const store = { weeks: {}, sort: { key: "rank", dir: 1 }, highlight: null, futureView: "dynasty",
    tradeFilter: { team: "", season: "" } };
  let charts = [];

  // -- helpers --------------------------------------------------------------
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = (n, d = 1) => (n == null || Number.isNaN(n) ? "–" : Number(n).toFixed(d));
  const pct = (n, d = 1) => (n == null ? "–" : `${(100 * n).toFixed(d)}%`);
  const signed = (n, d = 1) => (n == null ? "–" : `${n > 0 ? "+" : n < 0 ? "−" : ""}${Math.abs(n).toFixed(d)}`);
  const record = (m) => `${m.wins}-${m.losses}${m.ties ? `-${m.ties}` : ""}`;
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const ordinal = (n) => `${n}${["th", "st", "nd", "rd"][(n % 100 > 10 && n % 100 < 14) || n % 10 > 3 ? 0 : n % 10]}`;
  const day = (ms) => new Date(ms).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });

  async function getJSON(path) {
    const res = await fetch(`data/${path}`, { cache: "no-cache" });
    if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
    return res.json();
  }
  async function load(key, path) {
    if (!store[key]) store[key] = await getJSON(path);
    return store[key];
  }
  async function loadWeek(week) {
    if (!store.weeks[week]) store.weeks[week] = await getJSON(`rankings/week-${week}.json`);
    return store.weeks[week];
  }
  const team = (rid) => store.meta.teams[rid] || { team_name: `Team ${rid}`, owner_name: "" };

  function initials(name) {
    const words = String(name).replace(/[^\p{L}\p{N}\s]/gu, "").trim().split(/\s+/).filter(Boolean);
    return ((words[0]?.[0] || "?") + (words[1]?.[0] || "")).toUpperCase();
  }
  function avatar(rid, cls = "") {
    const t = team(rid);
    const ini = esc(initials(t.team_name));
    if (t.avatar) {
      return `<img class="avatar ${cls}" src="${esc(t.avatar)}" alt="" loading="lazy" data-ini="${ini}">`;
    }
    return `<span class="avatar ${cls}" aria-hidden="true">${ini}</span>`;
  }
  // Broken avatar images fall back to initials.
  document.addEventListener("error", (e) => {
    const img = e.target;
    if (img.tagName === "IMG" && img.classList.contains("avatar")) {
      const span = document.createElement("span");
      span.className = img.className;
      span.textContent = img.dataset.ini || "?";
      img.replaceWith(span);
    }
  }, true);

  function moveBadge(change, prev) {
    if (prev == null) return `<span class="move same">NEW</span>`;
    if (change > 0) return `<span class="move up" aria-label="up ${change}">▲ ${change}</span>`;
    if (change < 0) return `<span class="move down" aria-label="down ${-change}">▼ ${-change}</span>`;
    return `<span class="move same" aria-label="no change">–</span>`;
  }
  function weekSelect(weeks, current, route) {
    const opts = weeks.slice().reverse().map((w) =>
      `<option value="${w}" ${w === current ? "selected" : ""}>Week ${w}</option>`).join("");
    return `<select class="select" aria-label="Week" data-route="${route}" id="week-select">${opts}</select>`;
  }
  function bindWeekSelect() {
    const sel = document.getElementById("week-select");
    if (sel) sel.addEventListener("change", () => { location.hash = `#/${sel.dataset.route}/${sel.value}`; });
  }
  const pickWeek = (w) => {
    const weeks = store.meta.weeks;
    const n = parseInt(w, 10);
    return weeks.includes(n) ? n : store.meta.latest_week;
  };

  // -- charts ----------------------------------------------------------------
  function destroyCharts() { charts.forEach((c) => c.destroy()); charts = []; }
  function chartDefaults() {
    if (!window.Chart) return false;
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    Chart.defaults.font.size = 12;
    Chart.defaults.color = css("--muted");
    Chart.defaults.borderColor = css("--border");
    // Merge, never replace: replacing the defaults object drops internals Chart.js needs to animate colors.
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) Chart.defaults.animation = false;
    else Chart.defaults.set("animation", { duration: 350 });
    Chart.defaults.maintainAspectRatio = false;
    Chart.defaults.plugins.legend.display = false;
    Object.assign(Chart.defaults.plugins.tooltip, {
      backgroundColor: css("--surface"), titleColor: css("--ink"), bodyColor: css("--ink-2"),
      borderColor: css("--border"), borderWidth: 1, padding: 10, cornerRadius: 8, boxPadding: 4,
      usePointStyle: true,
    });
    return true;
  }
  const axis = (extra = {}) => ({
    grid: { color: css("--border"), drawTicks: false },
    border: { color: css("--axis") },
    ticks: { padding: 6 },
    ...extra,
  });
  function noCharts(id) {
    const el = document.getElementById(id);
    if (el) el.innerHTML = `<p class="empty">Charts unavailable offline.</p>`;
  }

  // -- views -----------------------------------------------------------------
  async function viewRankings(weekParam) {
    const meta = store.meta;
    if (!meta.weeks.length) {
      app.innerHTML = `<p class="empty">No completed weeks yet. Check back after week 1.</p>`;
      return;
    }
    const week = pickWeek(weekParam);
    const data = await loadWeek(week);
    const cards = data.rankings.map((r) => {
      const t = team(r.roster_id);
      return `<li><a class="card" href="#/team/${r.roster_id}">
        <div class="card-top">
          <div><div class="rank num">${r.rank}</div>${moveBadge(r.change, r.prev_rank)}</div>
          ${avatar(r.roster_id)}
          <div style="min-width:0"><div class="team-name">${esc(t.team_name)}</div>
            <div class="team-meta num">${record(r.metrics)} · ${fmt(r.metrics.pf, 1)} PPG</div></div>
          <div class="score num"><b>${fmt(r.score, 1)}</b><span>score</span></div>
        </div>
        <div class="meter" aria-hidden="true"><i style="width:${Math.max(2, r.score)}%"></i></div>
        ${r.blurb ? `<p class="blurb">${esc(r.blurb)}</p>` : ""}
      </a></li>`;
    }).join("");

    app.innerHTML = `
      <div class="view-head">
        <div><h1>Week ${week} Power Rankings</h1>
          <div class="sub">Score 0–100 · tap a team for details${data.backfilled ? " · backfilled week, no write-ups" : ""}</div></div>
        ${weekSelect(meta.weeks, week, "rankings")}
      </div>
      <ol class="cards">${cards}</ol>
      <h2>League trends</h2>
      <div class="grid-2">
        <section class="panel">
          <h3>Rank movement</h3>
          <p class="sub">Power rank by week. Pick a team or tap a line to highlight it.</p>
          <div class="panel-controls"><label for="hl">Team</label>
            <select class="select" id="hl">${data.rankings.map((r) =>
              `<option value="${r.roster_id}">${esc(team(r.roster_id).team_name)}</option>`).join("")}</select></div>
          <div class="legend"><span><i style="background:${css("--accent")}"></i>Selected team</span>
            <span><i style="background:${css("--axis")}"></i>Everyone else</span></div>
          <div class="chart-box tall" id="rank-chart"><canvas aria-label="Rank by week for every team" role="img"></canvas></div>
        </section>
        <section class="panel">
          <h3>Points vs. max points</h3>
          <p class="sub">Per game through week ${week}. Further below the diagonal = more points left on the bench.</p>
          <div class="chart-box tall" id="scatter-chart"><canvas aria-label="Scatter of points per game against max points per game" role="img"></canvas></div>
        </section>
      </div>`;
    bindWeekSelect();

    if (!chartDefaults()) { noCharts("rank-chart"); noCharts("scatter-chart"); return; }
    const teams = await load("teams", "teams.json");
    const hl = document.getElementById("hl");
    if (store.highlight == null || !teams[store.highlight]) store.highlight = data.rankings[0].roster_id;
    hl.value = store.highlight;

    const n = Object.keys(meta.teams).length;
    const ids = Object.keys(teams);
    const datasets = ids.map((rid) => ({
      label: team(rid).team_name,
      rid: Number(rid),
      data: meta.weeks.map((w) => teams[rid].weekly.find((x) => x.week === w)?.rank ?? null),
      spanGaps: true,
      cubicInterpolationMode: "monotone",
    }));
    const style = () => datasets.forEach((d) => {
      const on = d.rid === Number(store.highlight);
      d.borderColor = on ? css("--accent") : css("--axis");
      d.borderWidth = on ? 3 : 1.5;
      d.pointRadius = on ? 4 : 0;
      d.pointHoverRadius = 5;
      d.pointBackgroundColor = css("--accent");
      d.order = on ? 0 : 1;
    });
    style();
    const rankChart = new Chart(document.querySelector("#rank-chart canvas"), {
      type: "line",
      data: { labels: meta.weeks.map((w) => `W${w}`), datasets },
      options: {
        interaction: { mode: "index", intersect: false },
        scales: {
          y: axis({ reverse: true, min: 1, max: n, ticks: { stepSize: 1, padding: 6 }, title: { display: true, text: "Rank" } }),
          x: axis({ grid: { display: false } }),
        },
        plugins: {
          tooltip: {
            itemSort: (a, b) => a.parsed.y - b.parsed.y,
            callbacks: { label: (c) => ` #${c.parsed.y} ${c.dataset.label}` },
            filter: (c, i, all) => all.length <= 6 || c.dataset.rid === Number(store.highlight) || c.parsed.y <= 3,
          },
        },
        onClick: (evt) => {
          const hit = rankChart.getElementsAtEventForMode(evt, "nearest", { intersect: false }, false)[0];
          if (hit) { store.highlight = datasets[hit.datasetIndex].rid; hl.value = store.highlight; style(); rankChart.update(); }
        },
      },
    });
    hl.addEventListener("change", () => { store.highlight = Number(hl.value); style(); rankChart.update(); });
    charts.push(rankChart);

    const pts = data.rankings.map((r) => ({ x: r.metrics.max_pf, y: r.metrics.pf, rid: r.roster_id, eff: r.metrics.efficiency }));
    const bounds = (vals) => [Math.floor((Math.min(...vals) - 2) / 5) * 5, Math.ceil((Math.max(...vals) + 2) / 5) * 5];
    const [xLo, xHi] = bounds(pts.map((p) => p.x));
    const [yLo, yHi] = bounds(pts.map((p) => p.y));
    const lo = Math.min(xLo, yLo), hi = Math.max(xHi, yHi);
    charts.push(new Chart(document.querySelector("#scatter-chart canvas"), {
      type: "scatter",
      data: {
        datasets: [
          { data: pts, backgroundColor: css("--accent"), borderColor: css("--surface"), borderWidth: 2, pointRadius: 6, pointHoverRadius: 8 },
          { type: "line", data: [{ x: lo, y: lo }, { x: hi, y: hi }], borderColor: css("--axis"), borderDash: [4, 4], borderWidth: 1, pointRadius: 0, pointHoverRadius: 0 },
        ],
      },
      options: {
        scales: {
          x: axis({ min: xLo, max: xHi, title: { display: true, text: "Max PF per game" } }),
          y: axis({ min: yLo, max: yHi, title: { display: true, text: "PF per game" } }),
        },
        plugins: {
          tooltip: {
            filter: (c) => c.datasetIndex === 0,
            callbacks: {
              title: (items) => team(items[0].raw.rid).team_name,
              label: (c) => [` PF ${fmt(c.raw.y)} · Max ${fmt(c.raw.x)}`, ` Efficiency ${pct(c.raw.eff)}`],
            },
          },
        },
        onClick: (evt, els) => { const el = els.find((e) => e.datasetIndex === 0); if (el) location.hash = `#/team/${pts[el.index].rid}`; },
      },
    }));
  }

  async function viewTeam(rid) {
    const teams = await load("teams", "teams.json");
    const t = teams[rid];
    if (!t) { app.innerHTML = `<p class="empty">Team not found. <a href="#/rankings">Back to rankings</a></p>`; return; }
    const meta = store.meta;
    const latest = meta.latest_week ? await loadWeek(meta.latest_week) : null;
    const row = latest?.rankings.find((r) => r.roster_id === Number(rid));
    const s = t.season;
    const name = (id) => esc(team(id).team_name);

    const tiles = s ? [
      ["Power score", fmt(row?.score), row ? `Rank #${row.rank} of ${latest.rankings.length}` : ""],
      ["Points / game", fmt(s.pf), `${fmt(s.pf_total, 0)} total`],
      ["Max PF / game", fmt(s.max_pf), `${fmt(s.bench_pg)} left on bench / wk`],
      ["Efficiency", pct(s.efficiency), "PF ÷ Max PF"],
      ["Luck", `${signed(s.luck, 1)} W`, `vs. ${fmt(s.expected_wins)} expected wins`],
      ["Consistency", `±${fmt(s.consistency)}`, "std. dev. of weekly PF"],
      ["All-play", `${s.allplay_w}-${s.allplay_l}${s.allplay_t ? `-${s.allplay_t}` : ""}`, pct(s.allplay_pct)],
      ["Schedule faced", fmt(s.sos), "opponents' avg PPG"],
      ["Schedule left", t.remaining_sos == null ? "–" : fmt(t.remaining_sos), "remaining opponents' PPG"],
      ["Recent form", fmt(s.recent_form), `decay ${meta.decay}`],
      ["Streak", s.streak || "–", `High ${fmt(s.high)} · Low ${fmt(s.low)}`],
      ["Record", record(s), `PA ${fmt(s.pa)} / game`],
    ].map(([l, v, h]) => `<div class="tile"><div class="label">${l}</div><div class="value num">${esc(v)}</div><div class="hint">${esc(h)}</div></div>`).join("") : "";

    const sched = t.schedule.map((g) => g.future
      ? `<li class="future"><span class="left"><span class="wk">W${g.week}</span><span class="res">·</span><span>vs ${name(g.opponent)}</span></span><span>upcoming</span></li>`
      : `<li><span class="left"><span class="wk">W${g.week}</span><span class="res ${g.result || ""}">${g.result || "–"}</span>
          ${g.opponent != null ? `<a href="#/team/${g.opponent}"><span>${g.playoff ? "🏆 " : ""}vs ${name(g.opponent)}</span></a>` : `<span>${g.playoff ? "Playoffs" : "Bye"}</span>`}</span>
          <span class="num">${fmt(g.pf, 2)}${g.pa != null ? ` – ${fmt(g.pa, 2)}` : ""}</span></li>`).join("");

    const h2h = t.h2h.length ? t.h2h.sort((a, b) => (b.w - b.l) - (a.w - a.l)).map((h) =>
      `<li><span class="left">${avatar(h.opponent)}<a href="#/team/${h.opponent}"><span>${name(h.opponent)}</span></a></span>
        <span class="num"><b>${h.w}-${h.l}${h.t ? `-${h.t}` : ""}</b> <span class="team-meta">${fmt(h.pf / (h.w + h.l + h.t))}–${fmt(h.pa / (h.w + h.l + h.t))}</span></span></li>`).join("")
      : `<li>No games against current teams yet.</li>`;

    const mk = s?.worst_mistake;
    const at = t.alltime;
    app.innerHTML = `
      <a class="back" href="#/rankings">‹ Rankings</a>
      <div class="team-hero">
        ${avatar(rid, "lg")}
        <div style="min-width:0"><h1>${esc(t.team_name)}</h1>
          <div class="team-meta">${esc(t.owner_name)}${s ? ` · ${record(s)}` : ""}
            ${row ? ` · <span class="pill ${row.change > 0 ? "up" : row.change < 0 ? "down" : ""}">#${row.rank}${row.prev_rank != null && row.change ? ` ${row.change > 0 ? "▲" : "▼"}${Math.abs(row.change)}` : ""}</span>` : ""}</div></div>
      </div>
      ${row?.blurb ? `<p class="blurb" style="margin:0 0 14px">${esc(row.blurb)}</p>` : ""}
      <div class="tiles">${tiles}</div>
      <h2>Season</h2>
      <div class="grid-2">
        <section class="panel"><h3>Weekly score vs. max</h3><p class="sub">Bars: points scored. Line: best possible lineup.</p>
          <div class="legend"><span><i class="dot" style="background:${css("--accent")}"></i>Points</span><span><i style="background:${css("--series-2")}"></i>Max points</span></div>
          <div class="chart-box" id="score-chart"><canvas role="img" aria-label="Weekly points and max points"></canvas></div></section>
        <section class="panel"><h3>Rank history</h3><p class="sub">Power rank after each week.</p>
          <div class="chart-box" id="rank-hist"><canvas role="img" aria-label="Power rank by week"></canvas></div></section>
      </div>
      ${mk ? `<section class="panel" style="margin-top:12px"><h3>Worst lineup mistake</h3>
        <p class="sub" style="margin:4px 0 0">Week ${mk.week}: started ${esc(mk.started_name)} (${fmt(mk.started_points, 2)}) over ${esc(mk.benched_name)} (${fmt(mk.benched_points, 2)}), costing ${fmt(mk.gain, 2)} points.</p></section>` : ""}
      <h2>Schedule</h2>
      <section class="panel"><ul class="list">${sched}</ul></section>
      <h2>Head-to-head (all seasons)</h2>
      <section class="panel"><ul class="list">${h2h}</ul></section>
      ${at ? `<h2>All-time</h2><section class="panel"><ul class="list">
        <li><span>Seasons</span><span class="num">${at.seasons.length} (${esc(at.seasons.slice().sort().join(", "))})</span></li>
        <li><span>Regular season</span><span class="num">${at.wins}-${at.losses}${at.ties ? `-${at.ties}` : ""} (${pct(at.win_pct)})</span></li>
        <li><span>Playoff games</span><span class="num">${at.playoff_wins}-${at.playoff_losses}</span></li>
        <li><span>Championships</span><span class="num">${at.championships ? "🏆".repeat(Math.min(at.championships, 5)) : "0"}</span></li>
        <li><span>Points for</span><span class="num">${fmt(at.pf, 0)}</span></li></ul></section>` : ""}`;

    if (!chartDefaults()) { noCharts("score-chart"); noCharts("rank-hist"); return; }
    const labels = t.weekly.map((w) => `W${w.week}`);
    charts.push(new Chart(document.querySelector("#score-chart canvas"), {
      data: {
        labels,
        datasets: [
          { type: "line", label: "Max points", data: t.weekly.map((w) => w.max_pf), borderColor: css("--series-2"), backgroundColor: css("--series-2"), borderWidth: 2, pointRadius: 3, tension: 0.2, order: 0 },
          { type: "bar", label: "Points", data: t.weekly.map((w) => w.pf), backgroundColor: css("--accent"), borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: "bottom", maxBarThickness: 26, order: 1 },
        ],
      },
      options: {
        interaction: { mode: "index", intersect: false },
        scales: { y: axis({ beginAtZero: true }), x: axis({ grid: { display: false } }) },
        plugins: { tooltip: { callbacks: {
          label: (c) => ` ${c.dataset.label}: ${fmt(c.parsed.y, 2)}`,
          footer: (items) => { const w = t.weekly[items[0].dataIndex]; return `Efficiency ${pct(w.efficiency)}${w.result ? ` · ${w.result} vs ${team(w.opponent).team_name}` : ""}`; },
        } } },
      },
    }));
    const n = Object.keys(meta.teams).length;
    charts.push(new Chart(document.querySelector("#rank-hist canvas"), {
      type: "line",
      data: { labels, datasets: [{ label: "Rank", data: t.weekly.map((w) => w.rank), borderColor: css("--accent"), backgroundColor: css("--accent"), borderWidth: 2, pointRadius: 4, cubicInterpolationMode: "monotone" }] },
      options: {
        interaction: { mode: "index", intersect: false },
        scales: { y: axis({ reverse: true, min: 1, max: n, ticks: { stepSize: 1, padding: 6 } }), x: axis({ grid: { display: false } }) },
        plugins: { tooltip: { callbacks: { label: (c) => ` Rank #${c.parsed.y} · score ${fmt(t.weekly[c.dataIndex].score)}` } } },
      },
    }));
  }

  async function viewOdds() {
    const odds = await load("odds", "playoffs.json");
    if (!odds.teams.length) { app.innerHTML = `<p class="empty">Playoff odds appear after week 1.</p>`; return; }
    const status = {
      regular_season: `${odds.simulations.toLocaleString()} simulations of weeks ${odds.remaining_weeks[0] ?? ""}–${odds.remaining_weeks.at(-1) ?? ""} using each team's scoring average and spread.`,
      playoffs: `Regular season is over. Title odds simulate the rest of the bracket (${odds.simulations.toLocaleString()} runs).`,
      complete: "Season complete.",
    }[odds.status] || "";
    const bars = (t) => [["Playoffs", t.playoff_pct], ["Bye", t.bye_pct], ["#1 seed", t.first_seed_pct], ["Title", t.champ_pct]]
      .filter(([l]) => l !== "Bye" || odds.byes)
      .map(([l, v]) => `<span class="lab">${l}</span><span class="bar"><i style="width:${v}%"></i></span><span class="val num">${fmt(v, v >= 99.95 || v === 0 ? 0 : 1)}%</span>`).join("");
    app.innerHTML = `
      <div class="view-head"><div><h1>Playoff Odds</h1>
        <div class="sub">After week ${odds.as_of_week} · top ${odds.playoff_teams} make it${odds.byes ? `, top ${odds.byes} get byes` : ""}${odds.median_games ? " · median games included" : ""}</div></div></div>
      <p class="note" style="margin:-4px 0 12px">${esc(status)}</p>
      <div class="odds-row">${odds.teams.map((t) => `
        <a class="odds-card" href="#/team/${t.roster_id}">
          <div class="odds-head">${avatar(t.roster_id)}
            <div class="team-name">${esc(team(t.roster_id).team_name)}
              <div class="team-meta num">${t.record[0]}-${t.record[1]}${t.record[2] ? `-${t.record[2]}` : ""} · proj. ${fmt(t.proj_wins)} W · avg seed ${fmt(t.avg_seed)}</div></div>
            <div class="odds-big num">${fmt(t.playoff_pct, t.playoff_pct >= 99.95 || t.playoff_pct === 0 ? 0 : 1)}%</div></div>
          <div class="odds-bars">${bars(t)}</div>
        </a>`).join("")}</div>`;
  }

  const AWARD_ICONS = { high_score: "🔥", low_score: "🧊", blowout: "💥", closest: "😬", unluckiest: "🍀", best_manager: "🧠", worst_mistake: "🤦" };

  async function viewAwards(weekParam) {
    const [awards, tx] = await Promise.all([load("awards", "awards.json"), load("tx", "transactions.json")]);
    const weeks = Object.keys(awards.weeks).map(Number).sort((a, b) => a - b);
    if (!weeks.length) { app.innerHTML = `<p class="empty">Awards appear after week 1.</p>`; return; }
    const week = weeks.includes(Number(weekParam)) ? Number(weekParam) : weeks.at(-1);
    const cards = awards.weeks[week].map((a) => `
      <a class="award" href="#/team/${a.roster_id}"><span class="ico" aria-hidden="true">${AWARD_ICONS[a.key] || "🏅"}</span>
        <div style="min-width:0"><div class="title">${esc(a.title)}</div>
          <div class="who">${esc(team(a.roster_id).team_name)}${a.opponent != null ? ` <span class="team-meta">vs ${esc(team(a.opponent).team_name)}</span>` : ""}</div>
          <div class="detail num">${esc(a.detail)}</div></div></a>`).join("");
    const pickups = tx.best_pickups.slice(0, 8).map((p) => `
      <li><span class="left">${avatar(p.roster_id)}<span><b>${esc(p.player)}</b><br><span class="team-meta">${esc(team(p.roster_id).team_name)} · W${p.week}${p.faab != null ? ` · $${p.faab}` : ""}</span></span></span>
        <span class="num" style="text-align:right"><b>${fmt(p.points)}</b><br><span class="team-meta">${fmt(p.started_points)} started</span></span></li>`).join("")
      || `<li>No waiver pickups yet.</li>`;
    const trades = tx.trades.slice().reverse().map((t) => `
      <section class="panel"><h3>Week ${t.week} trade</h3>
        <p class="sub">Points scored in starting lineups since the trade.${t.margin ? ` Edge: ${esc(team(t.leader).team_name)} by ${fmt(t.margin)}.` : ""}</p>
        <ul class="list">${t.sides.map((s) => `
          <li><span class="left">${avatar(s.roster_id)}<span class="wrap"><b>${esc(team(s.roster_id).team_name)}</b> got<br>
            <span class="team-meta wrap">${esc([...s.received.map((r) => r.player), ...s.picks].join(", ") || "nothing")}</span></span></span>
            <span class="num"><b>${fmt(s.started_points)}</b></span></li>`).join("")}</ul></section>`).join("")
      || `<section class="panel"><p class="sub" style="margin:0">No trades yet. Cowards.</p></section>`;
    app.innerHTML = `
      <div class="view-head"><div><h1>Week ${week} Awards</h1><div class="sub">The good, the bad, and the benched</div></div>
        ${weekSelect(weeks, week, "awards")}</div>
      <div class="awards">${cards}</div>
      <h2>Best waiver pickups</h2>
      <p class="note" style="margin:-4px 0 10px">Points scored while on the team that added them.</p>
      <section class="panel"><ul class="list">${pickups}</ul></section>
      <h2>Trades</h2>
      <p class="note" style="margin:-4px 0 10px">This season's trades. <a class="link" href="#/trades">Full trade history and pick outcomes ›</a></p>
      ${trades}`;
    bindWeekSelect();
  }


  // -- future rankings ---------------------------------------------------------
  const playerTag = (p) => `${esc(p.name)} <span class="team-meta">${esc(p.pos || "")}${p.age != null ? ` ${p.age}` : ""}</span>`;
  const pickLabel = (p) => `${p.season} ${ordinal(p.round)}`;

  async function viewFuture(arg) {
    const fut = await load("future", "future.json");
    if (!fut.teams.length) { app.innerHTML = `<p class="empty">Future rankings aren't available yet.</p>`; return; }
    const views = [...fut.years.map(String), "dynasty"];
    const view = views.includes(arg) ? arg : (views.includes(store.futureView) ? store.futureView : "dynasty");
    store.futureView = view;
    const rows = fut.teams.slice().sort((a, b) => a.views[view].rank - b.views[view].rank);
    const top = Math.max(...rows.map((t) => t.views[view].score));
    const low = Math.min(...rows.map((t) => t.views[view].score));
    const weights = fut.year_weights.map((w, i) => `${fut.years[i]} ${Math.round(w * 100)}%`).join(" · ");
    const cards = rows.map((t) => {
      const v = t.views[view];
      const change = t.power_rank != null ? t.power_rank - v.rank : null;
      const firsts = t.picks.filter((p) => p.round === 1).length;
      const aging = v.aging.length ? `<p class="flag warn">⚠ Aging core: ${v.aging.map((p) => `${esc(p.name)} (${esc(p.pos)}, ${p.age})`).join(", ")}</p>` : "";
      const rising = t.rising.length ? `<p class="flag rise">↗ Rising: ${t.rising.slice(0, 3).map((p) => `${esc(p.name)} (${esc(p.pos)}, ${p.age ?? "?"})`).join(", ")}${t.rising.length > 3 ? ` +${t.rising.length - 3}` : ""}</p>` : "";
      return `<li><a class="card" href="#/future/team/${t.roster_id}">
        <div class="card-top">
          <div><div class="rank num">${v.rank}</div>${change == null ? "" : moveBadge(change, t.power_rank)}</div>
          ${avatar(t.roster_id)}
          <div style="min-width:0"><div class="team-name">${esc(team(t.roster_id).team_name)}</div>
            <div class="team-meta num">Power #${t.power_rank ?? "–"} now · ${firsts} future 1st${firsts === 1 ? "" : "s"}</div></div>
          <div class="score num"><b>${fmt(v.score, 1)}</b><span>proj. pts/wk</span></div>
        </div>
        <div class="meter" aria-hidden="true"><i style="width:${Math.max(4, top > low ? 30 + 70 * (v.score - low) / (top - low) : 100)}%"></i></div>
        <ol class="contrib">${v.top.map((p) => `<li><span>${playerTag(p)}</span><span class="num">${fmt(p.points, 1)}</span></li>`).join("")}</ol>
        ${aging}${rising}
      </a></li>`;
    }).join("");
    app.innerHTML = `
      <div class="view-head"><div><h1>Future Rankings</h1>
        <div class="sub">${view === "dynasty" ? `Dynasty score: ${esc(weights)}` : `Projected ${view} starting lineup`} · ▲▼ vs. current power rank</div></div></div>
      <nav class="seg" aria-label="Projection year">${views.map((v) =>
        `<a href="#/future/${v}" ${v === view ? `aria-current="page"` : ""}>${v === "dynasty" ? "Dynasty" : v}</a>`).join("")}</nav>
      <ol class="cards">${cards}</ol>
      <h2>How projections work</h2>
      <section class="panel"><p class="sub" style="margin:0">Each player's baseline is his recent points per game under this league's scoring (recent games count more, last season included), pulled toward a fringe starter's level when the sample is small. For players ${fut.young_age} or younger (or ${fut.young_years_exp} or fewer NFL seasons) it blends in snap share and targets/carries. Age curves then raise or lower it each year (RBs fade first, QBs last), and depth-chart situations add a nudge: backing up an aging or injured starter or gaining snaps helps, sitting behind a young starter or losing snaps hurts. A team's score is its best projected starting lineup plus a small bonus for each future draft pick it owns (1st ${fmt(fut.pick_values["1"] ?? 0, 1)}, 2nd ${fmt(fut.pick_values["2"] ?? 0, 1)}, 3rd ${fmt(fut.pick_values["3"] ?? 0, 1)} pts/wk).</p></section>`;
  }

  async function viewFutureTeam(rid) {
    const fut = await load("future", "future.json");
    const t = fut.teams.find((x) => x.roster_id === Number(rid));
    if (!t) { app.innerHTML = `<p class="empty">Team not found. <a href="#/future">Back to Future Rankings</a></p>`; return; }
    const views = [...fut.years.map(String), "dynasty"];
    const tiles = views.map((v) => {
      const x = t.views[v];
      return `<div class="tile"><div class="label">${v === "dynasty" ? "Dynasty" : v}</div><div class="value num">#${x.rank}</div>
        <div class="hint num">${fmt(x.score, 1)} pts/wk${x.picks_pts ? ` · picks +${fmt(x.picks_pts, 1)}` : ""}</div></div>`;
    }).join("");
    const head = fut.years.map((y) => `<th>${y}</th>`).join("");
    const body = t.players.map((p) => `<tr>
      <td class="l sticky"><span class="pname">${esc(p.name)}</span>
        <span class="team-meta">${esc(p.pos || "")}${p.age != null ? ` · ${p.age}` : ""}${p.nfl_team ? ` · ${esc(p.nfl_team)}` : ""}${p.young ? ` · <b class="young">young</b>` : ""}</span></td>
      ${p.proj.map((v, i) => `<td class="num${p.starter[i] ? " starter" : ""}">${fmt(v, 1)}</td>`).join("")}
      <td class="num">${p.games ? fmt(p.ppg, 1) : "–"}</td>
      <td class="l note-cell">${esc(p.reasons.join("; "))}</td></tr>`).join("");
    const picks = t.picks.length ? t.picks.map((p) => `<li><span>${pickLabel(p)}${p.original !== t.roster_id ? ` <span class="team-meta">from ${esc(team(p.original).team_name)}</span>` : ""}</span>
      <span class="num">+${fmt(fut.pick_values[String(p.round)] ?? 0, 1)}</span></li>`).join("") : `<li>No future picks.</li>`;
    app.innerHTML = `
      <a class="back" href="#/future">‹ Future Rankings</a>
      <div class="team-hero">${avatar(t.roster_id, "lg")}
        <div style="min-width:0"><h1>${esc(team(t.roster_id).team_name)}</h1>
          <div class="team-meta">Power #${t.power_rank ?? "–"} now · <a class="link" href="#/team/${t.roster_id}">Season stats ›</a></div></div></div>
      <div class="tiles">${tiles}</div>
      <h2>Player projections</h2>
      <p class="note" style="margin:-4px 0 10px">Projected points per game by season. <b>Bold</b> = in that year's best lineup. Last col: recent PPG.</p>
      <div class="table-wrap"><table><thead><tr><th class="l sticky">Player</th>${head}<th>Now</th><th class="l">Outlook</th></tr></thead>
        <tbody>${body}</tbody></table></div>
      <p class="scroll-hint">Swipe the table sideways for more columns.</p>
      <h2>Future draft picks</h2>
      <section class="panel"><ul class="list">${picks}</ul></section>`;
  }

  // -- trades --------------------------------------------------------------------
  function pickOutcome(p) {
    const lines = p.later.map((e) => `<li class="chain"><a href="#/trades/${esc(e.transaction_id)}">↳ Traded to ${esc(team(e.to).team_name)} on ${day(e.time)}</a></li>`);
    const o = p.outcome;
    const end = o.status === "made" ? `(${esc(o.player)})`
      : o.status === "unused" ? "(Pick not used)" : "(Pick not yet made)";
    lines.push(`<li class="chain result${o.status === "made" ? "" : " pending"}">${end}</li>`);
    return `<ul class="chain-list">${lines.join("")}</ul>`;
  }

  function tradeCard(t) {
    const names = t.sides.map((s) => esc(team(s.roster_id).team_name)).join(" ⇄ ");
    const sides = t.sides.map((s) => {
      const items = [
        ...s.players.map((p) => `<li class="asset">${esc(p.name)} <span class="team-meta">${esc(p.pos || "")}</span></li>`),
        ...s.picks.map((p) => `<li class="asset">${pickLabel(p)} <span class="team-meta">(from ${esc(team(p.original).team_name)})</span>${pickOutcome(p)}</li>`),
        ...(s.faab ? [`<li class="asset">$${s.faab} FAAB</li>`] : []),
      ];
      return `<div class="trade-side"><div class="trade-team">${avatar(s.roster_id)}<span><b>${esc(team(s.roster_id).team_name)}</b> received</span></div>
        <ul class="assets">${items.join("") || `<li class="asset team-meta">Nothing</li>`}</ul></div>`;
    }).join("");
    return `<section class="panel trade" id="trade-${esc(t.transaction_id)}">
      <div class="trade-head"><span class="team-meta num">${day(t.time)} · ${esc(t.season)} season${t.week > 1 ? ` · week ${t.week}` : ""}</span></div>
      <h3>${names}</h3>${sides}</section>`;
  }

  async function viewTrades(tid) {
    const data = await load("trades", "trades.json");
    if (!data.trades.length) { app.innerHTML = `<p class="empty">No trades yet. Cowards.</p>`; return false; }
    const f = store.tradeFilter;
    const target = tid ? data.trades.find((t) => t.transaction_id === tid) : null;
    const visible = (t) => (!f.team || t.roster_ids.includes(Number(f.team))) && (!f.season || t.season === f.season);
    if (target && !visible(target)) { f.team = ""; f.season = ""; }
    const shown = data.trades.filter(visible);
    const teamIds = Object.keys(store.meta.teams).sort((a, b) => team(a).team_name.localeCompare(team(b).team_name));
    app.innerHTML = `
      <div class="view-head"><div><h1>Trades</h1>
        <div class="sub">${shown.length} of ${data.trades.length} trades · newest first · what every pick became</div></div></div>
      <div class="filters">
        <select class="select" id="trade-team" aria-label="Team"><option value="">All teams</option>${teamIds.map((rid) =>
          `<option value="${rid}" ${String(f.team) === rid ? "selected" : ""}>${esc(team(rid).team_name)}</option>`).join("")}</select>
        <select class="select" id="trade-season" aria-label="Season"><option value="">All seasons</option>${data.seasons.map((s) =>
          `<option value="${esc(s)}" ${f.season === s ? "selected" : ""}>${esc(s)}</option>`).join("")}</select>
      </div>
      <div class="trades">${shown.map(tradeCard).join("") || `<p class="empty">No trades match.</p>`}</div>`;
    const onFilter = () => {
      store.tradeFilter = { team: document.getElementById("trade-team").value, season: document.getElementById("trade-season").value };
      if (location.hash !== "#/trades") history.replaceState(null, "", "#/trades");
      viewTrades();
    };
    document.getElementById("trade-team").addEventListener("change", onFilter);
    document.getElementById("trade-season").addEventListener("change", onFilter);
    const el = tid && document.getElementById(`trade-${tid}`);
    if (el) {
      el.scrollIntoView({ block: "start" });
      el.classList.add("flash");
      return true; // keep this scroll position
    }
    return false;
  }

  const STAT_COLS = [
    ["rank", "Rk", (r) => r.rank, (r) => r.rank],
    ["team", "Team", (r) => team(r.roster_id).team_name, null],
    ["score", "Score", (r) => r.score, (r) => fmt(r.score)],
    ["record", "W-L", (r) => r.metrics.win_pct, (r) => record(r.metrics)],
    ["win_pct", "Win %", (r) => r.metrics.win_pct, (r) => pct(r.metrics.win_pct, 0)],
    ["pf", "PF/g", (r) => r.metrics.pf, (r) => fmt(r.metrics.pf)],
    ["pa", "PA/g", (r) => r.metrics.pa, (r) => fmt(r.metrics.pa)],
    ["max_pf", "Max/g", (r) => r.metrics.max_pf, (r) => fmt(r.metrics.max_pf)],
    ["efficiency", "Eff", (r) => r.metrics.efficiency, (r) => pct(r.metrics.efficiency)],
    ["recent_form", "Form", (r) => r.metrics.recent_form, (r) => fmt(r.metrics.recent_form)],
    ["consistency", "SD", (r) => r.metrics.consistency, (r) => fmt(r.metrics.consistency)],
    ["allplay", "All-play", (r) => r.metrics.allplay_pct, (r) => `${r.metrics.allplay_w}-${r.metrics.allplay_l}`],
    ["luck", "Luck", (r) => r.metrics.luck, (r) => signed(r.metrics.luck, 2)],
    ["sos", "SOS", (r) => r.metrics.sos, (r) => fmt(r.metrics.sos)],
    ["bench", "Bench/g", (r) => r.metrics.bench_pg, (r) => fmt(r.metrics.bench_pg)],
    ["high", "High", (r) => r.metrics.high, (r) => fmt(r.metrics.high)],
  ];

  async function viewStats(weekParam) {
    const meta = store.meta;
    if (!meta.weeks.length) { app.innerHTML = `<p class="empty">Stats appear after week 1.</p>`; return; }
    const week = pickWeek(weekParam);
    const [data, hist, bt] = await Promise.all([loadWeek(week), load("history", "history.json"), load("backtest", "backtest.json")]);
    const { key, dir } = store.sort;
    const col = STAT_COLS.find((c) => c[0] === key) || STAT_COLS[0];
    const rows = data.rankings.slice().sort((a, b) => {
      const va = col[2](a), vb = col[2](b);
      return (typeof va === "string" ? va.localeCompare(vb) : va - vb) * dir;
    });
    const head = STAT_COLS.map(([k, label]) => {
      const sorted = k === key ? (dir === 1 ? "ascending" : "descending") : null;
      return `<th class="${k === "team" ? "l sticky" : ""}" ${sorted ? `aria-sort="${sorted}"` : ""}><button type="button" data-sort="${k}">${label}</button></th>`;
    }).join("");
    const body = rows.map((r) => `<tr>${STAT_COLS.map(([k, , , show]) => k === "team"
      ? `<td class="l sticky"><a href="#/team/${r.roster_id}">${esc(team(r.roster_id).team_name)}</a></td>`
      : `<td class="num">${esc(show(r))}</td>`).join("")}</tr>`).join("");

    const weights = Object.entries(meta.weights).map(([k, v]) => `${meta.metric_labels[k] || k} ${Math.round(v * 100)}%`).join(" · ");
    const alltime = hist.alltime.map((a) => `<tr>
      <td class="l sticky">${esc(a.name)}</td><td class="num">${a.seasons.length}</td>
      <td class="num">${a.wins}-${a.losses}${a.ties ? `-${a.ties}` : ""}</td><td class="num">${pct(a.win_pct)}</td>
      <td class="num">${a.playoff_wins}-${a.playoff_losses}</td><td class="num">${a.championships}</td><td class="num">${fmt(a.pf, 0)}</td></tr>`).join("");
    const best = Math.max(...bt.results.map((r) => r.accuracy || 0));
    const btRows = bt.results.slice().sort((a, b) => (b.accuracy || 0) - (a.accuracy || 0)).map((r) => `
      <li><span>${r.name === "Current weights" ? "<b>Current weights</b>" : esc(r.name)}${r.accuracy === best ? " 🥇" : ""}</span>
        <span class="num">${r.accuracy == null ? "–" : pct(r.accuracy)} <span class="team-meta">(${r.correct}/${r.games})</span></span></li>`).join("");

    app.innerHTML = `
      <div class="view-head"><div><h1>Full Stats</h1><div class="sub">Season to date through week ${week} · tap a column to sort</div></div>
        ${weekSelect(meta.weeks, week, "stats")}</div>
      <div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>
      <p class="scroll-hint">Swipe the table sideways for more columns. Luck = actual wins − all-play expected wins. SOS = opponents' avg PPG.</p>
      <h2>How the score works</h2>
      <section class="panel"><p class="sub" style="margin:0">Each metric is turned into a z-score across the league, then weighted: ${esc(weights)}. The weighted total is mapped to 0–100. Recent form weights each week by ${meta.decay}<sup>weeks ago</sup>.</p></section>
      <h2>Do the rankings work?</h2>
      <section class="panel"><p class="sub">How often the higher-ranked team won the next week (${esc(bt.seasons.join(", "))}).</p><ul class="list">${btRows}</ul></section>
      <h2>All-time records</h2>
      <div class="table-wrap"><table><thead><tr><th class="l sticky">Manager</th><th>Seasons</th><th>Record</th><th>Win %</th><th>Playoffs</th><th>Titles</th><th>PF</th></tr></thead>
        <tbody>${alltime}</tbody></table></div>`;
    bindWeekSelect();
    app.querySelectorAll("th button[data-sort]").forEach((b) => b.addEventListener("click", () => {
      const k = b.dataset.sort;
      store.sort = { key: k, dir: store.sort.key === k ? -store.sort.dir : (k === "rank" || k === "team" ? 1 : -1) };
      const x = window.scrollX, y = window.scrollY;
      const scroller = app.querySelector(".table-wrap").scrollLeft;
      viewStats(week).then(() => { window.scrollTo(x, y); app.querySelector(".table-wrap").scrollLeft = scroller; });
    }));
  }

  // -- router ----------------------------------------------------------------
  async function route() {
    const [name = "rankings", arg, arg2] = location.hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent);
    const tab = name === "team" ? "rankings" : name;
    document.querySelectorAll(".tabs a").forEach((a) => {
      if (a.dataset.tab === tab) { a.setAttribute("aria-current", "page"); a.scrollIntoView({ block: "nearest", inline: "nearest" }); }
      else a.removeAttribute("aria-current");
    });
    destroyCharts();
    try {
      if (name === "team") await viewTeam(arg);
      else if (name === "future" && arg === "team") await viewFutureTeam(arg2);
      else if (name === "future") await viewFuture(arg);
      else if (name === "trades") return await viewTrades(arg);
      else if (name === "odds") await viewOdds();
      else if (name === "awards") await viewAwards(arg);
      else if (name === "stats") await viewStats(arg);
      else await viewRankings(arg);
    } catch (err) {
      console.error(err);
      app.innerHTML = `<p class="empty">Couldn't load data (${esc(err.message)}). Try refreshing.</p>`;
    }
  }

  function setupTheme() {
    const btn = document.getElementById("theme-btn");
    const isDark = () => document.documentElement.dataset.theme === "dark" ||
      (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
    btn.addEventListener("click", () => {
      const next = isDark() ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem("theme", next); } catch (e) { /* storage unavailable */ }
      route();
    });
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      if (!document.documentElement.dataset.theme) route();
    });
  }

  async function start() {
    setupTheme();
    try {
      store.meta = await getJSON("meta.json");
    } catch (err) {
      app.innerHTML = `<p class="empty">No data yet. Run the pipeline to generate <code>site/data</code>.</p>`;
      return;
    }
    const meta = store.meta;
    document.getElementById("league-name").textContent = meta.league.name || "Power Rankings";
    document.title = `${meta.league.name || "League"} · Power Rankings`;
    const updated = new Date(meta.generated_at);
    document.getElementById("updated").textContent =
      `Updated ${updated.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}`;
    document.getElementById("demo-banner").hidden = !meta.demo;
    document.getElementById("foot-text").textContent =
      `${meta.league.season} season · ranked through week ${meta.latest_week || 0} · ` +
      `${meta.blurb_source === "anthropic" ? "write-ups by Claude" : "template write-ups"} · data from the Sleeper API · last updated ${updated.toLocaleString()}`;
    window.addEventListener("hashchange", () => { route().then((keep) => { if (!keep) window.scrollTo(0, 0); }); });
    route();
  }

  start();
})();
