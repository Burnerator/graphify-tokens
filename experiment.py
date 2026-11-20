#!/usr/bin/env python3
"""Does graphify reduce the tokens AI coding agents spend? A reproducible A/B experiment.

Conditions (per agent):
  base   - plain checkout of the corpus, no graph, no extra instructions.
  graph  - same checkout + graphify-out/ (built with `graphify update .`, AST only, no LLM)
           + the rules graphify's own installer writes (identical text in CLAUDE.md for
           Claude Code and AGENTS.md for Codex). graphify's hooks/skill are NOT installed.
Agents: Claude Code (`claude -p`, stream-json traces) and Codex (`codex exec --json`).

  experiment.py prepare --work DIR --wrapper PATH/graphify-safe.sh
  experiment.py run     --work DIR --wrapper PATH --out DIR [--reps 3] [--agents claude codex]
  experiment.py analyze --out DIR

Every session's full trace is saved (sanitized); results.json + REPORT.md are derived from
those traces only, so anyone can re-check every number.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

# --------------------------------------------------------------------------- corpora

INTERNAL_NOTE = "our internal workspace repository (not public)"

# `repo: None` marks an internal corpus: its published sessions stay in runs/ as historical
# data, but it cannot be cloned, so `prepare` and `run` skip it. Only public corpora are
# reproducible.
CORPORA = {
    "anvil": {
        "repo": None,  # internal, not publicly available
        "ref": "6cd8286a0dcb85fec7e604683e7da74d49f800c6",  # benchmark harness merged
        "note": INTERNAL_NOTE,
    },
    "httpx": {
        "repo": "https://github.com/encode/httpx.git",
        "ref": "26d48e0634e6ee9cdc0533996db289ce4b430177",  # tag 0.28.1
    },
}

# Each rubric item is a regex (case-insensitive) for a key point a complete answer mentions.
# Every item was checked to exist in the corpus at the pinned ref (see README).
QUESTIONS = {
    "anvil": [
        (
            "ttft",
            "How does the benchmark measure time-to-first-token and tokens per second? "
            "Name the functions involved.",
            [r"stream_chat", r"ttft", r"perf_counter|clock", r"decode_tps", r"completion_tokens"],
        ),
        (
            "settings",
            "Which modules use config.Settings and for what?",
            [r"__main__", r"bench", r"from_env", r"nvidia_api_key|api.key", r"log_level|logging"],
        ),
        (
            "secret",
            "How does the code make sure the NVIDIA API key is never written to the "
            "benchmark results?",
            [r"repr\s*=\s*False", r"redact", r"Endpoint", r"api_key", r"test"],
        ),
    ],
    "httpx": [
        (
            "pool",
            "How does httpx implement connection pooling? Which classes and modules are involved?",
            [r"httpcore", r"ConnectionPool", r"HTTPTransport", r"Limits", r"keepalive"],
        ),
        (
            "get_flow",
            "Trace what happens when calling httpx.get(): list the functions/classes "
            "involved in order until the request reaches the transport.",
            [
                r"_api\.py|httpx\.request",
                r"build_request",
                r"_send_handling_auth",
                r"_send_handling_redirects",
                r"_send_single_request",
                r"handle_request",
            ],
        ),
        (
            "timeouts",
            "Where are timeouts configured and where are they enforced?",
            [
                r"class Timeout|Timeout\b",
                r"_config\.py",
                r"extensions",
                r"httpcore",
                r"connect|read|write|pool",
            ],
        ),
        (
            "redirects",
            "How are HTTP redirects handled? Name the methods involved.",
            [
                r"_send_handling_redirects",
                r"_build_redirect_request",
                r"follow_redirects",
                r"max_redirects",
                r"TooManyRedirects",
            ],
        ),
        (
            "clients",
            "What is the relationship between Client, AsyncClient and BaseClient?",
            [
                r"BaseClient",
                r"(?<!Async)\bClient\s*\(\s*BaseClient",
                r"AsyncClient\s*\(\s*BaseClient",
                r"_client\.py",
                r"async|await",
            ],
        ),
    ],
}
SUFFIX = " Answer concisely (max ~250 words), citing file paths."

GRAPH_RULES = """## graphify

This project has a graphify knowledge graph at graphify-out/.

