"""Local HTTP interface for the fixed QuantBridge snapshot."""
import argparse
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qs, urlencode, urlsplit

from .metrics import DATABASE, METHOD, RULES, WINDOWS, metadata, open_snapshot, research_data, risk_data, verify_snapshot
from .assistant import QUESTIONS, ModelUnavailable, checked_plan, model_config, model_plan, read_turns, valid_symbols
from .publication import publish, read_result

STATIC = Path(__file__).with_name("web")
ASSETS = {"/": ("index.html", "text/html"), "/app.css": ("app.css", "text/css"),
          "/app.js": ("app.js", "text/javascript"), "/assistant": ("assistant.html", "text/html"),
          "/assistant.js": ("assistant.js", "text/javascript")}


def query_values(query, allowed):
    args = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
    if args.keys() - allowed or any(len(v) != 1 or not v[0].strip() for v in args.values()):
        raise ValueError("Unrecognized, empty or repeated parameter.")
    return {k: v[0].strip() for k, v in args.items()}


def api_result(db, path, query, digest):
    meta = metadata(db)
    version = f"{meta['dataset_version']}.{meta['contract_version']}.{METHOD}.{digest[:12]}"
    common = {"snapshot": meta["dataset_version"], "rules": meta["contract_version"],
              "method": METHOD, "version": version, "database_sha256": digest,
              "holdings_date": meta["holdings_as_of"]}
    if path == "/v1/catalog":
        query_values(query, set())
        return dict(common, funds=[r[0] for r in db.execute("SELECT fund_id FROM funds ORDER BY fund_id")],
                    windows=list(WINDOWS), history_start=meta["history_start"], history_end=meta["history_end"])
    risk = path == "/v1/risk-data"
    args = query_values(query, {"date", "version"} | ({"fund", "window"} if risk else {"symbols", "windows"}))
    if args.get("version", version) != version:
        raise ValueError("This service cannot reproduce the requested version.")
    end = date.fromisoformat(args.get("date", meta["holdings_as_of"])).isoformat()
    if risk:
        data = risk_data(db, args.get("fund", "IVV").upper(), end, int(args.get("window", 20)))
    else:
        symbols = list(dict.fromkeys(s.strip().upper() for s in args.get("symbols", "NVDA").split(",")))
        if len(symbols) > 30 or any(not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,14}", s) for s in symbols):
            raise ValueError("Enter 1–30 comma-separated stock symbols.")
        windows = list(dict.fromkeys(int(w) for w in args.get("windows", "20,60").split(",")))
        data = research_data(db, symbols, windows, end)
    return dict(common, data=data)


def inspect_assets(server, args, catalog):
    funds, symbols = args.get("funds"), args.get("symbols")
    if (args.keys() != {"funds", "symbols"} or not valid_symbols(symbols, 0)
            or not valid_symbols(funds, 0) or set(funds) - set(catalog["funds"])):
        raise ModelUnavailable("The model requested an invalid asset lookup.")
    with open_snapshot(server.database, server.digest) as db:
        coverage = {}
        for symbol in symbols:
            prices = db.execute("SELECT source_id FROM history_universe WHERE symbol=? AND role='selected_equity'",
                                (symbol,)).fetchone()
            held = db.execute("SELECT DISTINCT fund_id FROM fund_holdings WHERE ticker=? AND asset_class='Equity' "
                              "AND currency='USD' AND market_currency='USD' ORDER BY fund_id", (symbol,))
            coverage[symbol] = {"held_by":[r[0] for r in held if not funds or r[0] in funds],
                                "price_source":prices[0] if prices else None}
        definitions = {"concentration":RULES["valuation"]["top5"], "sector":RULES["valuation"]["sector"],
                       "price_field":RULES["returns"]["price_field"], "volatility":RULES["returns"]["volatility"],
                       "return":RULES["returns"]["window_return"]}
        return dict(definitions=definitions, coverage=coverage, version=catalog["version"])


def assistant_result(server, payload, planner=None):
    turns = read_turns(payload)
    with open_snapshot(server.database, server.digest) as db:
        catalog = api_result(db, "/v1/catalog", "", server.digest)
    record = getattr(server, "record_event", None)
    if planner is None:
        raw = model_plan(turns, catalog, lambda args: inspect_assets(server, args, catalog), record)
    else:
        raw = planner(turns, catalog)
    plan = checked_plan(raw, catalog)
    action = plan["action"]
    reply = dict(action=action, version=catalog["version"], as_of=catalog["holdings_date"], selection=plan)
    if action == "clarify":
        message = plan.get("question") or QUESTIONS[plan["reason"]]
        if record:
            record({"event":"tool", "name":action, "arguments":plan, "result":message})
        return dict(reply, reason=plan["reason"], message=message)
    decision = dict(plan)
    coverage_question = decision.pop("coverage_question", None)
    report = calculate(server, catalog, decision, record)
    if report.get("gaps") and coverage_question:
        report["coverage_question"] = coverage_question
    return report


