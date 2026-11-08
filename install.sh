#!/bin/sh
# install.sh [--all | --builder | --operations | <name>...] [--force] [--list]
#
# Copies <squad>/<name>/agent.md to $CLAUDE_AGENTS_DIR/<name>.md (default
# ~/.claude/agents/<name>.md). Refuses to overwrite an existing file unless
# --force, printing which names it skipped. POSIX sh, no bashisms.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEST_DIR="${CLAUDE_AGENTS_DIR:-$HOME/.claude/agents}"

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

force=0
names=""
mode=""

for arg in "$@"; do
    case "$arg" in
        --force)
            force=1
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
    echo "usage: install.sh [--all | --builder | --operations | <name>...] [--force]" >&2
    echo "       install.sh --list" >&2
    exit 1
fi

status=0
for name in $names; do
    install_one "$name" "$force" || status=1
done

exit $status
