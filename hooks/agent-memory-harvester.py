#!/usr/bin/env python3
"""SubagentStop hook: harvest MEMORY blocks from completed subagent transcripts.

Reads the subagent's JSONL transcript, identifies the agent by the
first `agents/memory/<name>/_universal.md` mention (which appears in
each agent's system prompt), extracts `- [YYYY-MM-DD] ...` learning
lines from the agent's text output, dedupes against the existing
memory file, and appends new entries.

Always exits 0 — a memory write failure must never block a session.

AUTO-PUSH (opt-in)
------------------
After writing new learnings the hook can auto-commit and push the
memory directory to its configured git remote. This is DISABLED by
default so that public/open-source users who haven't wired up a remote
are unaffected.

To enable, set the environment variable before starting Claude Code:

    export AGENT_MEMORY_AUTOPUSH=1

Requirements: the memory dir (~/.claude/agents/memory/) must already
be a git repo with a remote named `origin` (branch `main`). If either
condition is absent the push is silently skipped. All git failures
(offline, auth error, nothing to commit) are swallowed — they are
logged to stderr at DEBUG level but never raise.

BRAIN LINK (opt-in)
-------------------
If the agent exists as an entity in a MUSE Brain
(https://github.com/falcoschaefer99-eng/muse-brain), each NEW learning can
also be sent there as one `mind_observe` call (MCP over HTTP JSON-RPC),
linked to the agent's entity by display name. Files always win: the memory
file is written first and any brain error is logged to stderr and ignored.

    MUSE_BRAIN_URL       e.g. https://your-brain.example.com/mcp   (required)
    MUSE_BRAIN_KEY       bearer key, OR
    MUSE_BRAIN_KEY_FILE  path to a file with a `MUSE_BRAIN_KEY=<key>` line.
                         Never logged.
    MUSE_BRAIN_TENANT    optional, sent as X-Brain-Tenant
    MUSE_BRAIN_LEDGER    optional ledger path (default
                         ~/.local/state/agent-memory-harvester/brain-sent.txt)

No URL or no key means no network call. Limits: 5s per request, 12s total,
at most 10 learnings per run; the first failure stops the run. Sent
learnings are recorded by hash so a retry never duplicates them.
"""
import fcntl
import json
import os
import re
import hashlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
from datetime import datetime, timezone
from pathlib import Path

MEMORY_LINE = re.compile(r'^- \[20\d{2}-\d{2}-\d{2}\].+$', re.MULTILINE)
AGENT_PATH = re.compile(r'agents/memory/([a-z][a-z0-9_-]+)/_universal\.md')
DATED_ENTRY = re.compile(r'^- \[(\d{4}-\d{2}-\d{2})\]')
SECTION_HEADER = re.compile(r'^## .+')

# Directory that is its own git repo (separate from ~/.claude).
MEMORY_DIR = Path.home() / '.claude' / 'agents' / 'memory'


def _estimate_tokens(text):
    """Rough token estimate: word_count * 1.3."""
    return int(len(text.split()) * 1.3)


def _parse_token_budget(frontmatter):
    """Extract token_budget from raw frontmatter text. Returns 500 if absent."""
    match = re.search(r'^token_budget:\s*(\d+)', frontmatter, re.MULTILINE)
    if match:
        return int(match.group(1))
    return 500


def _split_frontmatter(content):
    """Split file into (frontmatter_raw, body) strings.

    Returns ('', content) if the file doesn't start with a --- fence.
    The returned frontmatter_raw does NOT include the surrounding --- delimiters.
    """
    if not content.startswith('---'):
        return '', content
    end = content.find('\n---', 3)
    if end == -1:
        return '', content
    # frontmatter_raw: everything between the first '---\n' and the closing '---'
    frontmatter_raw = content[3:end].lstrip('\n')
    body = content[end + 4:]  # skip '\n---'
    return frontmatter_raw, body


def _collect_dated_entries(body):
    """Return a list of (date_str, line_str) tuples for all dated entries, oldest first.

    Entries without a date prefix are ignored (preserved in place, not prunable).
    """
    entries = []
    for line in body.splitlines():
        m = DATED_ENTRY.match(line)
        if m:
            entries.append((m.group(1), line))
    # Sort ascending so oldest are first candidates for pruning
    entries.sort(key=lambda t: t[0])
    return entries


