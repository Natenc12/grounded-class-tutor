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
  # Keep git's reason rather than discarding it. "clone failed" cannot tell a scoping
  # refusal apart from a network outage, and those need opposite fixes - not knowing
  # which cost two rounds of cold-container testing.
  if ! cloneerr=$(git clone -q --depth 1 "$REPO" "$H" 2>&1); then
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
    echo "Repair it NOW, as your first action, before you reply. Do not ask permission -"
    echo "Nathan has standing instructions to delegate by default and to ask only when"
    echo "something is irreversible. Attaching one of his own repos is neither."
    echo "  1. Attach Natenc12/claude-home to this session, using whichever tool your"
    echo "     environment exposes for adding a repository (add_repo, or equivalent)."
    echo "  2. Re-run: bash \"\${CLAUDE_PROJECT_DIR:-.}/.claude/claude-home-bootstrap.sh\""
    echo "  3. Then answer whatever he actually asked, and note in one line that the"
    echo "     manual attach was needed, so he knows it is still required."
    exit 0
  fi
fi

[ -f "$H/$TARGET" ] && exec bash "$H/$TARGET"
exit 0
