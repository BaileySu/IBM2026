"use strict";
const $ = id => document.getElementById(id);
let turns = [];
let busy = false;
function element(tag, text) {
  const node = document.createElement(tag);
  node.textContent = text;
  return node;
}
function pct(v) { return v == null ? "—" : (Number(v) * 100).toFixed(2) + "%"; }
function table(headers, rows) {
  const frame = element("div", "");
  frame.className = "scroll";
  const grid = document.createElement("table");
  const head = grid.createTHead().insertRow();
  headers.forEach(label => head.append(element("th", label)));
  const body = grid.createTBody();
  for (const row of rows) {
    const tr = body.insertRow();
    row.forEach(value => tr.append(element("td", value)));
  }
  frame.append(grid);
  return frame;
}
function render(question, reply) {
  const box = element("article", "");
  box.append(element("h2", question));
  if (reply.action === "clarify") {
    box.append(element("p", reply.message));
    for (const row of reply.coverage || []) {
      box.append(element("p", row.fund + " · " + row.covered_securities + " of " + row.total_securities
        + " holdings covered (" + pct(row.coverage_weight) + " of equity market value)."));
      const missing = element("details","");
      missing.append(element("summary","Holdings without a complete volatility window"),element("p",row.missing_symbols.join(", ")));
      box.append(missing);
    }
  } else {
    for (const result of reply.results) {
      const d = result.data;
      if (result.kind === "research") {
        box.append(element("h3", "Security research · " + reply.as_of));
        box.append(table(["Stock","Sessions","Return","Ann. volatility","Prices","Status"],
          d.map(r => [r.symbol,r.window,pct(r.window_return),pct(r.annualized_volatility),
            r.observations + "/" + r.required_prices,r.reason || r.status])));
      } else {
        box.append(element("h3", d.fund + " · " + d.window + " sessions · " + d.as_of));
        box.append(element("p", "Top five: " + pct(d.top5_weight) + " of equity market value. Price coverage: "
          + pct(d.coverage_weight) + " (" + d.covered_securities + " of " + d.total_securities + " holdings)."));
        box.append(table(["Largest holdings","Weight","Ann. volatility"],
          d.securities.map(r => [r.symbol,pct(r.weight),pct(r.annualized_volatility)])));
        if (d.focus_holdings) {
          box.append(element("h3","Requested holdings"), table(["Stock","USD market value","Weight","Ann. volatility","Status"],
            d.focus_holdings.map(r => [r.symbol,(r.value_cents / 100).toLocaleString("en-US",{minimumFractionDigits:2}),
              pct(r.weight),pct(r.annualized_volatility),r.reason || r.status])));
        }
        if (d.covered_holdings) {
          box.append(element("h3","Holdings with price coverage"),
            table(["Stock","Weight","Ann. volatility"], d.covered_holdings.map(r =>
              [r.symbol,pct(r.weight),pct(r.annualized_volatility)])));
        }
        const details = element("details","");
        details.append(element("summary","Sector weights"),table(["Sector","Weight"], d.sectors.map(s => [s.sector,pct(s.weight)])));
        box.append(details);
      }
      const link = element("a", "Open full calculation and sources");
      link.href = result.source;
      box.append(link);
    }
    const hasGaps = reply.gaps.length > 0;
    if (hasGaps) {
      box.append(element("p",reply.coverage_question || ("Missing price history: " + [...new Set(reply.gaps.map(r => r.symbol))].join(", ")
        + ". Save the available results with these gaps marked, or wait for the missing history.")));
    }
    const decision = element("div","");
    const save = element("button",hasGaps ? "Accept and save partial results" : "Save verified result");
    save.type = "button";
    decision.append(save);
    if (hasGaps) {
      const wait = element("button","Wait for price history");
      wait.type = "button";
      wait.onclick = () => decision.replaceChildren(element("p","On hold until the required price history is available."));
      decision.append(wait);
    }
    save.onclick = async () => {
      for (const control of decision.querySelectorAll("button")) control.disabled = true;
      try {
        const res = await fetch("/v1/results",{method:"POST",headers:{"Content-Type":"application/json"},
          body:JSON.stringify({selection:reply.selection,version:reply.version,accept_partial:hasGaps})});
        const saved = await res.json();
        if (!res.ok) throw new Error(saved.error || "Could not save this result.");
        const link = element("a","Open saved result · " + saved.id.slice(0,12));
        link.href = saved.source;
        decision.replaceChildren(link);
      } catch (err) {
        $("notice").textContent = err.message;
        for (const control of decision.querySelectorAll("button")) control.disabled = false;
      }
    };
    box.append(decision);
  }
  $("answers").prepend(box);
}
async function start() {
  const [model, catalog] = await Promise.all(["/v1/assistant/status","/v1/catalog"].map(async path => {
    const res = await fetch(path);
    if (!res.ok) throw new Error("Cannot read the workbench configuration.");
    return res.json();
  }));
  $("snapshot").textContent = "Holdings snapshot · " + catalog.holdings_date;
  $("model").textContent = model.configured ? model.provider + " · " + model.model
    + " receives your question, definitions and requested securities’ coverage. Calculations run locally." : "";
  $("notice").textContent = model.configured ? "" : "Set up a model in the local server to enable questions. Portfolio risk and security research are available.";
  $("ask").disabled = !model.configured;
}
$("ask-form").addEventListener("submit", async event => {
  event.preventDefault();
  const question = $("question").value.trim();
  if (!question || busy || $("ask").disabled) return;
  const pending = [...turns,{role:"user",content:question}];
  busy = true;
  $("ask").disabled = $("reset").disabled = $("question").disabled = true;
  $("notice").textContent = "Interpreting your question…";
  try {
    const response = await fetch("/v1/assistant", {method:"POST",
      headers:{"Content-Type":"application/json"}, body:JSON.stringify({turns:pending})});
    const reply = await response.json();
    if (!response.ok) throw new Error(reply.error || "Could not process this question.");
    render(question, reply);
    const context = reply.action === "clarify" && reply.reason !== "coverage" ? reply.message :
      JSON.stringify({selection:reply.selection,question:reply.message,
        missing:[...new Set((reply.gaps || []).map(r => r.symbol))]});
    turns = [...pending,{role:"assistant",content:context}];
    $("question").value = "";
    $("reset").hidden = false;
    $("notice").textContent = turns.length >= 12 ? "Start a new conversation to ask another question." : "";
  } catch (err) { $("notice").textContent = err.message; }
  finally {
    busy = false;
    $("reset").disabled = $("question").disabled = false;
    $("ask").disabled = turns.length >= 12;
    $("question").focus();
  }
});
$("reset").addEventListener("click", () => {
  turns = []; $("answers").replaceChildren(); $("notice").textContent = ""; $("question").value = "";
  $("reset").hidden = true; $("ask").disabled = false; $("question").focus();
});
start().catch(err => { $("notice").textContent = err.message; });
