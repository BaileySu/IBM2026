"""Acceptance checks against original public files, not generated market data."""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from datetime import datetime, timezone
from decimal import Decimal, localcontext, ROUND_HALF_UP
import hashlib
import io
import json
import math
from pathlib import Path
import sqlite3
import statistics
import uuid

ROOT = Path(__file__).resolve().parents[1]
D = Decimal


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def original_holdings(path):
    # Deliberately independent of the ingestion parser.
    records = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
    header_index = next(i for i, row in enumerate(records) if row and row[0] == "Ticker")
    header = records[header_index]
    return [dict(zip(header, row)) for row in records[header_index + 1:]
            if len(row) == len(header) and row[3] in ("Equity", "Money Market", "Cash", "Cash Collateral and Margins", "Futures")]


def number(value):
    return D(str(value).replace(",", ""))


def window(connection, symbol, end, count):
    if count not in (20, 60):
        raise ValueError("Unsupported window")
    dates = [r[0] for r in connection.execute("SELECT session_date FROM sessions WHERE session_date<=? ORDER BY sequence DESC LIMIT ?", (end, count + 1))]
    if len(dates) != count + 1 or dates[0] != end:
        raise ValueError("Insufficient exact-date history")
    prices = {r[0]: r[1] for r in connection.execute("SELECT session_date, close_split_dividend_adjusted FROM daily_prices WHERE symbol=?", (symbol,))}
    values = []
    for day in reversed(dates):
        if day not in prices or prices[day] is None or number(prices[day]) <= 0:
            raise ValueError("Missing or invalid price; no fill")
        values.append(number(prices[day]))
    return values


def calculations(prices):
    with localcontext() as context:
        context.prec = 40
        returns = [b / a - 1 for a, b in zip(prices, prices[1:])]
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        return {"window_return": str(prices[-1] / prices[0] - 1),
                "sample_variance": str(variance), "annualized_security_volatility": str((variance * 252).sqrt())}


