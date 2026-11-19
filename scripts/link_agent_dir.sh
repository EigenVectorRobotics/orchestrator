#!/usr/bin/env bash
# Make the agent's folder in the workspace: links to what is in git, and real folders for what changes on each run.
#
#   $EIGEN_VECTOR_WS/agent/
#     skills     -> <repo>/skill_registry/skills          the skill library (write_skill saves here)
#     runbook.md -> <repo>/orchestrator/agent/runbook.md  what the robot is
#     docs       -> <repo>/orchestrator/agent/docs        the agent's codebase docs
#     .claude/settings.json -> <repo>/orchestrator/agent/claude_settings.json   the task session's permissions
#     knowledge/                                          claims and their log (not in git)
#     stamps.yaml                                         what each skill was proved against (not in git)
#
# Safe to run again. It never deletes a file: a real file or folder where a link must go stops the script.
# The old layout moves in: agent/contracts/<name>.yaml and agent/code/<name>.py go into skills/<name>/, and the files
# of a real agent/docs/ go into the docs folder in git. A real agent/runbook.md is renamed runbook.md.local, for a
# person to merge into the one in git.
#
# Usage: scripts/link_agent_dir.sh [AGENT_DIR]     (default: $EIGEN_VECTOR_WS/agent)
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
agent="${1:-${EIGEN_VECTOR_WS:?set EIGEN_VECTOR_WS or give the agent folder}/agent}"

mkdir -p "$agent/knowledge" "$agent/.claude"

link() {        # link NAME TARGET
    local path="$agent/$1" target="$2"

    if [[ -L "$path" ]]; then
        ln -sfn "$target" "$path"
    elif [[ -e "$path" ]]; then
        echo "stop: $path is a real file or folder; move it into $target, then run this again" >&2
        exit 1
    else
        ln -s "$target" "$path"
    fi

    echo "$path -> $target"
}

migrate() {     # the old layout: one folder of contracts, one folder of code
    local skills="$repo/skill_registry/skills" file name

    for file in "$agent"/contracts/*.yaml; do
        [[ -e "$file" ]] || continue
        name="$(basename "$file" .yaml)"

        if [[ -e "$skills/$name/contract.yaml" ]]; then
            echo "stop: $skills/$name already exists; compare it with $file by hand" >&2
            exit 1
        fi

        mkdir -p "$skills/$name"
        mv "$file" "$skills/$name/contract.yaml"
        [[ -e "$agent/code/$name.py" ]] && mv "$agent/code/$name.py" "$skills/$name/code.py"
        echo "moved $name into $skills/$name"
    done

    for folder in contracts code; do      # what is left (e.g. old/) stays for a person to look at
        [[ -d "$agent/$folder" ]] && echo "left: $agent/$folder (old versions); delete it when you no longer need it"
    done
    return 0
}

migrate_docs() {    # a real docs folder: its files go into git, one by one, never over a file there
    local target="$repo/orchestrator/agent/docs" file

    for file in "$agent"/docs/*; do
        [[ -e "$file" ]] || continue

        if [[ -e "$target/$(basename "$file")" ]]; then
            echo "stop: $target/$(basename "$file") already exists; compare it with $file by hand" >&2
            exit 1
        fi

        mv "$file" "$target/"
        echo "moved $file into $target"
    done

    rmdir "$agent/docs"
}

[[ -d "$agent/contracts" ]] && migrate
[[ -d "$agent/docs" && ! -L "$agent/docs" ]] && migrate_docs

if [[ -f "$agent/runbook.md" && ! -L "$agent/runbook.md" ]]; then
    mv -n "$agent/runbook.md" "$agent/runbook.md.local"
    echo "renamed $agent/runbook.md to runbook.md.local: merge it into $repo/orchestrator/agent/runbook.md, then delete it"
fi

link skills     "$repo/skill_registry/skills"
link runbook.md "$repo/orchestrator/agent/runbook.md"
link docs       "$repo/orchestrator/agent/docs"
link .claude/settings.json "$repo/orchestrator/agent/claude_settings.json"
