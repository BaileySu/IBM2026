const $ = id => document.getElementById(id);
const pct = n => n === null ? "—" : (100 * Number(n)).toFixed(2) + "%";
const money = cents => (cents / 100).toLocaleString("en-US", {style:"currency", currency:"USD", maximumFractionDigits:0});
const labels = {READY:"Available", BLOCKED:"Price window incomplete", NOT_COVERED:"No price history"};
let catalog, funds = [], limit = 12, research = null, generation = 0;

function node(tag, text, css) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (css) element.className = css;
  return element;
}

function link(path, args) {
  return path + "?" + new URLSearchParams({...args, version:catalog.version});
}

async function read(url) {
  const response = await fetch(url);
  const data = await response.json();
  if (!response.ok) throw Error(data.error || "Unable to read the snapshot.");
  return data;
}

function table(headers, rows) {
  const t = node("table"), head = t.createTHead().insertRow(), body = t.createTBody();
  headers.forEach(h => head.append(node("th", h)));
  rows.forEach(values => {
    const row = body.insertRow();
    values.forEach(value => row.insertCell().textContent = value);
  });
  if (!rows.length) {
    const cell = body.insertRow().insertCell();
    cell.colSpan = headers.length;
    cell.textContent = "No holdings match this search.";
  }
  return t;
}

function bars(id, items) {
  const max = Math.max(...items.map(i => Number(i.weight)));
  $(id).replaceChildren(...items.map(i => {
    const row = node("div", undefined, "bar-row"), track = node("span", undefined, "track");
    const fill = node("span", undefined, "fill");
    fill.style.width = (100 * Number(i.weight) / max) + "%";
    track.append(fill);
    row.append(node("span", i.symbol || i.sector), track, node("strong", pct(i.weight)));
    return row;
  }));
}

function selected() { return funds.find(f => f.fund === $("fund").value); }

function showHoldings() {
  const fund = selected();
  if (!fund) return;
  const query = $("search").value.trim().toUpperCase();
  const matches = fund.securities.filter(row => row.symbol.includes(query));
  $("holdings").replaceChildren(table(["Symbol", "Market value", "Equity weight", "Annualized volatility", "Price history"],
    matches.slice(0,limit).map(row => [row.symbol, money(row.value_cents), pct(row.weight),
      pct(row.annualized_volatility), row.status === "BLOCKED" ? row.reason : labels[row.status]])));
  $("count").textContent = Math.min(limit,matches.length) + " of " + matches.length + " holdings";
  $("more").hidden = matches.length <= limit;
}

function detail() {
  const f = selected();
  if (!f) return;
  bars("top", f.securities.slice(0,5));
  bars("sectors", f.sectors);
  $("risk-json").href = link("/v1/risk-data", {fund:f.fund, window:f.window, date:f.as_of});
  limit = 12;
  showHoldings();
}

async function showRisk(ticket) {
  const w = $("risk-window").value;
  const responses = await Promise.all(catalog.funds.map(fund => read(link("/v1/risk-data", {fund, window:w}))));
  if (ticket !== generation) return;
  funds = responses.map(r => r.data);
  $("fund-cards").replaceChildren(...funds.map(f => {
    const box = node("article", undefined, "fund-card");
    box.append(node("h3",f.fund),node("p",pct(f.top5_weight),"metric"),node("div","in the five largest holdings","caption"),
      node("p",pct(f.coverage_weight) + " of equity value has a complete " + w + "-session price window (" +
        f.covered_securities + " of " + f.total_securities + " securities).","coverage"));
    return box;
  }));
  detail();
}

const csvFields = ["symbol","as_of","window","start","observations","required_prices","window_return",
  "annualized_volatility","status","reason","source_id","version"];

function clearResearch() {
  research = null;
  $("csv").disabled = true;
  $("research-json").removeAttribute("href");
  $("research-results").replaceChildren();
}

async function runResearch(ticket) {
  const fields = new FormData($("research-form"));
  const url = link("/v1/research-data",{symbols:fields.get("symbols"),windows:fields.get("windows")});
  const response = await read(url);
  if (ticket !== generation) return;
  research = response;
  $("research-results").replaceChildren(table(["Symbol","Sessions","Valid prices","Window return","Annualized volatility","Coverage"],
    response.data.map(r => [r.symbol,r.window,r.observations + " / " + r.required_prices,pct(r.window_return),
      pct(r.annualized_volatility),r.status === "BLOCKED" ? r.reason : labels[r.status]])));
  $("csv").disabled = false;
  $("research-json").href = url;
}

function download() {
  if (!research) return;
  const quote = v => '"' + String(v ?? "").replaceAll('"','""') + '"';
  const rows = research.data.map(r => ({...r,version:research.version}));
  const lines = [csvFields.join(","), ...rows.map(r => csvFields.map(k => quote(r[k])).join(","))];
  const url = URL.createObjectURL(new Blob([lines.join("\r\n")],{type:"text/csv;charset=utf-8"}));
  const a = node("a");
  a.href = url;
  a.download = "quantbridge-research-" + research.holdings_date + ".csv";
  a.click();
  setTimeout(() => URL.revokeObjectURL(url),1000);
}

async function refresh() {
  if (!catalog) return;
  const ticket = ++generation, isRisk = location.hash !== "#research";
  $("risk").hidden = true;
  $("research").hidden = isRisk;
  clearResearch();
  for (const [id,active] of [["risk-link",isRisk],["research-link",!isRisk]]) {
    if (active) $(id).setAttribute("aria-current","page"); else $(id).removeAttribute("aria-current");
  }
  $("notice").textContent = "Reading snapshot…";
  try {
    if (isRisk) await showRisk(ticket); else await runResearch(ticket);
    if (ticket !== generation) return;
    $("risk").hidden = !isRisk;
    $("notice").textContent = "";
  } catch (error) {
    if (ticket === generation) $("notice").textContent = error.message;
  }
}

async function start() {
  try {
    catalog = await read("/v1/catalog");
    document.querySelectorAll(".snapshot").forEach(p => p.textContent = "Holdings snapshot · " + catalog.holdings_date);
    $("fund").replaceChildren(...catalog.funds.map(f => new Option(f,f)));
    $("definitions").hidden = false;
    $("versions").textContent = "Snapshot " + catalog.snapshot + " · Rulebook " + catalog.rules + " · " + catalog.method;
    window.addEventListener("hashchange",refresh);
    $("risk-window").addEventListener("change",refresh);
    $("fund").addEventListener("change",detail);
    $("search").addEventListener("input",() => {limit=12; showHoldings();});
    $("more").addEventListener("click",() => {limit+=30; showHoldings();});
    $("research-form").addEventListener("submit",event => {event.preventDefault(); refresh();});
    $("research-form").addEventListener("input",() => {generation++; clearResearch(); $("notice").textContent="Run the comparison to apply your changes.";});
    $("csv").addEventListener("click",download);
    await refresh();
  } catch (error) { $("notice").textContent = error.message; }
}
start();
