const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const money = (v) => "¥" + Number(v || 0).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const num = (v, d = 2) => (v === null || v === undefined ? "—" : Number(v).toFixed(d));
const cls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
const sign = (v, d = 2) => (v > 0 ? "+" : "") + Number(v || 0).toFixed(d);
const ACT = { buy: "买入", sell: "卖出", hold: "继续持有" };
const SIDE = { buy: "买入", sell: "卖出" };

let S = null, ledgerTab = "positions", evFilter = "", selSym = null;

async function post(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) alert((await r.json()).detail || "操作失败");
  refresh();
}

document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-act]");
  if (b) {
    if (b.dataset.confirm && !confirm(b.dataset.confirm)) return;
    if (b.dataset.act === "reset" && !confirm("清空本地台账并重新开始？")) return;
    post("/api/control", { action: b.dataset.act });
  }
  const t = e.target.closest("[data-tab]");
  if (t) { ledgerTab = t.dataset.tab; render(); }
  const ev = e.target.closest("[data-ev]");
  if (ev) { evFilter = ev.dataset.ev; render(); }
  const j = e.target.closest("[data-jump]");
  if (j) $(j.dataset.jump).scrollIntoView({ behavior: "smooth" });
  const row = e.target.closest("[data-sym]");
  if (row) { selSym = row.dataset.sym; render(); }
});
$("speed").addEventListener("change", (e) => post("/api/control", { action: "speed", value: Number(e.target.value) }));

function table(cols, rows, opts = {}) {
  if (!rows.length) return `<div class="empty">${opts.empty || "暂无数据"}</div>`;
  return `<table><thead><tr>${cols.map((c) => `<th>${c[0]}</th>`).join("")}</tr></thead><tbody>${rows
    .map((r) => `<tr ${opts.row ? opts.row(r) : ""}>${cols.map((c) => `<td class="${c[2] || ""}">${c[1](r)}</td>`).join("")}</tr>`)
    .join("")}</tbody></table>`;
}

function renderHeader() {
  const c = S.clock;
  $("modeBadge").textContent = { sim: "模拟", paper: "仿真", live: "实盘" }[S.mode] + " · " + S.broker;
  $("clockBadge").textContent = `● ${c.phase} · ${c.now.slice(5)}`;
  $("liveBadge").textContent = S.mode === "live" ? "实盘写已启用" : "模拟撮合";
  $("liveBadge").className = "pill " + (S.mode === "live" ? "red" : "gray");
  const halted = S.risk_state.halted, paused = S.risk_state.paused;
  $("runBadge").textContent = halted ? "● 熔断" : paused ? "● 已暂停" : "● 运行中 (短线智能策略)";
  $("runBadge").className = "pill " + (halted ? "red" : paused ? "amber" : "green");
  $("acctBadge").textContent = `${S.account} · ${S.strategy}`;
  $("simCtrls").style.display = c.simulated ? "" : "none";
  $("phase").textContent = `${c.date} · ${c.phase}`;
}

function renderPipeline() {
  const color = { 已完成: "green", 运行中: "blue", 待命: "gray", 熔断: "red" };
  $("pipeline").innerHTML = S.pipeline.map((p) => `<div class="stage"><span class="pill ${color[p.status]}">${p.status}</span>
    <div class="t">${p.title}</div><div class="d">${esc(p.desc)}</div><div class="tm">时点 ${p.time}</div></div>`).join("");
}

