#!/usr/bin/env python3
"""graphify-tokens v2: follow graphify's OFFICIAL integration instructions exactly.

Conditions
  base            nothing extra                                     (Claude, Codex)
  rules-official  graph + the exact text written by `graphify claude install` (CLAUDE.md)
                  or `graphify codex install` (AGENTS.md), without hooks  (Claude, Codex)
  hooks           graph + full `graphify claude install` (rules + PreToolUse/SessionStart
                  hooks)                                                    (Claude only)
  strict          as `hooks`, with GRAPHIFY_HOOK_STRICT=1 (graphify's documented strict
                  policy: indexed Read/Grep denied until a recent graph query)  (Claude only)
  (v1's `graph` = graphify's generic Devin-style rules text, kept as `rules-generic`.)

Tasks
  A  understand: the 8 architecture questions of v1 (same corpora, same rubrics)
  B  review: 4 real merged httpx PRs; corpus = PR head, graph built on the PR's BASE commit
     (as graphify's PR guide recommends); rubric = changed symbols + their real callers,
     derived statically from the diff (not from the graph).

Controls (as implemented, see REPORT.md "Agents and controls"): official installs run inside
throwaway corpus copies with a fake HOME, and the harness aborts if the installer leaves any
file in that fake HOME. A PATH shim makes a bare `graphify` resolve to the allowlist wrapper,
which strips its own subprocess environment; hooks run graphify's real `hook-guard` through
hookwrap.sh with a minimal environment. The agents themselves inherit the operator's
environment and global settings, and the real binary stays reachable by absolute path.

  experiment_v2.py prepare --work DIR --wrapper SAFE.sh --graphify REAL_BIN
  experiment_v2.py run --work DIR --out DIR [--reps 3] [--only TASK:CORPUS:ID] [--conds ...]
  experiment_v2.py analyze --out DIR [--v1 runs/2026-11-17]
  experiment_v2.py report --out DIR --v1 runs/2026-11-17          # writes REPORT.md
  experiment_v2.py sanitize --raw RAW_DIR --key KEY_FILE            # rebuild redacted runs/
  experiment_v2.py export-conditions --work DIR                     # installer-written files
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import hmac
import json
import os
import re
import shlex
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import experiment as V1  # parsers, rubrics, sessions, sanitizer (unchanged from v1)

ROOT = Path(__file__).resolve().parent
CONDS = {"claude": ["base", "rules-official", "hooks", "strict"],
         "codex": ["base", "rules-official"]}

# Task B: real merged PRs of encode/httpx (squash merges: base = sha^, head = sha)
PRS = {
    "pr3377": {"sha": "e9cabc8e1", "title": "Lazy-load certifi and httpcore",
               "url": "https://github.com/encode/httpx/pull/3377"},
    "pr2879": {"sha": "f8981f3d1", "title": "Add 'proxy' parameter, deprecate 'proxies'",
               "url": "https://github.com/encode/httpx/pull/2879"},
    "pr3139": {"sha": "392dbe45f", "title": "Add support for zstd decoding",
               "url": "https://github.com/encode/httpx/pull/3139"},
    "pr3389": {"sha": "7b19cd5f4", "title": "Move utility functions from _utils.py to _client.py",
               "url": "https://github.com/encode/httpx/pull/3389"},
}
REVIEW_PROMPT = ("Review the changes between commit {base} and HEAD in this repository "
                 "(`git diff {base}..HEAD`). Explain what changed, point out possible bugs or "
                 "risks, and list the other parts of the codebase affected by the change "
                 "(callers, importers, behaviour). Answer concisely (max ~300 words), citing "
                 "file paths.")
MAX_RUBRIC = 8


def sh(cmd: list[str], cwd: Path | None = None, env: dict | None = None) -> str:
    return subprocess.run(cmd, cwd=cwd, env=env, check=True, capture_output=True,  # noqa: S603
                          text=True).stdout


# --------------------------------------------------------------------------- PR rubric

def _defs_touched(src: str, changed_lines: set[int]) -> set[str]:
    """Top-level/method names whose body overlaps the changed lines (Python AST)."""
    names: set[str] = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            span = set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
            if span & changed_lines and not node.name.startswith("__"):
                names.add(node.name)
    return names


def pr_rubric(repo: Path, base: str, head: str) -> dict:
    """Changed symbols (from the diff + AST of both sides) and files that reference them."""
    files = [f for f in sh(["git", "diff", "--name-only", base, head, "--", "httpx/*.py"],
                           cwd=repo).split() if f]
    symbols: set[str] = set()
    for f in files:
        diff = sh(["git", "diff", "-U0", base, head, "--", f], cwd=repo)
        new_lines, old_lines = set(), set()
        for m in re.finditer(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", diff, re.M):
            o, oc, n, nc = int(m[1]), int(m[2] or 1), int(m[3]), int(m[4] or 1)
            old_lines |= set(range(o, o + oc))
            new_lines |= set(range(n, n + nc))
        for rev, lines in ((head, new_lines), (base, old_lines)):
            try:
                src = sh(["git", "show", f"{rev}:{f}"], cwd=repo)
            except subprocess.CalledProcessError:
                continue
            symbols |= _defs_touched(src, lines)
    callers: dict[str, int] = {}
    for sym in sorted(symbols):
        out = subprocess.run(["git", "grep", "-l", "-w", sym, head, "--", "httpx/*.py"],  # noqa: S603, S607
                             cwd=repo, capture_output=True, text=True, check=False).stdout
        for line in out.splitlines():
            path = line.split(":", 1)[1]
            if path not in files:
                callers[path] = callers.get(path, 0) + 1
    top_callers = [p for p, _ in sorted(callers.items(), key=lambda kv: (-kv[1], kv[0]))][:3]
    items = ([re.escape(Path(f).name) for f in files][:3]
             + [r"\b" + re.escape(s) + r"\b" for s in sorted(symbols)][:3]
             + [re.escape(Path(p).name) for p in top_callers])[:MAX_RUBRIC]
    return {"files": files, "symbols": sorted(symbols), "callers": top_callers,
            "rubric": items}


# --------------------------------------------------------------------------- prepare

def _official_install(platform: str, corpus: Path, graphify: Path) -> None:
    """Run graphify's own installer inside a throwaway corpus with a fake HOME."""
    with tempfile.TemporaryDirectory(prefix="gt-home-") as home:
        sh([str(graphify), platform, "install"], cwd=corpus,
           env={"HOME": home, "PATH": "/usr/bin:/bin", "GRAPHIFY_BACKEND": "none",
                "GRAPHIFY_NO_TIPS": "1", "GRAPHIFY_NO_CTA": "1"})
        leaked = [p for p in Path(home).rglob("*") if p.is_file()]
        if leaked:
            raise RuntimeError(f"installer wrote outside the corpus: {leaked}")


def _harden_hooks(corpus: Path, graphify: Path, strict: bool) -> None:
    """Route every graphify hook command through hookwrap.sh (same binary, same mode)."""
    path = corpus / ".claude" / "settings.json"
    cfg = json.loads(path.read_text())
    wrap = ROOT / "hookwrap.sh"
    n = 0
    for groups in cfg.get("hooks", {}).values():
        for g in groups:
            for h in g.get("hooks", []):
                h["command"] = hook_command(h.get("command", ""), wrap, graphify, strict)
                n += 1
    if not n:
        raise RuntimeError("no graphify hooks found")
    path.write_text(json.dumps(cfg, indent=2))


def hook_command(installed: str, wrap: Path, graphify: Path, strict: bool) -> str:
    """Rewrite one installer-written hook (`<graphify> hook-guard <mode>`) to go through
    hookwrap.sh. Both the parsing and the result are shell-quoted, so checkout paths with
    spaces survive."""
    parts = shlex.split(installed)
    if (len(parts) != 3 or parts[1] != "hook-guard" or not re.fullmatch(r"\w+", parts[2])
            or Path(parts[0]).resolve() != graphify.resolve()):
        raise RuntimeError(f"unexpected hook command: {installed}")
    argv = [str(wrap), str(graphify), parts[2]] + (["strict"] if strict else [])
    return " ".join(shlex.quote(a) for a in argv)


def _copy_corpus(src: Path, dst: Path, keep_git: bool) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=True,
                    ignore=None if keep_git else shutil.ignore_patterns(".git"))
    for f in ("CLAUDE.md", "AGENTS.md"):
        (dst / f).unlink(missing_ok=True)
    for d in (".claude", ".codex"):
        shutil.rmtree(dst / d, ignore_errors=True)


def _make_conditions(graphed: Path, stem: str, work: Path, graphify: Path,
                     keep_git: bool) -> None:
    """From a corpus that already has graphify-out/, derive every condition directory."""
    for cond in ("rules-official-claude", "rules-official-codex", "hooks", "strict"):
        _copy_corpus(graphed, work / f"{stem}-{cond}", keep_git)
    _official_install("claude", work / f"{stem}-rules-official-claude", graphify)
    shutil.rmtree(work / f"{stem}-rules-official-claude" / ".claude")  # rules only, no hooks
    _official_install("codex", work / f"{stem}-rules-official-codex", graphify)
    for cond, strict in (("hooks", False), ("strict", True)):
        _official_install("claude", work / f"{stem}-{cond}", graphify)
        _harden_hooks(work / f"{stem}-{cond}", graphify, strict)