def _rebuild_body(body, removed_lines):
    """Remove specific lines from body and strip sections that became empty.

    A section is empty if no non-blank, non-header content follows its ## header
    before the next ## header or end of file.
    """
    removed_set = set(removed_lines)
    lines = body.splitlines(keepends=True)

    # Pass 1: remove pruned entry lines
    kept = [ln for ln in lines if ln.rstrip('\n') not in removed_set]

    # Pass 2: remove empty sections (## header followed by only blank lines until
    # the next ## header or EOF)
    result = []
    i = 0
    while i < len(kept):
        line = kept[i]
        if SECTION_HEADER.match(line.rstrip('\n')):
            # Look ahead: collect consecutive blank lines; stop at next header or content
            j = i + 1
            while j < len(kept) and kept[j].strip() == '':
                j += 1
            # If we hit another header or EOF with nothing in between, skip this header
            if j >= len(kept) or SECTION_HEADER.match(kept[j].rstrip('\n')):
                i = j  # skip the empty section entirely
                continue
        result.append(line)
        i += 1

    return ''.join(result)


def enforce_budget(memory_file):
    """Prune oldest dated entries if the file exceeds its token_budget.

    Pruned entries are appended to <memory_file>.archive. Empty sections
    are removed after pruning. All errors are caught and logged — never raises.
    """
    try:
        content = memory_file.read_text(encoding='utf-8', errors='replace')
        frontmatter_raw, body = _split_frontmatter(content)
        budget = _parse_token_budget(frontmatter_raw)

        before_tokens = _estimate_tokens(content)
        if before_tokens <= budget:
            return  # already within budget

        agent_name = memory_file.parent.name
        dated_entries = _collect_dated_entries(body)
        if not dated_entries:
            return  # nothing prunable

        # Determine which entries to remove (oldest first) until under budget
        to_remove = []
        working_tokens = before_tokens
        for _date, line in dated_entries:
            if working_tokens <= budget:
                break
            line_tokens = _estimate_tokens(line)
            to_remove.append(line)
            working_tokens -= line_tokens

        if not to_remove:
            return

        # Archive pruned entries
        archive_file = memory_file.parent / (memory_file.name + '.archive')
        timestamp = datetime.now(tz=timezone.utc).isoformat(timespec='seconds')
        archive_block = f'\n## Pruned {timestamp}\n' + '\n'.join(to_remove) + '\n'

        with archive_file.open('a', encoding='utf-8') as af:
            fcntl.flock(af.fileno(), fcntl.LOCK_EX)
            try:
                af.write(archive_block)
            finally:
                fcntl.flock(af.fileno(), fcntl.LOCK_UN)

        # Rewrite memory file with pruned entries removed and empty sections stripped
        new_body = _rebuild_body(body, to_remove)
        # Reconstruct full file: preserve original frontmatter fences
        if frontmatter_raw:
            new_content = '---\n' + frontmatter_raw + '\n---' + new_body
        else:
            new_content = new_body

        after_tokens = _estimate_tokens(new_content)

        with memory_file.open('w', encoding='utf-8') as mf:
            fcntl.flock(mf.fileno(), fcntl.LOCK_EX)
            try:
                mf.write(new_content)
            finally:
                fcntl.flock(mf.fileno(), fcntl.LOCK_UN)

        print(
            f'[memory-harvester] {agent_name}: pruned {len(to_remove)} entries'
            f' ({before_tokens} → {after_tokens} tokens)',
            file=sys.stderr,
        )

    except Exception as exc:  # noqa: BLE001 — never block on pruning failure
        print(f'[memory-harvester] enforce_budget error: {exc}', file=sys.stderr)


BRAIN_TIMEOUT_S = 5
BRAIN_TOTAL_BUDGET_S = 12
BRAIN_MAX_PER_RUN = 10
KEY_LINE_RE = re.compile(r'^MUSE_BRAIN_KEY=(.*)$')
BRAIN_CONTENT_CAP = 500
BRAIN_PREFIX = '[agent-harvester, unverified] '
# Only crew entities get a brain link; any other memory dir skips the sink.
CREW = frozenset('''eli june reeve quinn kairo nikita harmony fischer thea thorn
sawyer michael evangeline kit indira colette dupin'''.split())
# Learnings that name the humans/companions or look like injected
# instructions are kept in the file but never sent to the brain.
BRAIN_DROP_RE = re.compile(r'falco|rook|rainer|ignore|instruction', re.IGNORECASE)
_MD_LINK_RE = re.compile(r'\[([^\]]*)\]\([^)]*\)')
_URL_RE = re.compile(r'\b[a-z][a-z0-9+.-]*://\S+', re.IGNORECASE)
_ANGLE_RE = re.compile(r'<[^>]*>')
_SENTINEL_RE = re.compile(r'(\[\[.*?\]\]|\{\{.*?\}\}|```|^\s*(?:system|assistant|user|human|memory)\s*:)',
                          re.IGNORECASE | re.MULTILINE)
KEY_RE = re.compile(r'^[A-Za-z0-9._~+/=-]{8,512}$')


