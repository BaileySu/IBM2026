"""Download public source snapshots without inventing missing observations."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
import re
from pathlib import Path
import urllib.parse
import urllib.request

FUNDS = {
    "IVV": "https://www.ishares.com/us/products/239726/ishares-core-s-p-500-etf/latest-holdings.csv",
    "OEF": "https://www.ishares.com/us/products/239723/ishares-s-p-100-etf/latest-holdings.csv",
}


HOLDINGS_COLUMNS = {"Ticker", "Name", "Sector", "Asset Class", "Market Value", "Quantity",
                    "Price", "Exchange", "Currency", "Market Currency"}


def holdings_csv(raw: bytes) -> tuple[str, list[dict]]:
    lines = raw.decode("utf-8-sig").splitlines()
    dates = [next(csv.reader([line]))[1] for line in lines if line.startswith("Fund Holdings as of,")]
    if len(dates) != 1:
        raise ValueError("Missing or ambiguous holdings date")
    day = datetime.strptime(dates[0], "%b %d, %Y").date().isoformat()
    # iShares has reordered and renamed columns between exports, so locate the
    # header by name and map the weight column to the name used downstream.
    start = next((i for i, line in enumerate(lines) if line.startswith("Ticker,")), None)
    if start is None:
        raise ValueError("No holdings table in response")
    header = next(csv.reader([lines[start]]))
    weight = next((name for name in ("Weight (%)", "Market Weight") if name in header), None)
    missing = HOLDINGS_COLUMNS - set(header)
    if missing or weight is None:
        raise ValueError(f"Holdings table lacks columns: {sorted(missing) or ['Weight (%)']}")
    rows = []
    for row in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
        # Later exports print "-" for weights that round below 0.01%.
        # The reported weight is stored for reference only; calculations use market value.
        row.setdefault("Weight (%)", "0" if row.get(weight) == "-" else row.get(weight))
        if not row.get("Asset Class") or not row.get("Market Value"):
            continue
        if row["Market Value"] == "-":
            continue
        # Disclosures after the data must not be interpreted as holdings.
        try:
            Decimal(row["Market Value"].replace(",", ""))
        except Exception:
            continue
        rows.append(row)
    if not rows:
        raise ValueError("No holdings in response")
    return day, rows


def fetch(url: str, path: Path, kind: str) -> dict:
    if path.exists():
        raise FileExistsError(path)
    request = urllib.request.Request(url, headers={"User-Agent": "QuantBridge/0.1 educational research", "Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read()
        content_type = response.headers.get("Content-Type")
        final_url = response.geturl()
    if kind == "holdings":
        holdings_csv(data)
    else:
        result = json.loads(data)["chart"]
        if result.get("error") or not result.get("result"):
            raise ValueError("No chart data")
        if not result["result"][0].get("timestamp"):
            raise ValueError("Empty price history")
    path.write_bytes(data)
    return {"file": "raw/" + path.name, "url": url, "resolved_url": final_url,
            "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
            "original_available_at": None, "content_type": content_type,
            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "kind": kind}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args()
    root = args.directory.resolve()
    raw_dir = root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    records = []
    funds = {}
    for fund, url in FUNDS.items():
        path = raw_dir / (fund + "_holdings.csv")
        record = fetch(url, path, "holdings")
        day, rows = holdings_csv(path.read_bytes())
        record.update({"fund": fund, "as_of_date": day, "rows": len(rows)})
        records.append(record)
        funds[fund] = (day, rows)
        print(f"Downloaded {fund}: {day}, {len(rows)} holdings", flush=True)
    dates = {v[0] for v in funds.values()}
    if len(dates) != 1:
        raise ValueError(f"Holdings dates differ: {dates}; do not silently align")
    eligible = {}
    for fund, (_, rows) in funds.items():
        eligible[fund] = {r["Ticker"]: r for r in rows if r["Asset Class"] == "Equity" and r.get("Currency") == "USD" and r.get("Market Currency") == "USD"}
    # Share-class tickers such as "BRK B" use a different symbol at the price
    # source and cannot be queried by the service, so they are left out.
    common = {t for t in set(eligible["IVV"]) & set(eligible["OEF"]) if re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,14}", t)}
    selected = sorted(common, key=lambda ticker: (-Decimal(eligible["IVV"][ticker]["Market Value"].replace(",", "")), ticker))[:10]
    if len(selected) != 10:
        raise ValueError("Fewer than ten common USD equities")
    for ticker in selected + ["SPY"]:
        # These selected tickers are verified against returned vendor symbols.
        url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(ticker, safe="") + "?range=1y&interval=1d&events=div%2Csplits"
        path = raw_dir / (ticker + "_daily.json")
        record = fetch(url, path, "daily_history")
        result = json.loads(path.read_bytes())["chart"]["result"][0]
        if result["meta"]["symbol"] != ticker or result["meta"]["currency"] != "USD":
            raise ValueError("Unexpected symbol or currency")
        record.update({"symbol": ticker, "observations": len(result["timestamp"]), "source_timezone": result["meta"].get("exchangeTimezoneName")})
        records.append(record)
        print(f"Downloaded {ticker}: {len(result['timestamp'])} daily observations", flush=True)
    manifest = {"dataset_version": "qb-real-v1", "synthetic": False, "funds": list(FUNDS),
                "holdings_as_of": next(iter(dates)), "history_symbols": selected,
                "selection_rule": "Ten largest common USD equities with plain ticker symbols, ranked by IVV reported market value; not the full fund history universe.",
                "session_reference": "SPY observed daily dates; not an official exchange holiday calendar",
                "original_publication_timestamps_available": False,
                "point_in_time_backtest_supported": False,
                "records": records}
    (root / "source_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"holdings_as_of": manifest["holdings_as_of"], "history_symbols": selected, "files": len(records)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
