"""Export research rows from a running QuantBridge service to standard output."""
import argparse
import csv
import json
import sys
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="+", help="One or more stock symbols")
    parser.add_argument("--windows", default="20,60", help="Comma-separated session windows")
    parser.add_argument("--date", help="Last reference session, ISO date")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    params = {"symbols": ",".join(args.symbols), "windows": args.windows}
    if args.date:
        params["date"] = args.date
    url = f"http://127.0.0.1:{args.port}/v1/research-data?" + urlencode(params)
    try:
        with urlopen(url, timeout=10) as response:
            payload = json.load(response)
    except URLError as exc:
        parser.exit(1, f"Cannot read research data at {url}: {exc}\n")
    rows = [dict(row, version=payload["version"]) for row in payload["data"]]
    out = csv.DictWriter(sys.stdout, fieldnames=list(rows[0]), lineterminator="\n")
    out.writeheader()
    out.writerows(rows)


if __name__ == "__main__":
    main()