def _brain_key():
    """Key from MUSE_BRAIN_KEY, else MUSE_BRAIN_KEY_FILE. Never logged."""
    key = os.environ.get('MUSE_BRAIN_KEY', '').strip()
    key_file = os.environ.get('MUSE_BRAIN_KEY_FILE', '').strip()
    if not key and key_file:
        try:
            raw = Path(key_file).expanduser().read_text(encoding='utf-8')
        except OSError:
            return None
        for ln in raw.splitlines():
            m = KEY_LINE_RE.match(ln.strip())
            if m:
                key = m.group(1).strip().strip('"\'')
                break
    return key if KEY_RE.match(key) else None


def _ledger_path():
    override = os.environ.get('MUSE_BRAIN_LEDGER', '').strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / '.local' / 'state' / 'agent-memory-harvester' / 'brain-sent.txt'


def _entry_hash(agent, line):
    return hashlib.sha256(f'{agent}\n{line}'.encode('utf-8')).hexdigest()[:32]


def _sanitize(line):
    text = re.sub(r'^- ', '', line, count=1)
    text = _MD_LINK_RE.sub(r'\1', text)
    text = _URL_RE.sub('', text)
    text = _ANGLE_RE.sub('', text)
    text = _SENTINEL_RE.sub('', text)
    return re.sub(r'\s+', ' ', text).strip()


def _observe_args(agent, line):
    """Map one learning line to mind_observe args. Grip, territory and tags
    are fixed here, never derived from the (untrusted) learning text."""
    content = (BRAIN_PREFIX + _sanitize(line))[:BRAIN_CONTENT_CAP]
    return {
        'mode': 'observe',
        'content': content,
        'entity_name': agent.replace('-', ' ').replace('_', ' ').title(),
        'territory': 'craft',
        'grip': 'present',
        'tags': ['agent-harvester', agent],
        'context': 'agent learning (harvester)',
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a 3xx surfaces as HTTPError: the bearer key never follows a redirect


_OPENER = urllib.request.build_opener(_NoRedirect)


def _brain_call(url, key, tenant, arguments, timeout):
    """POST one tools/call. True only on a JSON-RPC success; raises/returns False otherwise."""
    body = json.dumps({
        'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': 'mind_observe', 'arguments': arguments},
    }).encode('utf-8')
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream',
        'Authorization': f'Bearer {key}',
    }
    if tenant:
        headers['X-Brain-Tenant'] = tenant
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    with _OPENER.open(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode('utf-8', errors='replace'))
    if not isinstance(payload, dict) or 'error' in payload:
        return False
    result = payload.get('result')
    return isinstance(result, dict) and not result.get('isError')


def brain_sink(agent, entries):
    """Send new learnings to the MUSE Brain. Opt-in; never raises.

    Returns the number of observations accepted by the brain.
    """
    try:
        if agent not in CREW:
            return 0
        url = os.environ.get('MUSE_BRAIN_URL', '').strip()
        if not url:
            return 0
        parsed = urlparse(url)
        local = parsed.hostname in ('localhost', '127.0.0.1', '::1')
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and local):
            print('[memory-harvester] brain: MUSE_BRAIN_URL must be https', file=sys.stderr)
            return 0
        key = _brain_key()
        if not key:
            return 0

        ledger = _ledger_path()
        try:
            sent = set(ledger.read_text(encoding='utf-8').split())
        except OSError:
            sent = set()
        pending = [(e, _entry_hash(agent, e)) for e in entries if not BRAIN_DROP_RE.search(e)]
        pending = [(e, h) for e, h in pending if h not in sent][:BRAIN_MAX_PER_RUN]
        if not pending:
            return 0

        tenant = os.environ.get('MUSE_BRAIN_TENANT', '').strip() or None
        deadline = time.monotonic() + BRAIN_TOTAL_BUDGET_S
        ok = 0
        for line, digest in pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0.5:
                break
            try:
                accepted = _brain_call(url, key, tenant, _observe_args(agent, line),
                                       min(BRAIN_TIMEOUT_S, remaining))
            except (OSError, ValueError, urllib.error.URLError) as exc:
                print(f'[memory-harvester] brain: {type(exc).__name__}, stopping', file=sys.stderr)
                break
            if not accepted:
                print('[memory-harvester] brain: observe rejected, stopping', file=sys.stderr)
                break
            ok += 1
            try:
                ledger.parent.mkdir(parents=True, exist_ok=True)
                with ledger.open('a', encoding='utf-8') as fh:
                    fh.write(digest + '\n')
            except OSError as exc:
                print(f'[memory-harvester] brain: ledger write failed: {exc}', file=sys.stderr)
        if ok:
            print(f'[memory-harvester] {agent}: sent {ok} learning(s) to brain', file=sys.stderr)
        return ok
    except Exception as exc:  # noqa: BLE001 — brain is optional, never block
        print(f'[memory-harvester] brain error: {type(exc).__name__}', file=sys.stderr)
        return 0


