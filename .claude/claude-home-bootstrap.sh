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
H="$HOME/claude-home"
REPO="https://github.com/Natenc12/claude-home.git"
TARGET="${1:-cloud/session-start.sh}"

# This repo is public, so the hook must do nothing for anyone but Nathan.
#
# A checkout already on disk is proof enough. Otherwise require CLAUDE_HOME_ENABLE=1,
# which he sets in his own cloud environment's variables. Guarding on CLAUDE_CODE_REMOTE
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
  if ! git clone -q --depth 1 "$REPO" "$H" 2>/dev/null; then
    # Say so out loud. SessionStart stdout reaches the session's context, and a silent
    # miss is indistinguishable from everything having worked.
    echo "NOTE: the global claude-home config could not be cloned in this container, so the"
    echo "usual global instructions, skills, and memory are NOT loaded. Private-repo access"
    echo "may be scoped to the attached repo; attaching claude-home to the session fixes it."
    exit 0
  fi
fi

[ -f "$H/$TARGET" ] && exec bash "$H/$TARGET"
exit 0
