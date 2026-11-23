# QuantBridge

QuantBridge lets an investment researcher ask about fund risk, clarify the measure, and revise the analysis while keeping the original result. A portfolio risk view and a separate research export use the same holdings, price history and metric definitions, served from one SQLite snapshot.

- [Watch the QuantBridge pitch and demo (MP4, 3 min 8 sec)](QuantBridge_Demo.mp4)
- [Problem and solution statement](QuantBridge_Problem_and_Solution.txt)
- [IBM Bob technology statement](QuantBridge_IBM_Bob_Technology_Statement.txt)

In the demo, the researcher compares IVV and OEF, changes NVDA volatility from 20 to 60 trading sessions, and adds JPM. Both funds hold JPM. The October 21, 2026 snapshot has no JPM price history, so the workbench displays the holdings and asks whether to save a partial result.

## Project layout

| Path | Contents |
|---|---|
| `quantbridge/` | Data download and ingestion, metric calculations, HTTP service, assistant and web pages |
| `contracts/real_stage1.json` | Metric definitions shared by both consumers |
| `data/real/2026-10-23/source_manifest.json` | Source URLs, retrieval times and SHA-256 hashes of the October 21, 2026 inputs |
| `evaluation/` | Regression tests, acceptance scripts and the recorded acceptance reference |

The repository does not redistribute the downloaded holdings files, price histories or the SQLite database built from them. The commands below download current public data and build a local snapshot.

## How IBM Bob was used

IBM Bob, in Ask mode, reviewed how the calculation handles a missing closing price. The service already blocked an incomplete window, but a missing price row, an empty price field and a zero price produced the same message. Following that review, the window query checks both the stock identifier and the adjusted closing price, so it reports which of the three causes affected each date. A second Bob review against the database schema confirmed that each fund has a single holdings snapshot, so the holdings logic stayed unchanged. The regression test `test_missing_row_null_and_zero_report_distinct_reasons` in [evaluation/test_data_service.py](evaluation/test_data_service.py) checks all three causes in both windows.

## Data and measures

The October 21, 2026 snapshot used in the demo contains 613 holding records from IVV and OEF and 1,430 daily observations across ten stocks and SPY. Fund weights use the market value of U.S. dollar equity holdings. Stock returns and volatility use split- and dividend-adjusted closing prices. SPY supplies the observed trading calendar. A 20-session calculation requires 21 consecutive prices, and a 60-session calculation requires 61. An incomplete window leaves the affected metric blank.

The [metric contract](contracts/real_stage1.json) specifies the definitions. The [source manifest](data/real/2026-10-23/source_manifest.json) identifies the inputs and records their hashes. A snapshot supports retrospective analysis only, because the sources provide no first-publication timestamps or revision history.

## Run the workbench

Use Python 3.14 with its standard library. Run the commands from the repository root. The service listens only on the loopback interface.

Download a source folder, build the database and start the service:

```sh
python -B -m quantbridge.fetch_real --directory data/real/new
python -B -m quantbridge.ingest_real --sources data/real/new --output data/quantbridge_real_stage1.sqlite
python -B -m quantbridge.server
```

The build refuses to overwrite an existing database. Dates, holdings and results depend on the day the data is downloaded, so they will differ from the October 21, 2026 figures in the demo.

Open http://127.0.0.1:8765/ to compare funds or run a security comparison. The second consumer exports CSV from the running service:

```sh
python -B -m quantbridge.research NVDA JPM --windows 20,60
```

## Natural-language queries

Set the server environment variables `QUANTBRIDGE_AI_PROVIDER=deepseek` and `DEEPSEEK_API_KEY` using your own account. The model defaults to `deepseek-flash`. The assistant asks its questions in English, including when the original request is in another language. Restart the service and open http://127.0.0.1:8765/assistant. Each question can make up to three requests billed to that API account. A query may inspect definitions and requested securities' coverage before running a calculation. Prices, holdings values and calculations stay in the local service.

Try asking for fund risk. Once the assistant clarifies the measure, compare concentration in IVV and OEF with 20-session NVDA volatility. Then change the window to 60 sessions and add JPM. Review the coverage decision before saving the revised result. Saved results are stored by content hash under `data/results/`.

## Tests and acceptance checks

```sh
python -B -m unittest discover -v -s evaluation
```

The tests compare results with the recorded October 21, 2026 reference in `evaluation/results/real_stage1_acceptance_v1.json`. They use offline fixtures for model requests. Snapshot tests run when the October source files are in `data/real/2026-10-23/raw/` and the database built from them is at `data/quantbridge_real_stage1.sqlite`; otherwise they are skipped with a message. The source manifest lists each original file and its SHA-256 hash, so the same files can be checked before use:

```sh
python -B -m quantbridge.ingest_real --sources data/real/2026-10-23 --output data/quantbridge_real_stage1.sqlite
python -B -m evaluation.verify_real_stage1 --sources data/real/2026-10-23 --database data/quantbridge_real_stage1.sqlite --scratch <temporary-directory> --report <new-report-path>
```

To run the billed three-turn acceptance case, configure the model account and run `python -B -m evaluation.verify_live_revision --report <new-report-path>`. The case records actual tool requests, replies and checks and saves the two confirmed results.

The holdings layout check accepts a CSV with `fund,position_id,symbol,industry,reported_value,as_of`. Position IDs follow the source row order starting at one for each fund. The check requests the amount unit, reconciles every position in cents and compares both consumers' results. Run `python -B -m quantbridge.holdings_layout --help` for the command.

## Sources

Holdings come from [iShares IVV](https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf) and [iShares OEF](https://www.ishares.com/us/products/239723/ishares-sp-100-etf). Daily prices come from Yahoo Finance, which [describes its adjusted close](https://in.help.yahoo.com/kb/adjusted-close-sln28256.html).