def verify(source_root, database, scratch):
    checks = []

    def check(name, condition, detail=None):
        checks.append({"name": name, "status": "PASS" if condition else "FAIL", "detail": detail})
        if not condition:
            raise AssertionError(name + ": " + str(detail))

    source_manifest = json.loads((source_root / "source_manifest.json").read_text(encoding="utf-8"))
    build_manifest = json.loads(database.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    check("real_source_declaration", source_manifest["synthetic"] is False)
    check("source_file_hashes", all(sha(source_root / r["file"]) == r["sha256"] for r in source_manifest["records"]), len(source_manifest["records"]))
    check("database_hash", sha(database) == build_manifest["database_sha256"])
    check("source_manifest_hash", sha(source_root / "source_manifest.json") == build_manifest["source_manifest_sha256"])
    check("builder_contract_hashes", all(sha(ROOT / p) == value for p, value in build_manifest["build_source_sha256"].items()))
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    portfolios = {}
    metrics = []
    rounding = []
    cross_source = []
    try:
        check("sqlite_integrity", connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok")
        check("foreign_keys", not connection.execute("PRAGMA foreign_key_check").fetchall())
        check("no_synthetic_or_invented_publication_time", connection.execute("SELECT COUNT(*) FROM source_files WHERE synthetic<>0 OR original_available_at IS NOT NULL").fetchone()[0] == 0)
        try:
            connection.execute("DELETE FROM sessions")
        except sqlite3.OperationalError:
            check("read_only_connection_blocks_write", True)
        else:
            check("read_only_connection_blocks_write", False)
        end = source_manifest["holdings_as_of"]
        check("fund_dates_agree", {r[0] for r in connection.execute("SELECT holdings_as_of FROM funds")} == {end})
        symbols = source_manifest["history_symbols"]
        selected_source = {}
        for fund in source_manifest["funds"]:
            raw = original_holdings(source_root / "raw" / (fund + "_holdings.csv"))
            rows = connection.execute("SELECT * FROM fund_holdings WHERE fund_id=? ORDER BY source_row", (fund,)).fetchall()
            check(f"{fund}_all_source_rows_preserved", len(rows) == len(raw), len(raw))
            for original, row in zip(raw, rows):
                assert original["Ticker"] == row["ticker"] and original["Name"] == row["name"]
                assert original["Sector"] == row["sector"] and original["Asset Class"] == row["asset_class"]
                assert number(original["Market Value"]) == D(row["market_value_cents"]) / 100
                assert number(original["Weight (%)"]) == D(row["reported_weight_bps"]) / 100
                assert number(original["Quantity"]) == number(row["quantity"])
                assert number(original["Price"]) == number(row["price"])
                assert original["Currency"] == row["currency"] and original["Market Currency"] == row["market_currency"]
                assert original["Exchange"] == row["exchange"]
            check(f"{fund}_fields_and_precision_match_source", True)
            equities = [r for r in rows if r["asset_class"] == "Equity" and r["currency"] == "USD" and r["market_currency"] == "USD"]
            raw_equities = [r for r in raw if r["Asset Class"] == "Equity" and r["Currency"] == "USD" and r["Market Currency"] == "USD"]
            exact_total = D(sum(r["market_value_cents"] for r in equities)) / 100
            independent_total = sum(number(r["Market Value"]) for r in raw_equities)
            check(f"{fund}_valuation_independent_source_total", exact_total == independent_total and exact_total > 0, str(exact_total))
            by_symbol = defaultdict(Decimal)
            by_sector = defaultdict(Decimal)
            for row in equities:
                value = D(row["market_value_cents"]) / 100
                by_symbol[row["ticker"]] += value
                by_sector[row["sector"]] += value
                residual = number(row["quantity"]) * number(row["price"]) - value
                if abs(residual) > D("0.01"):
                    rounding.append({"fund": fund, "ticker": row["ticker"], "difference_usd": str(residual), "within_display_price_rounding": abs(residual) <= abs(number(row["quantity"])) * D("0.005") + D("0.005")})
                if row["ticker"] in symbols:
                    selected_source[fund, row["ticker"]] = row
            check(f"{fund}_selected_ten_present", set(symbols).issubset(by_symbol))
            top5 = sorted(by_symbol.items(), key=lambda item: (-item[1], item[0]))[:5]
            check(f"{fund}_sector_values_reconcile", sum(by_sector.values()) == exact_total)
            check(f"{fund}_top5_independent_source_order", [r[0] for r in top5] == [r["Ticker"] for r in sorted(raw_equities, key=lambda row: -number(row["Market Value"]))[:5]])
            selected_value = sum(by_symbol[s] for s in symbols)
            portfolios[fund] = {"snapshot_rows": len(rows), "equity_rows": len(equities), "excluded_non_equity_rows": len(rows) - len(equities),
                "equity_market_value_usd": str(exact_total), "top5_symbols": [r[0] for r in top5],
                "top5_equity_weight": str(sum(v for _, v in top5) / exact_total),
                "sector_equity_weights": {s: str(v / exact_total) for s, v in by_sector.items()},
                "selected_history_universe_equity_weight": str(selected_value / exact_total)}
        check("no_unexplained_valuation_residuals", all(r["within_display_price_rounding"] for r in rounding), rounding)
        days = [r[0] for r in connection.execute("SELECT session_date FROM sessions ORDER BY sequence")]
        check("130_reference_sessions_end_on_snapshot", len(days) == 130 and days[-1] == end and len(set(days)) == 130)
        for symbol in symbols + ["SPY"]:
            raw = json.loads((source_root / "raw" / (symbol + "_daily.json")).read_bytes())["chart"]["result"][0]
            indices = {datetime.fromtimestamp(t, timezone.utc).date().isoformat(): i for i, t in enumerate(raw["timestamp"])}
            rows = connection.execute("SELECT * FROM daily_prices WHERE symbol=? ORDER BY session_date", (symbol,)).fetchall()
            check(f"{symbol}_complete_aligned_history", [r["session_date"] for r in rows] == days)
            for row in rows:
                index = indices[row["session_date"]]
                assert number(row["close_split_adjusted"]) == number(raw["indicators"]["quote"][0]["close"][index])
                assert number(row["close_split_dividend_adjusted"]) == number(raw["indicators"]["adjclose"][0]["adjclose"][index])
                assert row["provider_timestamp"] == raw["timestamp"][index]
                for field in ("open", "high", "low"):
                    original_value = raw["indicators"]["quote"][0][field][index]
                    assert (row[field] is None and original_value is None) or number(row[field]) == number(original_value)
                assert row["volume"] == raw["indicators"]["quote"][0]["volume"][index]
            check(f"{symbol}_history_matches_original_response", True)
            if symbol == "SPY":
                continue
            for n in (20, 60):
                values = window(connection, symbol, end, n)
                result = calculations(values)
                original_values = [float(raw["indicators"]["adjclose"][0]["adjclose"][indices[day]]) for day in days[-n-1:]]
                daily = [b / a - 1 for a, b in zip(original_values, original_values[1:])]
                expected_variance = statistics.variance(daily)
                expected_vol = statistics.stdev(daily) * math.sqrt(252)
                assert abs(float(result["sample_variance"]) - expected_variance) <= 1e-12
                assert abs(float(result["annualized_security_volatility"]) - expected_vol) <= 1e-12
                assert abs(float(result["window_return"]) - (original_values[-1] / original_values[0] - 1)) <= 1e-12
                metrics.append({"symbol": symbol, "window": n, **result})
            check(f"{symbol}_20_and_60_day_independent_calculations", True)
            vendor_close = number(rows[-1]["close_split_adjusted"])
            for fund in source_manifest["funds"]:
                holding_price = number(selected_source[fund, symbol]["price"])
                cross_source.append({"fund": fund, "symbol": symbol, "holdings_price": str(holding_price), "history_close": str(vendor_close), "difference_usd": str(vendor_close - holding_price)})
        check("selected_snapshot_price_crosscheck", all(abs(number(r["difference_usd"])) <= D("0.02") for r in cross_source), "20 comparisons, absolute difference <= USD 0.02")
        # Negative checks change only an in-memory copy of genuine observations.
        memory = sqlite3.connect(":memory:")
        try:
            connection.backup(memory)
            memory.execute("DELETE FROM daily_prices WHERE symbol=? AND session_date=?", (symbols[0], days[-5]))
            try:
                window(memory, symbols[0], end, 20)
            except ValueError:
                check("missing_interior_price_blocks_no_fill", True)
            else:
                check("missing_interior_price_blocks_no_fill", False)
            for n in (20, 60):
                try:
                    window(memory, symbols[1], days[n-1], n)
                except ValueError:
                    check(f"window_{n}_rejects_only_{n}_prices", True)
                else:
                    check(f"window_{n}_rejects_only_{n}_prices", False)
                check(f"window_{n}_accepts_{n+1}_prices", len(window(memory, symbols[1], days[n], n)) == n + 1)
            for label, args in (("unknown_symbol", ("UNKNOWN", end, 20)), ("unsupported_window", (symbols[1], end, 10))):
                try:
                    window(memory, *args)
                except ValueError:
                    check(label + "_blocks", True)
                else:
                    check(label + "_blocks", False)
        finally:
            memory.close()
    finally:
        connection.close()
    from quantbridge.ingest_real import build
    repeated = scratch.resolve() / ("qb-real-repeat-" + uuid.uuid4().hex + ".sqlite")
    if repeated.parent != scratch.resolve():
        raise ValueError("Unexpected temporary output path")
    try:
        build(source_root, repeated)
        check("offline_rebuild_identical_database", sha(repeated) == sha(database))
    finally:
        repeated.unlink(missing_ok=True)
    return {"status": "PASS", "checks_passed": len(checks), "checks": checks,
            "database_sha256": sha(database), "as_of_date": end, "history_start": days[0], "history_end": days[-1],
            "portfolio_results": portfolios, "security_results": metrics,
            "valuation_rounding_residuals": rounding, "cross_source_price_comparison": cross_source,
            "point_in_time_backtest_supported": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--scratch", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    if args.report.exists():
        parser.error("Existing report would be overwritten")
    with localcontext() as context:
        context.prec = 40
        result = verify(args.sources, args.database, args.scratch)
    result["executed_at_utc"] = datetime.now(timezone.utc).isoformat()
    result["verification_code_sha256"] = sha(Path(__file__))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("status", "checks_passed", "as_of_date", "history_start", "history_end", "portfolio_results")}, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
