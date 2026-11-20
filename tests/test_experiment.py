"""Unit tests for the experiment harness (stdlib unittest, no network, no agents)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import experiment as E  # noqa: E402


def _s(agent="claude"):
    return E.Session("id", agent, "httpx", "pool", "graph", 0, 0)


class TestSchedule(unittest.TestCase):
    def test_full_schedule_size_and_counterbalance(self):
        jobs = E.schedule(3, ["claude", "codex"])
        n_q = sum(len(q) for q in E.QUESTIONS.values())
        self.assertEqual(len(jobs), n_q * 3 * 2 * 2)
        # each (question, rep, agent) has both conditions, and graph goes first ~half the time
        firsts = [j["cond"] for j in jobs[::2]]
        self.assertLessEqual(abs(firsts.count("base") - firsts.count("graph")), 2)

    def test_only_filter_is_applied(self):  # regression: --only was ignored in the first run
        jobs = E.select_jobs(1, ["claude", "codex"], "httpx:timeouts")
        self.assertEqual(len(jobs), 4)
        self.assertTrue(all(j["corpus"] == "httpx" and j["qid"] == "timeouts" for j in jobs))
        with self.assertRaises(SystemExit):
            E.select_jobs(1, ["claude"], "httpx:nope")

    def test_main_passes_only_to_run(self):
        calls = {}
        orig = E.run
        E.run = lambda *a, **k: calls.setdefault("args", a)
        try:
            E.main(["run", "--work", "w", "--wrapper", "x", "--out", "o", "--only", "httpx:pool"])
        finally:
            E.run = orig
        self.assertEqual(calls["args"][-1], "httpx:pool")


class TestParsers(unittest.TestCase):
    def test_parse_claude_counts_graphify_calls_and_output(self):
        lines = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Bash",
                 "input": {"command": "/x/graphify-safe.sh query \"pool\""}},
                {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "a.py"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "N" * 1000},
                {"type": "tool_result", "tool_use_id": "t2", "content": "code"}]}},
            {"type": "result", "is_error": False, "num_turns": 3, "total_cost_usd": 0.12,
             "result": "uses ConnectionPool", "usage": {
                 "input_tokens": 10, "cache_read_input_tokens": 1000,
                 "cache_creation_input_tokens": 200, "output_tokens": 50,
                 "output_tokens_details": {"thinking_tokens": 7}}},
        ]
        s = _s()
        E.parse_claude([json.dumps(x) for x in lines], s)
        self.assertTrue(s.ok)
        self.assertEqual((s.tool_calls, s.graphify_calls), (2, 1))
        self.assertGreater(s.graphify_output_chars, 1000)
        self.assertEqual(s.total_tokens, 10 + 1000 + 200 + 50)
        self.assertEqual((s.reasoning_tokens, s.cost_usd, s.turns), (7, 0.12, 3))

    def test_parse_codex_splits_cached_input(self):
        lines = [
            {"type": "item.completed", "item": {"type": "command_execution",
             "command": "/bin/zsh -lc 'graphify-safe.sh explain Client'",
             "aggregated_output": "X" * 300}},
            {"type": "item.completed", "item": {"type": "command_execution",
             "command": "rg -n Timeout httpx", "aggregated_output": "Y" * 50}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "draft"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "final"}},
            {"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 600,
                                                 "output_tokens": 40,
                                                 "reasoning_output_tokens": 5}},
        ]
        s = _s("codex")
        E.parse_codex([json.dumps(x) for x in lines] + ["not json"], s)
        self.assertTrue(s.ok)
        self.assertEqual((s.input_tokens, s.cache_read_tokens), (400, 600))  # no double count
        self.assertEqual((s.tool_calls, s.graphify_calls, s.graphify_output_chars), (2, 1, 300))
        self.assertEqual(s.answer, "final")

    def test_graphify_detection_needs_a_query_subcommand(self):
        self.assertTrue(E._is_graphify("graphify-safe.sh path A B"))
        self.assertTrue(E._is_graphify("/x/scripts/graphify-safe.sh query \"pool\""))
        self.assertTrue(E._is_graphify("/bin/zsh -lc '/x/graphify-safe.sh' explain Client"))
        self.assertFalse(E._is_graphify("cat graphify-out/GRAPH_REPORT.md"))
        # regression: the repo directory is called graphify-tokens; `find -path` is not a query
        self.assertFalse(E._is_graphify(
            "find /Users/a/wks/graphify-tokens/work/httpx-base -path '*/httpx/_*.py'"))


class TestScoringAndSanitizing(unittest.TestCase):
    def test_parse_codex_error_after_completion_is_a_failure(self):
        s = E.Session("x", "codex", "httpx", "q", "base", 0, 0)
        E.parse_codex(['{"type": "turn.completed", "usage": {}}',
                       '{"type": "turn.failed", "error": {"message": "boom"}}'], s)
        self.assertFalse(s.ok)
        self.assertIn("boom", s.error)

    def test_parse_claude_sums_tokens_over_all_models(self):
        s = E.Session("x", "claude", "httpx", "q", "base", 0, 0)
        E.parse_claude([json.dumps({"type": "result", "is_error": False, "result": "a",
                                    "usage": {"input_tokens": 1, "output_tokens": 2,
                                              "cache_read_input_tokens": 3},
                                    "modelUsage": {
                                        "big": {"inputTokens": 1, "outputTokens": 2,
                                                "cacheReadInputTokens": 3,
                                                "cacheCreationInputTokens": 4},
                                        "small": {"inputTokens": 10, "outputTokens": 1}}})], s)
        self.assertEqual(s.total_tokens, 1 + 2 + 3 + 4 + 10 + 1)

    def test_client_rubric_does_not_match_async_client(self):
        rub = [r for c, qs in E.QUESTIONS.items() for _, _, rs in qs for r in rs
               if "Client" in r and "Async" in r and "BaseClient" in r and "(?<!" in r]
        self.assertTrue(rub)
        self.assertEqual(E.score("class AsyncClient(BaseClient)", rub), 0)
        self.assertEqual(E.score("class Client(BaseClient)", rub), 1)

    def test_reparse_keeps_a_failed_exit_code(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            sess = Path(d) / "sessions"
            sess.mkdir()
            qid = E.QUESTIONS["httpx"][0][0]
            meta = {"id": "s", "agent": "claude", "corpus": "httpx", "question": qid,
                    "cond": "base", "rep": 0, "order": 0, "wall_s": 1, "returncode": 1}
            (sess / "s.json").write_text(json.dumps(meta))
            (sess / "s.trace.jsonl").write_text(json.dumps(
                {"type": "result", "is_error": False, "result": "a", "usage": {}}) + "\n")
            E.reparse(Path(d))
            rec = json.loads((sess / "s.json").read_text())
            self.assertFalse(rec["ok"])
            self.assertEqual(rec["returncode"], 1)

    def test_parse_claude_failure_subtype_is_not_ok(self):
        s = E.Session("x", "claude", "httpx", "q", "base", 0, 0)
        E.parse_claude([json.dumps({"type": "result", "subtype": "error_max_turns",
                                    "is_error": False, "usage": {}})], s)
        self.assertFalse(s.ok)

    def test_parse_claude_earlier_error_result_fails_session(self):
        s = E.Session("x", "claude", "httpx", "q", "base", 0, 0)
        E.parse_claude([json.dumps({"type": "result", "subtype": "error_during_execution",
                                    "is_error": True, "usage": {}}),
                        json.dumps({"type": "result", "subtype": "success",
                                    "is_error": False, "usage": {}, "result": "a"})], s)
        self.assertFalse(s.ok)

    def test_rubric_items_are_case_insensitive_regexes(self):
        rubric = [r"ConnectionPool", r"keepalive"]
        self.assertEqual(E.score("uses connectionpool; max_keepalive_connections", rubric), 2)
        self.assertEqual(E.score("nothing", rubric), 0)

    def test_sanitize_removes_local_paths(self):
        work = Path("/Users/alice/wks/graphify-tokens/work")
        out = E.sanitize(f"{work}/httpx-graph/x.py and /Users/alice/.cache and "
                         "/private/tmp/abc/def", work)
        self.assertNotIn("alice", out)
        self.assertIn("<work>/httpx-graph/x.py", out)
        self.assertIn("<tmp>", out)

    def test_every_rubric_item_compiles(self):
        import re

        for qs in E.QUESTIONS.values():
            for _, _, rubric in qs:
                for pat in rubric:
                    re.compile(pat)


class TestInternalCorpus(unittest.TestCase):
    """anvil is an internal repository: its published sessions stay, but it is never cloned."""

    def test_anvil_is_internal_and_httpx_public(self):
        self.assertFalse(E.is_public("anvil"))
        self.assertTrue(E.is_public("httpx"))
        self.assertEqual(E.public_corpora(), ["httpx"])
        self.assertIn("anvil", E.QUESTIONS)  # rubrics kept: the published runs are reparsed

    def test_jobs_skip_internal_corpus_and_keep_order_indices(self):
        full = E.schedule(1, ["claude", "codex"])
        jobs = E.select_jobs(1, ["claude", "codex"])
        self.assertTrue(jobs)
        self.assertEqual(jobs, [j for j in full if j["corpus"] == "httpx"])

    def test_only_on_internal_corpus_stops_cleanly(self):
        with self.assertRaises(SystemExit) as cm:
            E.select_jobs(1, ["claude"], "anvil:ttft")
        self.assertIn("not public", str(cm.exception))

    def test_prepare_never_clones_internal_corpus(self):
        calls = []
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(E, "CORPORA", {"anvil": E.CORPORA["anvil"]}), \
                mock.patch.object(E, "sh", lambda cmd, **k: calls.append(cmd) or ""):
            info = E.prepare(Path(d), Path("wrapper"))
            self.assertEqual(info, {})
            self.assertEqual(json.loads((Path(d) / "prepare.json").read_text()), {})
        self.assertEqual(calls, [])

    def test_no_link_to_internal_repository_outside_historical_runs(self):
        needle = "Burnerator/" + "anvil"
        for path in ROOT.rglob("*"):
            rel = path.relative_to(ROOT)
            if (not path.is_file() or rel.parts[0] in {"runs", "work", ".git", ".venv"}
                    or "__pycache__" in rel.parts or ".ruff_cache" in rel.parts):
                continue
            self.assertNotIn(needle, path.read_text(errors="ignore"), str(rel))


if __name__ == "__main__":
    unittest.main()
