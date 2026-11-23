"""Read-only risk and research views over the versioned market snapshot."""
from collections import Counter
from contextlib import closing, contextmanager
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics

ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "data/quantbridge_real_stage1.sqlite"
RULES = json.loads((ROOT / "contracts/real_stage1.json").read_text(encoding="utf-8"))
WINDOWS = tuple(RULES["scope"]["windows"])
METHOD = "security-return-volatility-v1"


def verify_snapshot(path=DATABASE):
    manifest = json.loads(path.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != manifest["database_sha256"]:
        raise ValueError("The database differs from its accepted snapshot.")
    rules_digest = hashlib.sha256((ROOT / "contracts/real_stage1.json").read_bytes()).hexdigest()
    if rules_digest != manifest["build_source_sha256"]["contracts/real_stage1.json"]:
        raise ValueError("The metric contract differs from its accepted version.")
    return digest


@contextmanager
def open_snapshot(path=DATABASE, expected_digest=None):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        # Fingerprint the same read transaction that will supply the result.
        if expected_digest and hashlib.sha256(db.serialize()).hexdigest() != expected_digest:
            raise sqlite3.DatabaseError("Snapshot changed since service startup.")
        yield db


def metadata(db):
    return dict(db.execute("SELECT key, value FROM dataset_metadata"))


def security_metrics(db, symbol, as_of, window):
    if window not in WINDOWS:
        raise ValueError("Choose a 20- or 60-session window.")
    row = dict(symbol=symbol, as_of=as_of, window=window, start=None, observations=0,
               required_prices=window + 1, window_return=None, annualized_volatility=None,
               status="BLOCKED", reason="", source_id=None)
    stock = db.execute("SELECT source_id FROM history_universe WHERE symbol=? AND role='selected_equity'",
                       (symbol,)).fetchone()
    if stock is None:
        row.update(status="NOT_COVERED", reason="Outside the ten-stock price universe.")
        return row
    row["source_id"] = stock[0]
    # Anchor the window to the reference calendar before joining prices. A missing
    # observation must leave a gap, never pull an older price into the window.
    samples = db.execute("""
        SELECT s.session_date, p.symbol, p.close_split_dividend_adjusted
        FROM sessions AS s LEFT JOIN daily_prices AS p
          ON p.session_date=s.session_date AND p.symbol=?
        WHERE s.session_date<=? ORDER BY s.sequence DESC LIMIT ?
    """, (symbol, as_of, window + 1)).fetchall()
    prices = []
    invalid = []
    for day, matched_symbol, raw in reversed(samples):
        if raw is None:
            invalid.append((day, "missing price row" if matched_symbol is None else "null adjusted close"))
            continue
        try:
            price = Decimal(raw)
        except InvalidOperation:
            invalid.append((day, "invalid adjusted close"))
            continue
        if price.is_finite() and price > 0:
            prices.append(price)
        else:
            invalid.append((day, "zero adjusted close" if price.is_zero() else "invalid adjusted close"))
    row.update(start=samples[-1][0] if samples else None, observations=len(prices))
    if not samples or samples[0][0] != as_of or len(samples) < window + 1:
        row["reason"] = "Insufficient reference sessions ending on the requested date."
    elif invalid:
        row["reason"] = "; ".join(f"{day}: {problem}" for day, problem in invalid)
    else:
        with localcontext() as ctx:
            ctx.prec = 40
            returns = [b / a - 1 for a, b in zip(prices, prices[1:])]
            vol = statistics.stdev(returns) * Decimal(RULES["returns"]["annualization"]).sqrt()
            row.update(status="READY", window_return=str(prices[-1] / prices[0] - 1),
                       annualized_volatility=str(vol))
    return row


def research_data(db, symbols, windows, as_of):
    if not symbols or not windows:
        raise ValueError("Select at least one security and one window.")
    return [security_metrics(db, symbol, as_of, w) for symbol in symbols for w in windows]


def risk_data(db, fund, as_of, window):
    if window not in WINDOWS:
        raise ValueError("Choose a 20- or 60-session window.")
    source = db.execute("SELECT source_id FROM funds WHERE fund_id=? AND holdings_as_of=?",
                        (fund, as_of)).fetchone()
    if source is None:
        raise ValueError("No holdings snapshot for that fund and date.")
    by_stock, by_sector = Counter(), Counter()
    for ticker, sector, cents in db.execute("""
        SELECT ticker,sector,market_value_cents FROM fund_holdings
        WHERE fund_id=? AND asset_class='Equity' AND currency='USD' AND market_currency='USD'
    """, (fund,)):
        by_stock[ticker] += cents
        by_sector[sector] += cents
    total = sum(by_stock.values())
    if total <= 0:
        raise ValueError("The USD equity market value must be positive.")
    ranked = sorted(by_stock, key=lambda k: (-by_stock[k], k))
    selected = {r[0] for r in db.execute("SELECT symbol FROM history_universe WHERE role='selected_equity'")}
    metrics = {s: security_metrics(db, s, as_of, window) for s in ranked if s in selected}
    ready = [s for s, m in metrics.items() if m["status"] == "READY"]
    securities = []
    for symbol in ranked:
        m = metrics.get(symbol)
        securities.append(dict(symbol=symbol, value_cents=by_stock[symbol],
                               weight=str(Decimal(by_stock[symbol]) / total),
                               status=m["status"] if m else "NOT_COVERED",
                               annualized_volatility=m["annualized_volatility"] if m else None,
                               reason=m["reason"] if m else "Outside the ten-stock price universe."))
    return dict(fund=fund, as_of=as_of, window=window, source_id=source[0], total_cents=total,
                top5_weight=str(Decimal(sum(by_stock[s] for s in ranked[:5])) / total),
                coverage_weight=str(Decimal(sum(by_stock[s] for s in ready)) / total),
                covered_securities=len(ready), total_securities=len(ranked), securities=securities,
                sectors=[dict(sector=k, value_cents=v, weight=str(Decimal(v) / total))
                         for k, v in sorted(by_sector.items(), key=lambda item: (-item[1], item[0]))])