def calculate(server, catalog, plan, record=None):
    action = plan["action"]
    params = {"date": plan.get("date", catalog["holdings_date"]), "version": catalog["version"]}
    reply = dict(action=action, selection=plan, version=catalog["version"], as_of=params["date"])
    calls = [("risk", dict(fund=f, window=w)) for f in plan.get("funds", []) for w in plan["windows"]]
    symbols = plan.get("symbols", [])
    if symbols:
        calls.append(("research", dict(symbols=",".join(symbols), windows=",".join(map(str, plan["windows"])))))
    results = []
    gaps = []
    fund_gaps = []
    with open_snapshot(server.database, server.digest) as db:
        for kind, args in calls:
            path = f"/v1/{kind}-data"
            query = urlencode(dict(params, **args))
            data = api_result(db, path, query, server.digest)["data"]
            if kind == "risk":
                holdings = data["securities"]
                data = dict(data, securities=holdings[:5])
                if plan["scope"] == "all_volatility" and data["covered_securities"] < data["total_securities"]:
                    incomplete = {k:data[k] for k in ("fund","window","covered_securities","total_securities","coverage_weight")}
                    incomplete["missing_symbols"] = [h["symbol"] for h in holdings if h["status"] != "READY"]
                    incomplete["source"] = f"{path}?{query}"
                    fund_gaps.append(incomplete)
                if symbols:
                    data["focus_holdings"] = [h for h in holdings if h["symbol"] in symbols]
                if plan["scope"] == "covered_volatility":
                    data["covered_holdings"] = [h for h in holdings if h["status"] == "READY"]
            else:
                gaps = [{k:r[k] for k in ("symbol","window","status","reason")} for r in data if r["status"] != "READY"]
            result = dict(kind=kind, data=data, source=f"{path}?{query}")
            results.append(result)
            if record:
                record({"event":"query", "name":kind, "arguments":dict(params, **args), "source":result["source"]})
    if fund_gaps:
        return dict(reply, action="clarify", reason="coverage", message=QUESTIONS["coverage"],
                    coverage=fund_gaps)
    return dict(reply, results=results, gaps=gaps)


def save_calculation(server, payload):
    if not isinstance(payload, dict) or set(payload) != {"selection","version","accept_partial"} or type(payload["accept_partial"]) is not bool:
        raise ValueError("Provide the selection, snapshot version and coverage decision.")
    with open_snapshot(server.database, server.digest) as db:
        catalog = api_result(db, "/v1/catalog", "", server.digest)
    if payload["version"] != catalog["version"]:
        raise ValueError("The snapshot version has changed.")
    try:
        plan = checked_plan(payload["selection"], catalog)
    except ModelUnavailable as exc:
        raise ValueError("Invalid calculation selection.") from exc
    if plan["action"] == "clarify" or "coverage_question" in plan:
        raise ValueError("Resolve the open question before saving a result.")
    report = calculate(server, catalog, plan)
    if report["action"] == "clarify" or (report["gaps"] and not payload["accept_partial"]):
        raise ValueError("Coverage is incomplete. Confirm the available results or wait for the missing history.")
    report["checks"] = {"database_sha256":server.digest, "rules":catalog["rules"], "method":catalog["method"],
                        "coverage_decision":"partial_accepted" if report["gaps"] else "ready"}
    result_id = publish(server.results_dir, report)
    return {"id":result_id, "source":"/v1/results/"+result_id, "checks":report["checks"]}


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, content, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        request = urlsplit(self.path)
        saved = re.fullmatch(r"/v1/results/([a-f0-9]{64})", request.path)
        if saved:
            try:
                content = read_result(self.server.results_dir, saved[1])
            except FileNotFoundError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            except (ValueError, OSError):
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)
                return
            self.reply(HTTPStatus.OK, content, "application/json")
            return
        if request.path == "/v1/assistant/status":
            self.reply(HTTPStatus.OK, json.dumps(model_config()).encode(), "application/json")
            return
        if request.path in ASSETS:
            name, mime = ASSETS[request.path]
            self.reply(HTTPStatus.OK, (STATIC / name).read_bytes(), mime)
            return
        if request.path not in {"/v1/catalog", "/v1/risk-data", "/v1/research-data"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            with open_snapshot(self.server.database, self.server.digest) as db:
                result = api_result(db, request.path, request.query, self.server.digest)
            status = HTTPStatus.OK
        except ValueError as exc:
            result, status = {"error": str(exc)}, HTTPStatus.BAD_REQUEST
        except sqlite3.DatabaseError:
            result = {"error": "The accepted snapshot is unavailable or has changed."}
            status = HTTPStatus.SERVICE_UNAVAILABLE
        self.reply(status, json.dumps(result, allow_nan=False).encode(), "application/json")

    def do_POST(self):
        if self.path not in ("/v1/assistant","/v1/results"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        host = self.headers.get("Host")
        hosts = {f"{name}:{self.server.server_port}" for name in ("localhost", "127.0.0.1")}
        if host not in hosts or self.headers.get("Origin") not in (None, f"http://{host}"):
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if self.headers.get_content_type() != "application/json":
            self.send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 64_000:
                raise ValueError("Invalid request length.")
            payload = json.loads(self.rfile.read(size))
            result = assistant_result(self.server, payload) if self.path == "/v1/assistant" else save_calculation(self.server, payload)
            status = HTTPStatus.OK
        except ValueError as exc:
            result, status = {"error": str(exc)}, HTTPStatus.BAD_REQUEST
        except ModelUnavailable as exc:
            result, status = {"error": str(exc)}, HTTPStatus.SERVICE_UNAVAILABLE
        except (sqlite3.DatabaseError, OSError):
            result, status = {"error": "The accepted snapshot is unavailable or has changed."}, HTTPStatus.SERVICE_UNAVAILABLE
        self.reply(status, json.dumps(result, allow_nan=False).encode(), "application/json")


def local_server(port, database=DATABASE, results_dir=None):
    digest = verify_snapshot(database)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.database, server.digest = database, digest
    server.results_dir = results_dir or database.parent / "results"
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not (DATABASE.exists() and DATABASE.with_suffix(".manifest.json").exists()):
        parser.exit(1, "No snapshot found in data/. Build one first as described in README.md.\n")
    with local_server(args.port) as server:
        print(f"QuantBridge is running at http://127.0.0.1:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