Rules:
- For codebase or architecture questions, when `graphify-out/graph.json` exists, first run `{w} query "<question>"` (or `{w} path "<A>" "<B>"` / `{w} explain "<concept>"`). These return a scoped subgraph, usually much smaller than `GRAPH_REPORT.md` or raw grep output.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context
"""  # verbatim from graphify 0.9.111 installer, `graphify` replaced by the safe wrapper path

CLAUDE_MODEL = "sonnet"


def is_public(corpus: str) -> bool:
    """True if the corpus can be cloned (and so reproduced) from a public repository."""
    return bool(CORPORA.get(corpus, {}).get("repo"))


def public_corpora() -> list[str]:
    return [name for name in CORPORA if is_public(name)]


def skip_internal(jobs: list[dict]) -> list[dict]:
    """Drop jobs on internal corpora (not publicly available), saying so once per corpus."""
    for name in sorted({j["corpus"] for j in jobs if not is_public(j["corpus"])}):
        print(f"skipping corpus {name!r}: {CORPORA[name]['note']}", file=sys.stderr)
    return [j for j in jobs if is_public(j["corpus"])]


def sh(cmd: list[str], cwd: Path | None = None, **kw) -> str:
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,  # noqa: S603
        **kw,
    ).stdout


# --------------------------------------------------------------------------- prepare


def prepare(work: Path, wrapper: Path) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    info = {}
    for name, c in CORPORA.items():
        if not is_public(name):
            print(f"skipping corpus {name!r}: {c['note']}", file=sys.stderr)
            continue
        src = work / f"{name}-src"
        if not src.exists():
            sh(["git", "clone", "--quiet", c["repo"], str(src)])
        sh(["git", "checkout", "--quiet", "--detach", c["ref"]], cwd=src)
        for cond in ("base", "graph"):
            dst = work / f"{name}-{cond}"
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git"))
            for f in ("CLAUDE.md", "AGENTS.md"):  # neutral starting point for both conditions
                (dst / f).unlink(missing_ok=True)
        g = work / f"{name}-graph"
        out = sh([str(wrapper), "update", "."], cwd=g)
        m = re.search(r"Rebuilt: (\d+) nodes, (\d+) edges, (\d+) communities", out)
        rules = GRAPH_RULES.format(w=wrapper)
        (g / "CLAUDE.md").write_text(rules)
        (g / "AGENTS.md").write_text(rules)
        py = [p for p in g.rglob("*.py") if "graphify-out" not in p.parts]
        info[name] = {
            **c,
            "py_files": len(py),
            "py_lines": sum(len(p.read_text(errors="replace").splitlines()) for p in py),
            "graph": dict(zip(("nodes", "edges", "communities"), map(int, m.groups()), strict=True))
            if m
            else None,
        }
    (work / "prepare.json").write_text(json.dumps(info, indent=2))
    return info


# --------------------------------------------------------------------------- run


@dataclass
class Session:
    id: str
    agent: str
    corpus: str
    question: str
    cond: str
    rep: int
    order: int
    wall_s: float = 0.0
    ok: bool = False
    error: str | None = None
    input_tokens: int = 0  # uncached input
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float | None = None  # Claude only (API-equivalent)
    turns: int = 0
    tool_calls: int = 0
    graphify_calls: int = 0
    graphify_output_chars: int = 0
    tool_output_chars: int = 0
    answer: str = ""
    rubric_hits: int = 0
    rubric_total: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
            + self.output_tokens
        )


def claude_cmd(prompt: str, wrapper: Path) -> list[str]:
    return [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        CLAUDE_MODEL,
        "--no-session-persistence",
        "--max-budget-usd",
        "1.50",
        "--disallowedTools",
        "Agent",
        "Task",
        "WebSearch",
        "WebFetch",
        "Write",
        "Edit",
        "--allowedTools",
        "Read",
        "Grep",
        "Glob",
        f"Bash({wrapper} *)",
        "Bash(ls *)",
    ]


def codex_cmd(prompt: str, cwd: Path) -> list[str]:
    return [
        "codex",
        "exec",
        "--json",
        "--ephemeral",
        "--skip-git-repo-check",
        "-s",
        "read-only",
        "-C",
        str(cwd),
        prompt,
    ]


_GRAPHIFY_CALL = re.compile(r"graphify(?:-safe\.sh)?['\"]?\s+(?:query|path|explain)\b")


def _is_graphify(cmd: str) -> bool:
    """Only an actual invocation counts (`graphify[-safe.sh] query|path|explain ...`).
    Merely mentioning 'graphify' (e.g. in a directory name) does not."""
    return _GRAPHIFY_CALL.search(cmd) is not None


def parse_claude(lines: list[str], s: Session) -> None:
    pending: dict[str, bool] = {}
    failed = False  # any error result fails the session, even if a later one succeeds
    for line in lines:
        e = json.loads(line)
        if e.get("type") == "assistant":
            for c in e["message"].get("content", []):
                if c.get("type") == "tool_use":
                    s.tool_calls += 1
                    cmd = json.dumps(c.get("input", {}))
                    pending[c["id"]] = c.get("name") == "Bash" and _is_graphify(cmd)
                    s.graphify_calls += pending[c["id"]]
        elif e.get("type") == "user":
            for c in e["message"].get("content", []):
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    n = len(json.dumps(c.get("content")))
                    s.tool_output_chars += n
                    if pending.get(c.get("tool_use_id")):
                        s.graphify_output_chars += n
        elif e.get("type") == "result":
            u = e.get("usage") or {}
            s.input_tokens = u.get("input_tokens", 0)
            s.cache_read_tokens = u.get("cache_read_input_tokens", 0)
            s.cache_write_tokens = u.get("cache_creation_input_tokens", 0)
            s.output_tokens = u.get("output_tokens", 0)
            s.reasoning_tokens = (u.get("output_tokens_details") or {}).get("thinking_tokens", 0)
            mu = e.get("modelUsage") or {}
            if mu:  # all models the session used (incl. auxiliary calls), not just the main one
                s.input_tokens = sum(m.get("inputTokens", 0) for m in mu.values())
                s.cache_read_tokens = sum(m.get("cacheReadInputTokens", 0) for m in mu.values())
                s.cache_write_tokens = sum(m.get("cacheCreationInputTokens", 0)
                                           for m in mu.values())
                s.output_tokens = sum(m.get("outputTokens", 0) for m in mu.values())
            s.cost_usd = e.get("total_cost_usd")
            s.turns = e.get("num_turns", 0)
            s.answer = e.get("result") or ""
            # same terminal rule as the v2 harness: no error flag and a success subtype,
            # in every result event of the session
            failed = failed or bool(e.get("is_error")) or e.get("subtype") not in (None,
                                                                                   "success")
            s.ok = not failed


def parse_codex(lines: list[str], s: Session) -> None:
    answers = []
    for line in lines:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        it = e.get("item") or {}
        if e.get("type") == "item.completed" and it.get("type") == "command_execution":
            s.tool_calls += 1
            out = len(it.get("aggregated_output") or "")
            s.tool_output_chars += out
            if _is_graphify(it.get("command", "")):
                s.graphify_calls += 1
                s.graphify_output_chars += out
        elif e.get("type") == "item.completed" and it.get("type") == "agent_message":
            answers.append(it.get("text", ""))
        elif e.get("type") == "turn.completed":
            u = e.get("usage") or {}
            cached = u.get("cached_input_tokens", 0)
            # Codex reports input_tokens INCLUDING the cached part
            s.input_tokens += max(0, u.get("input_tokens", 0) - cached)
            s.cache_read_tokens += cached
            s.cache_write_tokens += u.get("cache_write_input_tokens", 0)
            s.output_tokens += u.get("output_tokens", 0)
            s.reasoning_tokens += u.get("reasoning_output_tokens", 0)
            s.turns += 1
            s.ok = True
        elif e.get("type") in ("turn.failed", "error"):
            s.error = str(e)[:300]
    if s.error:
        s.ok = False
    s.answer = answers[-1] if answers else ""


def score(answer: str, rubric: list[str]) -> int:
    return sum(1 for pat in rubric if re.search(pat, answer, re.I))


def sanitize(text: str, work: Path) -> str:
    text = text.replace(str(work), "<work>")
    text = re.sub(r"/(?:Users|home)/[^/\s\"']+", "~", text)
    text = re.sub(r"/(?:private/)?(?:tmp|var/folders)/[^\s\"']*", "<tmp>", text)
    return text


def schedule(reps: int, agents: list[str]) -> list[dict]:
    """Counterbalanced: condition order alternates per (question, rep, agent)."""
    plan, k = [], 0
    for corpus, qs in QUESTIONS.items():
        for qi, (qid, text, rubric) in enumerate(qs):
            for rep in range(reps):
                for ai, agent in enumerate(agents):
                    order = ("base", "graph") if (qi + rep + ai) % 2 == 0 else ("graph", "base")
                    for cond in order:
                        plan.append(
                            {
                                "corpus": corpus,
                                "qid": qid,
                                "text": text,
                                "rubric": rubric,
                                "rep": rep,
                                "agent": agent,
                                "cond": cond,
                                "order": k,
                            }
                        )
                        k += 1
    return plan


def select_jobs(reps: int, agents: list[str], only: str | None = None) -> list[dict]:
    """The sessions to run; `only` (corpus:question) restricts it, e.g. for a pilot."""
    jobs = schedule(reps, agents)  # order indices stay those of the full published design
    if only:
        jobs = [j for j in jobs if f"{j['corpus']}:{j['qid']}" == only]
        if not jobs:
            raise SystemExit(f"--only {only!r} matches no question")
        corpus = jobs[0]["corpus"]
        if not is_public(corpus):
            raise SystemExit(f"--only {only!r}: corpus {corpus!r} is "
                             f"{CORPORA[corpus]['note']} and cannot be reproduced")
    return skip_internal(jobs)


def inputs_sha256(*extra) -> str:
    """Fingerprint of the task inputs defined in code (questions, rubrics, prompt suffix)."""
    blob = json.dumps([QUESTIONS, SUFFIX, CLAUDE_MODEL, *extra], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def run(
    work: Path, wrapper: Path, out: Path, reps: int, agents: list[str], only: str | None = None
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "sessions").mkdir(exist_ok=True)
    env_info = {
        "started_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "claude_version": sh(["claude", "--version"]).strip(),
        "codex_version": sh(["codex", "--version"]).strip(),
        "claude_model": CLAUDE_MODEL,
        "codex_model": "CLI default (not overridden)",
        "graphify_version": sh([str(wrapper), "--version"]).strip(),
        "reps": reps,
        "agents": agents,
        "prepare": json.loads((work / "prepare.json").read_text()),
        "inputs_sha256": inputs_sha256(),
    }
    env_path = out / "environment.json"
    if env_path.exists():  # never mix configurations in one run directory
        prev = json.loads(env_path.read_text())
        diff = [k for k in ("claude_version", "codex_version", "claude_model", "codex_model",
                            "graphify_version", "prepare", "inputs_sha256")
                if prev.get(k) is not None and prev.get(k) != env_info.get(k)]
        if diff:
            raise SystemExit(f"--out was recorded with a different configuration "
                             f"({', '.join(diff)}); use a new output directory")
    env_path.write_text(json.dumps(env_info, indent=2))
    done = {p.stem for p in (out / "sessions").glob("*.json")
            if json.loads(p.read_text()).get("ok")}  # failed sessions are retried
    for job in select_jobs(reps, agents, only):
        sid = f"{job['agent']}-{job['corpus']}-{job['qid']}-r{job['rep']}-{job['cond']}"
        if sid in done:  # resumable
            continue
        cwd = work / f"{job['corpus']}-{job['cond']}"
        prompt = job["text"] + SUFFIX
        cmd = claude_cmd(prompt, wrapper) if job["agent"] == "claude" else codex_cmd(prompt, cwd)
        s = Session(
            sid,
            job["agent"],
            job["corpus"],
            job["qid"],
            job["cond"],
            job["rep"],
            job["order"],
            rubric_total=len(job["rubric"]),
        )
        t0 = time.time()
        returncode = None
        try:
            p = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,  # noqa: S603
                stdin=subprocess.DEVNULL,
                timeout=900,
                check=False,
            )
            lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
            returncode = p.returncode
            (parse_claude if job["agent"] == "claude" else parse_codex)(lines, s)
            if p.returncode != 0:  # a success event is not enough if the process failed
                s.ok = False
                s.error = s.error or f"exit {p.returncode}"
            if not s.ok and not s.error:
                s.error = (p.stderr or "no result")[-300:]
        except subprocess.TimeoutExpired:
            lines, s.ok, s.error = [], False, "timeout"
        s.wall_s = round(time.time() - t0, 2)
        s.rubric_hits = score(s.answer, job["rubric"])
        (out / "sessions" / f"{sid}.trace.jsonl").write_text(
            sanitize("\n".join(lines), work) + "\n"
        )
        rec = asdict(s) | {"total_tokens": s.total_tokens, "returncode": returncode}
        (out / "sessions" / f"{sid}.json").write_text(sanitize(json.dumps(rec, indent=2), work))
        print(
            f"{sid:48} ok={s.ok} tok={s.total_tokens:>7} gfy={s.graphify_calls} "
            f"rubric={s.rubric_hits}/{s.rubric_total} {s.wall_s:.0f}s",
            flush=True,
        )


# --------------------------------------------------------------------------- analyze


def _med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def reparse(out: Path) -> None:
    """Recompute every metric from the saved traces (the single source of truth)."""
    rubrics = {(c, qid): rub for c, qs in QUESTIONS.items() for qid, _, rub in qs}
    for meta_path in sorted((out / "sessions").glob("*.json")):
        old = json.loads(meta_path.read_text())
        trace = meta_path.with_name(meta_path.stem + ".trace.jsonl")
        lines = [ln for ln in trace.read_text().splitlines() if ln.strip()]
        s = Session(old["id"], old["agent"], old["corpus"], old["question"], old["cond"],
                    old["rep"], old["order"], wall_s=old["wall_s"],
                    rubric_total=len(rubrics[(old["corpus"], old["question"])]))
        (parse_claude if s.agent == "claude" else parse_codex)(lines, s)
        if old.get("returncode") not in (0, None):  # the process failed: stays a failure
            s.ok = False
        if not s.ok:
            s.error = old.get("error") or "no result in trace"
        s.rubric_hits = score(s.answer, rubrics[(s.corpus, s.question)])
        # recomputed fields overwrite; fields added by other tools (e.g. publication) stay.
        # Published records carry the v2 graph-use definition (gfy_* fields, mixed commands
        # excluded from output); v1's legacy parser must not overwrite it.
        new = asdict(s) | {"total_tokens": s.total_tokens}
        if "gfy_attempted" in old:
            for k in ("graphify_calls", "graphify_output_chars"):
                new.pop(k)
        rec = old | new
        meta_path.write_text(json.dumps(rec, indent=2))


def analyze(out: Path) -> dict:
    reparse(out)
    rows = [json.loads(p.read_text()) for p in sorted((out / "sessions").glob("*.json"))]
    groups = {}
    for r in rows:
        groups.setdefault((r["agent"], r["corpus"], r["cond"]), []).append(r)
    summary = []
    for (agent, corpus, cond), rs in sorted(groups.items()):
        ok = [r for r in rs if r["ok"]]
        summary.append(
            {
                "agent": agent,
                "corpus": corpus,
                "cond": cond,
                "n": len(rs),
                "ok": len(ok),
                "median_total_tokens": _med([r["total_tokens"] for r in ok]),
                "median_uncached_in_plus_out": _med(
                    [r["input_tokens"] + r["output_tokens"] for r in ok]
                ),
                "median_output_tokens": _med([r["output_tokens"] for r in ok]),
                "median_cost_usd": _med([r["cost_usd"] for r in ok]),
                "sum_cost_usd": sum(r["cost_usd"] or 0 for r in ok) if agent == "claude" else None,
                "median_tool_calls": _med([r["tool_calls"] for r in ok]),
                "median_wall_s": _med([r["wall_s"] for r in ok]),
                "graphify_adoption": (sum(1 for r in ok if r["graphify_calls"] > 0) / len(ok))
                if ok
                else None,
                "median_graphify_output_chars": _med(
                    [r["graphify_output_chars"] for r in ok if r["graphify_calls"]]
                ),
                "mean_rubric_score": (
                    statistics.mean(r["rubric_hits"] / r["rubric_total"] for r in ok)
                    if ok
                    else None
                ),
            }
        )
    # paired per question: graph vs base (median over reps)
    paired = []
    for agent in sorted({r["agent"] for r in rows}):
        for corpus in sorted({r["corpus"] for r in rows}):
            for qid in sorted({r["question"] for r in rows if r["corpus"] == corpus}):

                def med_tok(cond, a=agent, c=corpus, q=qid):
                    return _med(
                        [
                            r["total_tokens"]
                            for r in rows
                            if r["ok"]
                            and r["agent"] == a
                            and r["corpus"] == c
                            and r["question"] == q
                            and r["cond"] == cond
                        ]
                    )

                b, g = med_tok("base"), med_tok("graph")
                if b and g:
                    paired.append(
                        {
                            "agent": agent,
                            "corpus": corpus,
                            "question": qid,
                            "base_tokens": b,
                            "graph_tokens": g,
                            "delta_pct": round(100 * (g / b - 1), 1),
                        }
                    )
    res = {
        "environment": json.loads((out / "environment.json").read_text()),
        "summary": summary,
        "paired": paired,
        "sessions": len(rows),
        "failed": [r["id"] for r in rows if not r["ok"]],
    }
    (out / "results.json").write_text(json.dumps(res, indent=2))
    (out / "REPORT.md").write_text(render_report(res))
    return res


def _f(v, nd=0, pct=False):
    if v is None:
        return "—"
    return f"{v:.0%}" if pct else (f"{v:,.{nd}f}" if isinstance(v, float) else f"{v:,}")


def render_report(res: dict) -> str:
    env = res["environment"]
    lines = [
        "# graphify-tokens: report",
        "",
        f"Run started {env['started_utc']} · {res['sessions']} sessions · "
        f"failed: {len(res['failed'])} · reps: {env['reps']}",
        "",
        f"- Claude Code: `{env['claude_version']}`, model `{env['claude_model']}`",
        f"- Codex: `{env['codex_version']}`, model: {env['codex_model']}",
        f"- graphify: `{env['graphify_version']}`",
        "",
        "## Corpora",
        "",
        "| Corpus | Commit | Python files | Lines | Graph nodes / edges |",
        "|---|---|---|---|---|",
    ]
    for name, c in env["prepare"].items():
        g = c.get("graph") or {}
        lines.append(f"| {name} | `{c['ref'][:7]}` | {c['py_files']} | {c['py_lines']:,} | "
                     f"{g.get('nodes', '—')} / {g.get('edges', '—')} |")
    lines += ["", "## Summary (medians per session, successful sessions only)", "",
              "| Agent | Corpus | Condition | OK | Total tokens | Uncached in+out | Output | "
              "Cost $ (Claude) | Tool calls | graphify used | Rubric score | Wall s |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in res["summary"]:
        lines.append(
            f"| {r['agent']} | {r['corpus']} | {r['cond']} | {r['ok']}/{r['n']} | "
            f"{_f(r['median_total_tokens'])} | {_f(r['median_uncached_in_plus_out'])} | "
            f"{_f(r['median_output_tokens'])} | {_f(r['median_cost_usd'], 3)} | "
            f"{_f(r['median_tool_calls'])} | {_f(r['graphify_adoption'], pct=True)} | "
            f"{_f(r['mean_rubric_score'], pct=True)} | {_f(r['median_wall_s'], 1)} |")
    lines += ["", "## Paired by question (median total tokens over reps)", "",
              "| Agent | Corpus | Question | base | graph | Δ |", "|---|---|---|---|---|---|"]
    for r in res["paired"]:
        lines.append(f"| {r['agent']} | {r['corpus']} | {r['question']} | "
                     f"{_f(r['base_tokens'])} | {_f(r['graph_tokens'])} | "
                     f"{r['delta_pct']:+.1f}% |")
    lines += ["", "## How to check any number", "",
              "Every row comes from `sessions/<id>.json`, derived from `sessions/<id>.trace.jsonl` "
              "(the agent's own event stream, sanitized). Re-run `python3 experiment.py analyze "
              "--out <run>` to regenerate this file.", ""]
    if res["failed"]:
        lines += ["## Failed sessions", ""] + [f"- `{x}`" for x in res["failed"]] + [""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--wrapper", type=Path, required=True)
    r = sub.add_parser("run")
    r.add_argument("--work", type=Path, required=True)
    r.add_argument("--wrapper", type=Path, required=True)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--reps", type=int, default=3)
    r.add_argument("--only", help="corpus:question, e.g. httpx:timeouts (pilot)")
    r.add_argument("--agents", nargs="+", default=["claude", "codex"], choices=["claude", "codex"])
    a = sub.add_parser("analyze")
    a.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.cmd == "prepare":
        print(json.dumps(prepare(args.work.resolve(), args.wrapper.resolve()), indent=2))
    elif args.cmd == "run":
        run(args.work.resolve(), args.wrapper.resolve(), args.out.resolve(), args.reps,
            args.agents, args.only)
    else:
        print(json.dumps(analyze(args.out)["summary"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
