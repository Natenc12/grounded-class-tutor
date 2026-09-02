#!/usr/bin/env bash
# Tests for .claude/claude-home-bootstrap.sh and scripts/cloud-bootstrap.sh.
#
#   bash tests/scripts/test-bootstrap.sh
#
# Self-contained: sandboxes HOME, works in throwaway directories, never touches the
# real ~/claude-home and never reaches the network.
#
# The attack rows come from an adversarial review that executed arbitrary code through
# four separate routes. Each hostile origin below is one that WAS accepted and run.

set -uo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BOOT="$SRC/.claude/claude-home-bootstrap.sh"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
export HOME="$TMP/sandbox-home"; mkdir -p "$HOME"
printf '[init]\n\tdefaultBranch = main\n[user]\n\tname = t\n\temail = t@e.invalid\n' > "$HOME/.gitconfig"
CDPATH=
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$1"; [ -n "${2:-}" ] && printf '       %s\n' "$2"; }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "want [$3] got [$2]"; fi; }

# A checkout that announces itself if anything runs it.
plant() { # plant <dir> <origin-url>
  rm -rf "$1"; mkdir -p "$1/cloud"; git init -q "$1"
  git -C "$1" remote add origin "$2"
  # Record the directory it ran from: a test that only checks "something ran" cannot
  # see a change in how the path was resolved.
  # Record the path AS PASSED, not re-resolved: `pwd -P` inside the payload canonicalises
  # again and hides whether the caller resolved the symlink itself.
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$(dirname "$(dirname "$0")")" > "%s/MARKER"\n' "$TMP" > "$1/cloud/session-start.sh"
  cp "$1/cloud/session-start.sh" "$1/cloud/sync.sh"
}

echo "== hostile origins must never execute =="
# Every URL here was ACCEPTED and executed by the previous version.
i=0
for url in \
  "https://evil.example.com/Natenc12/claude-home.git" \
  "https://github.com/attacker/Natenc12/claude-home.git" \
  "https://user:pw@evil.example.com/Natenc12/claude-home" \
  "ssh://git@evil.example.com:2222/Natenc12/claude-home.git" \
  "file:///tmp/evil/Natenc12/claude-home" \
  "https://github.com/attacker/not-my-claude-home.git" \
  "evil.com:anything/claude-home" \
  "gh-evil:anything/claude-home.git" \
  "/tmp/evil/claude-home" \
  "../evil/claude-home"
do
  i=$((i+1)); W="$TMP/atk$i/work"; mkdir -p "$W"
  plant "$TMP/atk$i/claude-home" "$url"
  rm -f "$TMP/MARKER"
  OUT=$( cd "$W" && HOME="$TMP/atk$i/home" CLAUDE_CODE_REMOTE=true CLAUDE_HOME_ENABLE=1 \
         bash "$BOOT" 2>&1 )
  check "no exec via sibling: $url" "$([ -e "$TMP/MARKER" ] && echo PWNED || echo safe)" "safe"
done

# The same, through the FALLBACK rather than a loop candidate. The matcher only gated
# the discovery loop; the fallback re-adopted $HOME/claude-home unvalidated and ran it.
plant "$TMP/fb/claude-home" "https://github.com/attacker/not-my-claude-home.git"
rm -f "$TMP/MARKER"
mkdir -p "$TMP/fb/work"
( cd "$TMP/fb/work" && HOME="$TMP/fb" CLAUDE_CODE_REMOTE=true CLAUDE_HOME_ENABLE=1 bash "$BOOT" >/dev/null 2>&1 )
check "no exec via \$HOME fallback" "$([ -e "$TMP/MARKER" ] && echo PWNED || echo safe)" "safe"

# And through CLAUDE_HOME pointing straight at it.
rm -f "$TMP/MARKER"
( cd "$TMP/fb/work" && CLAUDE_HOME="$TMP/fb/claude-home" HOME="$TMP/fb" CLAUDE_CODE_REMOTE=true \
    CLAUDE_HOME_ENABLE=1 bash "$BOOT" >/dev/null 2>&1 )
check "no exec via CLAUDE_HOME"    "$([ -e "$TMP/MARKER" ] && echo PWNED || echo safe)" "safe"

