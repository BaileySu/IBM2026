"""Service acceptance against the independently archived stage-one results."""
from contextlib import closing
from decimal import Decimal
import json
import sqlite3
import unittest

from quantbridge.metrics import DATABASE, ROOT, open_snapshot, research_data, risk_data, security_metrics, verify_snapshot
from quantbridge.server import api_result
from evaluation import HAS_SNAPSHOT, SKIP_REASON

END = "2026-10-21"


@unittest.skipUnless(HAS_SNAPSHOT, SKIP_REASON)
class SnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.golden = json.loads((ROOT / "evaluation/results/real_stage1_acceptance_v1.json").read_bytes())
        cls.digest = verify_snapshot()

    def setUp(self):
        self.db = self.enterContext(open_snapshot())

    def close_enough(self, actual, expected):
        self.assertLessEqual(abs(Decimal(actual) - Decimal(expected)), Decimal("1e-12"))

    def test_portfolio_matches_independent_reference(self):
        for fund, ref in self.golden["portfolio_results"].items():
            for w in (20, 60):
                with self.subTest(fund=fund, window=w):
                    result = risk_data(self.db, fund, END, w)
                    self.assertEqual(Decimal(result["total_cents"]) / 100, Decimal(ref["equity_market_value_usd"]))
                    self.close_enough(result["top5_weight"], ref["top5_equity_weight"])
                    self.close_enough(result["coverage_weight"], ref["selected_history_universe_equity_weight"])
                    self.assertEqual([s["symbol"] for s in result["securities"][:5]], ref["top5_symbols"])
                    self.assertEqual(result["covered_securities"], 10)
                    self.assertEqual({s["sector"] for s in result["sectors"]}, set(ref["sector_equity_weights"]))
                    for sector in result["sectors"]:
                        self.close_enough(sector["weight"], ref["sector_equity_weights"][sector["sector"]])

    def test_all_security_windows_and_cross_consumer_reuse(self):
        ivv = {w: {s["symbol"]: s for s in risk_data(self.db,"IVV",END,w)["securities"]} for w in (20,60)}
        for ref in self.golden["security_results"]:
            symbol, w = ref["symbol"], ref["window"]
            with self.subTest(symbol=symbol, window=w):
                row = research_data(self.db, [symbol], [w], END)[0]
                self.assertEqual(row["status"], "READY")
                self.assertEqual(row["observations"], w+1)
                self.close_enough(row["window_return"], ref["window_return"])
                self.close_enough(row["annualized_volatility"], ref["annualized_security_volatility"])
                self.assertEqual(row["annualized_volatility"], ivv[w][symbol]["annualized_volatility"])

    def test_held_but_outside_history_universe(self):
        for fund in ("IVV","OEF"):
            jpm = next(s for s in risk_data(self.db,fund,END,20)["securities"] if s["symbol"] == "JPM")
            self.assertGreater(jpm["value_cents"], 0)
            self.assertEqual(jpm["status"], "NOT_COVERED")
            self.assertIsNone(jpm["annualized_volatility"])
        self.assertEqual(security_metrics(self.db, "SPY", END, 20)["status"], "NOT_COVERED")

    def test_missing_interior_price_only_blocks_affected_windows(self):
        day = self.db.execute("SELECT session_date FROM sessions ORDER BY sequence DESC LIMIT 1 OFFSET 35").fetchone()[0]
        with closing(sqlite3.connect(":memory:")) as copy:
            self.db.backup(copy)
            copy.execute("DELETE FROM daily_prices WHERE symbol=? AND session_date=?", ("NVDA", day))
            short, long = research_data(copy, ["NVDA"], [20,60], END)
            self.assertEqual(short["status"], "READY")
            self.assertEqual(long["status"], "BLOCKED")
            self.assertIn(day, long["reason"])
            self.assertEqual(long["observations"], 60)
            self.assertIsNone(long["window_return"])
            self.assertIsNone(long["annualized_volatility"])
            self.assertEqual(risk_data(copy,"IVV",END,60)["covered_securities"], 9)

    def test_missing_row_null_and_zero_report_distinct_reasons(self):
        with closing(sqlite3.connect(":memory:")) as copy:
            self.db.backup(copy)
            key = ("NVDA","2026-10-07")
            for change, expected in [
                ("UPDATE daily_prices SET close_split_dividend_adjusted='0'", "zero adjusted close"),
                ("UPDATE daily_prices SET close_split_dividend_adjusted=NULL", "null adjusted close"),
                ("DELETE FROM daily_prices", "missing price row"),
            ]:
                copy.execute(change + " WHERE symbol=? AND session_date=?", key)
                for window in (20,60):
                    row = security_metrics(copy,"NVDA",END,window)
                    self.assertEqual(row["reason"],key[1] + ": " + expected)
                    self.assertEqual(row["observations"],window)
                    self.assertIsNone(row["annualized_volatility"])
                    self.assertIsNone(row["window_return"])

    def test_snapshot_date_window_and_permissions(self):
        for end in ("2026-10-18","2026-04-17","2026-11-22"):
            self.assertEqual(security_metrics(self.db,"NVDA",end,20)["status"], "BLOCKED")
        with self.assertRaises(ValueError):
            risk_data(self.db,"IVV","2026-10-20",20)
        with self.assertRaises(ValueError):
            risk_data(self.db,"IVV",END,21)
        with self.assertRaises(sqlite3.OperationalError):
            self.db.execute("UPDATE sessions SET sequence=sequence WHERE 0")

    def test_versioned_replay_and_invalid_inputs(self):
        path, query = "/v1/research-data", "symbols=NVDA,JPM&windows=20,60"
        first = api_result(self.db, path, query, self.digest)
        replay = api_result(self.db, path, query + "&version=" + first["version"], self.digest)
        self.assertEqual(first, replay)
        self.assertEqual(first["database_sha256"], self.golden["database_sha256"])
        for invalid in ("symbols=NVDA&symbols=JPM","version=old","symbols=,","windows=21","as_known=true"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                api_result(self.db, path, invalid, self.digest)

    def test_changed_snapshot_cannot_serve_an_earlier_version(self):
        with self.assertRaises(sqlite3.DatabaseError), open_snapshot(DATABASE, "different"):
            self.fail("The changed snapshot was accepted.")


if __name__ == "__main__":
    unittest.main()
