#!/bin/sh
# install.sh [--all | --builder | --operations | <name>...] [--force] [--no-hook] [--list]
#
# Copies <squad>/<name>/agent.md to $CLAUDE_AGENTS_DIR/<name>.md (default
# ~/.claude/agents/<name>.md). Refuses to overwrite an existing file unless
# --force, printing which names it skipped. POSIX sh, no bashisms.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEST_DIR="${CLAUDE_AGENTS_DIR:-$HOME/.claude/agents}"
HOOKS_DIR="${CLAUDE_HOOKS_DIR:-$HOME/.claude/hooks}"
HARVESTER_SRC="$SCRIPT_DIR/hooks/agent-memory-harvester.py"

# Squad -> space-separated list of agent names that actually have a
# generated builder/<name>/ or operations/<name>/ directory. Scans the
# filesystem at run time, so this always reflects whichever agents the
# generator most recently shipped -- never hardcode a roster here.
builder_agents() {
    for dir in "$SCRIPT_DIR"/builder/*/; do
        [ -f "${dir}agent.md" ] || continue
        basename "$dir"
    done
}

operations_agents() {
    for dir in "$SCRIPT_DIR"/operations/*/; do
        [ -d "$dir" ] || continue
        [ -f "${dir}agent.md" ] || continue
        basename "$dir"
    done
}

all_agents() {
    builder_agents
    operations_agents
}

validate_name() {
    # Only [A-Za-z0-9_-], and non-empty. Rejects '/' and '..' as a side
    # effect (neither character is in the allowed set) before any path is
    # built from user input.
    case "$1" in
        "")
            return 1
            ;;
        *[!A-Za-z0-9_-]*)
            return 1
            ;;
    esac
    return 0
}

find_agent_file() {
    name="$1"
    if [ -f "$SCRIPT_DIR/builder/$name/agent.md" ]; then
        echo "$SCRIPT_DIR/builder/$name/agent.md"
        return 0
    fi
    if [ -f "$SCRIPT_DIR/operations/$name/agent.md" ]; then
        echo "$SCRIPT_DIR/operations/$name/agent.md"
        return 0
    fi
    return 1
}

print_list() {
    printf '%-10s %-12s %s\n' "AGENT" "SQUAD" "SOURCE"
    for name in $(builder_agents); do
        printf '%-10s %-12s %s\n' "$name" "builder" "builder/$name/agent.md"
    done
    for name in $(operations_agents); do
        printf '%-10s %-12s %s\n' "$name" "operations" "operations/$name/agent.md"
    done
}

install_one() {
    name="$1"
    force="$2"
    if ! validate_name "$name"; then
        echo "error: invalid agent name '$name' — only [A-Za-z0-9_-] allowed, no / or .." >&2
        return 1
    fi
    src=$(find_agent_file "$name") || {
        echo "skip: $name (no agent.md found — not built yet, or not a real agent name)" >&2
        return 1
    }
    dest="$DEST_DIR/$name.md"
    if [ -e "$dest" ] && [ "$force" != "1" ]; then
        echo "skip: $name ($dest already exists — pass --force to overwrite)"
        return 0
    fi
    mkdir -p "$DEST_DIR"
    cp "$src" "$dest"
    echo "installed: $name -> $dest"
}

# Offer the memory harvester (a SubagentStop hook that appends every agent's
# MEMORY: block to its memory file). Never edits settings.json: copies the
# script only if you say yes, then prints the snippet for you to paste.
offer_harvester() {
    [ -f "$HARVESTER_SRC" ] || return 0
    hook_path="$HARVESTER_SRC"
    echo ""
    echo "Memory harvester (optional): without it, agents' MEMORY: blocks are not saved to their memory files."
    if [ -t 0 ] && [ -t 1 ]; then
        printf 'Copy it to %s/agent-memory-harvester.py? [y/N] ' "$HOOKS_DIR"
        read -r answer || answer=""
        case "$answer" in
            y|Y|yes|YES)
                dest_hook="$HOOKS_DIR/agent-memory-harvester.py"
                if [ -e "$dest_hook" ] && ! cmp -s "$HARVESTER_SRC" "$dest_hook"; then
                    echo "skip copy: $dest_hook already exists and differs (not overwriting)"
                else
                    mkdir -p "$HOOKS_DIR"
                    cp "$HARVESTER_SRC" "$dest_hook"
                    chmod 755 "$dest_hook"
                    hook_path="$dest_hook"
                    echo "copied: $dest_hook"
                fi
                ;;
        esac
    fi
    echo "To enable, add this to the \"hooks\" object in ~/.claude/settings.json (merge with any existing SubagentStop entry):"
    cat <<EOF

    "SubagentStop": [
      {
        "matcher": "*",
        "hooks": [
          { "type": "command", "command": "python3 $hook_path", "timeout": 20 }
        ]
      }
    ]

Optional MUSE Brain link: set MUSE_BRAIN_URL plus MUSE_BRAIN_KEY (or MUSE_BRAIN_KEY_FILE)
in the "env" object of the same file. See the README, Memory section.
EOF
}

force=0
names=""
mode=""
install_hook=1

for arg in "$@"; do
    case "$arg" in
        --force)
            force=1
            ;;
        --no-hook)
            install_hook=0
            ;;
        --list)
            print_list
            exit 0
            ;;
        --all)
            mode="all"
            ;;
        --builder)
            mode="builder"
            ;;
        --operations)
            mode="operations"
            ;;
        -*)
            echo "unknown flag: $arg" >&2
            exit 1
            ;;
        *)
            names="$names $arg"
            ;;
    esac
done

case "$mode" in
    all)
        names=$(all_agents)
        ;;
    builder)
        names=$(builder_agents)
        ;;
    operations)
        names=$(operations_agents)
        ;;
esac

names=$(echo "$names" | tr -s ' \n' ' ')

if [ -z "$(echo "$names" | tr -d ' ')" ]; then
    echo "usage: install.sh [--all | --builder | --operations | <name>...] [--force] [--no-hook]" >&2
    echo "       install.sh --list" >&2
    exit 1
fi

status=0
for name in $names; do
    install_one "$name" "$force" || status=1
done

if [ "$install_hook" = "1" ]; then
    offer_harvester
fi

exit $status