# A symlinked sibling: -P canonicalizes the path but never re-validates.
plant "$TMP/sym/real" "https://evil.example.com/Natenc12/claude-home.git"
mkdir -p "$TMP/sym/work"; ln -sfn "$TMP/sym/real" "$TMP/sym/claude-home"
rm -f "$TMP/MARKER"
( cd "$TMP/sym/work" && HOME="$TMP/sym/home" CLAUDE_CODE_REMOTE=true CLAUDE_HOME_ENABLE=1 bash "$BOOT" >/dev/null 2>&1 )
check "no exec via symlinked sibling" "$([ -e "$TMP/MARKER" ] && echo PWNED || echo safe)" "safe"

echo "== the legitimate remote forms must be accepted =="
# Value here is entirely in the ACCEPT rows: the reject rows are the cases the author
# already had in mind and are least likely to regress.
j=0
for url in \
  "https://github.com/Natenc12/claude-home.git" \
  "git@github.com:Natenc12/claude-home.git" \
  "ssh://git@github.com/Natenc12/claude-home" \
  "https://github.com/natenc12/claude-home" \
  "https://github.com/Natenc12/claude-home.git/"
do
  j=$((j+1)); W="$TMP/ok$j/work"; mkdir -p "$W"
  plant "$TMP/ok$j/claude-home" "$url"
  rm -f "$TMP/MARKER"
  ( cd "$W" && HOME="$TMP/ok$j/home" CLAUDE_CODE_REMOTE=true CLAUDE_HOME_ENABLE=1 bash "$BOOT" >/dev/null 2>&1 )
  check "accepts: $url" "$([ -e "$TMP/MARKER" ] && echo used || echo skipped)" "used"
done

echo "== the local-path gate, which nothing else exercises =="
# Without a case that sets CLAUDE_HOME_ALLOW_LOCAL=1, everything after the gate is
# unreachable: replacing the scp-guard and the suffix check with `return 0` passed the
# whole suite. These three rows are the only thing covering that branch.
plant "$TMP/loc/claude-home" "$TMP/forge/claude-home"
mkdir -p "$TMP/loc/work"; rm -f "$TMP/MARKER"
( cd "$TMP/loc/work" && HOME="$TMP/loc/home" CLAUDE_CODE_REMOTE=true CLAUDE_HOME_ENABLE=1 \
    bash "$BOOT" >/dev/null 2>&1 )
check "local path refused without the flag" "$([ -e "$TMP/MARKER" ] && echo used || echo refused)" "refused"
rm -f "$TMP/MARKER"
( cd "$TMP/loc/work" && HOME="$TMP/loc/home" CLAUDE_CODE_REMOTE=true CLAUDE_HOME_ENABLE=1 \
    CLAUDE_HOME_ALLOW_LOCAL=1 bash "$BOOT" >/dev/null 2>&1 )
check "local path allowed with the flag"    "$([ -e "$TMP/MARKER" ] && echo used || echo refused)" "used"
# The flag must not weaken the host allowlist.
plant "$TMP/loc/claude-home" "https://evil.example.com/x/claude-home.git"
rm -f "$TMP/MARKER"
( cd "$TMP/loc/work" && HOME="$TMP/loc/home" CLAUDE_CODE_REMOTE=true CLAUDE_HOME_ENABLE=1 \
    CLAUDE_HOME_ALLOW_LOCAL=1 bash "$BOOT" >/dev/null 2>&1 )
check "hostile host refused even with the flag" "$([ -e "$TMP/MARKER" ] && echo used || echo refused)" "refused"

# An all-caps but legitimate origin must still be accepted: the strip globs are
# case-sensitive, so stripping before lowercasing left ".GIT" and silently refused it.
plant "$TMP/uc/claude-home" "HTTPS://GITHUB.COM/NATENC12/CLAUDE-HOME.GIT"
mkdir -p "$TMP/uc/work"; rm -f "$TMP/MARKER"
( cd "$TMP/uc/work" && HOME="$TMP/uc/home" CLAUDE_CODE_REMOTE=true CLAUDE_HOME_ENABLE=1 \
    bash "$BOOT" >/dev/null 2>&1 )
check "uppercase legitimate origin accepted" "$([ -e "$TMP/MARKER" ] && echo used || echo refused)" "used"