def prepare(work: Path, wrapper: Path, graphify: Path) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    bindir = work / "bin"  # `graphify` on the agents' PATH -> safe wrapper
    bindir.mkdir(exist_ok=True)
    shim = bindir / "graphify"
    shim.unlink(missing_ok=True)
    shim.symlink_to(wrapper)
    info: dict = {"task_a": {}, "task_b": {}}
    # Task A: v1 already prepared <corpus>-base and <corpus>-graph (graph built, no .git)
    for name in V1.public_corpora():  # internal corpora are not prepared by v1 either
        graphed = work / f"{name}-graph"
        if not (graphed / "graphify-out" / "graph.json").exists():
            raise RuntimeError(f"run v1 prepare first: missing {graphed}")
        _make_conditions(graphed, name, work, graphify, keep_git=False)
        info["task_a"][name] = {"from": f"{name}-graph"}
    # Task B: PR review corpora (with .git so agents can diff)
    src = work / "httpx-src"
    for pid, pr in PRS.items():
        head = sh(["git", "rev-parse", pr["sha"]], cwd=src).strip()
        base = sh(["git", "rev-parse", f"{head}^"], cwd=src).strip()
        b = work / f"{pid}-base"
        _copy_corpus(src, b, keep_git=True)
        sh(["git", "checkout", "--quiet", "--detach", head], cwd=b)
        g = work / f"{pid}-graphed"
        _copy_corpus(src, g, keep_git=True)
        sh(["git", "checkout", "--quiet", "--detach", base], cwd=g)
        sh([str(wrapper), "update", "."], cwd=g)       # graph of the BASE branch
        sh(["git", "checkout", "--quiet", "--detach", head], cwd=g)  # review the PR head
        _make_conditions(g, pid, work, graphify, keep_git=True)
        info["task_b"][pid] = {**pr, "head": head, "base": base,
                               **pr_rubric(src, base, head)}
    (work / "prepare_v2.json").write_text(json.dumps(info, indent=2))
    return info


# --------------------------------------------------------------------------- run

def tasks(work: Path) -> list[dict]:
    info = json.loads((work / "prepare_v2.json").read_text())
    out = []
    for corpus, qs in V1.QUESTIONS.items():
        for qid, text, rubric in qs:
            out.append({"task": "A", "corpus": corpus, "qid": qid, "stem": corpus,
                        "prompt": text + V1.SUFFIX, "rubric": rubric})
    for pid, pr in info["task_b"].items():
        out.append({"task": "B", "corpus": "httpx", "qid": pid, "stem": pid,
                    "prompt": REVIEW_PROMPT.format(base=pr["base"][:12]),
                    "rubric": pr["rubric"]})
    return out


def corpus_dir(work: Path, stem: str, agent: str, cond: str) -> Path:
    if cond == "base":
        return work / f"{stem}-base"
    if cond == "rules-official":
        return work / f"{stem}-rules-official-{agent}"
    return work / f"{stem}-{cond}"


def schedule(work: Path, reps: int, conds: dict[str, list[str]]) -> list[dict]:
    plan, k = [], 0
    for ti, t in enumerate(tasks(work)):
        for rep in range(reps):
            for agent, cs in conds.items():
                order = cs if (ti + rep) % 2 == 0 else list(reversed(cs))
                for cond in order:
                    plan.append({**t, "agent": agent, "cond": cond, "rep": rep, "order": k})
                    k += 1
    return plan


def claude_cmd(prompt: str, wrapper: Path) -> list[str]:
    return ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
            "--model", V1.CLAUDE_MODEL, "--no-session-persistence", "--max-budget-usd", "1.50",
            "--disallowedTools", "Agent", "Task", "WebSearch", "WebFetch", "Write", "Edit",
            "--allowedTools", "Read", "Grep", "Glob", "Bash(graphify *)", f"Bash({wrapper} *)",
            "Bash(ls *)", "Bash(git diff *)", "Bash(git log *)", "Bash(git show *)"]


def run(work: Path, wrapper: Path, out: Path, reps: int, conds: dict[str, list[str]],
        only: str | None = None, task: str | None = None) -> None:
    # TASK:CORPUS:ID on an internal corpus: stop before probing CLIs or writing output
    V1.reject_internal_only(only, (only or "").split(":")[1] if (only or "").count(":") >= 2
                            else None)
    (out / "sessions").mkdir(parents=True, exist_ok=True)
    env_info = {
        "started_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        # only the CLIs actually used are required (and queried)
        "claude_version": sh(["claude", "--version"]).strip() if "claude" in conds else None,
        "codex_version": sh(["codex", "--version"]).strip() if "codex" in conds else None,
        "claude_model": V1.CLAUDE_MODEL, "codex_model": "CLI default (not overridden)",
        "graphify_version": sh([str(wrapper), "--version"]).strip(),
        "reps": reps, "conds": conds, "only": only,
        "prepare": json.loads((work / "prepare_v2.json").read_text()),
        # task A prompts/rubrics live in code, and task A corpora come from v1's prepare
        "inputs_sha256": V1.inputs_sha256(REVIEW_PROMPT, MAX_RUBRIC,
                                          json.loads((work / "prepare.json").read_text())),
    }
    env_path = out / "environment.json"
    prev = json.loads(env_path.read_text()) if env_path.exists() else {}
    check_resume_config(prev, env_info)
    for k in ("claude_version", "codex_version"):  # keep versions recorded by earlier runs
        env_info[k] = env_info[k] or prev.get(k)
    runs = prev.get("invocations", [])
    env_info["invocations"] = runs + [{"conds": conds, "reps": reps, "only": only,
                                       "task": task, "at": env_info["started_utc"]}]
    env_path.write_text(json.dumps(env_info, indent=2))
    jobs = schedule(work, reps, conds)
    if task:
        jobs = [j for j in jobs if j["task"] == task]
    if only:
        jobs = [j for j in jobs if f"{j['task']}:{j['corpus']}:{j['qid']}" == only]
        if not jobs:
            raise SystemExit(f"--only {only!r} matches nothing")
    jobs = V1.skip_internal(jobs)
    done = completed_sessions(out / "sessions")  # failed sessions are retried
    base_env = {**os.environ, "PATH": f"{work / 'bin'}:{os.environ['PATH']}"}
    for job in jobs:
        sid = f"{job['agent']}-{job['task']}-{job['qid']}-r{job['rep']}-{job['cond']}"
        if sid in done:
            continue
        cwd = corpus_dir(work, job["stem"], job["agent"], job["cond"])
        hook_log = out / "sessions" / f"{sid}.hooks.tsv"
        hook_log.unlink(missing_ok=True)
        env = {**base_env, "GT_HOOK_LOG": str(hook_log)}
        cmd = (claude_cmd(job["prompt"], wrapper) if job["agent"] == "claude"
               else V1.codex_cmd(job["prompt"], cwd))
        s = V1.Session(sid, job["agent"], job["corpus"], job["qid"], job["cond"], job["rep"],
                       job["order"], rubric_total=len(job["rubric"]))
        t0 = time.time()
        returncode = None
        try:
            p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,  # noqa: S603
                               stdin=subprocess.DEVNULL, timeout=900, check=False)
            lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
            returncode = p.returncode
            (V1.parse_claude if job["agent"] == "claude" else V1.parse_codex)(lines, s)
            finalize_status(s, job["agent"], lines, returncode, p.stderr)
        except subprocess.TimeoutExpired:
            lines, s.ok, s.error = [], False, "timeout"
        s.wall_s = round(time.time() - t0, 2)
        s.rubric_hits = V1.score(s.answer, job["rubric"])
        hooks = hook_stats(hook_log)
        (out / "sessions" / f"{sid}.trace.jsonl").write_text(
            V1.sanitize("\n".join(lines), work) + "\n")
        rec = (asdict(s) | {"total_tokens": s.total_tokens, "task": job["task"],
                            "returncode": returncode, **hooks, **graph_use(lines, job["agent"])})
        (out / "sessions" / f"{sid}.json").write_text(
            V1.sanitize(json.dumps(rec, indent=2), work))
        if hook_log.exists():
            hook_log.write_text(V1.sanitize(hook_log.read_text(), work))
        print(f"{sid:44} ok={s.ok} tok={s.total_tokens:>7} gfy={s.graphify_calls} "
              f"hooks={hooks['hook_fires']}/{hooks['hook_denials']} "
              f"rubric={s.rubric_hits}/{s.rubric_total} {s.wall_s:.0f}s", flush=True)


_RESUME_KEYS = ("claude_version", "codex_version", "claude_model", "codex_model",
                "graphify_version", "prepare", "inputs_sha256")


def check_resume_config(prev: dict, new: dict) -> None:
    """Refuse to add sessions to a directory recorded with another configuration (CLI
    versions, models, graphify version or prepared corpora/rubrics)."""
    diff = [k for k in _RESUME_KEYS
            if prev.get(k) is not None and new.get(k) is not None and prev[k] != new[k]]
    if diff:
        raise SystemExit(f"--out was recorded with a different configuration ({', '.join(diff)});"
                         " use a new output directory")


def completed_sessions(sessions: Path) -> set[str]:
    """Session ids to skip on resume: only successful completions."""
    return {p.stem for p in sessions.glob("*.json")
            if not p.name.endswith(".trace.json") and json.loads(p.read_text()).get("ok")}


def trace_errors(agent: str, lines: list[str]) -> list[str]:
    """Error signals inside an agent's event stream (independent of the exit code)."""
    errs = []
    for line in lines:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if agent == "claude" and e.get("type") == "result" and (
                e.get("is_error") or e.get("subtype") not in (None, "success")):
            errs.append(f"result {e.get('subtype')}")
        elif agent == "codex" and e.get("type") in ("turn.failed", "error"):
            errs.append(str(e)[:200])
    return errs


def finalize_status(s: V1.Session, agent: str, lines: list[str], returncode: int | None,
                    stderr: str | None = "") -> None:
    """A session counts as a successful completion only if the agent reached its success
    terminal event, reported no terminal error event, and the process exited 0."""
    errs = trace_errors(agent, lines)
    if returncode not in (0, None) or errs:
        s.ok = False
    if not s.ok and not s.error:
        why = errs[0] if errs else f"exit {returncode}" if returncode else "no result event"
        s.error = (why + " " + (stderr or "")[-250:]).strip()


_GFY_SUB = re.compile(r"graphify(?:-safe\.sh)?['\"]?\s+(query|path|explain|section)\b")
_SHELL_WRAP = re.compile(r"^\s*/bin/(?:ba|z)?sh\s+-l?c\s+(['\"])(.*)\1\s*$", re.S)


def _inner_command(cmd: str) -> str:
    m = _SHELL_WRAP.match(cmd)  # Codex runs commands as `/bin/zsh -lc '...'`
    return m[2] if m else cmd


_GFY_ONLY_SEGMENT = re.compile(
    r"^(?:(?:if|then|else)\s+)?(?:\S*graphify(?:-safe\.sh)?\b|head\b|tail\b|echo\b|fi$|"
    r"which\s+graphify\b|test\s+-[fe]\s+graphify-out\b|\[\s+-[fe]\s+graphify-out\b)")


