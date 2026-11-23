PRAGMA foreign_keys = ON;
CREATE TABLE dataset_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
CREATE TABLE source_files (
  source_id TEXT PRIMARY KEY, relative_path TEXT NOT NULL UNIQUE,
  url TEXT NOT NULL, sha256 TEXT NOT NULL, retrieved_at_utc TEXT NOT NULL,
  original_available_at TEXT CHECK(original_available_at IS NULL),
  synthetic INTEGER NOT NULL CHECK(synthetic=0)
) STRICT;
CREATE TABLE funds (
  fund_id TEXT PRIMARY KEY, name TEXT NOT NULL,
  holdings_as_of TEXT NOT NULL, source_id TEXT NOT NULL REFERENCES source_files(source_id)
) STRICT;
CREATE TABLE fund_holdings (
  fund_id TEXT NOT NULL REFERENCES funds(fund_id), source_row INTEGER NOT NULL,
  ticker TEXT NOT NULL, name TEXT NOT NULL, sector TEXT NOT NULL,
  asset_class TEXT NOT NULL, market_value_cents INTEGER NOT NULL,
  reported_weight_bps INTEGER NOT NULL, quantity TEXT NOT NULL, price TEXT NOT NULL,
  currency TEXT NOT NULL, market_currency TEXT NOT NULL, exchange TEXT NOT NULL,
  PRIMARY KEY (fund_id, source_row)
) STRICT;
CREATE TABLE history_universe (
  symbol TEXT PRIMARY KEY, name TEXT NOT NULL, currency TEXT NOT NULL CHECK(currency='USD'),
  role TEXT NOT NULL CHECK(role IN ('selected_equity', 'session_reference')),
  source_id TEXT NOT NULL REFERENCES source_files(source_id)
) STRICT;
CREATE TABLE sessions (
  session_date TEXT PRIMARY KEY, sequence INTEGER NOT NULL UNIQUE,
  reference_symbol TEXT NOT NULL CHECK(reference_symbol='SPY')
) STRICT;
CREATE TABLE daily_prices (
  symbol TEXT NOT NULL REFERENCES history_universe(symbol),
  session_date TEXT NOT NULL REFERENCES sessions(session_date),
  provider_timestamp INTEGER NOT NULL, open TEXT, high TEXT, low TEXT,
  close_split_adjusted TEXT, close_split_dividend_adjusted TEXT,
  volume INTEGER, source_id TEXT NOT NULL REFERENCES source_files(source_id),
  PRIMARY KEY(symbol, session_date)
) STRICT;
CREATE TABLE corporate_actions (
  symbol TEXT NOT NULL REFERENCES history_universe(symbol), event_type TEXT NOT NULL,
  provider_event_id TEXT NOT NULL, event_date TEXT NOT NULL,
  raw_event_json TEXT NOT NULL, source_id TEXT NOT NULL REFERENCES source_files(source_id),
  PRIMARY KEY(symbol, event_type, provider_event_id)
) STRICT;
