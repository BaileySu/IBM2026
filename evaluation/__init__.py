"""Independent acceptance utilities. Keep outside future Agent tool access."""
from quantbridge.metrics import DATABASE, ROOT

RAW = ROOT / "data/real/2026-10-23/raw"
HAS_SNAPSHOT = DATABASE.exists() and DATABASE.with_suffix(".manifest.json").exists()
HAS_SOURCES = all((RAW / f"{fund}_holdings.csv").exists() for fund in ("IVV", "OEF"))
SKIP_REASON = "Requires the October 21, 2026 source files and SQLite snapshot under data/. See README.md."
