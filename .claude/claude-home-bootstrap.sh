#!/usr/bin/env bash
# Fetch the global claude-home config, then hand off to it.
#
# This file cannot live in claude-home, which is the whole point: on a cold container
# claude-home is not on disk yet, so whatever clones it has to ship with the repo the
# session actually opens. An earlier version guarded on the presence of
# claude-home/cloud/session-start.sh and so could never bootstrap - the script that does
# the cloning lived inside the repo being cloned.
#
# Called as the SessionStart hook. Argument: the script to run inside claude-home.

set -uo pipefail
CDPATH=
REPO="https://github.com/Natenc12/claude-home.git"

# Find an existing checkout before assuming $HOME. A cloud session started with
# claude-home attached alongside this repo gets it at /home/user/claude-home while
# $HOME is /root, and assuming $HOME there clones a redundant second copy that
# nothing links to. Identify it by its origin URL, not by its path.
# Identify the checkout by its origin's repo NAME, matched exactly. A substring glob
# accepted any origin merely CONTAINING "claude-home": a sibling directory whose remote
# was ".../not-my-claude-home.git" was adopted as the config source, and this script
# then ran a script out of it. Demonstrated, not theoretical.
claude_home_repo() {
  local u l
  u=$(git -C "$1" remote get-url origin 2>/dev/null) || return 1
  # Strip trailing .git and / repeatedly and in any order. Doing it once, in a fixed
  # order, rejected the legitimate ".../claude-home.git/".
  while :; do
    case "$u" in
      */)    u=${u%/} ;;
      *.git) u=${u%.git} ;;
      *)     break ;;
    esac
  done
  # An allowlist of exact remotes. Matching the path tail (*/Natenc12/claude-home)
  # never bound the HOST, so https://evil.example.com/Natenc12/claude-home,
  # https://github.com/attacker/Natenc12/claude-home, file:// and ssh:// variants,
  # and credentialed URLs all passed and were executed from. All demonstrated.
  l=$(printf '%s' "$u" | tr 'A-Z' 'a-z')   # GitHub owners are case-insensitive
  case "$l" in
    https://github.com/natenc12/claude-home|\
    http://github.com/natenc12/claude-home|\
    ssh://git@github.com/natenc12/claude-home|\
    git@github.com:natenc12/claude-home) return 0 ;;
  esac
  # Everything else is refused: any other host, and every scp-style host:path remote
  # (with or without a user - "evil.com:x/claude-home" has no :// and no @, so it
  # previously fell through the host test entirely and was accepted).
  #
  # A local path is the test harness's case and must be opted into explicitly. Left
  # on by default it was the whole attack: `git init && git remote add origin
  # ../claude-home` next to the checkout was enough to execute code.
  [ "${CLAUDE_HOME_ALLOW_LOCAL:-}" = "1" ] || return 1
  case "$u" in *://*|*:*) return 1 ;; esac
  [ "${u##*/}" = "claude-home" ]
}
H=""
for c in "${CLAUDE_HOME:-}" "${HOME:-}/claude-home" "/home/user/claude-home" "${PWD:-.}/../claude-home"; do
  # -P resolves symlinks. Without it a planted ../claude-home symlink was honored and
  # the logical path kept, so the check and the use could refer to different directories.
  if [ -n "$c" ] && [ -e "$c/.git" ] && claude_home_repo "$c"; then
    H=$(cd -P -- "$c" 2>/dev/null && pwd -P) && [ -n "$H" ] && break
  fi
done
H="${H:-${CLAUDE_HOME:-${HOME:-.}/claude-home}}"
# The loop validates each candidate, but this fallback re-adopts a path without
# checking it. An impostor checkout at $HOME/claude-home was therefore accepted and
# executed from - the exact hole the matcher was added to close. Demonstrated.
# An existing checkout that is not claude-home means we stop, silently: a contributor
# who happens to have such a directory must see nothing.
if [ -e "$H/.git" ] && ! claude_home_repo "$H"; then
  exit 0
fi
export CLAUDE_HOME="$H"
TARGET="${1:-cloud/session-start.sh}"

# This repo is public, so the hook must do nothing for anyone but the repo owner.
#
# A checkout already on disk is proof enough. Otherwise require CLAUDE_HOME_ENABLE=1,
# set in the owner's own cloud environment variables. Guarding on CLAUDE_CODE_REMOTE
# alone is not enough: a contributor opening this repo in THEIR cloud session also has
# it set, and would get a failed clone of a private repo plus a confusing note in their
# context, retried every turn.
if [ ! -d "$H/.git" ] && [ "${CLAUDE_HOME_ENABLE:-}" != "1" ]; then
  exit 0
fi

# On the laptop, ~/.claude/settings.json already wires these hooks, and running them
# again here fires everything twice per event. Let the user-level config own the laptop.
if [ -d "$H/.git" ] && [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

if [ ! -d "$H/.git" ]; then
  # Keep git's reason rather than discarding it. "clone failed" cannot tell a scoping
  # refusal apart from a network outage, and those need opposite fixes - not knowing
  # which cost two rounds of cold-container testing.
  # Fail fast instead of blocking on an interactive username prompt, which would
  # stall the whole session start until the hook timeout.
  if ! cloneerr=$(GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=true git clone -q --depth 1 "$REPO" "$H" 2>&1); then
    # Say so out loud. SessionStart stdout reaches the session's context and stderr does
    # NOT (measured in a live container), so this is the only channel that works. A silent
    # miss is indistinguishable from everything having worked.
    #
    # The recovery steps have to live HERE, not in claude-home/cloud/session-start.sh:
    # in this exact failure claude-home is precisely what is missing, so anything written
    # inside it is unreachable. Same trap as the guard that once tested for
    # claude-home/cloud/session-start.sh before cloning claude-home.
    echo "NOTE: the global claude-home config could not be cloned in this container, so the"
    echo "usual global instructions, skills, and memory are NOT loaded."
    echo "git said: $(printf '%s' "$cloneerr" | head -2 | tr '\n' ' ')"
    echo
    echo "This is expected on a cold cloud container: a session is served private repos"
    echo "only while they are attached to it, and claude-home usually is not."
    echo
    echo "To load it, the repository has to be attached to this session:"
    echo "  1. Attach Natenc12/claude-home to this session, using whichever tool your"
    echo "     environment exposes for adding a repository (add_repo, or equivalent)."
    echo "  2. Re-run: bash \"\${CLAUDE_PROJECT_DIR:-.}/.claude/claude-home-bootstrap.sh\""
    echo "  3. Report that the manual attach was needed."
    exit 0
  fi
fi

# Not exec: that would make this hook's exit status whatever the handed-off script
# returns, and a Stop hook exiting 2 blocks the stop and loops the session.
# $TARGET comes from .claude/settings.json, which is tracked. Unconstrained, a change
# to the JSON alone could point this at any path relative to $H, including ../ escapes.
case "$TARGET" in
  cloud/session-start.sh|cloud/sync.sh) ;;
  *) exit 0 ;;
esac
[ -f "$H/$TARGET" ] && bash "$H/$TARGET"
exit 0
