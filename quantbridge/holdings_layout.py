"""Validate a changed holdings export (same snapshot) against both consumers."""
import argparse
from contextlib import closing
import csv
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import sqlite3

from .metrics import DATABASE, WINDOWS, open_snapshot, verify_snapshot
from .server import api_result


def compare_layout(raw, unit, database=DATABASE):
    if unit not in ("USD", "cents"):
        raise ValueError("Is reported_value expressed in US dollars or integer cents? Specify USD or cents.")
    digest = verify_snapshot(database)
    with open_snapshot(database, digest) as source, closing(sqlite3.connect(":memory:")) as alternate:
        source.backup(alternate)
        alternate.row_factory = sqlite3.Row
        catalog = api_result(source, "/v1/catalog", "", digest)
        baseline = {(r[0],r[1]):tuple(r[2:]) for r in source.execute(
            "SELECT fund_id,source_row,ticker,sector,market_value_cents FROM fund_holdings")}
        rows = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        if sorted(rows.fieldnames or []) != sorted(["fund","position_id","symbol","industry","reported_value","as_of"]):
            raise ValueError("Expected the agreed holdings columns.")
        seen = set()
        updates = []
        for row in rows:
            key = (row["fund"],int(row["position_id"]))
            if key in seen or key not in baseline or row["as_of"] != catalog["holdings_date"]:
                raise ValueError("Duplicate, unknown or differently dated position.")
            try:
                amount = Decimal(row["reported_value"]) * (100 if unit == "USD" else 1)
                if not amount.is_finite() or amount != amount.to_integral_value():
                    raise ValueError("Position amount must resolve to exact cents.")
                cents = int(amount)
            except (InvalidOperation, TypeError) as exc:
                raise ValueError("Invalid position amount.") from exc
            if (row["symbol"], row["industry"], cents) != baseline[key]:
                raise ValueError("Mapped position differs from the accepted source. Check the mapping and unit.")
            seen.add(key)
            updates.append((row["symbol"],row["industry"],cents,*key))
        if seen != baseline.keys():
            raise ValueError("The changed export did not supply every position.")
        alternate.executemany("UPDATE fund_holdings SET ticker=?, sector=?, market_value_cents=? WHERE fund_id=? AND source_row=?", updates)
        queries = [("/v1/risk-data",f"fund={fund}&window={w}") for fund in catalog["funds"] for w in WINDOWS]
        for symbol, in source.execute("SELECT symbol FROM history_universe WHERE role='selected_equity' UNION SELECT 'JPM'"):
            queries.append(("/v1/research-data", f"symbols={symbol}&windows=20,60"))
        for path, query in queries:
            if api_result(alternate,path,query,digest)["data"] != api_result(source,path,query,digest)["data"]:
                raise ValueError("Consumer results changed for " + query)
        return {"status":"PASS", "holdings":len(seen), "unit":unit, "source_sha256":hashlib.sha256(raw).hexdigest(),
                "snapshot_sha256":digest, "position_values":"all matched to the cent",
                "verified_queries":[path+"?"+query for path,query in queries]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--unit", choices=["USD","cents"])
    args = parser.parse_args()
    try:
        result = compare_layout(args.csv.read_bytes(),args.unit)
    except ValueError as exc:
        parser.exit(2,str(exc)+"\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