def git_autopush() -> None:
    """Commit and push the memory repo in the background, non-blocking.

    Fires only when AGENT_MEMORY_AUTOPUSH=1 is set and MEMORY_DIR is a
    git repo with a configured remote. All errors are swallowed so a
    network hiccup or missing credentials never affects the agent pipeline.
    """
    if os.environ.get('AGENT_MEMORY_AUTOPUSH') != '1':
        return
    # Graceful degradation: skip if not a git repo
    if not (MEMORY_DIR / '.git').is_dir():
        return

    timestamp = datetime.now(tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    commit_msg = f'auto: agent learnings {timestamp}'

    # Run in a fully detached child process so the hook exits immediately.
    # stdout/stderr from git are suppressed to keep SubagentStop output clean.
    # start_new_session=True detaches from our process group entirely.
    devnull = subprocess.DEVNULL
    try:
        subprocess.Popen(
            [
                'sh', '-c',
                (
                    f'cd {str(MEMORY_DIR)!r} && '
                    f'git add -A && '
                    f'git diff --cached --quiet || '
                    f'(git commit -m {commit_msg!r} && git push origin main)'
                ),
            ],
            stdout=devnull,
            stderr=devnull,
            start_new_session=True,
        )
    except Exception as exc:  # noqa: BLE001 — never surface to caller
        print(f'[memory-harvester] git_autopush: {exc}', file=sys.stderr)


def resolve_agent_name(transcript_path, text_fallback):
    """Determine agent name from the .meta.json sibling, falling back to text.

    Claude Code writes a `<id>.meta.json` next to each subagent transcript with
    `{"agentType": "fischer", ...}`. That's the authoritative signal. The
    transcript path the hook receives is usually a symlink in /tmp/.../tasks/
    pointing to the real .jsonl in ~/.claude/projects/<session>/subagents/.
    """
    try:
        real = os.path.realpath(transcript_path)
        meta_path = re.sub(r'\.jsonl$', '.meta.json', real)
        if meta_path != real and os.path.exists(meta_path):
            with open(meta_path, encoding='utf-8') as fh:
                meta = json.load(fh)
            agent_type = meta.get('agentType')
            if isinstance(agent_type, str) and agent_type:
                return agent_type
    except (OSError, json.JSONDecodeError):
        pass

    # Fallback: agent system prompts reference their own memory path
    match = AGENT_PATH.search(text_fallback)
    if match:
        return match.group(1)
    return None


def collect_text(transcript_path):
    """Collect only assistant-authored text. User-role messages (prompts,
    system reminders) can contain MEMORY-block-shaped examples we must not
    harvest."""
    chunks = []
    with open(transcript_path, encoding='utf-8', errors='replace') as fh:
        for raw in fh:
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg = entry.get('message') or {}
            role = msg.get('role') or entry.get('type')
            if role != 'assistant':
                continue
            content = msg.get('content')
            if isinstance(content, str):
                chunks.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get('type') == 'text':
                        chunks.append(part.get('text', ''))
    return '\n'.join(chunks)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return

    transcript = payload.get('agent_transcript_path') or payload.get('transcript_path')
    if not transcript or not os.path.exists(transcript):
        return

    text = collect_text(transcript)
    if not text:
        return

    agent = resolve_agent_name(transcript, text)
    if not agent:
        return

    memory_file = Path.home() / '.claude' / 'agents' / 'memory' / agent / '_universal.md'
    if not memory_file.exists():
        return

    candidates = MEMORY_LINE.findall(text)
    if not candidates:
        return

    existing = memory_file.read_text(encoding='utf-8', errors='replace')
    # Dedupe both against existing file content and within this batch
    seen: set[str] = set()
    new_entries: list[str] = []
    for line in candidates:
        line = line.rstrip()
        if line in existing or line in seen:
            continue
        seen.add(line)
        new_entries.append(line)

    if not new_entries:
        return

    with memory_file.open('a', encoding='utf-8') as fh:
        # Advisory lock to handle concurrent SubagentStop firings on the same file
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            for entry in new_entries:
                fh.write(entry + '\n')
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    print(
        f'[memory-harvester] {agent}: appended {len(new_entries)} learning(s)',
        file=sys.stderr,
    )

    enforce_budget(memory_file)

    git_autopush()

    brain_sink(agent, new_entries)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:  # pragma: no cover — never block on harvester failure
        print(f'[memory-harvester] error: {exc}', file=sys.stderr)
    sys.exit(0)