function renderRisk() {
  const a = S.asset, r = S.risk_cfg, st = S.risk_state;
  $("riskRun").textContent = st.halted ? "熔断" : st.paused ? "已暂停" : "运行中";
  $("roBadge").textContent = st.read_only ? "🔒 只读防误触" : "可交易";
  $("roBadge").className = "pill " + (st.read_only ? "green" : "gray");
  $("reconBadge").textContent = S.recon.status + (S.recon.ts ? " " + S.recon.ts : "");
  $("reconBadge").className = "pill " + (S.recon.status === "对账差异" ? "red" : "gray");
  $("btnRO").classList.toggle("on", st.read_only);
  $("btnPause").textContent = st.paused ? "恢复调度" : "暂停调度";
  $("btnUnhalt").hidden = !st.halted;
  $("kEquity").textContent = money(a.equity);
  $("kEquityS").textContent = "本地台账总资产";
  $("kPnl").textContent = (a.day_pnl >= 0 ? "+" : "") + money(a.day_pnl).replace("¥", "¥");
  $("kPnlS").textContent = `前日定格 ${money(a.base)} · ${sign(a.day_pnl_pct)}%`;
  $("kPnlBox").className = "kpi " + (a.day_pnl > 0 ? "up" : a.day_pnl < 0 ? "down" : "");
  const cr = a.equity ? a.cash / a.equity : 0, pr = a.equity ? a.position_value / a.equity : 0;
  $("kCash").textContent = money(a.available);
  $("kCashBar").style.width = (cr * 100).toFixed(1) + "%";
  $("kCashS").textContent = `现金占比 ${(cr * 100).toFixed(1)}%`;
  $("kPos").textContent = money(a.position_value);
  $("kPosBar").style.width = (pr * 100).toFixed(1) + "%";
  $("kPosS").textContent = `实时仓位暴露 ${(pr * 100).toFixed(1)}%`;
  const row = (k, v) => `<div class="row"><span>${k}</span><b>${v}</b></div>`;
  $("limits").innerHTML = `
    <div class="limit"><div class="hd">资金与仓位限额 <span class="pill">已发布</span></div>
      ${row("单日买入上限", money(r.daily_buy_limit) + `（已用 ${money(a.bought_today)}）`)}
      ${row("单笔委托上限", money(r.order_amount_limit))}
      ${row("总仓位 / 单票占比", `${r.max_total_position * 100}% / ${r.max_single_position * 100}%`)}</div>
    <div class="limit"><div class="hd">配额与报单限制 <span class="pill">${r.max_orders_per_day} 笔/日</span></div>
      ${row("策略持仓席位", `${S.positions.length} / ${r.max_seats}`)}
      ${row("单笔最大数量", r.max_order_qty + " 股")}
      ${row("今日报单 / 单日撤单上限", `${a.orders_today} / ${r.max_cancels_per_day} 笔`)}</div>
    <div class="limit"><div class="hd">交易许可与对账 <span class="pill">${st.read_only ? "关闭" : "开放"}</span></div>
      ${row("调度状态", st.paused ? "暂停" : "运行")}
      ${row("LLM 预算", `${money(S.llm.spent)} / ${money(S.llm.budget)} · ${S.llm.provider}`)}
      ${row("对账阻断", st.halted || "无当前阻断")}</div>`;
  $("cfgVer").textContent = `风控版本 ${r.version} · 以当前账户已发布规则为准 · 柜台现金 ${money(S.counter.cash)}`;
}