echo "== origins that only pass through the userinfo/port rewrite =="
# Every other ACCEPT row above is caught by the outer allowlist before the rewrite ever
# runs, so the rewrite branch had no positive coverage at all: deleting it wholesale
# still passed the suite, while silently refusing the CI, token and clone-with-username
# origins it exists to support.
k=0
for url in \
  "https://natenc12@github.com/Natenc12/claude-home.git" \
  "https://x-access-token:TOKEN@github.com/Natenc12/claude-home.git" \
  "https://oauth2:tok@github.com/Natenc12/claude-home" \
  "ssh://git@github.com:22/Natenc12/claude-home.git" \
  "https://github.com:443/Natenc12/claude-home.git"
do
  k=$((k+1)); W="$TMP/rw$k/work"; mkdir -p "$W"
  plant "$TMP/rw$k/claude-home" "$url"
  rm -f "$TMP/MARKER"
  ( cd "$W" && HOME="$TMP/rw$k/home" CLAUDE_CODE_REMOTE=true CLAUDE_HOME_ENABLE=1 bash "$BOOT" >/dev/null 2>&1 )
  check "accepts via rewrite: $url" "$([ -e "$TMP/MARKER" ] && echo used || echo refused)" "used"
done

# And the rewrite must not turn a hostile authority into an allowlisted one. git would
# genuinely connect to evil.com for this URL, so accepting it would be the real bug.
plant "$TMP/rwx/claude-home" "https://github.com@evil.com/Natenc12/claude-home.git"
mkdir -p "$TMP/rwx/work"; rm -f "$TMP/MARKER"
( cd "$TMP/rwx/work" && HOME="$TMP/rwx/home" CLAUDE_CODE_REMOTE=true CLAUDE_HOME_ENABLE=1 bash "$BOOT" >/dev/null 2>&1 )
check "rewrite does not launder host@evil" "$([ -e "$TMP/MARKER" ] && echo used || echo refused)" "refused"

echo "== a contributor sees nothing =="
mkdir -p "$TMP/c/work"; cd "$TMP/c/work"
run_quiet() { # run_quiet <label> <env...> -- ; asserts exit 0 and zero bytes
  local label="$1"; shift
  local out rc
  out=$(env "$@" bash "$BOOT" 2>&1); rc=$?
  check "$label exit 0"    "$rc" "0"
  check "$label is silent" "${#out}" "0"
}
run_quiet "laptop"          HOME="$TMP/c/home"
run_quiet "own cloud session" HOME="$TMP/c/home" CLAUDE_CODE_REMOTE=true
run_quiet "HOME unset"      -u HOME
out=$(env HOME="$TMP/c/home" CLAUDE_CODE_REMOTE=true bash "$BOOT" cloud/sync.sh 2>&1)
check "stop-hook path is silent" "${#out}" "0"

echo "== the laptop double-fire guard =="
# On a machine that is not a cloud container, the user's own ~/.claude/settings.json
# already wires these hooks; running them from the repo too fires everything twice per
# event. That guard had no coverage in either suite.
plant "$TMP/lap/claude-home" "https://github.com/Natenc12/claude-home.git"
mkdir -p "$TMP/lap/work"; rm -f "$TMP/MARKER"
( cd "$TMP/lap/work" && HOME="$TMP/lap" env -u CLAUDE_CODE_REMOTE bash "$BOOT" >/dev/null 2>&1 )
check "laptop does not hand off"  "$([ -e "$TMP/MARKER" ] && echo fired || echo quiet)" "quiet"
rm -f "$TMP/MARKER"
( cd "$TMP/lap/work" && HOME="$TMP/lap" CLAUDE_CODE_REMOTE=true bash "$BOOT" >/dev/null 2>&1 )
check "a container does hand off"  "$([ -e "$TMP/MARKER" ] && echo fired || echo quiet)" "fired"

echo "== a legitimate symlinked checkout is canonicalised =="
# The one existing symlink row plants a HOSTILE origin, so the matcher refuses it before
# path resolution matters. Nothing covered -P on a checkout that is actually accepted.
plant "$TMP/sl/real" "https://github.com/Natenc12/claude-home.git"
mkdir -p "$TMP/sl/work"; ln -sfn "$TMP/sl/real" "$TMP/sl/claude-home"
rm -f "$TMP/MARKER"
( cd "$TMP/sl/work" && HOME="$TMP/sl/home" CLAUDE_CODE_REMOTE=true CLAUDE_HOME_ENABLE=1 bash "$BOOT" >/dev/null 2>&1 )
# It must resolve to the PHYSICAL directory, not the symlink path - otherwise the
# directory that was validated and the one that is used can differ.
check "symlinked checkout resolves physically" \
  "$(cat "$TMP/MARKER" 2>/dev/null)" "$(cd -P "$TMP/sl/real" && pwd -P)"

