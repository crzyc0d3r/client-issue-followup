"""Offline behavior tests and request contracts from the documented APIs."""

import copy
import json
import tempfile
import unittest
import sys
from pathlib import Path

import jsonschema
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from followup.contracts import choices_for, validate_choice_response
from followup.harness import TrueForgeReviewer
from followup.pipeline import run, save
from followup.services import JevDecider, Replay, decision_payload


ROOT = Path(__file__).resolve().parents[1]


def read(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def assert_contract(instance, file, schema):
    spec = read(file)
    jsonschema.Draft202012Validator({
        "$ref": f"#/components/schemas/{schema}",
        "components": spec["components"],
    }).validate(instance)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.requests, self.issues, self.fixtures = (
            read("data/requests.json"), read("data/issues.json"), read("data/replay.json"))

    def test_replay_routes_escalates_corrects_and_checkpoints_every_write(self):
        service = Replay(self.fixtures)
        result = run(self.requests, self.issues, service, service, mode="replay")
        self.assertEqual([m["choice"] for m in result["matches"]], ["issue_101", "issue_103", "no_match"])
        self.assertEqual(result["summary"], {"requests": 3, "reviewed": 1, "review_changed": 1,
                                             "unmatched": 1, "external_writes_executed": 0})
        self.assertEqual(len(result["proposals"]), 6)
        self.assertTrue(all(p["approval"] == "required" and not p["executed"] for p in result["proposals"]))
        self.assertEqual(len({p["id"] for p in result["proposals"]}), 6)
        with tempfile.TemporaryDirectory() as tmp:
            path = save(result, tmp)
            self.assertEqual(json.loads(path.read_text())["summary"]["reviewed"], 1)
            with self.assertRaises(FileExistsError):
                save(result, tmp)

    def test_threshold_is_strict_less_than(self):
        fixture = copy.deepcopy(self.fixtures)
        fixture["decisions"]["export"]["answers"]["match"]["confidence"] = 0.8
        service = Replay(fixture)
        result = run(self.requests[:1], self.issues, service, service, threshold=0.8)
        self.assertFalse(result["matches"][0]["reviewed"])

    def test_invalid_exports_fail_before_services(self):
        service = Replay(self.fixtures)
        for issues in (self.issues + self.issues[:1], [{**self.issues[0], "url": "javascript:alert(1)"}]):
            with self.subTest(issues=issues), self.assertRaises(ValueError):
                run(self.requests, issues, service, service)

    def test_response_validation_rejects_unknown_invalid_and_inconsistent_choices(self):
        original = self.fixtures["decisions"]["export"]
        changes = [
            lambda a: a.update(choice="unknown"),
            lambda a: a.update(confidence=float("nan")),
            lambda a: a.update(confidence=True),
            lambda a: a["probabilities"].update(issue_101=0.1),
            lambda a: a["probabilities"].update(unknown=0.0),
            lambda a: a.update(choice="issue_102"),
        ]
        for mutate in changes:
            response = copy.deepcopy(original)
            mutate(response["answers"]["match"])
            with self.subTest(response=response), self.assertRaises(ValueError):
                validate_choice_response(response, choices_for(self.issues))

    def test_live_jev_request_and_response_match_official_contract(self):
        response = self.fixtures["decisions"]["export"]
        assert_contract(response, "contracts/typesafe.openapi.json", "SystemOneResponse")
        def fake_http(method, url, body, token):
            self.assertEqual((method, url), ("POST", "https://api.typesafe.ai/v1/systemone"))
            self.assertEqual(token, "test-only-key")
            assert_contract(body, "contracts/typesafe.openapi.json", "SystemOneRequest")
            self.assertEqual(body, decision_payload(self.requests[0], self.issues, "jev-latest"))
            return response
        decision = JevDecider("test-only-key", http=fake_http).decide(self.requests[0], self.issues)
        self.assertEqual(decision["choice"], "issue_101")

    def test_trueforge_session_turn_polling_and_json_response(self):
        calls = []
        def fake_http(method, url, body, token):
            calls.append((method, url, body))
            self.assertIsNone(token)
            if url.endswith("/sessions"):
                assert_contract(body, "contracts/trueforge.requests.json", "CreateSessionRequest")
                self.assertEqual(body["agent"]["spec"]["mcp_servers"], [])
                self.assertEqual(body["agent"]["spec"]["config"]["sandbox"],
                                 {"enabled": True, "environment_name": "followup-analysis"})
                return {"data": {"id": "session-1"}}
            if method == "POST":
                assert_contract(body, "contracts/trueforge.requests.json", "CreateTurnRequest")
                self.assertFalse(body["stream"])
                return {"data": {"id": "turn-1", "state": {"status": "running"}}}
            return {"data": {"id": "turn-1", "state": {
                "status": "done", "required_actions": [], "output": {
                    "content": json.dumps(self.fixtures["reviews"]["schedule"])}}}}
        reviewer = TrueForgeReviewer("http://localhost:8790", "anthropic/configured-opus",
                                     sandbox_environment="followup-analysis", http=fake_http, sleep=lambda _: None)
        review = reviewer.review(self.requests[1], self.issues,
                                  self.fixtures["decisions"]["schedule"]["answers"]["match"])
        self.assertEqual(review["choice"], "issue_103")
        self.assertEqual([c[0] for c in calls], ["POST", "POST", "GET"])
        self.assertTrue(calls[-1][1].endswith("/sessions/session-1/turns/turn-1"))

    def test_trueforge_never_auto_approves_paused_tools(self):
        responses = iter([{"data": {"id": "session-1"}}, {"data": {"id": "turn-1", "state": {
            "status": "paused", "required_actions": [{"type": "tool.approval_required"}]}}}])
        calls = []
        def fake_http(*args):
            calls.append(args)
            return next(responses)
        reviewer = TrueForgeReviewer("http://localhost:8790", "anthropic/configured-opus", http=fake_http)
        with self.assertRaisesRegex(RuntimeError, "no tool approval"):
            reviewer.review(self.requests[1], self.issues,
                            self.fixtures["decisions"]["schedule"]["answers"]["match"])
        self.assertEqual(len(calls), 2)

    def test_trueforge_timeout_is_bounded(self):
        responses = iter([{"data": {"id": "session-1"}}, {"data": {"id": "turn-1", "state": {"status": "running"}}}])
        reviewer = TrueForgeReviewer("http://localhost:8790", "anthropic/configured-opus",
                                     http=lambda *args: next(responses), timeout=0)
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            reviewer.review(self.requests[1], self.issues,
                            self.fixtures["decisions"]["schedule"]["answers"]["match"])


class McpTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_discovery_and_replay_tool_invocation(self):
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "followup.mcp_server", "--replay", str(ROOT / "data/replay.json")],
            cwd=str(ROOT), env={"PYTHONDONTWRITEBYTECODE": "1"},
        )
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                discovered = await session.list_tools()
                self.assertEqual([tool.name for tool in discovered.tools], ["match_request"])
                self.assertTrue(discovered.tools[0].annotations.readOnlyHint)
                result = await session.call_tool("match_request", {
                    "request": read("data/requests.json")[1],
                    "issues": read("data/issues.json"), "threshold": 0.8,
                })
                self.assertFalse(result.isError)
                payload = result.structuredContent
                self.assertEqual(payload["mode"], "replay")
                self.assertTrue(payload["review_required"])
                self.assertEqual(payload["decision"]["choice"], "issue_102")
                self.assertEqual(payload["external_writes_executed"], 0)


if __name__ == "__main__":
    unittest.main()
