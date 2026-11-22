# QuantBridge

QuantBridge lets an investment researcher ask about fund risk, clarify the measure, and revise the analysis while keeping the original result. A portfolio view and a separate research export use the same holdings, price history, and metric definitions.

- [Download the three-minute demo (MP4)](https://raw.githubusercontent.com/BaileySu/IBM2026/main/QuantBridge_Demo.mp4)
- [Download the source and tests (ZIP)](https://raw.githubusercontent.com/BaileySu/IBM2026/main/QuantBridge_Source.zip)
- [Read the problem and solution](QuantBridge_Problem_and_Solution.txt)

In the demo, the researcher compares IVV and OEF, changes NVDA volatility from 20 to 60 trading sessions, and adds JPM. Both funds hold JPM. The October 21, 2026 snapshot has no JPM price history, so the workbench displays the holdings and asks whether to save a partial result.

## Run locally

Extract the source archive and follow its README to fetch public inputs, build the SQLite database, and start the workbench with Python 3.14. The natural-language assistant uses a locally configured DeepSeek API key. The fund comparison and research export also run directly from the local snapshot.