echo "== the hook's exit status is never the child's =="
# exec made this hook return whatever the handed-off script returned, and a Stop hook
# exiting 2 blocks the stop and loops the session.
plant "$TMP/ex/claude-home" "https://github.com/Natenc12/claude-home.git"
printf '#!/usr/bin/env bash\nexit 2\n' > "$TMP/ex/claude-home/cloud/sync.sh"
mkdir -p "$TMP/ex/work"
( cd "$TMP/ex/work" && HOME="$TMP/ex" CLAUDE_CODE_REMOTE=true CLAUDE_HOME_ENABLE=1 bash "$BOOT" cloud/sync.sh >/dev/null 2>&1 )
check "child exit 2 does not propagate" "$?" "0"

echo "== the handoff target cannot escape claude-home =="
mkdir -p "$TMP/esc/outside"
printf '#!/usr/bin/env bash\necho PWNED > "%s/MARKER"\n' "$TMP" > "$TMP/esc/outside/payload.sh"
plant "$TMP/esc/claude-home" "https://github.com/Natenc12/claude-home.git"
rm -f "$TMP/MARKER"; mkdir -p "$TMP/esc/work"
( cd "$TMP/esc/work" && HOME="$TMP/esc" CLAUDE_CODE_REMOTE=true CLAUDE_HOME_ENABLE=1 \
    bash "$BOOT" "../outside/payload.sh" >/dev/null 2>&1 )
check "target cannot traverse out" "$([ -e "$TMP/MARKER" ] && echo PWNED || echo safe)" "safe"

echo "== cloud-bootstrap refuses outside a container =="
cp "$SRC/scripts/cloud-bootstrap.sh" "$TMP/cb.sh" 2>/dev/null
mkdir -p "$TMP/envtest"; printf 'DATABASE_URL=postgresql://sentinel/keepme\n' > "$TMP/envtest/.env"
BEFORE=$(cat "$TMP/envtest/.env")
( cd "$TMP/envtest" && env -u CLAUDE_CODE_REMOTE bash "$SRC/scripts/cloud-bootstrap.sh" >/dev/null 2>&1 )
check "refusal leaves .env byte-identical" "$(cat "$TMP/envtest/.env")" "$BEFORE"
# Assert the GUARD fired, not merely that the script failed. On a Mac without pgvector
# it exits 1 further down for an unrelated reason, so "non-zero" passed even with the
# guard deleted - and on a Mac that HAS pgvector the script would reach the .env rewrite.
GOUT=$( cd "$TMP/envtest" && env -u CLAUDE_CODE_REMOTE bash "$SRC/scripts/cloud-bootstrap.sh" 2>&1 >/dev/null ); GRC=$?
check "the guard itself refuses"  "$(printf '%s' "$GOUT" | grep -c 'Refusing here')" "1"
check "refusal exit code is 2"    "$GRC" "2"

# The matcher is duplicated in the private claude-home repo. If that checkout is here,
# assert the two copies still agree - a divergence means this hook accepts a checkout
# the other half refuses, or the reverse. Skipped silently when it is absent, so a
# contributor running this suite sees nothing about it.
CH="${CLAUDE_HOME:-$TMP/nope}"
[ -d "$CH" ] || CH="/Users/${SUDO_USER:-${USER:-nobody}}/claude-home"
echo "== the matcher copy in claude-home agrees =="
if [ -f "$CH/cloud/session-start.sh" ]; then
  logic() { sed -n "/^$2() {/,/^}/p" "$1" | grep -v '^[[:space:]]*#' | grep -v '^[[:space:]]*$' \
    | sed 's/[[:space:]]*#.*$//;s/^[[:space:]]*//;s/[[:space:]]*$//' | sed "s/$2/M/"; }
  check "matcher matches claude-home's copy" \
    "$(logic "$BOOT" claude_home_repo | cksum)" \
    "$(logic "$CH/cloud/session-start.sh" is_claude_home | cksum)"
else
  # Say so out loud. Vanishing silently made the total drop from 44 to 43 with no
  # explanation, and a check that can disappear unannounced is not coverage.
  ok "matcher drift check skipped (no claude-home checkout here)"
fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
