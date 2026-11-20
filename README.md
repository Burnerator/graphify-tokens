# graphify-tokens

**Does [graphify](https://github.com/Graphify-Labs/graphify) reduce the tokens AI coding agents
spend on a codebase?** A reproducible A/B experiment with **Claude Code** and **Codex**, by
[Burnerator](https://www.burnerator.com).

**Short answer, for the open-source package (`graphifyy` 0.9.111) on two Python repos:** we did
not see it. In 280 sessions, every graphify setup had a **higher** median token volume than no
graphify for Claude Code (+12 % to +63 % per session against the pooled baseline, depending on
task and integration; +6 % to +55 % against the same-day baseline only; the sign also holds
in the most common Claude tool-count subset), and about the same
for Codex (−11 % to +9 % with the official text, depending on the baseline). Answer coverage
moved by a few points in most cells. These are observed token volumes under the run design
described below (base sessions ran in separate batches), not a causal decomposition. Full
numbers, method and caveats: **[REPORT.md](REPORT.md)**.

> graphify's "80% fewer tokens" headline refers to Graphify Cloud, a hosted product we did not
> test. See [Sources](#sources).

Everything needed to check or redo this is here:

| Path | What |
|---|---|
| `experiment.py` | v1 harness: corpora, task A questions and rubrics, trace parsers |
| `experiment_v2.py` | v2 harness: official integrations, hooks/strict, task B, report, redaction |
| `hookwrap.sh` | transparent logger around graphify's own Claude Code hook |
| `scripts/graphify-safe.sh` | allow-list wrapper that a bare `graphify` resolves to for the agents |
| `conditions/` | reference exports: instruction files as the installers wrote them; Claude hook settings as instrumented (redacted paths) |
| `runs/2026-11-17/` | v1: 96 sessions (task A, `base` and `rules-generic`) |
| `runs/2026-11-18/` | v2: 184 sessions (tasks A and B, `base`, `rules-official`, `hooks`, `strict`) |
| `runs/pilot-v2/` | the 6-session v2 pilot; byte-identical copies of 6 records in `runs/2026-11-18`, counted once there |
| `REPORT.md` | generated from the session files |
| `runs/*/run.log` | historical progress output printed during the runs (older token and graph-command definitions; not used by the report) |

Each session has `<id>.json` (metrics + final answer), `<id>.trace.jsonl` (the agent's own
event stream) and, for the v2 `hooks` and `strict` sessions, `<id>.hooks.tsv` (instrumented
hook firings and their decisions). Published traces are redacted: local paths, the login
name, the host name, emails, credential-shaped strings, the operator's installed
plugins/connectors (the `init` event keeps only the
experiment's tool names plus a count of others), plan-usage events and opaque thinking
signatures are removed, and session/request/message identifiers are replaced by keyed
pseudonyms (links between events are preserved). The token, status, answer, rubric, tool and
graphify fields of `<id>.json` are recomputed from the published trace (hook counts from the
published `hooks.tsv`), and a test checks that (`TestPublishedData`). The redaction only
publishes known run files and refuses anything else.

If you get different results, please [open an issue](https://github.com/Burnerator/graphify-tokens/issues).

## Design in one screen

- **Corpora** (pinned commits): anvil @ `6cd8286`, our internal workspace repository (not
  public; 9 Python files, 1.1k lines), and [encode/httpx @ `26d48e0`](https://github.com/encode/httpx/tree/26d48e0634e6ee9cdc0533996db289ce4b430177)
  (60 files, 17.8k lines), cloned by `prepare`. The anvil sessions are kept in `runs/` for
  transparency, but the anvil corpus cannot be reproduced publicly. The httpx benchmark (task
  A on httpx and all of task B) can be rerun from public, pinned sources, though a fresh run
  can measure different numbers (see [Reproduce](#reproduce)); every published number, anvil
  included, can be recomputed from the checked-in traces.
- **Task A, understand:** 8 architecture questions with regex rubrics (mention coverage).
- **Task B, review:** 4 real merged httpx PRs ([#3377](https://github.com/encode/httpx/pull/3377),
  [#2879](https://github.com/encode/httpx/pull/2879), [#3139](https://github.com/encode/httpx/pull/3139),
  [#3389](https://github.com/encode/httpx/pull/3389)); graph built on the PR's base commit,
  rubric of 5–8 items derived statically from the diff: up to 3 changed Python files under
  `httpx/`, up to 3 touched symbols (functions or classes, first three alphabetically) and up to
  3 other package files that mention the most distinct touched symbols (whole words, `git grep
  -lw`). A symbol counts as touched if a changed line falls anywhere in its definition, body
  included.
- **Conditions:** `base` (nothing), `rules-generic` (v1: graphify's generic rules text),
  `rules-official` (exact `graphify claude install` / `graphify codex install` text),
  `hooks` (Claude: full `graphify claude install`, PreToolUse + SessionStart hooks),
  `strict` (Claude only: `hooks` + `GRAPHIFY_HOOK_STRICT=1`, set by the harness).
- **Agents:** `claude -p` (model alias `sonnet`; the traces report `claude-sonnet-5`) and
  `codex exec` (read-only sandbox, CLI default model, not reported by the CLI), 3 repetitions
  per question × condition, except the v2 task A base: 1 repetition, pooled with v1's 3.
- **Recorded versions:** Claude Code 2.1.282, Codex CLI 0.169.0, graphify 0.9.111.
- **Metrics:** from each agent's own event stream, total tokens (uncached + cache read + cache
  write + output; for Claude summed over all models a session used), uncached in+out, Claude's API-equivalent cost, tool calls, attempted and
  successful graphify commands; from our hook wrapper's TSV logs, instrumented hook
  firings/denials; from the regex rubric applied to the final answer, mean rubric coverage.

## How the published runs were invoked

The published data is **not** one interleaved run. This is the sequence that produced it,
written with fresh output directories (`runs/mine-*`) so that rerunning it never touches the
published data:

```bash
# v1 (2026-11-17): base and rules-generic alternate within each question-rep pair (96 sessions).
# The published run was two invocations into the same directory: --reps 1 first (rep 0), then
# --reps 3, which skipped completed sessions and ran reps 1-2 (order indices differ accordingly)
python3 experiment.py run --work work --wrapper "$SAFE" --out runs/mine-v1 --reps 1
python3 experiment.py run --work work --wrapper "$SAFE" --out runs/mine-v1 --reps 3

# v2 pilot (2026-11-18): PR #3389, rep 0, all six agent-conditions. Its Claude traces start at
# 07:31 UTC and its environment.json records a last start at 07:42, so it was restarted at
# least once with the same command
python3 experiment_v2.py run --work work --wrapper "$SAFE" --out runs/mine-pilot --reps 1 \
    --only B:httpx:pr3389
# the pilot's sessions were then copied into the v2 directory (same code and conditions),
# so the invocations below skipped them
mkdir -p runs/mine-v2/sessions && cp runs/mine-pilot/sessions/* runs/mine-v2/sessions/

# v2, three invocations into the same directory (184 sessions in total):
# 1) all graphify conditions, tasks A and B, 3 reps: 144 sessions,
#    140 new + 4 from the pilot                                    (published: 07:44 UTC)
python3 experiment_v2.py run --work work --wrapper "$SAFE" --out runs/mine-v2 --reps 3 \
    --conds claude:rules-official claude:hooks claude:strict codex:rules-official
# 2) task B base, 3 reps: 24 sessions, 22 new + 2 from the pilot    (published: 09:22 UTC)
python3 experiment_v2.py run --work work --wrapper "$SAFE" --out runs/mine-v2 --reps 3 \
    --task B --conds claude:base codex:base
# 3) task A base, 1 rep: drift check against v1 (16 sessions)       (published: 09:37 UTC)
python3 experiment_v2.py run --work work --wrapper "$SAFE" --out runs/mine-v2 --reps 1 \
    --task A --conds claude:base codex:base

python3 experiment_v2.py report --out runs/mine-v2 --v1 runs/mine-v1 --pilot runs/mine-pilot \
    --dest REPORT-mine.md
```

v2 records every invocation and its start time in its run's `environment.json` (v1 keeps only
its last start), and
the report's *Run design and order* section is generated from it, together with the actual
start-time windows of the Claude sessions (first recorded timestamp in each trace, excluding the copied pilot: task B's
graphify sessions started 08:58–09:21 UTC and its base sessions 09:22–09:37; in the pilot,
graphify 07:31–07:32 and base 07:42). A run directory refuses new sessions if its
recorded CLI versions, models, graphify version, prepared corpora and rubrics, or task inputs
defined in code differ from the current ones. The check does not cover the condition files,
hook and wrapper contents or launch flags: keep those unchanged within a run directory. Within an invocation, condition order
alternates per question and rep; across invocations it does not. Running
`experiment_v2.py run` once with the default conditions instead interleaves `base` with every
graphify condition (12 tasks × 3 reps × 6 agent-conditions = 216 sessions): that is a
**different design**, arguably a better one, and its results should be reported as such
(the report's condition table describes the published design).

## Reproduce

Two different things can be checked. **Recomputing the published numbers** from the
checked-in traces works for every session, anvil included (`experiment_v2.py report`, see
[REPORT.md](REPORT.md#reproduce)). **Rerunning the benchmark** covers the **public httpx
corpus only**: task A on httpx and all of task B. A fresh run uses the Claude model alias
`sonnet`, the Codex CLI's default model and your own global agent settings, so its
measurements can differ from ours. The
anvil corpus is our internal workspace repository (not public), so `prepare` and `run` skip
it (with a message) and `--only` on an anvil question stops with an error; its published
sessions remain in `runs/` and the report is still generated from them. Your own reports
will therefore have no anvil rows.

Requirements: macOS 12.3+ or Linux (the wrapper uses `readlink -f`), Python 3.11+, git,
graphify 0.9.111, and the CLI of each agent
you run, logged in ([Claude Code](https://docs.anthropic.com/en/docs/claude-code),
[Codex CLI](https://github.com/openai/codex)). `experiment_v2.py run` only queries the CLIs of
the agents in `--conds`; `experiment.py run` (v1) needs both.

```bash
python3 -m venv .venv && .venv/bin/pip install graphifyy==0.9.111
REAL="$PWD/.venv/bin/graphify"            # the real binary, used by prepare and the hooks
SAFE="$PWD/scripts/graphify-safe.sh"      # what a bare `graphify` resolves to for the agents

# 1. httpx corpus + graph (AST only, no LLM) and v1 conditions
python3 experiment.py prepare --work work --wrapper "$SAFE"
# 2. v2 conditions: runs graphify's official installers inside throwaway copies with a
#    fake HOME (aborts if they leave files in it), routes hooks through hookwrap.sh,
#    and puts a `graphify` -> wrapper shim first on the agents' PATH (work/bin)
python3 experiment_v2.py prepare --work work --wrapper "$SAFE" --graphify "$REAL"
# 3. run: the published sequence above, or one interleaved run (a different design).
#    The report's task A drift check needs a v1 run as well:
python3 experiment.py    run --work work --wrapper "$SAFE" --out runs/mine-v1 --reps 3
python3 experiment_v2.py run --work work --wrapper "$SAFE" --out runs/mine-interleaved --reps 3
# 4. results
python3 experiment_v2.py report --out runs/mine-interleaved --v1 runs/mine-v1 \
    --dest REPORT-mine.md
# tests (no network, no agents)
python3 -m unittest discover -s tests
```

Notes:

- **Controls, as implemented.** Our audit of graphify found that some subcommands send code
  to an LLM if an API key is in the environment, and `install`/`hook install` write to tracked
  files. So in v2 the agents' `PATH` starts with a shim that makes a bare `graphify` resolve to
  `scripts/graphify-safe.sh`, which allows only `update`, `query`, `path`, `explain`,
  `section` and runs graphify with a stripped environment and no LLM backend. This is not a
  sandbox: the agents themselves inherit the operator's environment and global settings, the
  real binary stays reachable by absolute path, and the Codex hook file written by
  `graphify codex install` points at the real binary (`hook-check`, a no-op). Claude's
  `--allowedTools` list pre-approves tools but does not forbid others; with the operator's
  global settings Claude also ran other shell commands such as `grep`, `sed` and `test` (and
  `find` or a `python3 -c` one-liner in some base sessions), in every condition.
  Claude hooks run graphify's real `hook-guard` (which never calls an LLM) through
  `hookwrap.sh` with a minimal environment. The installer check only verifies that nothing
  was left in the fake `HOME`.
- **Status.** A published session counts as a successful completion if the agent reached
  its success terminal event without a terminal error event (recoverable events such as
  `api_retry` are allowed; the published runs predate exit-code
  recording). Since this release, both harnesses also require exit code 0, mark an error
  event after completion as a failure, and retry failed sessions on resume; v2 records the
  exit code in each session (`returncode`).
- **Cost:** Claude reports an API-equivalent cost of about $0.10–0.20 per session with
  `sonnet`. With a subscription login it comes out of your plan quota instead of being billed;
  our runs were not billed.
- **Before publishing your own runs:** keep the raw runs outside the repository and rebuild
  the redacted `runs/` with
  `python3 experiment_v2.py sanitize --raw ../raw-runs --key ../pseudonym.key`
  (the key is created on first use; keep it private and reuse it so pseudonyms stay stable).
  It rebuilds `runs/` from scratch, refuses overlapping paths and unknown files, and is not a
  substitute for reviewing the exported tree before publishing it.

## Sources

- graphify's "80% fewer tokens" claim is made for Graphify Cloud: [README, Graphify Cloud](https://github.com/Graphify-Labs/graphify#graphify-cloud).
- The open-source README's benchmark measures retrieval quality (e.g. key-fact coverage 82.0 %
  vs a grep/read baseline 70.8 %): [README, Benchmarks](https://github.com/Graphify-Labs/graphify#benchmarks-0497-recall10-0-llm-credits),
  [BENCHMARKS.md](https://github.com/Graphify-Labs/graphify/blob/v8/BENCHMARKS.md).
- The open-source docs' `graphify benchmark` *estimates* per-query token reduction against
  reading the whole corpus naively and describes the ratio as "a guide to scale, not a billing
  figure": [Token benchmark](https://docs.graphify.com/open-source/features/token-benchmark).
  This experiment measures end-to-end agent consumption, a different quantity.
- Building the graph on the PR's base branch follows graphify's PR guide ("Run `graphify update .`
  on the base branch first"): [Review pull requests with graph context](https://docs.graphify.com/open-source/features/pr-impact).
  We did not use `graphify prs` itself.

## Limitations

Read the [Limitations section of the report](REPORT.md#limitations) before quoting numbers:
small samples, base sessions in separate batches, day-to-day drift, the operator's global
installation, two Python repos, regex mention-coverage rubrics, and Claude vs Codex token
totals not comparable with each other.

## License

MIT for the harness code, the report and our own text (see `LICENSE`). The corpora are not
vendored, but the published traces contain **excerpts** of them (tool
outputs such as file contents, diffs and tests):

- httpx excerpts are © Encode OSS Ltd. and redistributed under httpx's BSD 3-Clause license,
  reproduced in [`NOTICE`](NOTICE);
- anvil excerpts come from our internal workspace repository (not public) at the pinned
  commit; that code is Burnerator's own and these excerpts are published under MIT;
- graphify material (the installer-written instruction text and hook configuration in
  `conditions/`, its generic rules text, and graphify command output quoted in the traces) is
  graphify's and redistributed under the Apache License 2.0; see [`NOTICE`](NOTICE) and
  [`third_party/graphify/`](third_party/graphify/).

The httpx and graphify excerpts keep their original licenses; this repository's MIT license
does not apply to them.