def is_compound(cmd: str) -> bool:
    """True if the shell command also runs something other than graphify, so its output
    cannot be attributed to graphify alone. Pipes into `head`/`tail`, `echo` separators,
    `which graphify` and the `if test -f graphify-out/graph.json; then ...; fi` guard that
    graphify's own instructions suggest do not count. Quoted strings are ignored."""
    bare = re.sub(r'"(?:\\.|[^"\\])*"|\'[^\']*\'', '""', _inner_command(cmd))
    if re.search(r"\$\(|`", bare):
        return True
    segments = [x.strip() for x in re.split(r"&&|\|\||;|\||\n", bare) if x.strip()]
    return not all(_GFY_ONLY_SEGMENT.match(x) for x in segments)


def graph_use(lines: list[str], agent: str) -> dict:
    """graphify use in one session, from the agent's own event stream.

    A *graphify command* is a shell command containing `graphify[-safe.sh] query|path|
    explain|section`. It is *successful* if it exited 0 (Codex `exit_code`) or its tool
    result is not an error (Claude). Output characters are summed only over successful
    commands that are NOT compound (`&&`, `;`, pipes...), since a compound command's
    output cannot be attributed to graphify alone; compound ones are counted separately."""
    cmds: dict[str, str] = {}
    res = {"gfy_attempted": 0, "gfy_succeeded": 0, "gfy_compound": 0,
           "gfy_invocations": 0, "gfy_output_chars": 0}

    def add(cmd: str, ok: bool, out_len: int) -> None:
        k = len(_GFY_SUB.findall(cmd))
        if not k:
            return
        res["gfy_attempted"] += 1
        res["gfy_invocations"] += k
        compound = is_compound(cmd)
        res["gfy_compound"] += compound
        if ok:
            res["gfy_succeeded"] += 1
            if not compound:
                res["gfy_output_chars"] += out_len

    for line in lines:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if agent == "claude":
            msg = e.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            if e.get("type") == "assistant":
                for c in content:
                    if c.get("type") == "tool_use" and c.get("name") == "Bash":
                        cmds[c["id"]] = (c.get("input") or {}).get("command", "")
            elif e.get("type") == "user":
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result" and \
                            c.get("tool_use_id") in cmds:
                        add(cmds.pop(c["tool_use_id"]), not c.get("is_error"),
                            len(json.dumps(c.get("content"))))
        else:
            it = e.get("item") or {}
            if e.get("type") == "item.completed" and it.get("type") == "command_execution":
                add(it.get("command", ""), it.get("exit_code") == 0,
                    len(it.get("aggregated_output") or ""))
    for cmd in cmds.values():  # tool_use without a result: attempted, not successful
        add(cmd, False, 0)
    return res


def hook_stats(log: Path) -> dict:
    fires = denials = nudges = 0
    if log.exists():
        for line in log.read_text().splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            fires += 1
            decision = " ".join(parts[3:]).lower()
            if (parts[2] == "2" or '"deny"' in decision
                    or "permissiondecision\": \"deny" in decision):
                denials += 1
            elif decision.strip():
                nudges += 1
    return {"hook_fires": fires, "hook_denials": denials, "hook_nudges": nudges}


# --------------------------------------------------------------------------- analyze

def _med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _with_trace_fields(r: dict, p: Path) -> dict:
    """Fresh runs (and v1 records) may lack the graph-use and tool-set fields that the
    publication pipeline adds: derive them from the session's trace."""
    if "gfy_attempted" in r and "other_tools" in r:
        return r
    trace = p.with_name(p.stem + ".trace.jsonl")
    lines = [ln for ln in trace.read_text().splitlines() if ln.strip()] if trace.exists() else []
    gu = graph_use(lines, r["agent"])
    init = next((json.loads(ln) for ln in lines if '"subtype": "init"' in ln
                 or '"subtype":"init"' in ln), {})
    other = init.get("_other_tools")
    if other is None and "tools" in init:
        other = sum(1 for t in init["tools"] if t not in _PUBLIC_TOOLS)
    return {**gu, "other_tools": other if r["agent"] == "claude" else None, **r}


def load_rows(out: Path, v1: Path | None = None) -> list[dict]:
    """All session records of a v2 run, plus (optionally) v1's task-A sessions."""
    rows = []
    for p in sorted((out / "sessions").glob("*.json")):
        r = _with_trace_fields(json.loads(p.read_text()), p)
        r["source"] = "v2"
        rows.append(r)
    if v1:  # task A base + rules-generic sessions from v1 (same versions, previous day)
        for p in sorted((v1 / "sessions").glob("*.json")):
            r = _with_trace_fields(json.loads(p.read_text()), p)
            r["task"] = "A"
            r["cond"] = {"graph": "rules-generic"}.get(r["cond"], r["cond"])
            r["source"] = "v1"
            rows.append(r)
    return rows


def analyze(out: Path, v1: Path | None = None) -> dict:
    rows = load_rows(out, v1)
    groups: dict = {}
    for r in rows:
        key = (r.get("task", "A"), r["agent"], r["corpus"], r["cond"], r.get("source", "v2"))
        groups.setdefault(key, []).append(r)
    summary = []
    for (task, agent, corpus, cond, source), rs in sorted(groups.items()):
        ok = [r for r in rs if r["ok"]]
        summary.append({
            "task": task, "agent": agent, "corpus": corpus, "cond": cond, "source": source,
            "n": len(rs), "ok": len(ok),
            "median_total_tokens": _med([r["total_tokens"] for r in ok]),
            "median_uncached_in_plus_out": _med([r["input_tokens"] + r["output_tokens"]
                                                 for r in ok]),
            "median_cost_usd": _med([r.get("cost_usd") for r in ok]),
            "median_tool_calls": _med([r["tool_calls"] for r in ok]),
            "graphify_adoption": (sum(1 for r in ok if r.get("gfy_succeeded")) / len(ok))
            if ok else None,
            "graphify_attempted": (sum(1 for r in ok if r.get("gfy_attempted")) / len(ok))
            if ok else None,
            "median_hook_fires": _med([r.get("hook_fires") for r in ok]),
            "sessions_with_denials": sum(1 for r in ok if r.get("hook_denials")),
            "mean_rubric_score": statistics.mean(r["rubric_hits"] / r["rubric_total"]
                                                 for r in ok) if ok else None,
        })
    res = {"summary": summary, "sessions": len(rows),
           "failed": [r["id"] for r in rows if not r["ok"]]}
    (out / "results_v2.json").write_text(json.dumps(res, indent=2))
    return res


# --------------------------------------------------------------------------- report

COND_ORDER = ["base", "rules-generic", "rules-official", "hooks", "strict"]


def _fmt(v, kind: str = "int") -> str:
    if v is None:
        return "—"
    if kind == "pct":
        return f"{v:.0%}"
    if kind == "delta":
        return f"{v:+.0%}"
    if kind == "usd":
        return f"{v:.3f}"
    if kind == "x":
        return f"{v:.2f}×"
    return f"{v:,.0f}" if isinstance(v, float) and v >= 100 else (
        f"{v:,.1f}".rstrip("0").rstrip(".") if isinstance(v, float) else f"{v:,}")


def _geomean(xs: list[float]) -> float:
    return statistics.geometric_mean(xs)


def _ok(rows: list[dict], **kw) -> list[dict]:
    return [r for r in rows if r["ok"] and all(r.get(k) == v for k, v in kw.items())]


def comparisons(rows: list[dict], same_run_base: bool = False) -> list[dict]:
    """Every graphify condition vs base, per (task, agent, corpus).

    By default the base for task A pools v1 (3 reps) and v2 (1 rep) sessions: same harness,
    versions and corpora. With `same_run_base=True` each condition is compared only with the
    base sessions of its own run (v2 conditions vs v2 base, 1 rep per question for task A;
    rules-generic vs v1 base), as a sensitivity check. Two effect sizes are reported:
      * delta of the group medians (all questions pooled), and
      * the per-question ratio cond/base of median tokens, summarised by its geometric mean
        and by how many questions got cheaper (paired by question, robust to question mix).
    """
    out = []
    keys = sorted({(r.get("task", "A"), r["agent"], r["corpus"]) for r in rows})
    for task, agent, corpus in keys:
        all_base = _ok(rows, task=task, agent=agent, corpus=corpus, cond="base")
        conds = [c for c in COND_ORDER
                 if c != "base" and _ok(rows, task=task, agent=agent, corpus=corpus, cond=c)]
        for cond in conds:
            cs = _ok(rows, task=task, agent=agent, corpus=corpus, cond=cond)
            base = all_base
            if same_run_base:  # sensitivity: only the base sessions of the condition's run
                src = cs[0].get("source", "v2")
                base = [r for r in all_base if r.get("source", "v2") == src]
            if not base:
                continue
            b_med = _med([r["total_tokens"] for r in base])
            b_rub = statistics.mean(r["rubric_hits"] / r["rubric_total"] for r in base)
            c_med = _med([r["total_tokens"] for r in cs])
            ratios = []
            for q in sorted({r["question"] for r in cs}):
                bq = _med([r["total_tokens"] for r in base if r["question"] == q])
                cq = _med([r["total_tokens"] for r in cs if r["question"] == q])
                if bq and cq:
                    ratios.append(cq / bq)
            out.append({
                "task": task, "agent": agent, "corpus": corpus, "cond": cond,
                "source": cs[0].get("source", "v2"),
                "n_base": len(base), "n": len(cs),
                "base_median_tokens": b_med, "median_tokens": c_med,
                "delta_median": c_med / b_med - 1,
                "q_ratio_geomean": _geomean(ratios) if ratios else None,
                "q_ratio_min": min(ratios) if ratios else None,
                "q_ratio_max": max(ratios) if ratios else None,
                "q_cheaper": sum(1 for x in ratios if x < 1), "q_total": len(ratios),
                "base_rubric": b_rub,
                "rubric": statistics.mean(r["rubric_hits"] / r["rubric_total"] for r in cs),
            })
    return out


