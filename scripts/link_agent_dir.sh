#!/usr/bin/env bash
# Make the agent's folder in the workspace: links to what is in git, and real folders for what changes on each run.
#
#   $EIGEN_VECTOR_WS/agent/
#     skills     -> <repo>/skill_registry/skills          the skill library (write_skill saves here)
#     runbook.md -> <repo>/orchestrator/agent/runbook.md  what the robot is
#     docs       -> <repo>/orchestrator/agent/docs        the agent's codebase docs
#     .claude/settings.json                               the task session's permissions, made from
#                                                         orchestrator/agent/claude_settings.json (not a link)
#     knowledge/                                          claims and their log (not in git)
#     stamps.yaml                                         what each skill was proved against (not in git)
#
#   $EIGEN_VECTOR_WS/agent_discovery/                     where the discovery session starts
#     skills, runbook.md, docs                            the same links
#     CLAUDE.md, AGENTS.md -> <repo>/orchestrator/agent/discovery_session.md
#     .claude/skills/discover, .codex/skills/discover -> <repo>/orchestrator/agent/skills/discover
#     .claude/settings.json                               made from claude_settings.json and claude_settings_discovery.json
#     .codex/rules/agent.rules                            the same shell rules for Codex (both folders have one)
#     progress.md                                         the state of discovery (the discover skill makes it)
#
# The settings are made, not linked: the task session's hide each stack repo in src/ by its absolute path.
# Safe to run again. It never deletes a file: a real file or folder where a link must go stops the script, and so
# does a settings file that agent_settings.py did not make.
# The old layout moves in: agent/contracts/<name>.yaml and agent/code/<name>.py go into skills/<name>/, and the files
# of a real agent/docs/ go into the docs folder in git. A real agent/runbook.md is renamed runbook.md.local, for a
# person to merge into the one in git.
#
# Usage: scripts/link_agent_dir.sh [AGENT_DIR [DISCOVERY_DIR [SRC_DIR]]]
#   (defaults: $EIGEN_VECTOR_WS/agent, $EIGEN_VECTOR_WS/agent_discovery, $EIGEN_VECTOR_WS/src)
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
agent="${1:-${EIGEN_VECTOR_WS:?set EIGEN_VECTOR_WS or give the agent folder}/agent}"
discovery="${2:-$(dirname "$agent")/agent_discovery}"
src="${3:-$(dirname "$agent")/src}"

mkdir -p "$agent/knowledge" "$agent/.claude" "$discovery/.claude/skills" "$discovery/.codex/skills"

link() {        # link NAME TARGET [FOLDER]
    local path="${3:-$agent}/$1" target="$2"

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

for folder in "$agent" "$discovery"; do
    link skills     "$repo/skill_registry/skills"     "$folder"
    link runbook.md "$repo/orchestrator/agent/runbook.md" "$folder"
    link docs       "$repo/orchestrator/agent/docs"   "$folder"
done

for name in CLAUDE.md AGENTS.md; do            # what Claude Code and Codex read first
    link "$name" "$repo/orchestrator/agent/discovery_session.md" "$discovery"
done

for tool in .claude .codex; do
    link "$tool/skills/discover" "$repo/orchestrator/agent/skills/discover" "$discovery"
done

stack=()                                     # the stack repos: a task session uses the skills, not their code
for folder in "$src"/*; do                 # as named in src/: agent_settings.py denies both the name and the target
    real="$(realpath "$folder")"
    [[ -d "$real" && "$repo/" != "$real/"* ]] && stack+=("${folder%/}")
done

settings="$repo/orchestrator/agent/claude_settings.json"
python3 "$repo/scripts/agent_settings.py" "$agent/.claude/settings.json" "$settings" --hide "${stack[@]}" \
    --codex "$agent/.codex/rules/agent.rules"
python3 "$repo/scripts/agent_settings.py" "$discovery/.claude/settings.json" "$settings" \
    "$repo/orchestrator/agent/claude_settings_discovery.json" --no-edit "$discovery/skills" \
    --codex "$discovery/.codex/rules/agent.rules"
echo "made the settings and Codex rules of $agent (hides ${#stack[@]} stack repos from Claude Code) and $discovery"
