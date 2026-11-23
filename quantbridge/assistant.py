"""Route natural-language questions to the versioned snapshot queries."""
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .metrics import WINDOWS


class ModelUnavailable(RuntimeError):
    pass


GUIDE = """Understand requests in any supported input language and write your replies in English. Call one registered tool at a time.
For a vague risk request ask a concise, original clarification about the metric and fund.
Offer only supported risk measures: concentration, sector weights or individual stock volatility.
Aggregate fund or portfolio volatility is unavailable. Never offer it as an available choice.
For a concrete request first inspect authorized assets, then choose the appropriate calculation.
Use evidence about missing data, never invent a price, coverage, fund or numeric result.
On a revision preserve the prior fund(s), measures, focus stocks and date unless changed.
A stock explicitly requested within a fund risk view belongs in risk.symbols. Include requested
stocks even if their price history is missing. Return available holdings and show the gap so the
user can decide whether to accept partial results. When inspection finds a requested stock has
no price source, supply coverage_question in English asking whether to use the marked
partial results or wait for data. Never claim to have fetched missing data. Only scope all_volatility requests volatility
for all holdings. A risk summary or named stock uses summary. Standalone stock returns use research.
If the user gives no window, default fund summaries to 20 sessions and stock comparisons to both.
Only 20 and 60 sessions are supported. Use the catalog date by default. Live or historical
as-known requests require clarification. Explain that first-publication timestamps and revision
history are unavailable for historical as-known backtests. Ask to narrow requests outside supported definitions.
Never supply a numerical answer yourself. Only a local calculation can produce a result."""

SYMBOLS = {"type": "array", "items": {"type": "string"}, "maxItems": 30}
PERIODS = {"type": "array", "items": {"type": "integer", "enum": list(WINDOWS)}}
FUNDS = {"type": "array", "items": {"type": "string", "enum": ["IVV", "OEF"]}}
DATE = {"type": "string", "description": "Optional requested ISO date."}
GAP_QUESTION = {"type":"string", "description":"Optional question about a verified gap in requested stocks."}


def tool(name, description, properties, required):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required,
                           "additionalProperties": False}}}


TOOLS = [
    tool("clarify", "Ask the user to resolve ambiguity in English.",
         {"reason": {"type": "string", "enum": ["risk_measure","window","entity","live","unsupported"]},
          "question": {"type": "string"}}, ["reason","question"]),
    tool("inspect_assets", "Look up approved definitions, source fields and coverage for relevant stocks.",
         {"funds": FUNDS, "symbols": SYMBOLS}, ["funds","symbols"]),
    tool("risk", "Compute fund holdings, concentration, sector weights and volatility; include named stocks.",
         {"funds": FUNDS, "windows": PERIODS, "scope": {"type": "string", "enum":
          ["summary","all_volatility","covered_volatility"]}, "symbols": SYMBOLS, "date": DATE, "coverage_question":GAP_QUESTION},
         ["funds","windows","scope"]),
    tool("research", "Compare named securities' returns and volatility.",
         {"symbols": SYMBOLS, "windows": PERIODS, "date": DATE, "coverage_question":GAP_QUESTION}, ["symbols","windows"]),
]

QUESTIONS = {
    "risk_measure": "Do you mean holdings concentration, sector weights, or volatility of individual holdings? Which fund should I use (IVV or OEF)?",
    "window": "The snapshot supports 20 and 60 trading sessions. Which window would you like?",
    "entity": "Which fund (IVV or OEF) or stock symbol would you like to study?",
    "live": "This workbench uses a dated snapshot. Would you like to use the snapshot date shown below?",
    "unsupported": "I can compare fund concentration and sectors, or calculate stock returns and volatility. Which of these would answer your question?",
    "coverage": "Price history covers only part of the holdings. Use the covered securities, or stop here until the missing histories are available?",
}



def model_config():
    enabled = os.getenv("QUANTBRIDGE_AI_PROVIDER", "").lower() == "deepseek"
    model = os.getenv("QUANTBRIDGE_AI_MODEL", "deepseek-flash").strip()
    return {"configured": bool(enabled and model and os.getenv("DEEPSEEK_API_KEY")),
            "model": model if enabled else None, "provider": "DeepSeek" if enabled else None}


def read_turns(payload):
    turns = payload.get("turns") if isinstance(payload, dict) else None
    if not isinstance(turns, list) or not 1 <= len(turns) <= 11 or len(turns) % 2 == 0:
        raise ValueError("Start a new question after six turns.")
    for index, item in enumerate(turns):
        role = "assistant" if index % 2 else "user"
        if (not isinstance(item, dict) or item.keys() != {"role", "content"} or item["role"] != role
                or not isinstance(item["content"], str) or not 1 <= len(item["content"].strip()) <= 1200):
            raise ValueError("Enter a question of up to 1,200 characters.")
    return turns