def drift(rows: list[dict]) -> list[dict]:
    """Task A base: v1 (2026-11-17) vs v2 (2026-11-18) medians, same questions."""
    out = []
    for agent in ("claude", "codex"):
        for corpus in V1.CORPORA:
            v1 = _ok(rows, task="A", agent=agent, corpus=corpus, cond="base", source="v1")
            v2 = _ok(rows, task="A", agent=agent, corpus=corpus, cond="base", source="v2")
            if v1 and v2:
                m1 = _med([r["total_tokens"] for r in v1])
                m2 = _med([r["total_tokens"] for r in v2])
                out.append({"agent": agent, "corpus": corpus, "n_v1": len(v1), "n_v2": len(v2),
                            "v1": m1, "v2": m2, "delta": m2 / m1 - 1})
    return out


def graph_use_table(rows: list[dict]) -> list[dict]:
    """graphify use per cell (task, agent, corpus, condition, run), over successful sessions.

    Denominator = successful session completions of the cell. `output_chars` is the median,
    over sessions with at least one successful non-compound graphify command, of the
    characters those commands returned (measured on the published traces)."""
    groups: dict = {}
    for r in rows:
        if r["ok"] and r["cond"] != "base":
            groups.setdefault((r.get("task", "A"), r["agent"], r["corpus"], r["cond"],
                               r.get("source", "v2")), []).append(r)
    order = {c: i for i, c in enumerate(COND_ORDER)}
    out = []
    for (task, agent, corpus, cond, source), rs in sorted(
            groups.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2], order[kv[0][3]])):
        with_out = [r["gfy_output_chars"] for r in rs if r.get("gfy_output_chars")]
        out.append({
            "task": task, "agent": agent, "corpus": corpus, "cond": cond, "source": source,
            "n": len(rs),
            "attempted": sum(1 for r in rs if r.get("gfy_attempted")),
            "succeeded": sum(1 for r in rs if r.get("gfy_succeeded")),
            "median_succeeded_cmds": _med([r.get("gfy_succeeded", 0) for r in rs]),
            "compound_cmds": sum(r.get("gfy_compound", 0) for r in rs),
            "n_with_output": len(with_out),
            "median_output_chars": _med(with_out),
        })
    return out


def base_graph_use(rows: list[dict]) -> list[str]:
    """Base sessions that nevertheless ran a graphify command (should be none)."""
    return [r["id"] for r in rows if r["cond"] == "base" and r.get("gfy_attempted")]


def tool_set_section(rows: list[dict]) -> list[str]:
    """Claude's init event lists the tools available in the session. The operator's
    installation exposed two different tool sets during the runs; this section shows how
    they are spread over the conditions and repeats the comparison within the most common
    tool set only."""
    cl = [r for r in rows if r["agent"] == "claude" and r["ok"]]
    sizes = sorted({r.get("other_tools") for r in cl if r.get("other_tools") is not None})
    if len(sizes) < 2:
        return []
    counts: dict = {}
    for r in cl:
        counts[r.get("other_tools")] = counts.get(r.get("other_tools"), 0) + 1
    mode = max(counts, key=counts.get)
    L = ["### Sensitivity: Claude sessions in the most common tool-count subset", "",
         "Claude's `init` event lists the tools available to the session. Besides the "
         "experiment's tools, the operator's installation exposed either "
         + " or ".join(f"**{n}**" for n in sizes) + " other tools (any tool outside the experiment's "
         "named set: plugins, connectors and other CLI features; names redacted), and the two tool sets are **not balanced across conditions**:", "",
         "| Task | Corpus | Condition | Run | " + " | ".join(f"{n} other tools" for n in sizes)
         + " |", "|---|---|---|---|" + "---|" * len(sizes)]
    order = {c: i for i, c in enumerate(COND_ORDER)}
    cells: dict = {}
    for r in cl:
        k = (r.get("task", "A"), r["corpus"], r["cond"], r.get("source", "v2"))
        cells.setdefault(k, {}).setdefault(r.get("other_tools"), 0)
        cells[k][r.get("other_tools")] += 1
    for k in sorted(cells, key=lambda k: (k[0], k[1], order.get(k[2], 9), k[3])):
        L.append(f"| {k[0]} | {k[1]} | {k[2]} | {k[3]} | "
                 + " | ".join(str(cells[k].get(n, 0)) for n in sizes) + " |")
    same = [r for r in rows if r["agent"] != "claude" or r.get("other_tools") == mode]
    full = {(c["task"], c["corpus"], c["cond"]): c for c in comparisons(rows)
            if c["agent"] == "claude"}
    L += ["", f"The Claude comparison restricted to the **{mode}-extra-tool subset** (the most "
          "common count; pooled base as in the headline). Equal counts do not prove identical "
          "tools, and the filter changes which repetitions enter each median:", "",
          "| Task | Corpus | Condition | Base n | n | vs base (medians) | Per-question ratio, "
          "geo-mean | Headline delta |", "|---|---|---|---|---|---|---|---|"]
    flips = []
    for c in comparisons(same):
        if c["agent"] != "claude":
            continue
        h = full.get((c["task"], c["corpus"], c["cond"]))
        if h and (h["delta_median"] > 0) != (c["delta_median"] > 0):
            flips.append(f"{c['task']} {c['corpus']} {c['cond']}")
        L.append(f"| {c['task']} | {c['corpus']} | {c['cond']} | {c['n_base']} | {c['n']} | "
                 f"{_fmt(c['delta_median'], 'delta')} | {_fmt(c['q_ratio_geomean'], 'x')} | "
                 f"{_fmt(h['delta_median'], 'delta') if h else '—'} |")
    L += ["", ("In this subset every Claude comparison keeps the sign of the headline; "
               if not flips else f"In this subset the sign changes for: {', '.join(flips)}; ")
          + "the sizes change. The tool-count imbalance remains a possible confound.", ""]
    return L


def _hhmm(ts: str) -> str:
    return ts[11:16] + " UTC" if ts else "?"


def reps_of(rows: list[dict], cond: str) -> dict:
    """{(source, task): repetitions} for a condition (max sessions per agent and question)."""
    counts: dict = {}
    for r in rows:
        if r["cond"] == cond:
            k = (r.get("source", "v2"), r.get("task", "A"), r["agent"], r["question"])
            counts[k] = counts.get(k, 0) + 1
    out: dict = {}
    for (src, task, _, _), n in counts.items():
        out[(src, task)] = max(out.get((src, task), 0), n)
    return out


def _reps_text(rows: list[dict], cond: str) -> str:
    by = reps_of(rows, cond)
    parts = []
    for src in sorted({s for s, _ in by}):
        tasks = ", ".join(f"{t} {by[(src, t)]} rep{'s' if by[(src, t)] > 1 else ''}"
                          for t in sorted(t for s2, t in by if s2 == src))
        parts.append(f"{src} ({tasks})")
    return " + ".join(parts) or "—"


def _interleaved(inv: list[dict]) -> bool | None:
    """True if every v2 invocation ran base together with graphify conditions."""
    if not inv:
        return None
    has = [(any("base" in cs for cs in x.get("conds", {}).values()),
            any(c != "base" for cs in x.get("conds", {}).values() for c in cs)) for x in inv]
    return all(b and g for b, g in has)


def _inv_label(x: dict) -> str:
    conds = "; ".join(f"{a}: {', '.join(cs)}" for a, cs in x.get("conds", {}).items())
    task = f"task {x['task']}" if x.get("task") else "tasks A and B"
    only = f", only {x['only']}" if x.get("only") else ""
    reps = x.get("reps")
    return f"{task}{only}, {reps} rep{'' if reps == 1 else 's'}, {conds}"