function renderLedger() {
  document.querySelectorAll("#ledgerTabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === ledgerTab));
  const v = {
    positions: () => table([
      ["标的 / 策略", (p) => `<b>${esc(p.name)}</b> <span class="pill blue">${p.board}</span><div class="muted">${p.symbol} · ${p.strategy} · T+${p.days_held}</div>`],
      ["持仓 / 可卖", (p) => `<b>${p.qty}</b><div class="muted">可卖 ${p.sellable}</div>`],
      ["成本 / 现价", (p) => `${num(p.cost, 4)}<div class="${cls(p.chg_pct)}">¥${num(p.price)} ${sign(p.chg_pct)}%</div>`],
      ["持仓市值", (p) => money(p.value)],
      ["当日盈亏", (p) => `<span class="${cls(p.day_pnl)}">${sign(p.day_pnl)}</span>`],
      ["浮动盈亏", (p) => `<span class="${cls(p.float_pnl)}">${sign(p.float_pnl)}<br>${sign(p.float_pct)}%</span>`],
      ["更新时点", (p) => (p.updated_ts || "").slice(11, 19)],
    ], S.positions, { empty: "空仓" }),
    orders: () => table([
      ["时间", (o) => o.ts.slice(11, 19)], ["标的", (o) => `${esc(o.name)} <span class="muted">${o.symbol}</span>`],
      ["方向", (o) => `<span class="${o.side === "buy" ? "up" : "down"}">${SIDE[o.side]}</span>`],
      ["委托价", (o) => num(o.price)], ["数量", (o) => o.qty], ["成交", (o) => `${o.filled_qty} @ ${num(o.avg_price)}`],
      ["状态", (o) => `<span class="pill">${o.status}</span>`], ["柜台编号", (o) => o.broker_id], ["备注", (o) => esc(o.note), "reason"],
    ], S.orders),
    fills: () => table([
      ["时间", (f) => f.ts.slice(11, 19)], ["标的", (f) => `${esc(f.name)} <span class="muted">${f.symbol}</span>`],
      ["方向", (f) => `<span class="${f.side === "buy" ? "up" : "down"}">${SIDE[f.side]}</span>`],
      ["成交价", (f) => num(f.price)], ["数量", (f) => f.qty], ["金额", (f) => money(f.price * f.qty)], ["费用", (f) => num(f.fee)],
    ], S.fills),
    closed: () => table([
      ["标的", (c) => `${esc(c.name)} <span class="muted">${c.symbol}</span>`], ["开仓", (c) => c.open_date], ["平仓", (c) => c.close_date],
      ["数量", (c) => c.qty], ["成本", (c) => num(c.cost, 3)], ["平仓价", (c) => num(c.close_price)],
      ["盈亏", (c) => `<span class="${cls(c.pnl)}">${sign(c.pnl)}</span>`],
    ], S.closed),
  };
  $("ledger").innerHTML = v[ledgerTab]();
}

function renderEvents() {
  document.querySelectorAll("#evTabs button").forEach((b) => b.classList.toggle("on", b.dataset.ev === evFilter));
  const evs = S.events.filter((e) => !evFilter || e.category === evFilter);
  $("events").innerHTML = evs.length ? evs.map((e) => `<div class="ev ${e.level}"><div class="h"><span class="pill">${esc(e.category)}</span>
    <span>${e.ts.replace("T", " ").slice(5, 19)}</span></div><div class="t">${esc(e.title)}</div><div class="d">${esc(e.detail)}</div></div>`).join("")
    : `<div class="empty">暂无事件</div>`;
}

function renderLLM() {
  const R = S.rounds, cur = R[0];
  $("interval").textContent = `${S.llm.interval} 分钟滚动节拍`;
  if (!cur) {
    $("roundTitle").textContent = "";
    $("macroBox").innerHTML = `<div class="muted">等待 09:25 盘前首轮推演…</div>`;
    $("rejBox").innerHTML = $("holdBox").innerHTML = $("histList").innerHTML = "";
    $("intents").innerHTML = "";
    return;
  }
  $("roundTitle").textContent = `（第 ${cur.round_no} 轮 · ${cur.ts}）`;
  const live = S.intents.filter((i) => ["已发布", "执行中"].includes(i.status) && i.action !== "hold").length;
  $("roundBadge").textContent = live ? `● ${live} 笔有效意向` : "● 守纪观望 · 未盲目买入";
  $("roundBadge").className = "pill " + (live ? "green" : "");
  const bias = cur.position_bias;
  $("macroBox").innerHTML = `<div class="hd">🌐 宏观市场态度与趋势研判 <span class="pill ${bias < 0 ? "amber" : bias > 0 ? "green" : ""}">仓位风险倾向：${sign(bias, 0)}</span>
    <span class="muted">${esc(cur.provider)}</span></div>${esc(cur.macro)}`;
  $("rejBox").innerHTML = `<div class="hd">🛡 选股风控与不买入归因（严守纪律）</div>${esc(cur.rejections)}`;
  $("holdBox").innerHTML = `<b>持仓复评结论：</b><span class="muted">${esc(cur.holdings_review)}</span>`;
  $("histSum").textContent = `查看今日盘中历史推演过程（共已推演 ${R.length} 轮）`;
  $("histList").innerHTML = R.slice(1).map((r) => `<div class="hist"><div class="hd"><span>第 ${r.round_no} 轮推演（${r.ts}）</span>
    <span class="muted">${r.intents_count ? r.intents_count + " 笔意向" : "风控未开仓"}</span></div>
    <div><span class="lbl">风控归因：</span>${esc(r.rejections)}</div><div><span class="lbl2">市场观点：</span>${esc(r.macro)}</div></div>`).join("");
  $("intents").innerHTML = table([
    ["标的", (i) => `${i.symbol} ${esc(i.name)} <span class="muted">(${i.exchange})</span>`],
    ["动作", (i) => `<span class="${i.action === "buy" ? "up" : i.action === "sell" ? "down" : ""}">${ACT[i.action]}</span>`],
    ["状态", (i) => `<span class="pill">${i.status}</span>`],
    ["价格区间", (i) => (i.price_low ? `${num(i.price_low)} – ${num(i.price_high)}` : "—")],
    ["目标量", (i) => i.target_qty || "—"], ["配额", (i) => (i.amount ? money(i.amount) : "—")], ["档位", (i) => i.tier || "—"],
    ["置信度", (i) => num(i.confidence)], ["到期", (i) => i.expire], ["理由", (i) => esc(i.reason), "reason"],
  ], S.intents, { empty: "当前无活动意向" });
}

function radarSVG(f, avg) {
  const keys = Object.keys(f), n = keys.length, R = 110;
  const pt = (i, v) => { const a = -Math.PI / 2 + (2 * Math.PI * i) / n; return [Math.cos(a) * R * v / 100, Math.sin(a) * R * v / 100]; };
  const poly = (vals) => vals.map((v, i) => pt(i, v).join(",")).join(" ");
  let s = "";
  [25, 50, 75, 100].forEach((l) => (s += `<polygon points="${poly(keys.map(() => l))}" fill="none" stroke="currentColor" stroke-opacity=".12"/>`));
  keys.forEach((k, i) => { const [x, y] = pt(i, 100), [tx, ty] = pt(i, 122);
    s += `<line x1="0" y1="0" x2="${x}" y2="${y}" stroke="currentColor" stroke-opacity=".12"/>`;
    s += `<text x="${tx}" y="${ty + 4}" text-anchor="middle">${k}</text>`; });
  if (avg) s += `<polygon points="${poly(keys.map((k) => avg[k]))}" fill="none" stroke="#8aa7ea" stroke-dasharray="4 3"/>`;
  s += `<polygon points="${poly(keys.map((k) => f[k]))}" fill="#3b6fe0" fill-opacity=".18" stroke="#3b6fe0" stroke-width="2"/>`;
  keys.forEach((k, i) => { const [x, y] = pt(i, f[k]); s += `<circle cx="${x}" cy="${y}" r="3" fill="#3b6fe0"/>`; });
  return s;
}

function renderPool() {
  const P = S.pool;
  $("pool").innerHTML = table([
    ["级别", (p) => `<span class="pill ${p.stage === 2 ? "blue" : ""}">${p.stage === 2 ? "二级" : "一级"}</span>`],
    ["标的", (p) => `<b>${esc(p.name)}</b> <span class="muted">${p.symbol}</span>`], ["行业", (p) => esc(p.industry || "")],
    ["现价", (p) => num(p.price)], ["涨幅", (p) => `<span class="${cls(p.chg_pct)}">${sign(p.chg_pct)}%</span>`],
    ["LLM 置信", (p) => num(p.score, 0)], ["逻辑", (p) => esc(p.note), "reason"],
  ], P, { empty: "09:15 冻结预筛池", row: (p) => `class="click ${p.symbol === selSym ? "sel" : ""}" data-sym="${p.symbol}"` });
  if (!P.length) { $("radar").innerHTML = ""; $("factorBars").innerHTML = ""; return; }
  const p = P.find((x) => x.symbol === selSym) || P[0];
  const keys = Object.keys(p.factors), avg = {};
  keys.forEach((k) => (avg[k] = P.reduce((s, x) => s + (x.factors[k] || 0), 0) / P.length));
  $("radar").innerHTML = radarSVG(p.factors, avg) + `<text x="0" y="146" text-anchor="middle">${esc(p.name)}（实线） vs 候选池均值（虚线）</text>`;
  $("factorBars").innerHTML = keys.map((k) => `<div class="factor"><span>${k}</span><span class="fb"><i class="${p.factors[k] >= 80 ? "hi" : ""}"
    style="width:${p.factors[k]}%"></i></span><b>${p.factors[k]}</b></div>`).join("");
}

function renderIntel() {
  $("intel").innerHTML = table([
    ["来源", (i) => `<span class="pill">${esc(i.source)}</span>`], ["标的", (i) => i.symbol], ["内容", (i) => esc(i.title), "reason"],
    ["情感", (i) => `<span class="${cls(i.sentiment)}">${sign(i.sentiment)}</span>`], ["新颖度", (i) => num(i.novelty)],
  ], S.intel, { empty: "09:00 开始采集" });
}

function render() {
  if (!S) return;
  renderHeader(); renderPipeline(); renderRisk(); renderLedger(); renderEvents(); renderLLM(); renderPool(); renderIntel();
}

async function refresh() {
  try {
    const s = await (await fetch("/api/state")).json();
    if (s.clock) { S = s; render(); }
    if (s.busy) $("roundBadge").textContent = "● " + s.busy;
  } catch (e) { $("clockBadge").textContent = "● 连接断开"; }
}
refresh();
setInterval(refresh, 1500);
