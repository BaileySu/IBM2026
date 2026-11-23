"""Run the billed, three-turn risk-revision acceptance case against the local service."""
import argparse
import json
import sys
import traceback
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from quantbridge.metrics import ROOT, verify_snapshot
from quantbridge.server import local_server

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", dest="output", type=Path, required=True, help="New JSON evidence file.")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        output.write("{}\n")
    record = {"started_utc":datetime.now(timezone.utc).isoformat(), "driver":"local HTTP acceptance with scripted test-user inputs",
              "events":[], "turns":[], "checks":[], "status":"IN_PROGRESS"}
    def flush():
        args.output.write_text(json.dumps(record,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    def event(e):
        record["events"].append(dict(turn=turn, **e))
        flush()
    def check(label, cond):
        record["checks"].append({"check":label,"passed":bool(cond)})
        flush()
        if not cond:
            raise AssertionError(label)
    def request(path, body=None):
        data=None if body is None else json.dumps(body,ensure_ascii=False).encode()
        req=Request(base+path,data=data,headers={"Content-Type":"application/json"})
        with urlopen(req,timeout=110) as response:
            return json.load(response)
    def close(a,b):
        return abs(Decimal(a)-Decimal(b))<=Decimal("1e-12")
    def save(reply, partial):
        return request("/v1/results",dict(selection=reply["selection"],version=reply["version"],accept_partial=partial))
    def inspect_reply(reply, window, symbols):
        plan=reply["selection"]
        check(f"{window}d fund view, both funds and expected window",reply["action"]=="risk" and set(plan["funds"])=={"IVV","OEF"} and plan["windows"]==[window])
        check(f"{window}d requested stock focus retained",set(plan.get("symbols",[]))==symbols)
        for result in reply["results"]:
            replay=request(result["source"])
            check(f"{window}d versioned replay",replay["version"]==reply["version"])
            data=result["data"]
            if result["kind"]=="risk":
                fund=data["fund"]
                ref=golden["portfolio_results"][fund]
                check(f"{window}d {fund} concentration, coverage and 5 holdings",close(data["top5_weight"],ref["top5_equity_weight"]) and close(data["coverage_weight"],ref["selected_history_universe_equity_weight"]) and [r["symbol"] for r in data["securities"]]==ref["top5_symbols"])
                if "JPM" in symbols:
                    j=next(r for r in data["focus_holdings"] if r["symbol"]=="JPM")
                    check(f"{fund} JPM held with missing volatility",j["value_cents"]>0 and j["annualized_volatility"] is None and j["status"]=="NOT_COVERED")
            else:
                check(f"{window}d security table equals independent HTTP consumer",data==replay["data"])
                nv=next(r for r in data if r["symbol"]=="NVDA")
                ref=next(r for r in golden["security_results"] if r["symbol"]=="NVDA" and r["window"]==window)
                check(f"{window}d NVDA reference and observations",nv["observations"]==window+1 and close(nv["window_return"],ref["window_return"]) and close(nv["annualized_volatility"],ref["annualized_security_volatility"]))
                if "JPM" in symbols:
                    j=next(r for r in data if r["symbol"]=="JPM")
                    check("JPM missing prices preserved",j["observations"]==0 and j["required_prices"]==61 and j["window_return"] is None and j["annualized_volatility"] is None)
    golden=json.loads((ROOT/"evaluation/results/real_stage1_acceptance_v1.json").read_bytes())
    server=local_server(0)
    server.record_event=event
    base=f"http://127.0.0.1:{server.server_port}"
    worker=Thread(target=server.serve_forever,daemon=True)
    worker.start()
    history=[]
    turn=0
    try:
        record["snapshot_sha256_before"]=verify_snapshot()
        record["service"]=request("/v1/assistant/status")
        for turn, text in enumerate(["I'd like to look at risk.",
                "Compare IVV and OEF top-five concentration and sector weights. Focus on NVDA's 20-session stock volatility.",
                "Change it to 60 sessions and include JPM."], 1):
            history.append({"role":"user","content":text})
            entry={"input":text}
            record["turns"].append(entry)
            flush()
            reply=request("/v1/assistant",{"turns":history})
            entry["response"]=reply
            flush()
            if turn==1:
                check("model asks in its own words before running a calculation",reply["action"]=="clarify" and reply["message"]==reply["selection"].get("question") and "results" not in reply)
                context=reply["message"]
            else:
                window=20 if turn==2 else 60
                inspect_reply(reply,window,{"NVDA"} if window==20 else {"NVDA","JPM"})
                context=json.dumps({"selection":reply["selection"],"missing":list(dict.fromkeys(g["symbol"] for g in reply["gaps"]))},ensure_ascii=False)
                if turn==2:
                    first_saved=save(reply,False)
                    before=json.dumps(request(first_saved["source"]),sort_keys=True)
                    entry["confirmed_result"]=first_saved
                else:
                    check("agent inspected coverage before calculation",any(e.get("name")=="inspect_assets" and e["turn"]==3 and e["result"]["coverage"].get("JPM",{}).get("price_source","missing") is None for e in record["events"]))
                    check("actual question about the JPM gap",bool(reply.get("coverage_question")) and "JPM" in reply["coverage_question"])
                    try:
                        save(reply,False)
                    except HTTPError as exc:
                        check("incomplete publication rejected before confirmation",exc.code==400)
                    else:
                        check("incomplete publication rejected before confirmation",False)
                    saved=save(reply,True)
                    entry["test_user_decision"]="Accept marked partial results"
                    entry["confirmed_result"]=saved
                    check("separate 60d partial receipt",saved["id"]!=first_saved["id"] and request(saved["source"])["checks"]["coverage_decision"]=="partial_accepted")
                    check("original 20d receipt unchanged",before==json.dumps(request(first_saved["source"]),sort_keys=True))
            history.append({"role":"assistant","content":context})
        check("source snapshot unchanged",record["snapshot_sha256_before"]==verify_snapshot())
        record["status"]="PASS"
    except Exception as exc:
        record["status"]="FAIL"
        record["failure"]=str(exc)
        traceback.print_exc()
    finally:
        server.shutdown()
        server.server_close()
        record["finished_utc"]=datetime.now(timezone.utc).isoformat()
        flush()
    print(json.dumps({"status":record["status"],"checks":len(record["checks"]),"turns":[(t["input"],t.get("response",{}).get("action"),t.get("response",{}).get("message"),t.get("response",{}).get("coverage_question")) for t in record["turns"]]},ensure_ascii=False))
    return 0 if record["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())

