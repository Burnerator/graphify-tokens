#!/usr/bin/env bash
# Transparent wrapper around graphify's own Claude Code hook (`graphify hook-guard <mode>`).
# - Passes the hook's stdin/stdout/stderr/exit code through UNCHANGED (vendor behaviour kept).
# - Runs graphify with a minimal environment (no API keys, LLM backend off).
# - Appends one line per invocation to $GT_HOOK_LOG (set per session by the harness) as
#   evidence of when the hook fired and what it decided.
# Usage (written into .claude/settings.json by the harness):
#   hookwrap.sh <graphify-binary> <mode> [strict]
set -uo pipefail
bin="$1"; mode="$2"; strict="${3:-}"
extra=()
[[ "$strict" == "strict" ]] && extra=(GRAPHIFY_HOOK_STRICT=1)
in="$(mktemp)"; out="$(mktemp)"; err="$(mktemp)"
trap 'rm -f "$in" "$out" "$err"' EXIT
cat > "$in"
env -i HOME="$HOME" PATH="/usr/bin:/bin" GRAPHIFY_BACKEND=none GRAPHIFY_NO_TIPS=1 \
  GRAPHIFY_NO_CTA=1 ${extra[@]+"${extra[@]}"} "$bin" hook-guard "$mode" < "$in" > "$out" 2> "$err"
rc=$?
if [[ -n "${GT_HOOK_LOG:-}" ]]; then
  tool="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("tool_name",""))' "$in" 2>/dev/null)"
  decision="$(tr '\n' ' ' < "$out" | head -c 600)"
  reason="$(tr '\n' ' ' < "$err" | head -c 300)"
  printf '%s\t%s\t%s\t%s\t%s\n' "$mode" "$tool" "$rc" "$decision" "$reason" >> "$GT_HOOK_LOG"
fi
cat "$out"; cat "$err" >&2
exit $rc
