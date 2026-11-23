"""Build the real-data SQLite snapshot from saved, hash-verified sources."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3

from quantbridge.fetch_real import holdings_csv

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def decimal_text(value: str) -> str:
    value = value.replace(",", "")
    if not Decimal(value).is_finite():
        raise ValueError("Non-finite source value")
    return value


def scaled(value: str, multiplier: int) -> int:
    result = Decimal(decimal_text(value)) * multiplier
    if result != result.to_integral_value():
        raise ValueError(f"Source precision exceeds schema: {value}")
    return int(result)


def candle_rows(path):
    result = json.loads(path.read_bytes())["chart"]["result"][0]
    timestamps = result["timestamp"]
    quote = result["indicators"]["quote"][0]
    adjusted = result["indicators"]["adjclose"][0]["adjclose"]
    arrays = [quote[k] for k in ("open", "high", "low", "close", "volume")] + [adjusted]
    if any(len(values) != len(timestamps) for values in arrays):
        raise ValueError("Mismatched daily data arrays")
    rows = {}
    for i, stamp in enumerate(timestamps):
        # U.S. daily candle timestamps here occur during the same UTC and NY date.
        day = datetime.fromtimestamp(stamp, timezone.utc).date().isoformat()
        if day in rows:
            raise ValueError("Duplicate daily observation")
        rows[day] = (stamp, *[None if quote[k][i] is None else str(quote[k][i]) for k in ("open", "high", "low", "close")],
                     None if adjusted[i] is None else str(adjusted[i]), quote["volume"][i])
    return result, rows


def build(source_root: Path, destination: Path) -> dict:
    source_root = source_root.resolve()
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(destination)
    manifest = json.loads((source_root / "source_manifest.json").read_text(encoding="utf-8"))
    if manifest["synthetic"] or manifest["point_in_time_backtest_supported"]:
        raise ValueError("Unexpected source declaration")
    for record in manifest["records"]:
        if sha(source_root / record["file"]) != record["sha256"]:
            raise ValueError(f"Changed source file: {record['file']}")
    source_by_file = {record["file"]: f"SRC{i:03d}" for i, record in enumerate(manifest["records"], 1)}
    _, reference = candle_rows(source_root / "raw/SPY_daily.json")
    end = manifest["holdings_as_of"]
    dates = sorted(day for day in reference if day <= end)[-130:]
    if len(dates) != 130 or dates[-1] != end:
        raise ValueError("Missing reference sessions at holdings date")
    destination.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(destination)
    try:
        connection.executescript((ROOT / "quantbridge/real_schema.sql").read_text(encoding="utf-8"))
        with connection:
            metadata = {"dataset_version": "qb-real-v1", "contract_version": "2.0.0", "synthetic": "false",
                        "holdings_as_of": end, "history_start": dates[0], "history_end": dates[-1],
                        "point_in_time_backtest_supported": "false", "historical_available_at": "unknown",
                        "source_manifest_sha256": sha(source_root / "source_manifest.json")}
            connection.executemany("INSERT INTO dataset_metadata VALUES (?,?)", metadata.items())
            for record in manifest["records"]:
                source = source_by_file[record["file"]]
                connection.execute("INSERT INTO source_files VALUES (?,?,?,?,?,NULL,0)",
                    (source, record["file"], record["url"], record["sha256"], record["retrieved_at_utc"]))
            for fund in manifest["funds"]:
                relative = "raw/" + fund + "_holdings.csv"
                raw = (source_root / relative).read_bytes()
                day, rows = holdings_csv(raw)
                if day != end:
                    raise ValueError("Holdings dates differ")
                name = raw.decode("utf-8-sig").splitlines()[0]
                connection.execute("INSERT INTO funds VALUES (?,?,?,?)", (fund, name, day, source_by_file[relative]))
                for index, row in enumerate(rows, 1):
                    connection.execute("INSERT INTO fund_holdings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (fund, index, row["Ticker"], row["Name"], row["Sector"], row["Asset Class"],
                         scaled(row["Market Value"], 100), scaled(row["Weight (%)"], 100),
                         decimal_text(row["Quantity"]), decimal_text(row["Price"]), row["Currency"], row["Market Currency"], row["Exchange"]))
            connection.executemany("INSERT INTO sessions VALUES (?,?,'SPY')", [(d, i) for i, d in enumerate(dates)])
            for symbol in manifest["history_symbols"] + ["SPY"]:
                relative = "raw/" + symbol + "_daily.json"
                result, rows = candle_rows(source_root / relative)
                source = source_by_file[relative]
                role = "session_reference" if symbol == "SPY" else "selected_equity"
                connection.execute("INSERT INTO history_universe VALUES (?,?,?,?,?)", (symbol, result["meta"].get("longName", symbol), result["meta"]["currency"], role, source))
                for day in dates:
                    if day not in rows:
                        raise ValueError(f"Missing daily date: {symbol} {day}; no filling allowed")
                    row = rows[day]
                    if row[4] is None or row[5] is None or Decimal(row[4]) <= 0 or Decimal(row[5]) <= 0:
                        raise ValueError(f"Missing/nonpositive close: {symbol} {day}")
                    connection.execute("INSERT INTO daily_prices VALUES (?,?,?,?,?,?,?,?,?,?)", (symbol, day, *row, source))
                for kind, events in result.get("events", {}).items():
                    for event_id, event in events.items():
                        event_date = datetime.fromtimestamp(event["date"], timezone.utc).date().isoformat()
                        connection.execute("INSERT INTO corporate_actions VALUES (?,?,?,?,?,?)", (symbol, kind, event_id, event_date, json.dumps(event, sort_keys=True), source))
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Invalid database")
        counts = {r[0]: connection.execute(f'SELECT COUNT(*) FROM "{r[0]}"').fetchone()[0]
                  for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()}
    except Exception:
        connection.close()
        destination.unlink(missing_ok=True)
        raise
    else:
        connection.close()
    return {"dataset_version": "qb-real-v1", "synthetic": False, "table_rows": counts,
            "holdings_as_of": end, "history_start": dates[0], "history_end": dates[-1],
            "database_sha256": sha(destination), "source_manifest_sha256": sha(source_root / "source_manifest.json"),
            "build_source_sha256": {p: sha(ROOT / p) for p in ("quantbridge/ingest_real.py", "quantbridge/real_schema.sql", "contracts/real_stage1.json")}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = args.output.with_suffix(".manifest.json")
    if manifest.exists():
        parser.error("Existing manifest would be overwritten")
    result = build(args.sources, args.output)
    manifest.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
