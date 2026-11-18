#!/usr/bin/env bash
# Safe wrapper for graphify (copied from Burnerator/anvil scripts/graphify-safe.sh, policy D-009).
# Default-deny ALLOWLIST:
#   update <path> | query <text> | path <A> <B> | explain <X> | section <X> | --version | --help
# Everything else (including `graphify <path>`, options before the subcommand, install,
# hook, claude, extract, label, cluster-only, tree, clone, add, ...) is rejected.
# For allowed commands it also: disables every LLM backend (incl. the `claude -p`
# fallback), runs with a minimal environment (no API keys / provider URLs) and
# disables auto-refresh, tips and CTAs.
# Limits: it does not sandbox the network or the filesystem, and the real binary in
# .venv/bin is still reachable directly — always call this wrapper by its path.
set -euo pipefail

repo_root="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
bin="${GRAPHIFY_SAFE_BIN:-$repo_root/.venv/bin/graphify}"

deny() {
  echo "graphify-safe: blocked by policy: $*" >&2
  echo "Allowed: update <path>, query, path, explain, section, --version, --help" >&2
  exit 2
}

[[ $# -ge 1 ]] || deny "no subcommand"
cmd="$1"
case "$cmd" in
  --version | --help | -h)
    [[ $# -eq 1 ]] || deny "extra arguments"
    ;;
  update)
    [[ $# -ge 2 && "$2" != -* ]] || deny "'update' requires an explicit path"
    for a in "${@:3}"; do
      [[ "$a" == "--force" ]] || deny "option '$a' not allowed for update"
    done
    ;;
  path)
    [[ $# -ge 3 && "$2" != -* && "$3" != -* ]] || deny "'path' requires two node names"
    ;;
  query | explain | section)
    [[ $# -ge 2 && "$2" != -* ]] || deny "'$cmd' requires an argument"
    ;;
  *)
    deny "'$cmd'"
    ;;
esac

exec env -i \
  HOME="$HOME" PATH="/usr/bin:/bin:/usr/sbin:/sbin" LANG="${LANG:-en_US.UTF-8}" \
  TERM="${TERM:-dumb}" TMPDIR="${TMPDIR:-/tmp}" \
  GRAPHIFY_BACKEND=none GRAPHIFY_NO_AUTO_REFRESH=1 GRAPHIFY_NO_TIPS=1 GRAPHIFY_NO_CTA=1 \
  "$bin" "$@"