def render_report(rows: list[dict], res: dict, env_v1: dict, env_v2: dict,
                  models: dict | None = None, paths: dict | None = None,
                  pilot_ok: bool = False, windows: list[dict] | None = None,
                  pilot_windows: list[dict] | None = None,
                  pilot_restarted: bool = False) -> str:
    models = models or {}
    paths = paths or {"v1": "runs/2026-11-17", "v2": "runs/2026-11-18", "pilot": None}
    cmp_ = comparisons(rows)
    sens = {(c["task"], c["agent"], c["corpus"], c["cond"]): c
            for c in comparisons(rows, same_run_base=True)}
    n_ok = res["sessions"] - len(res["failed"])
    inv = env_v2.get("invocations", [])
    interleaved = _interleaved(inv)
    base_reps = reps_of(rows, "base")
    a_v2 = base_reps.get(("v2", "A"))
    a_v2_n = len(_ok(rows, task="A", cond="base", source="v2"))
    t_graph = inv[0]["at"] if inv else ""
    L = [
        "# graphify-tokens: report",
        "",
        "> Generated by `python3 experiment_v2.py report` from the session files in "
        f"`{paths['v1']}` (v1) and `{paths['v2']}` (v2). Do not edit by hand.",
        "",
        f"**{res['sessions']} session records, {n_ok} successful session completions "
        f"({len(res['failed'])} failed).** "
        f"Claude Code `{(env_v2.get('claude_version') or 'n/a').split()[0]}` "
        f"(model alias `{env_v2['claude_model']}`, model id reported in the traces: "
        f"{', '.join(f'`{m}`' for m in sorted(models.get('claude', []))) or 'n/a'}), "
        f"Codex CLI `{env_v2.get('codex_version') or 'n/a'}` (model: {env_v2['codex_model']}; "
        "this CLI "
        "version does not report the model id in its JSON events, so it is not recorded), "
        f"`{env_v2['graphify_version']}` (open-source package `graphifyy`).",
        "",
        "## Headline",
        "",
        "Median total tokens per session, and change versus the same agent with no graphify "
        "(`base`). These are observed token volumes, not a causal decomposition"
        + (": in v2 the base and graphify sessions ran in separate invocations (see *Run "
           "design and order*)" if interleaved is False else "") + ". Rubric = share of "
        "expected key points the answer mentions (mention coverage, not correctness).",
        "",
        "| Task | Agent | Corpus | Condition | n | Median tokens | vs base (medians) | "
        "Per-question ratio, geo-mean (range) | Questions cheaper | Rubric, mean coverage (base → cond) |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in cmp_:
        L.append(
            f"| {c['task']} | {c['agent']} | {c['corpus']} | {c['cond']} | {c['n']} | "
            f"{_fmt(c['median_tokens'])} (base {_fmt(c['base_median_tokens'])}, "
            f"n={c['n_base']}) | {_fmt(c['delta_median'], 'delta')} | "
            f"{_fmt(c['q_ratio_geomean'], 'x')} ({_fmt(c['q_ratio_min'], 'x')}–"
            f"{_fmt(c['q_ratio_max'], 'x')}) | {c['q_cheaper']}/{c['q_total']} | "
            f"{_fmt(c['base_rubric'], 'pct')} → {_fmt(c['rubric'], 'pct')} |")
    L += [
        "",
        "Reading the ratio column: for each question, the median tokens of the condition "
        "divided by the median tokens of base on the same question; the column is the "
        "**geometric mean of these per-question median ratios** (`1.20×` = 20 % more), and "
        "the range is the cheapest and the most expensive question. The `base` rows pool all "
        f"base sessions of the same task, agent and corpus ({_reps_text(rows, 'base')}).",
        "",
        "### Sensitivity: each condition against the base of its own run only",
        "",
        "Same comparison, but v2 conditions are compared only with the v2 base (task A: "
        f"**{a_v2 or 0} rep{'' if a_v2 == 1 else 's'} per question**, {a_v2_n} sessions over "
        "both agents) and "
        "`rules-generic` only with the v1 base. "
        "Task B has a single baseline (v2 only), so its rows repeat the headline: there is "
        "no alternative-baseline check for task B (the tool-count check below does cover it).",
        "",
        "| Task | Agent | Corpus | Condition | Base n | vs base (medians) | Per-question ratio, "
        "geo-mean (range) | Pooled-base delta (headline) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for c in cmp_:
        s_ = sens.get((c["task"], c["agent"], c["corpus"], c["cond"]))
        if not s_:
            continue
        L.append(
            f"| {c['task']} | {c['agent']} | {c['corpus']} | {c['cond']} | {s_['n_base']} | "
            f"{_fmt(s_['delta_median'], 'delta')} | {_fmt(s_['q_ratio_geomean'], 'x')} "
            f"({_fmt(s_['q_ratio_min'], 'x')}–{_fmt(s_['q_ratio_max'], 'x')}) | "
            f"{_fmt(c['delta_median'], 'delta')} |")
    L += [
        "",
        "With few base repetitions per question the same-run deltas are noisy; effects that "
        "shrink or flip between the two tables should be treated as unresolved.",
        "",
    ]
    L += tool_set_section(rows)
    L += [
        "## Did the agents use the graph?",
        "",
        "Per cell, over successful sessions (`n`). A **graphify command** is a shell command "
        "containing one of the retrieval subcommands `graphify[-safe.sh] query|path|explain|"
        "section`; other subcommands (e.g. `get_node`, which the wrapper blocks) are not "
        "counted. *Attempted* = sessions "
        "with at least one; *succeeded* = sessions with at least one that exited 0 (Codex) "
        "or returned a non-error tool result (Claude). *Mixed* commands also run something "
        "other than graphify (e.g. `graphify query … ; rg …`), so their output cannot be "
        "attributed to graphify alone: they count as calls but are excluded from the output "
        "size (pipes into `head`/`tail`, `echo` separators, `which graphify` and the `if "
        "test -f graphify-out/graph.json` guard are not mixed, so their few extra characters "
        "are included). *Output chars* = median, over the sessions with **non-empty recorded "
        "output** from successful graphify-only commands (`n out`), of the characters those "
        "commands returned (summed per session), measured on the published traces.",
        "",
        "| Task | Agent | Corpus | Condition | Run | n | Attempted | Succeeded | Median "
        "successful cmds | Mixed cmds | n out | Median output chars |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for g in graph_use_table(rows):
        L.append(
            f"| {g['task']} | {g['agent']} | {g['corpus']} | {g['cond']} | {g['source']} | "
            f"{g['n']} | {g['attempted']} | {g['succeeded']} | "
            f"{_fmt(g['median_succeeded_cmds'])} | {g['compound_cmds']} | "
            f"{g['n_with_output']} | {_fmt(g['median_output_chars'])} |")
    stray = base_graph_use(rows)
    L += ["", f"Base sessions with a graphify command: {len(stray)}"
          + (f" ({', '.join(f'`{x}`' for x in stray)})" if stray else "") + ".", ""]
    L += [
        "The traces show graph queries and raw file reads side by side; this analysis does "
        "not establish which files each condition read, so it does not attribute the token "
        "increase to the graph output alone.",
        "",
        "## Methodology",
        "",
        "### Corpora (pinned)",
        "",
        "| Corpus | Commit | Python files | Lines | Graph nodes / edges |",
        "|---|---|---|---|---|",
    ]
    for name, c in env_v1["prepare"].items():
        g = c.get("graph") or {}
        label = (f"[{name}]({c['repo'].removesuffix('.git')}/tree/{c['ref']})"
                 if V1.is_public(name) else f"{name} (internal, not public)")
        L.append(f"| {label} | "
                 f"`{c['ref'][:12]}` | {c['py_files']} | {c['py_lines']:,} | "
                 f"{g.get('nodes', '—')} / {g.get('edges', '—')} |")
    L += [
        "",
        "The anvil corpus is " + V1.INTERNAL_NOTE + ": its sessions are kept here for "
        "transparency, but they cannot be reproduced publicly, and the harness skips it. "
        "The httpx benchmark (task A on httpx and all of task B) can be rerun from public, "
        "pinned sources; a fresh run can measure different numbers (model alias, Codex CLI "
        "default model, operator settings). Every published number, anvil included, can be "
        "recomputed from the checked-in traces.",
        "",
        "### Tasks",
        "",
        "- **A · understand** (v1 questions, unchanged): 3 questions on anvil and 5 on httpx, "
        "each with a regex rubric of 5–6 key points checked to exist at the pinned commit. "
        "See `QUESTIONS` in `experiment.py`.",
        "- **B · review**: review 4 real merged httpx pull requests. The corpus is checked out "
        "at the PR head (with `.git`, so `git diff <base>..HEAD` works); the graph is built on "
        "the PR's **base** commit, following graphify's PR-impact guide (\"Run `graphify "
        "update .` on the base branch first\"; we did not use `graphify prs` itself). The "
        "rubric "
        "(**5–8 items**: 8 for three PRs, 5 for #3389) is derived statically from the diff, "
        "not from the graph: **up to 3** changed Python files under `httpx/`, **up to 3** "
        "touched symbols (functions or classes whose definition span, body included, contains "
        "a changed line; AST; first three alphabetically), and **up to 3** other package "
        "files that mention the most distinct touched symbols as whole words (`git grep -lw`, "
        "one count per file and symbol; not a call-graph analysis). "
        "It measures whether the review mentions these, not whether it finds bugs or is "
        "correct.",
        "",
        "| PR | Title | Base → head |",
        "|---|---|---|",
    ]
    for pid, pr in env_v2["prepare"]["task_b"].items():
        L.append(f"| [#{pid.removeprefix('pr')}]({pr['url']}) | {pr['title']} | "
                 f"`{pr['base'][:9]}` → `{pr['head'][:9]}` |")
    L += [
        "",
        "### Conditions",
        "",
        "| Condition | Agents | Graph (`graphify-out/`) | Instructions | Hooks | Run |",
        "|---|---|---|---|---|---|",
        f"| `base` | Claude, Codex | — | none | — | {_reps_text(rows, 'base')} |",
        "| `rules-generic` | Claude, Codex | yes | graphify's generic rules text, in "
        f"`CLAUDE.md` and `AGENTS.md` | — | {_reps_text(rows, 'rules-generic')} |",
        "| `rules-official` | Claude, Codex | yes | exact text written by "
        "`graphify claude install` (`CLAUDE.md`) / `graphify codex install` (`AGENTS.md`) | "
        "Claude: removed. Codex: `.codex/hooks.json` kept as installed (`hook-check`, a "
        "documented no-op; it points at the real graphify binary) | "
        f"{_reps_text(rows, 'rules-official')} |",
        "| `hooks` | Claude | yes | as `rules-official` | full `graphify claude install`: "
        "PreToolUse `hook-guard search` (Bash/Grep) and `read` (Read/Glob), SessionStart "
        f"`compact` | {_reps_text(rows, 'hooks')} |",
        "| `strict` | Claude | yes | as `rules-official` | as `hooks`, with "
        "`GRAPHIFY_HOOK_STRICT=1` (graphify's strict policy: the first *eligible* raw Read/Grep is "
        "denied as a nudge, at most once per session, when there is no recent graph retrieval, "
        "and reissuing it is allowed. \"Recent\" is a checkout-wide timestamp, fresh for 30 minutes by default, and "
        "each strict checkout is reused across repetitions, so an earlier session's retrieval "
        "can suppress a later session's denial; eligibility and graph staleness are also "
        "checked. The hooks match only the tools "
        f"listed above) | {_reps_text(rows, 'strict')} |",
        "",
        "[`conditions/`](conditions/) holds reference exports: the instruction files exactly "
        "as the installers wrote them (v1's generic text with the wrapper path filled in), "
        "and the Claude hook settings **as instrumented** (commands rewritten to go through "
        "`hookwrap.sh`) with local paths replaced by placeholders. Graphs were built with "
        "`graphify update .` (AST only, **no LLM**).",
        "",
        "### Run design and order",
        "",
        (f"- **v1** (`{paths['v1']}`): two invocations into the same directory: first "
        "`--reps 1` (rep 0), then `--reps 3`, which skipped the completed sessions and ran "
        "reps 1-2 (the recorded order indices match `--reps 1` for rep 0 and `--reps 3` for "
        "reps 1-2). `base` and `rules-generic` alternate within each question-rep pair "
        f"(`environment.json` records the last start, {_hhmm(env_v1.get('started_utc'))})."
         if paths['v1'] == 'runs/2026-11-17' else
         f"- **v1** (`{paths['v1']}`): see its `environment.json` (v1 records only the last "
         f"start, {_hhmm(env_v1.get('started_utc'))}); `base` and `rules-generic` alternate "
         "within each question-rep pair."),
        f"- **v2** (`{paths['v2']}`): {len(inv)} invocation{'' if len(inv) == 1 else 's'} into "
        "the same directory, "
        "from `environment.json`:",
    ]
    for i, x in enumerate(inv):
        L.append(f"  {i + 1}. started {_hhmm(x['at'])}: {_inv_label(x)}.")
    base_inv = [x for x in inv[1:] if any("base" in cs for cs in x.get("conds", {}).values())
                and not any("base" in cs for cs in inv[0].get("conds", {}).values())]
    if base_inv:
        gap = (datetime.fromisoformat(base_inv[0]["at"].replace("Z", "+00:00"))
               - datetime.fromisoformat(t_graph.replace("Z", "+00:00")))
        L.append(f"  The first base invocation started {gap.seconds // 60} minutes after the "
                 "start of the first invocation (not after its task B part). Within an "
                 "invocation, condition order alternates per question and rep; across "
                 "invocations it does not.")
    if windows:
        L.append("- Start times of the v2 Claude sessions (first recorded timestamp in each trace, UTC; Codex "
                 "traces carry no timestamps; pilot copies excluded):")
        for w in windows:
            L.append(f"  - task {w['task']}, {w['kind']}: {w['first'][11:16]}–{w['last'][11:16]} "
                     f"({w['n']} sessions)")
    if paths.get("pilot") and not pilot_ok:
        L.append(f"- A pilot directory (`{paths['pilot']}`) was given, but its session files "
                 "are not byte-identical copies of sessions in the v2 directory.")
    if pilot_ok:
        L.append(
            f"- A pilot (`{paths['pilot']}`, identical code and conditions, run before "
            "invocation 1"
            + ("; its `environment.json` records only its last start, while its Claude traces "
               "start earlier, so it was restarted at least once" if pilot_restarted else "")
            + ") was copied into the v2 "
            "directory, so later invocations skipped those sessions. Its records are "
            "**byte-identical copies** (checked when this report was generated) and are "
            "counted **once** in the totals above. The pilot's *planned* order interleaved "
            "the conditions"
            + (", but its Claude traces show what actually ran: " + "; ".join(
                f"{w['kind']} started {w['first'][11:16]}\u2013{w['last'][11:16]} UTC"
                for w in pilot_windows)
               + ", so in practice the graphify sessions also ran before the base session "
               "there (Codex traces carry no timestamps)" if pilot_windows else "")
            + ".")
    L += [
        "",
        "### Agents and controls (as implemented)",
        "",
        "- Claude Code: `claude -p --output-format stream-json`, model alias `sonnet`, "
        "`--max-budget-usd 1.50`, no session persistence, `--disallowedTools Agent Task "
        "WebSearch WebFetch Write Edit`, `--allowedTools Read Grep Glob` plus Bash patterns "
        "for `graphify`, the wrapper, `ls` and `git diff/log/show`. The allow-list only "
        "pre-approves those tools; it does not forbid others. The operator's global Claude "
        "Code settings also applied, and the traces show Claude running other shell "
        "commands through Bash in every condition (e.g. `grep`, `sed`, `test`; `find` and a "
        "`python3 -c` one-liner in some base sessions).",
        "- Codex: `codex exec --json --ephemeral -s read-only`, CLI default model.",
        "- Both agents inherited the operator's environment and global configuration "
        "(settings, plugins, connectors, a global SessionStart hook with empty output). The "
        "configuration was not changed between conditions, but the set of tools Claude "
        "reported was not constant over the runs (see the tool-set sensitivity check); the "
        "names are redacted from the published traces.",
        "- In v2, `graphify` on the agents' `PATH` is a shim (`work/bin/graphify`) to "
        "`scripts/graphify-safe.sh`, an allow-list wrapper (`update`, `query`, `path`, "
        "`explain`, `section`) that runs graphify with a stripped environment and no LLM "
        "backend. The shim only changes what a bare `graphify` resolves to: the real binary "
        "remained reachable by absolute path, and the Codex `rules-official` hook file "
        "installed by graphify points at it (`hook-check`, a no-op). In v1 the instructions "
        "named the wrapper by its explicit path and the agents kept the caller's `PATH`.",
        "- Claude hooks run graphify's real `hook-guard` through `hookwrap.sh` (unchanged "
        "input/output, minimal environment), which logs each firing to "
        "`sessions/<id>.hooks.tsv`. These are the **instrumented hook firings**; other "
        "hooks (the operator's global SessionStart hook) appear only in the traces.",
        "- Official installers ran inside throwaway corpus copies with a fake `HOME`; the "
        "harness aborts if an installer leaves any file in that fake `HOME`. It does not "
        "monitor other locations.",
        "",
        "### What is measured",
        "",
        "- **Successful session completion**: the agent reached its success terminal event "
        "(Claude `result` without `is_error`; Codex `turn.completed`) and reported no "
        "terminal error event (recoverable events such as Claude's `api_retry` are allowed). "
        "The published runs predate exit-code recording; the harness now also requires exit "
        "code 0 and retries failed sessions on resume.",
        "- **Total tokens** = uncached input + cache read + cache write + output, as reported "
        "by each agent's own event stream. Claude: the per-model token counts of the final "
        "`result` event (`modelUsage`), summed over **all** models the session used (the main "
        "model and small auxiliary calls). The top-level `usage` field is not used; it can "
        "differ from `modelUsage` even for the main model. "
        "Codex: the sum over `turn.completed` events, where `input_tokens` includes the cached "
        "part, so uncached = `input_tokens − cached_input_tokens`.",
        "- **Uncached in+out** = uncached input + output (excludes cache reads and cache "
        "writes).",
        "- **Cost** = Claude's API-equivalent `total_cost_usd`. Not billed: the runs used a "
        "subscription login. Codex reports no cost.",
        "- **graphify used** = share of sessions with at least one *successful* graphify "
        "command (see the graph-use table for attempted vs successful).",
        "- **Hook fires / denials** = instrumented hook firings from the `hookwrap.sh` logs.",
        "- **Rubric** = share of rubric regexes (case-insensitive) found in the final answer.",
        "- `experiment_v2.py sanitize` recomputes the token, status, answer, rubric, tool "
        "and graphify fields of `sessions/*.json` from the **published (redacted) trace** "
        "next to it, and the hook counts from the published `hooks.tsv`; wall time and "
        "identity fields are kept from the original record. Answers are scored as "
        "published (redacted); before publication we checked that tokens, answers' scores "
        "and success flags were identical to those computed from the raw traces.",
        "- The graphify counts are **shell-level proxies** detected with a regex over the "
        "command text (v2 and v1 alike); output is the serialized tool result, which for "
        "`… | head` pipelines is truncated and for `which graphify && …` includes the extra "
        "line. A command counts as successful "
        "if the shell command exited 0 (Codex) or its tool result was not an error (Claude), "
        "even if graphify's own part failed or succeeded inside a mixed command, or graphify "
        "printed a diagnostic such as \"No node matching\" with exit 0. Claude's "
        "character counts are the length of the serialized tool result.",
        "",
        "## All groups",
        "",
        "Medians per session over successful sessions, except *Rubric*, which is the **mean** of "
        "each session's rubric coverage; `source` says which run the sessions come from.",
        "",
        "| Task | Agent | Corpus | Condition | Source | OK/n | Total tokens | Uncached in+out "
        "| Cost $ | Tool calls | graphify used | Hook fires | Sessions with denials | Rubric (mean) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    order = {c: i for i, c in enumerate(COND_ORDER)}
    for r in sorted(res["summary"], key=lambda r: (r["task"], r["agent"], r["corpus"],
                                                    order.get(r["cond"], 9), r["source"])):
        L.append(
            f"| {r['task']} | {r['agent']} | {r['corpus']} | {r['cond']} | {r['source']} | "
            f"{r['ok']}/{r['n']} | {_fmt(r['median_total_tokens'])} | "
            f"{_fmt(r['median_uncached_in_plus_out'])} | {_fmt(r['median_cost_usd'], 'usd')} | "
            f"{_fmt(r['median_tool_calls'])} | {_fmt(r['graphify_adoption'], 'pct')} | "
            f"{_fmt(r['median_hook_fires'])} | {r['sessions_with_denials']} | "
            f"{_fmt(r['mean_rubric_score'], 'pct')} |")
    L += ["", "## Drift check (task A, base, v1 vs v2)", "",
          "Same corpora and CLI versions, one day apart; v1 has 3 reps per question, v2 one. "
          "The Claude launch flags also differ slightly (v2 pre-approves `graphify` and "
          "`git diff/log/show` in addition to v1's wrapper and `ls` patterns). It shows that "
          "differences of this size occur; with these sample sizes it cannot establish a noise "
          "threshold, and it says nothing about the time gap between task B's batches.", "",
          "| Agent | Corpus | v1 median (n) | v2 median (n) | Δ |", "|---|---|---|---|---|"]
    for d in drift(rows):
        L.append(f"| {d['agent']} | {d['corpus']} | {_fmt(d['v1'])} ({d['n_v1']}) | "
                 f"{_fmt(d['v2'])} ({d['n_v2']}) | {_fmt(d['delta'], 'delta')} |")
    gen = next((c for c in cmp_ if c["task"] == "A" and c["agent"] == "codex"
                and c["corpus"] == "anvil" and c["cond"] == "rules-generic"), None)
    gen_s = sens.get(("A", "codex", "anvil", "rules-generic"))
    L += [
        "",
        "## Limitations",
        "",
        "- **Small samples.** "
        f"{min((r['n'] for r in res['summary']), default=0)}–"
        f"{max((r['n'] for r in res['summary']), default=0)} "
        "sessions per group; repetitions per condition: "
        + "; ".join(f"`{c}` {_reps_text(rows, c)}" for c in COND_ORDER if reps_of(rows, c))
        + ". No significance testing; read the per-question ratios, their range and the "
        "sensitivity tables, not single digits.",
    ]
    if interleaved is False:
        L.append(
            "- **Batch order, not interleaving, in v2.** Base sessions ran in separate "
            "invocations after the graphify conditions"
            + (" (the copied pilot was planned interleaved, but its Claude traces show the "
               "same graphify-then-base order)" if pilot_ok else "")
            + "; see *Run design and order*. Time-of-day or service-side changes between "
            "batches are not ruled out, for task B in particular, which has no independent "
            "sensitivity check.")
    L += [
        "- **Day-to-day drift** (table above) is of the same order as some of the smaller "
        "effects; effects of that size are not resolved by this data. v1 and v2 also differ "
        "slightly in Claude's pre-approved Bash patterns, so the pooled task A baseline mixes "
        "two launch configurations.",
        "- **The operator's normal installation** (global settings, plugins, connectors, a "
        "global SessionStart hook) was loaded in every session. It adds context, its "
        "permissions let Claude run extra shell commands, and the number of tools it exposed "
        "to Claude varied over the runs, unevenly across conditions (see the tool-set "
        "sensitivity check). Another installation may give different numbers.",
        "- **Claude and Codex token totals are not comparable with each other**: different "
        "models, tokenizers, caching and system prompts. Compare conditions within one agent.",
        "- **The rubric is regex mention coverage**, not a correctness judgement; quality "
        "moved by a few points in most cells, but not all: Codex `rules-generic` on anvil "
        + (f"went from {_fmt(gen['base_rubric'], 'pct')} (pooled base) to "
           f"{_fmt(gen['rubric'], 'pct')}" if gen else "dropped")
        + (f" ({_fmt(gen_s['base_rubric'], 'pct')} → {_fmt(gen_s['rubric'], 'pct')} against "
           "its own run's base)" if gen_s else "") + ".",
        "- **Mechanism not established.** The data shows token volumes per condition, graph "
        "queries and raw reads; it does not show which files each condition read or "
        "attribute the difference to the graph output alone.",
        "- **Token volume and API-equivalent cost only**: Claude's cost column is the "
        "API-equivalent figure; runs used subscription logins and nothing was billed.",
        "- **Two Python repos** (1.1k and 17.8k lines). graphify says its gains grow with "
        "repo size; very large repos are not covered.",
        "- **Graphify Cloud is not tested.** The \"80% fewer tokens\" headline in "
        "graphify's README refers to Graphify Cloud, a hosted product. The open-source "
        "README's benchmark measures retrieval quality (key-fact coverage). The open-source "
        "docs also describe `graphify benchmark`, which *estimates* per-query token "
        "reduction against reading the whole corpus naively (its docs show 83.6× on a "
        "code-only httpx graph: https://docs.graphify.com/open-source/features/token-benchmark"
        ") and calls itself \"a guide to scale, not a billing figure\"; this experiment "
        "measures what "
        "the agents actually consumed end to end, which is a different quantity. Sources "
        "are linked in the README.",
        "",
        "## Reproduce",
        "",
        "See [README.md](README.md#reproduce). Rerunning the benchmark covers the public "
        "httpx corpus only (task A on httpx and task B); the anvil corpus is "
        + V1.INTERNAL_NOTE + ", so its task A sessions are published for transparency only. "
        "Regenerate this file from the published sessions (anvil included) with:",
        "",
        "```bash",
        "python3 experiment_v2.py report --out runs/2026-11-18 --v1 runs/2026-11-17 "
        "--pilot runs/pilot-v2",
        "```",
        "",
    ]
    if res["failed"]:
        L += ["## Failed sessions", ""] + [f"- `{x}`" for x in res["failed"]] + [""]
    return "\n".join(L)


def models_seen(*run_dirs: Path) -> dict:
    """Model ids reported by the agents' own traces (Claude's init event)."""
    seen: dict = {"claude": set()}
    for d in run_dirs:
        for p in (d / "sessions").glob("claude-*.trace.jsonl"):
            for line in p.read_text().splitlines():
                if '"subtype": "init"' in line or '"subtype":"init"' in line:
                    m = json.loads(line).get("model")
                    if m:
                        seen["claude"].add(m)
    return seen


def claude_windows(out: Path, pilot: Path | None = None) -> list[dict]:
    """Earliest and latest first-event timestamp of Claude sessions per (task, base or
    graphify), excluding sessions copied from a pilot. Codex traces carry no timestamps."""
    skip = {p.name for p in (pilot / "sessions").glob("*")} if pilot else set()
    win: dict = {}
    for p in (out / "sessions").glob("claude-*.trace.jsonl"):
        if p.name in skip:
            continue
        m = re.search(r'"timestamp":\s*"([^"]+)"', p.read_text())
        if not m:
            continue
        task = p.name.split("-")[1]
        kind = "base" if p.name.endswith("-base.trace.jsonl") else "graphify"
        win.setdefault((task, kind), []).append(m[1])
    return [{"task": t, "kind": k, "first": min(v), "last": max(v), "n": len(v)}
            for (t, k), v in sorted(win.items())]


def _restarted(run: Path) -> bool:
    """True if some Claude trace of the run starts before its recorded (last) start."""
    started = json.loads((run / "environment.json").read_text()).get("started_utc", "")
    firsts = [w["first"] for w in claude_windows(run)]
    return bool(started and firsts and min(firsts) < started)


def pilot_is_copied(pilot: Path, out: Path) -> bool:
    """True if every pilot session file exists, byte-identical, in the main run."""
    files = [p for p in (pilot / "sessions").glob("*") if p.is_file()]
    return bool(files) and all((out / "sessions" / p.name).is_file()
                               and (out / "sessions" / p.name).read_bytes() == p.read_bytes()
                               for p in files)


def report(out: Path, v1: Path, dest: Path, pilot: Path | None = None) -> str:
    res = analyze(out, v1)
    text = render_report(load_rows(out, v1), res,
                         json.loads((v1 / "environment.json").read_text()),
                         json.loads((out / "environment.json").read_text()),
                         models_seen(out, v1),
                         {"v1": v1.as_posix(), "v2": out.as_posix(),
                          "pilot": pilot.as_posix() if pilot else None},
                         pilot_ok=bool(pilot) and pilot_is_copied(pilot, out),
                         windows=claude_windows(out, pilot),
                         pilot_restarted=bool(pilot) and _restarted(pilot),
                         pilot_windows=claude_windows(pilot) if pilot else None)
    dest.write_text(text)
    return text


# --------------------------------------------------------------------------- publishing

_INIT_KEEP = ("type", "subtype", "cwd", "session_id", "tools", "model", "permissionMode",
              "apiKeySource", "claude_code_version", "uuid")
# Tools the experiment allows or forbids explicitly; every other tool name in the init event
# (the operator's plugins, MCP connectors, extra features) is replaced by a count.
_PUBLIC_TOOLS = {"Bash", "Read", "Grep", "Glob", "Edit", "Write", "Agent", "Task",
                 "WebFetch", "WebSearch"}
_SECRET = re.compile(
    r"\b(?:sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}|nvapi-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}"
    r"|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|xox[abprs]-[A-Za-z0-9-]{10,})")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{12}\b")
_PREFIXED_ID = re.compile(r"\b(msg|req|toolu|srvtoolu)_[A-Za-z0-9]{12,}\b")


class Pseudonymizer:
    """Replace original session/request/message identifiers by stable pseudonyms
    (HMAC-SHA256 with a key kept outside the repo): links inside the data are preserved,
    the original identifiers are not published and cannot be recomputed without the key."""

    def __init__(self, key: bytes):
        self.key = key

    def _h(self, value: str) -> str:
        return hmac.new(self.key, value.encode(), hashlib.sha256).hexdigest()

    def __call__(self, text: str) -> str:
        def uuid_sub(m: re.Match) -> str:
            h = self._h(m[0].lower())
            return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"

        text = _UUID.sub(uuid_sub, text)
        return _PREFIXED_ID.sub(lambda m: f"{m[1]}_{self._h(m[0])[:24]}", text)


def scrub(text: str) -> str:
    """Second-pass redaction for publication (the run already applied V1.sanitize)."""
    text = re.sub(r"/(?:Users|home)/[^/\s\"'\\]+", "~", text)
    # Claude Code's per-project cache of large tool outputs (named after the local path)
    text = re.sub(r"~/\.claude/projects/[^/\s\"'\\]+", "<claude-output-cache>", text)
    text = re.sub(r"-(?:Users|home)-[A-Za-z0-9._]+-[^/\s\"'\\]*", "<local-path>", text)
    text = re.sub(r"~/[^\s\"'\\]*?/\.venv/", "<venv>/", text)
    text = re.sub(r"~/[^\s\"'\\]*?/scripts/graphify-safe\.sh",
                  "<anvil-checkout>/scripts/graphify-safe.sh", text)
    # local installation prefixes (package manager paths reveal the operator's setup)
    text = re.sub(r"/(?:opt/homebrew|usr/local/(?:Cellar|opt|lib/python[\d.]+))"
                  r"[^\s\"'\\]*", "<local-install>", text)
    # any other path in the operator's home, except dotfiles a library documents (~/.netrc)
    text = re.sub(r"~/(?!\.(?:netrc|graphify)\b)[A-Za-z0-9_][^\s\"'\\]*", "<local-path>",
                  text)
    # `ls -l` owner/group columns
    text = re.sub(r"(?m)((?:^|\\n|\n)[dlcbps-][rwxsStT-]{9}[@+.]?\s+\d+\s+)\S+(\s+)\S+",
                  r"\1user\2staff", text)
    user = os.environ.get("USER") or ""
    if len(user) >= 3:  # the operator's login name, wherever else it appears
        text = re.sub(rf"\b{re.escape(user)}\b", "user", text)
    text = re.sub(r"(?<![\w\\.+%-])[A-Za-z0-9._%+-]+@"
                  r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b", "<email>", text)
    # credential-shaped strings (API keys, tokens) and this machine's host name
    text = _SECRET.sub("<secret>", text)
    host = socket.gethostname().split(".")[0]
    if len(host) >= 4:
        text = re.sub(rf"\b{re.escape(host)}\b", "<host>", text, flags=re.I)
    return text


def _drop_signatures(o):
    if isinstance(o, dict):
        return {k: ("<redacted>" if k == "signature" else _drop_signatures(v))
                for k, v in o.items()}
    if isinstance(o, list):
        return [_drop_signatures(v) for v in o]
    return o


def scrub_trace_line(line: str, pseudo: Pseudonymizer | None = None) -> str:
    """Drop operator-specific fields (installed plugins, MCP connectors, plan usage), opaque
    thinking signatures, and pseudonymize identifiers."""
    try:
        e = json.loads(line)
    except json.JSONDecodeError:
        out = scrub(line)
        return pseudo(out) if pseudo else out
    if e.get("type") == "system" and e.get("subtype") == "init":
        tools = e.get("tools") or []
        e = {k: v for k, v in e.items() if k in _INIT_KEEP} | {"_redacted": "operator config"}
        e["tools"] = sorted(t for t in tools if t in _PUBLIC_TOOLS)
        e["_other_tools"] = sum(1 for t in tools if t not in _PUBLIC_TOOLS)
    elif e.get("type") == "rate_limit_event":
        e = {"type": "rate_limit_event", "_redacted": "plan usage"}
    elif e.get("type") == "result":  # account-level settings, not part of the session
        e = {k: v for k, v in e.items()
             if k not in ("fast_mode_state", "fast_mode_disabled_reason")}
    out = scrub(json.dumps(_drop_signatures(e), ensure_ascii=False))
    return pseudo(out) if pseudo else out


def rebuild_record(meta: dict, lines: list[str], hooks_tsv: Path | None,
                   rubric: list[str]) -> dict:
    """Recompute every metric of a session from its trace: tokens, status,
    answer, rubric, graphify use and instrumented hook firings. Identity fields, wall time
    and the recorded error message/exit code are kept from the original record."""
    s = V1.Session(meta["id"], meta["agent"], meta["corpus"], meta["question"], meta["cond"],
                   meta["rep"], meta["order"], wall_s=meta.get("wall_s", 0.0),
                   rubric_total=len(rubric))
    (V1.parse_claude if s.agent == "claude" else V1.parse_codex)(lines, s)
    finalize_status(s, s.agent, lines, meta.get("returncode"))
    if not s.ok:
        s.error = meta.get("error") or s.error
    s.rubric_hits = V1.score(s.answer, rubric)
    gu = graph_use(lines, s.agent)
    gu["other_tools"] = next((json.loads(ln).get("_other_tools") for ln in lines
                              if '"subtype": "init"' in ln), None)
    s.graphify_calls, s.graphify_output_chars = gu["gfy_attempted"], gu["gfy_output_chars"]
    rec = asdict(s) | {"total_tokens": s.total_tokens}
    if "task" in meta:
        rec["task"] = meta["task"]
    if "returncode" in meta:
        rec["returncode"] = meta["returncode"]
    if hooks_tsv is not None:
        rec |= hook_stats(hooks_tsv)
    return rec | gu


def _rubric_for(meta: dict, env: dict) -> list[str]:
    if meta.get("task") == "B":
        return env["prepare"]["task_b"][meta["question"]]["rubric"]
    return {qid: rub for qid, _, rub in V1.QUESTIONS[meta["corpus"]]}[meta["question"]]


_RUNLOG_NOTE = ("# Historical progress output printed while the run was executing. Its token "
                "totals use Claude's main-model `usage` field and an earlier graph-command "
                "detector; the published figures are in sessions/*.json and REPORT.md.\n")

# the only files a run directory may contain (anything else aborts the publication)
_PUBLISHABLE = re.compile(r"^[\w.-]+/(?:environment\.json|PLAN\.md|run\.log|results_v2\.json|"
                          r"sessions/[\w.-]+\.(?:json|trace\.jsonl|hooks\.tsv))$")


def sanitize_runs(raw: Path, runs: Path, key: bytes) -> dict:
    """Rebuild the published runs/ from the raw originals in `raw` (kept outside the repo).

    Every file is redacted and identifiers are pseudonymized; session records are then
    recomputed from the redacted traces, so all published numbers are reproducible from the
    published files.
    Deterministic: the same raw data and key always give the same output."""
    raw, runs = raw.resolve(), runs.resolve()
    if raw == runs or raw.is_relative_to(runs) or runs.is_relative_to(raw):
        raise ValueError("raw and runs directories must not overlap")
    unknown = [p for p in raw.rglob("*") if p.is_file() and not p.name.startswith(".")
               and not _PUBLISHABLE.search(p.relative_to(raw).as_posix())]
    if unknown:
        raise ValueError(f"unexpected files in raw runs (not published): {unknown[:5]}")
    pseudo = Pseudonymizer(key)
    final = runs
    final.parent.mkdir(parents=True, exist_ok=True)
    # a brand-new staging directory: nothing existing is ever deleted before the rebuild
    runs = Path(tempfile.mkdtemp(prefix=f".{final.name}.staging-", dir=final.parent))
    if raw.is_relative_to(runs) or runs.is_relative_to(raw):
        raise ValueError("staging directory overlaps the raw directory")
    written = 0
    envs: dict[Path, dict] = {}
    for src in sorted(raw.rglob("*")):
        if not src.is_file() or src.name.startswith("."):
            continue
        dst = runs / src.relative_to(raw)
        dst.parent.mkdir(parents=True, exist_ok=True)
        text = src.read_text()
        if src.name.endswith(".trace.jsonl"):
            new = "\n".join(scrub_trace_line(ln, pseudo) if ln.strip() else ln
                            for ln in text.split("\n"))
        elif src.parent.name == "sessions" and src.suffix == ".json":
            run_dir = src.parent.parent
            if run_dir not in envs:
                envs[run_dir] = json.loads((run_dir / "environment.json").read_text())
            # retained metadata (id, error message, wall time...) is redacted on its own;
            # trace-derived fields come from the already redacted trace and are not touched again
            meta = json.loads(pseudo(scrub(text)))
            trace = src.with_name(src.stem + ".trace.jsonl")
            # metrics are computed on the PUBLISHED (redacted) trace, so that anyone can
            # recompute every number in the record from the files in this repository
            lines = [scrub_trace_line(ln, pseudo) for ln in trace.read_text().splitlines()
                     if ln.strip()]
            tsv = src.with_name(src.stem + ".hooks.tsv")
            rec = rebuild_record(meta, lines, tsv if tsv.exists() or "task" in meta else None,
                                 _rubric_for(meta, envs[run_dir]))
            new = json.dumps(rec, indent=2)
        elif src.name == "run.log":
            new = (_RUNLOG_NOTE + pseudo(scrub(text)))
        else:
            new = pseudo(scrub(text))
        dst.write_text(new)
        written += 1
    # only replace the previous export once the whole rebuild succeeded
    if final.exists():
        shutil.rmtree(final)
    runs.rename(final)
    return {"written_files": written, "raw": str(raw)}


def export_conditions(work: Path, dest: Path) -> list[str]:
    """Copy the exact instruction/hook files graphify's installers wrote (task A, httpx)."""
    files = {"rules-official-claude/CLAUDE.md": "httpx-rules-official-claude/CLAUDE.md",
             "rules-official-codex/AGENTS.md": "httpx-rules-official-codex/AGENTS.md",
             "rules-official-codex/.codex/hooks.json":
                 "httpx-rules-official-codex/.codex/hooks.json",
             "hooks/CLAUDE.md": "httpx-hooks/CLAUDE.md",
             "hooks/.claude/settings.json": "httpx-hooks/.claude/settings.json",
             "strict/.claude/settings.json": "httpx-strict/.claude/settings.json",
             "rules-generic/CLAUDE.md": "httpx-graph/CLAUDE.md"}
    done = []
    for rel, src in files.items():
        s = (work / src).read_text()
        s = s.replace(str(ROOT), "<repo>")
        s = re.sub(r"/[^\s\"']*/\.venv/bin/graphify", "<real-graphify-binary>", s)
        s = re.sub(r"/[^\s\"']*/graphify-safe\.sh", "<graphify-safe.sh>", s)
        t = dest / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text(scrub(s))
        done.append(rel)
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--wrapper", type=Path, required=True)
    p.add_argument("--graphify", type=Path, required=True, help="real graphify binary")
    r = sub.add_parser("run")
    r.add_argument("--work", type=Path, required=True)
    r.add_argument("--wrapper", type=Path, required=True)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--reps", type=int, default=3)
    r.add_argument("--only", help="TASK:CORPUS:ID, e.g. B:httpx:pr3389")
    r.add_argument("--conds", nargs="+", help="agent:cond, e.g. claude:base codex:base")
    r.add_argument("--task", choices=["A", "B"])
    a = sub.add_parser("analyze")
    a.add_argument("--out", type=Path, required=True)
    a.add_argument("--v1", type=Path)
    rp = sub.add_parser("report", help="analyze + write REPORT.md")
    rp.add_argument("--out", type=Path, required=True)
    rp.add_argument("--v1", type=Path, required=True)
    rp.add_argument("--dest", type=Path, default=ROOT / "REPORT.md")
    rp.add_argument("--pilot", type=Path, help="pilot run whose sessions were copied into --out")
    sz = sub.add_parser("sanitize", help="rebuild redacted runs/ from raw originals")
    sz.add_argument("--raw", type=Path, required=True, help="raw runs, outside the repo")
    sz.add_argument("--runs", type=Path, default=ROOT / "runs")
    sz.add_argument("--key", type=Path, required=True,
                    help="pseudonymization key file, outside the repo (created if missing)")
    ec = sub.add_parser("export-conditions", help="copy installer-written files")
    ec.add_argument("--work", type=Path, required=True)
    ec.add_argument("--dest", type=Path, default=ROOT / "conditions")
    args = ap.parse_args(argv)
    if args.cmd == "prepare":
        print(json.dumps(prepare(args.work.resolve(), args.wrapper.resolve(),
                                 args.graphify.resolve()), indent=2))
    elif args.cmd == "run":
        conds = CONDS
        if args.conds:
            conds = {}
            for item in args.conds:
                agent, cond = item.split(":", 1)
                conds.setdefault(agent, []).append(cond)
        run(args.work.resolve(), args.wrapper.resolve(), args.out.resolve(), args.reps,
            conds, args.only, args.task)
    elif args.cmd == "report":
        report(args.out, args.v1, args.dest, args.pilot)
        print(f"wrote {args.dest}")
    elif args.cmd == "sanitize":
        raw, keyf = args.raw.resolve(), args.key.resolve()
        if raw.is_relative_to(ROOT) or keyf.is_relative_to(ROOT):
            raise SystemExit("--raw and --key must be outside the repository")
        if not keyf.exists():
            keyf.write_bytes(os.urandom(32))
            keyf.chmod(0o600)
        try:
            print(json.dumps(sanitize_runs(raw, args.runs, keyf.read_bytes()), indent=2))
        except ValueError as e:
            raise SystemExit(str(e)) from None
    elif args.cmd == "export-conditions":
        print("\n".join(export_conditions(args.work.resolve(), args.dest)))
    else:
        print(json.dumps(analyze(args.out, args.v1)["summary"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
