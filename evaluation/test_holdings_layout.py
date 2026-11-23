import csv
from decimal import Decimal
import io
import unittest

from quantbridge.fetch_real import holdings_csv
from quantbridge.holdings_layout import compare_layout
from quantbridge.metrics import ROOT
from evaluation import HAS_SNAPSHOT, HAS_SOURCES, SKIP_REASON


def changed_export():
    stream = io.StringIO()
    out = csv.writer(stream)
    out.writerow(["fund","position_id","symbol","industry","reported_value","as_of"])
    for fund in ("IVV","OEF"):
        path = ROOT / "data/real/2026-10-23/raw" / (fund + "_holdings.csv")
        day, rows = holdings_csv(path.read_bytes())
        for index, row in enumerate(rows, 1):
            amount = Decimal(row["Market Value"].replace(",","")) * 100
            out.writerow([fund,index,row["Ticker"],row["Sector"],int(amount),day])
    return stream.getvalue().encode()


@unittest.skipUnless(HAS_SNAPSHOT and HAS_SOURCES, SKIP_REASON)
class LayoutTests(unittest.TestCase):
    def test_confirm_units_then_match_every_position_and_both_consumers(self):
        raw = changed_export()
        with self.assertRaisesRegex(ValueError,"dollars or integer cents"):
            compare_layout(raw,None)
        with self.assertRaisesRegex(ValueError,"differs"):
            compare_layout(raw,"USD")
        result = compare_layout(raw,"cents")
        self.assertEqual(result["holdings"],613)
        self.assertEqual(len(result["verified_queries"]),15)

    def test_reject_incomplete_projection(self):
        raw = changed_export()
        with self.assertRaisesRegex(ValueError,"every position"):
            compare_layout(b"\n".join(raw.splitlines()[:-1]),"cents")
