"""Exercise the assistant boundary without using a model account."""
from io import BytesIO
import json
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from quantbridge.assistant import ModelUnavailable, checked_plan, model_plan, read_turns
from quantbridge.metrics import DATABASE, open_snapshot, verify_snapshot
from quantbridge.server import api_result, assistant_result, save_calculation
from quantbridge.publication import read_result
from evaluation import HAS_SNAPSHOT, SKIP_REASON


def question(text):
    return {"role": "user", "content": text}


@unittest.skipUnless(HAS_SNAPSHOT, SKIP_REASON)
class AssistantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = SimpleNamespace(database=DATABASE, digest=verify_snapshot())

    def route(self, turns, plan):
        return assistant_result(self.server, {"turns": turns}, lambda conversation, catalog: plan)

    def test_research_replays_verbatim_and_keeps_missing_metrics_empty(self):
        plan = dict(action="research", symbols=["NVDA", "JPM"], windows=[20, 60])
        reply = self.route([question("NVDA and JPM, 20 and 60 days")], plan)
        result, = reply["results"]
        link = urlsplit(result["source"])
        with open_snapshot() as db:
            saved = api_result(db, link.path, link.query, self.server.digest)
        self.assertEqual(result["data"], saved["data"])
        self.assertEqual(reply["version"], saved["version"])
        for row in result["data"]:
            if row["symbol"] == "JPM":
                self.assertEqual(row["status"], "NOT_COVERED")
                self.assertIsNone(row["annualized_volatility"])
            else:
                self.assertEqual(row["status"], "READY")

    def test_ambiguous_question_then_clarification(self):
        turns = [question("I want to see risk.")]
        first = self.route(turns, {"action": "clarify", "reason": "risk_measure"})
        self.assertIn("concentration", first["message"])
        self.assertNotIn("results", first)
        turns.extend([{"role":"assistant", "content":first["message"]}, question("Compare concentration across both funds.")])
        reply = self.route(turns, dict(action="risk", funds=["IVV", "OEF"], windows=[20], scope="summary"))
        self.assertEqual([r["data"]["fund"] for r in reply["results"]], ["IVV", "OEF"])

    def test_all_holdings_require_coverage_decision(self):
        turns = [question("Show volatility for every IVV holding.")]
        plan = dict(action="risk", funds=["IVV"], windows=[20], scope="all_volatility")
        reply = self.route(turns, plan)
        self.assertEqual(reply["reason"], "coverage")
        self.assertLess(reply["coverage"][0]["covered_securities"], reply["coverage"][0]["total_securities"])
        turns.extend([{"role":"assistant", "content":reply["message"]}, question("Use covered stocks.")])
        result = self.route(turns, dict(plan, scope="covered_volatility"))
        self.assertEqual(len(result["results"][0]["data"]["covered_holdings"]), 10)

    def test_invalid_selections_rejected_before_execution(self):
        catalog = {"funds":["IVV", "OEF"]}
        for plan in ({"action":"sql"}, {"action":"clarify","reason":[]},
                     dict(action="risk", funds=["OTHER"], windows=[20], scope="summary"),
                     dict(action="research", symbols=["NVDA"], windows=[21]),
                     dict(action="research", symbols=["NVDA"], windows=[20], answer="2.5%"),
                     dict(action="research", symbols=["NVDA"], windows=[20], date="now")):
            with self.subTest(plan=plan), self.assertRaises(ModelUnavailable):
                checked_plan(plan, catalog)

    def test_named_holdings_and_explicit_partial_publication(self):
        selection = dict(action="risk", funds=["OEF","IVV"], windows=[60], symbols=["AAPL","JPM"], scope="summary")
        result = self.route([question("Include AAPL and JPM")], selection)
        self.assertEqual([row["symbol"] for row in result["gaps"]], ["JPM"])
        for fund in result["results"][:2]:
            jpm = next(h for h in fund["data"]["focus_holdings"] if h["symbol"] == "JPM")
            self.assertGreater(jpm["value_cents"], 0)
            self.assertIsNone(jpm["annualized_volatility"])
        with TemporaryDirectory() as scratch:
            server = SimpleNamespace(**vars(self.server), results_dir=scratch)
            request = dict(selection=selection, version=result["version"], accept_partial=False)
            with self.assertRaises(ValueError):
                save_calculation(server, request)
            request["accept_partial"] = True
            saved = save_calculation(server, request)
            self.assertEqual(save_calculation(server, request), saved)
            self.assertEqual(json.loads(read_result(scratch,saved["id"]))["gaps"], result["gaps"])

    def test_bad_history_and_unconfigured_model(self):
        with self.assertRaises(ValueError):
            read_turns({"turns":[{"role":"system","content":"Hello"}]})
        with patch.dict("os.environ", {}, clear=True), patch("quantbridge.assistant.urlopen") as transport:
            with self.assertRaises(ModelUnavailable):
                model_plan([question("Risk?")], {})
            transport.assert_not_called()

    def test_model_must_inspect_then_address_an_observed_gap(self):
        turns = [question("Compare IVV with JPM in focus")]
        catalog = dict(funds=["IVV","OEF"], windows=[20,60], holdings_date="2026-10-21",
                       history_start="2026-04-17", history_end="2026-10-21")
        args = dict(funds=["IVV"], windows=[60], scope="summary", symbols=["JPM"])
        calls = [("inspect_assets",dict(funds=["IVV"],symbols=["JPM"])), ("risk",args),
                 ("risk",dict(args,coverage_question="Accept partial results for JPM or wait?"))]
        def respond(request, timeout):
            allowed = {t["function"]["name"] for t in json.loads(request.data)["tools"]}
            name, params = calls.pop(0)
            self.assertIn(name, allowed)
            if name == "inspect_assets":
                self.assertNotIn("risk", allowed)
            msg = {"tool_calls":[{"id":"c","function":{"name":name,"arguments":json.dumps(params)}}]}
            return BytesIO(json.dumps({"choices":[{"message":msg,"finish_reason":"tool_calls"}]}).encode())
        with patch.dict("os.environ", {"QUANTBRIDGE_AI_PROVIDER":"deepseek","DEEPSEEK_API_KEY":"example"}):
            with patch("quantbridge.assistant.urlopen",side_effect=respond):
                plan = model_plan(turns,catalog,lambda args:{"coverage":{"JPM":{"price_source":None}}})
        self.assertIn("JPM",plan["coverage_question"])
        self.assertFalse(calls)

    def test_provider_retries_a_non_english_clarification(self):
        catalog = dict(funds=["IVV", "OEF"], windows=[20, 60], holdings_date="2026-10-21",
                       history_start="2026-04-17", history_end="2026-10-21")
        # The first reply is a Chinese question ("Which risk would you like to see?"),
        # which the provider loop must send back for an English rewrite.
        questions = ["\u60a8\u60f3\u770b\u54ea\u79cd\u98ce\u9669\uff1f", "Which risk measure would you like to compare?"]
        responses = []
        for prompt in questions:
            arguments = json.dumps({"reason": "risk_measure", "question": prompt})
            call = {"id": "q", "type": "function", "function": {"name": "clarify", "arguments": arguments}}
            choice = {"finish_reason": "tool_calls", "message": {"tool_calls": [call]}}
            responses.append(BytesIO(json.dumps({"choices": [choice]}).encode()))
        with patch.dict("os.environ", {"QUANTBRIDGE_AI_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "example"}):
            with patch("quantbridge.assistant.urlopen", side_effect=responses) as transport:
                plan = model_plan([question("I want to see risk.")], catalog)
        self.assertEqual(plan["question"], questions[1])
        correction = json.loads(transport.call_args.args[0].data)["messages"][-1]
        self.assertEqual(correction["role"], "tool")
        self.assertIn("English", correction["content"])

    def test_provider_request_and_invalid_response(self):
        turns = [question("Risk?")]
        catalog = dict(funds=["IVV","OEF"], windows=[20,60], holdings_date="2026-10-21",
                       history_start="2026-04-17", history_end="2026-10-21", private_row="must stay local")
        args = {"reason":"risk_measure", "question":"Which risk measure and which fund?"}
        plan = dict(args, action="clarify")
        with patch.dict("os.environ", {"QUANTBRIDGE_AI_PROVIDER":"deepseek", "DEEPSEEK_API_KEY":"example"}):
            for content in [json.dumps(args), ""]:
                calls = [{"id":"example","type":"function","function":{"name":"clarify","arguments":content}}]
                payload = {"choices":[{"finish_reason":"tool_calls","message":{"content":None,"tool_calls":calls}}]}
                with patch("quantbridge.assistant.urlopen", return_value=BytesIO(json.dumps(payload).encode())) as mock:
                    if content:
                        self.assertEqual(model_plan(turns, catalog), plan)
                        request = mock.call_args.args[0]
                        self.assertNotIn(b"must stay local", request.data)
                        self.assertEqual(json.loads(request.data)["messages"][-1], turns[0])
                    else:
                        with self.assertRaises(ModelUnavailable):
                            model_plan(turns, catalog)