def model_plan(turns, catalog, inspect=None, record=None):
    config = model_config()
    if not config["configured"]:
        raise ModelUnavailable("A model has not been configured for this workbench.")
    scope = {key: catalog[key] for key in ("funds", "holdings_date", "history_start", "history_end", "windows")}
    messages = [{"role": "system", "content": GUIDE + "\nCatalog: " + json.dumps(scope)}, *turns]
    available = ([t for t in TOOLS if t["function"]["name"] in ("clarify","inspect_assets")]
                 if inspect is not None else [t for t in TOOLS if t["function"]["name"] != "inspect_assets"])
    coverage = {}
    for step in range(3):
        body = {"model": config["model"], "max_tokens": 650, "thinking": {"type": "disabled"},
                "tools": available, "tool_choice": "required", "messages": messages}
        request = Request("https://api.deepseek.com/chat/completions",
                          headers={"Authorization": "Bearer " + os.environ["DEEPSEEK_API_KEY"],
                                   "Content-Type": "application/json"}, data=json.dumps(body).encode())
        try:
            with urlopen(request, timeout=35) as response:
                result = json.load(response)
            choice = result["choices"][0]
            msg = choice["message"]
            if record:
                record({"event":"model", "id":result.get("id"), "model":result.get("model"),
                        "created":result.get("created"), "usage":result.get("usage"),
                        "message":{k: msg.get(k) for k in ("role","content","tool_calls")},
                        "finish_reason":choice.get("finish_reason")})
            calls = msg.get("tool_calls", [])
            if choice["finish_reason"] != "tool_calls" or len(calls) != 1:
                raise ModelUnavailable("The model did not select exactly one tool. Please retry.")
            call = calls[0]
            name = call["function"]["name"]
            args = json.loads(call["function"]["arguments"])
            if not isinstance(args, dict) or "action" in args or name not in [t["function"]["name"] for t in available]:
                raise ModelUnavailable("The model selected an invalid tool.")
        except HTTPError as exc:
            raise ModelUnavailable(f"The model service returned HTTP {exc.code}. Check access and account balance.") from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise ModelUnavailable("The model service could not be reached. Please retry.") from exc
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ModelUnavailable("The model returned unreadable tool arguments. Please retry.") from exc
        if name != "inspect_assets":
            if name == "clarify" and not args.get("question"):
                raise ModelUnavailable("The model did not supply its clarification.")
            question = args.get("question") if name == "clarify" else args.get("coverage_question")
            requested = args.get("symbols", [])
            needs_question = name != "clarify" and valid_symbols(requested, 0) and any(
                s in coverage and coverage[s]["price_source"] is None for s in requested)
            if needs_question and not question:
                evidence = {"validation_error":"The requested stocks include a verified price gap. Supply coverage_question in English."}
            elif isinstance(question, str) and re.search(r"[\u3400-\u9fff]", question):
                evidence = {"validation_error":"Rewrite the question in English and resubmit the selection."}
            else:
                return dict(args, action=name)
            if record:
                record({"event":"validation", "name":name, "result":evidence})
        else:
            evidence = inspect(args)
            coverage = evidence["coverage"]
            available = [t for t in TOOLS if t["function"]["name"] != "inspect_assets"]
            if record:
                record({"event":"tool", "name":name, "arguments":args, "result":evidence})
        messages.extend([{"role":"assistant", "content":msg.get("content"), "tool_calls":calls},
                         {"role":"tool", "tool_call_id":call["id"], "content":json.dumps(evidence)}])
    raise ModelUnavailable("The requested tool sequence could not be completed.")


def valid_symbols(values, minimum=1):
    return (isinstance(values, list) and minimum <= len(values) <= 30 and
            all(isinstance(v, str) and re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,14}", v) for v in values)
            and len(set(values)) == len(values))


def checked_plan(plan, catalog):
    error = ModelUnavailable("The model selected invalid tool parameters. Please rephrase the question.")
    if not isinstance(plan, dict):
        raise error
    action = plan.get("action")
    if action == "clarify":
        if (set(plan) - {"action", "reason", "question"} or not isinstance(plan.get("reason"), str)
                or plan["reason"] not in QUESTIONS or plan["reason"] == "coverage"
                or ("question" in plan and (not isinstance(plan["question"], str)
                    or not 1 <= len(plan["question"].strip()) <= 650))):
            raise error
        return plan
    if action not in ("risk", "research"):
        raise error
    risk = action == "risk"
    entities = "funds" if risk else "symbols"
    fields = {"action", entities, "windows"} | ({"scope"} if risk else set())
    optional = {"date", "coverage_question"} | ({"symbols"} if risk else set())
    if "coverage_question" in plan and (not isinstance(plan["coverage_question"], str) or
                                        not 1 <= len(plan["coverage_question"]) <= 650):
        raise error
    if not fields <= plan.keys() or plan.keys() - fields - optional:
        raise error
    values, windows = plan[entities], plan["windows"]
    if (not valid_symbols(values) or (risk and not valid_symbols(plan.get("symbols", []), 0))
            or not isinstance(windows, list) or not 1 <= len(windows) <= 2
            or any(type(w) is not int or w not in WINDOWS for w in windows) or len(set(windows)) != len(windows)):
        raise error
    if risk and (any(f not in catalog["funds"] for f in values)
                 or plan["scope"] not in ("summary", "all_volatility", "covered_volatility")):
        raise error
    if "date" in plan and (not isinstance(plan["date"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", plan["date"])):
        raise error
    return plan
