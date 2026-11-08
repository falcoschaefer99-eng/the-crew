#!/usr/bin/env python3
"""the-crew generator: regenerates this repo's full roster (eleven Builder
Squad + six Operations Squad agents) from ~/.claude/agents, plus two
per-agent source overrides read from Michael's and Evangeline's own public
release repos. Not a generic transform tool -- BUILDER_LIVE/OPERATIONS_LIVE
below are hardcoded lists of names, not config; add an agent by adding it
there.

Reads `<source_dir>/<name>.md` for each agent (or an override path -- see
source_overrides_for), applies the mechanical transform below, and writes:
  - builder/<name>/ or operations/<name>/: agent.md, README.md, portrait
  - plugins/{builder,operations}-squad/agents/<name>.md (copy of agent.md)
  - plugins/{builder,operations}-squad/.claude-plugin/plugin.json
  - .claude-plugin/marketplace.json
  - README.md (root, tables regenerated from frontmatter)

Idempotent: re-running with unchanged sources produces no diff. Never hand-
edit anything this script writes -- change the transform instead.

THE TRANSFORM, in order:
1. Frontmatter: keep name/display_name/role/team/description/model/tools/
   max_tokens/temperature. Drop api_model (house pin) and references (house
   paths). Force `team` to match the squad folder the agent is placed in
   (source frontmatter says "team: builder" for every house def regardless
   of actual squad -- the builder/operations split is a the-crew-only
   packaging decision, not a house concept, so we don't trust the source
   value). Add license: Apache-2.0, license_scope, and source: <repo url>.
2. `- Dispatch: Rook recognizes <X>` -> `- Dispatch: invoke with /<name>, or
   let your orchestrator dispatch on <X>`.
3. `### Brain Entity Integration` (to EOF or next heading) -> `### Memory`,
   built from the same "What X remembers" / "How ... works" bullet lists,
   dropping every step that mentions Rook, the cloud brain, or brain
   observations, and the "Memory as competitive advantage" paragraph.
   Colette's source has no such heading (a newer, differently-shaped def) --
   skipped for her, logged, not fabricated.
4. Name-generalization passes (scrub_stray_house_names, scrub_stray_rainer_
   names, scrub_stray_falco_mentions, the explicit per-agent rewrite tables)
   turn every remaining bare Rook/Rainer/Falco mention and every real
   private individual named in an illustrative example into role-generic
   phrasing. Not every survivor can be handled mechanically (a name inside
   a named-collaborator routing table, or a cross-substrate technique name,
   would be corrupted by a blind find-replace) -- those are left for the
   fail-loud scan to catch, reported, and excluded from that run rather
   than guessed at.
5. Sawyer/Thorn section-rewrites: drop the house-specific deploy protocol
   section entirely, replace the project-recipe section with one paragraph
   pointing at an external per-project memory file, and placeholder-pass
   every remaining concrete host/path/unit identifier file-wide (not just
   the sections that originally named them) against an external guard file
   -- see load_placeholders.
6. Fail-loud scan: after 1-5, ANY remaining match against the active rule
   set -- built at runtime from STATIC_FAIL_LOUD_PATTERNS (below, non-
   identifying shapes only) plus two external guard files (house-specific
   identifiers, private individuals' names) -- excludes that agent from
   the run and fails it (see main(): any blocked agent is exit 1, not a
   silent partial success). Reports every matching line in a file, not
   just the first, and reports a pattern's LABEL on a hit, never a
   pattern's own regex source (the private-names/house-patterns rules'
   sources ARE their guard lists; printing one on a hit would blast the
   whole list into the log over a single match).

Three things this generator protects and refuses to run without (each is
an external file, never checked into this repo, loaded at runtime, exit 2
if missing or empty -- see load_private_names / load_house_patterns /
load_placeholders and their --private-names / --house-patterns /
--placeholders flags): the private individuals it must never name, the
house-specific infrastructure identifiers it must never leak, and the
per-agent placeholder substitutions that keep Sawyer's and Thorn's
deploy/diagnostic examples generic. A generator that quietly runs
unprotected when a guard file goes missing is worse than one that stops.
"""
import argparse
import filecmp
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_URL = "https://github.com/falcoschaefer99-eng/the-crew"
DEFAULT_SOURCE_DIR = Path.home() / ".claude" / "agents"
DEFAULT_PORTRAITS_DIR = Path.home() / "AI" / "funkatorium-web" / "_deploy" / "images" / "crew"
DEFAULT_PRIVATE_NAMES_FILE = Path.home() / ".claude" / "the-crew-private-names.txt"
DEFAULT_HOUSE_PATTERNS_FILE = Path.home() / ".claude" / "the-crew-house-patterns.txt"
DEFAULT_PLACEHOLDERS_FILE = Path.home() / ".claude" / "the-crew-placeholders.txt"

# Full Wave 2 roster. Both squads are entirely live -- no "coming" rows.
BUILDER_LIVE = [
    "eli", "june", "reeve", "quinn", "kairo", "nikita", "harmony", "fischer",
    "thea", "thorn", "sawyer",
]
OPERATIONS_LIVE = ["kit", "indira", "colette", "dupin", "michael", "evangeline"]

FRONTMATTER_KEEP_ORDER = [
    "name", "display_name", "role", "team", "description",
    "model", "tools", "max_tokens", "temperature",
]

# Static rules only -- the private-individuals rule is NOT here. It's built
# at runtime from an external guard file (load_private_names /
# build_fail_loud_patterns below) precisely so this file never lists the
# names it protects. (label, compiled pattern) pairs -- fail_loud_scan
# reports the label, never the pattern's own regex source, so the private-
# names rule (below) never echoes its own name list into a hit report.
# Non-identifying rules only -- these don't name a specific box, domain, or
# unit, just a generic pattern shape (a bare house-orchestrator name, an
# un-redacted local path, an ssh target that isn't the placeholder). The
# house-infrastructure rules (specific hosts/units/domains) live in an
# external guard file, same reasoning as the private-names list: this
# module must not itself be the leak it's trying to prevent.
STATIC_FAIL_LOUD_PATTERNS = [
    ("mind_x(...) call", re.compile(r"mind_[a-z_]+\(")),
    ("bare Rook", re.compile(r"\bRook\b")),
    ("bare Falco", re.compile(r"\bFalco\b")),
    ("bare Rainer", re.compile(r"\bRainer\b")),
    ("/Users/ path (non-YOU placeholder)", re.compile(r"/Users/(?!YOU/)")),
    ("ssh (non-<host> target)", re.compile(r"ssh (?!<host>)")),
]


def load_house_patterns(path):
    """Read the house-infrastructure guard file: `label<TAB>python-regex`
    per line, '#' comments, blank lines ignored. Lives OUTSIDE this repo
    on purpose (default ~/.claude/the-crew-house-patterns.txt, overridable
    with --house-patterns). Returns a list of (label, compiled pattern)
    pairs -- empty if the file doesn't exist or has nothing usable in it,
    which the caller (main()) must treat as a hard refusal, same as
    load_private_names."""
    if not path.exists():
        return []
    patterns = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0]
        if not line.strip():
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        label, regex = parts[0].strip(), parts[1].strip()
        if label and regex:
            patterns.append((label, re.compile(regex)))
    return patterns


def load_private_names(path):
    """Read the private-names guard file: one name per line, '#' starts a
    comment (rest of the line dropped), blank lines ignored. Lives OUTSIDE
    this repo on purpose (default ~/.claude/the-crew-private-names.txt,
    overridable with --private-names). Returns a list of names -- empty if
    the file doesn't exist or has nothing in it after stripping comments/
    blanks. An empty return is a hard refusal to the caller, not "no
    private names to protect": see main()."""
    if not path.exists():
        return []
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.append(line)
    return names


def build_fail_loud_patterns(private_names, house_patterns):
    """STATIC_FAIL_LOUD_PATTERNS plus the house-infrastructure patterns
    (already labeled, loaded from the external guard file as-is) plus one
    whole-word, case-sensitive alternation pattern built from private_names
    -- the only place those names ever exist as a Python object, never as
    a literal in this module's source. Labeled "private name" generically
    (not, say, the matched name) so a hit report never echoes the guard
    list itself."""
    patterns = list(STATIC_FAIL_LOUD_PATTERNS) + list(house_patterns)
    if private_names:
        alternation = "|".join(re.escape(n) for n in private_names)
        patterns.append(("private name", re.compile(rf"\b({alternation})\b")))
    return patterns


class BuildError(Exception):
    """A fail-loud rule fired. Caller prints the message and stops -- no
    silent stripping."""


# ---------------------------------------------------------------------------
# Frontmatter: grouped by raw lines (not fully parsed YAML) so every field we
# keep is reproduced byte-identical, including multi-line description and
# the tools list.
# ---------------------------------------------------------------------------

def split_frontmatter(text):
    lines = text.split("\n")
    if lines[0] != "---":
        raise BuildError("source file has no '---' frontmatter opening")
    end = lines.index("---", 1)
    fm_lines = lines[1:end]
    body = "\n".join(lines[end + 1:])

    fields = []
    key = None
    block = []
    for line in fm_lines:
        m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*):", line)
        if m:
            if key is not None:
                fields.append((key, block))
            key = m.group(1)
            block = [line]
        else:
            block.append(line)
    if key is not None:
        fields.append((key, block))
    return fields, body


def scalar_value(raw_lines):
    """First-line scalar value for a single-line frontmatter field, e.g.
    ["name: june"] -> "june"."""
    first = raw_lines[0]
    return first.split(":", 1)[1].strip()


def folded_text(raw_lines):
    """YAML '>' folded-scalar text (description) joined into one string for
    prose extraction (one-liner, bullets) -- not used for output formatting,
    only for deriving README copy."""
    body_lines = raw_lines[1:] if raw_lines[0].rstrip().endswith(">") else [raw_lines[0].split(":", 1)[1]]
    return " ".join(l.strip() for l in body_lines if l.strip())


def build_frontmatter(fields, team_override):
    """team_override replaces the source's own `team` value. Every house
    def says "team: builder" regardless of actual squad (the builder/
    operations split is a the-crew-only packaging decision, not a house
    concept) -- so the source value isn't trusted; the caller passes what
    squad folder this agent is actually being placed in."""
    by_key = {}
    for k, v in fields:
        by_key.setdefault(k, v)
    ordered_lines = []
    for key in FRONTMATTER_KEEP_ORDER:
        if key not in by_key:
            continue
        if key == "team":
            ordered_lines.append(f"team: {team_override}")
        else:
            ordered_lines.extend(by_key[key])
    ordered_lines.append("license: Apache-2.0")
    ordered_lines.append(
        'license_scope: "Apache-2.0 covers the Protocol, Process and Output '
        'Format sections. Identity, chart, personality and lore are reserved '
        'character IP — see LICENSE and NOTICE."'
    )
    ordered_lines.append(f"source: {SOURCE_URL}")
    return "---\n" + "\n".join(ordered_lines) + "\n---"


# ---------------------------------------------------------------------------
# Body transform: Dispatch line + Memory section
# ---------------------------------------------------------------------------

DISPATCH_RE = re.compile(r"^- Dispatch: Rook recognizes (.+)$", re.MULTILINE)


def transform_dispatch(body, name):
    def repl(m):
        return f"- Dispatch: invoke with /{name}, or let your orchestrator dispatch on {m.group(1)}"
    return DISPATCH_RE.subn(repl, body)[0]


def scrub_stray_house_names(text, scrub_log, source_name):
    """See module docstring, JUNE'S ADDITION. Runs after transform_dispatch
    and the Memory-section rebuild, on the FULLY ASSEMBLED agent_md
    (frontmatter + body) -- not body alone -- so a stray "Rook" mention in
    a source's frontmatter `description` (e.g. indira.md's "so Rook and
    Falco can focus") gets caught too, not just body text. Verified
    zero-diff for every Wave-1 agent whose frontmatter has no house names."""
    def repl(m):
        line = m.group(0)
        new_line = line.replace("Rook/Rainer", "your orchestrator")
        new_line = re.sub(r"\bRook\b", "your orchestrator", new_line)
        if new_line != line:
            scrub_log.append((source_name, line, new_line))
        return new_line
    return re.sub(r"^.*\bRook\b.*$", repl, text, flags=re.MULTILINE)


INTERPLAY_ARROW_RE = re.compile(r"←|→|<-|->")
DISPATCH_LINE_RE = re.compile(r"^-\s*Dispatch:")


def scrub_stray_rainer_names(text, scrub_log, source_name):
    """Rainer (the creative-squad orchestrator) is a house name too, same
    as Rook -- but narrower: only mechanically generalized on Dispatch or
    Interplay lines (dupin.md "Dispatch: Rainer recognizes...", thorn.md
    "Thorn <- Rainer/Rook:"), matched by the same arrow glyphs used across
    every Interplay bullet -- unicode (←/→) for most sources, ASCII
    (<-/->) for dupin.md specifically (a real formatting difference, not a
    typo). Any OTHER bare "Rainer" (a memory-bullet illustrative example, a
    named creative-skill routing table) is left untouched here and caught
    by the fail-loud scan instead -- it might be legitimate product copy,
    and mechanically mangling it (e.g. "Rainer-on-Codex" -> "your
    orchestrator-on-Codex") would silently corrupt meaning rather than
    generalize a name. See build report for the lines this left for a
    human call."""
    def repl(m):
        line = m.group(0)
        stripped = line.strip()
        if not (DISPATCH_LINE_RE.match(stripped) or INTERPLAY_ARROW_RE.search(line)):
            return line
        # Collapse "Rainer/Rook"-shaped combos (after the Rook scrub above
        # has already turned the Rook half into "your orchestrator") into a
        # single "your orchestrator" rather than repeating the phrase twice.
        new_line = re.sub(r"Rainer/your orchestrator|your orchestrator/Rainer", "your orchestrator", line)
        if new_line == line:
            new_line = re.sub(r"\bRainer\b", "your orchestrator", line)
        if new_line != line:
            scrub_log.append((source_name, line, new_line))
        return new_line
    return re.sub(r"^.*\bRainer\b.*$", repl, text, flags=re.MULTILINE)


def scrub_stray_falco_mentions(text, scrub_log, source_name):
    """Symmetric to scrub_stray_house_names, for "Falco". Runs after the
    explicit FALCO_NAME_TABLE, so it only catches survivors the hand-
    written table didn't already rewrite with precise phrasing."""
    def repl(m):
        line = m.group(0)
        new_line = re.sub(r"\bFalco\b", "the user", line)
        if new_line != line:
            scrub_log.append((source_name, line, new_line))
        return new_line
    return re.sub(r"^.*\bFalco\b.*$", repl, text, flags=re.MULTILINE)


def apply_literal_table(text, entries, log):
    """Shared applier for every explicit old->new rewrite table in this
    file (the Falco/Rainer name tables and the Sawyer/Thorn placeholder
    passes) -- matched by literal substring text, not a fixed line number,
    since public-release/source line numbers can shift out from under a
    table entry written against an older copy. Replaces EVERY occurrence
    of each pair (not just the first), logging one line per occurrence
    actually replaced. Doesn't know or log the agent name -- callers
    (below) each add their own name to every tuple this returns."""
    for old, new in entries:
        found = False
        while old in text:
            found = True
            idx = text.find(old)
            line_no = text.count("\n", 0, idx) + 1
            text = text.replace(old, new, 1)
            log.append((line_no, old, new, "applied"))
        if not found:
            log.append((None, old, new, "NOT FOUND (checked, not assumed -- table entry skipped)"))
    return text


# The brief's small explicit table: precise, meaning-preserving rewrites for
# the lines that most needed hand-crafted phrasing rather than a mechanical
# "Falco" -> "the user" swap. Applied before scrub_stray_falco_mentions so
# these exact phrasings win.
FALCO_NAME_TABLE = {
    "indira": [
        ("what Falco actually needs", "what the user actually needs"),
        ("Falco's energy is low", "the user's energy is low"),
    ],
    "colette": [
        ("When Falco shows you", "When the user shows you"),
    ],
    "kit": [
        ("or future-Falco", "or your future self"),
        ("Falco iterates mockups", "this user iterates mockups"),
        ("Falco prefers", "this user prefers"),
    ],
    "evangeline": [
        ("(Falco's strategy)", "(the founder's stated strategy)"),
    ],
}


def apply_falco_table(text, name, report_log):
    local_log = []
    text = apply_literal_table(text, FALCO_NAME_TABLE.get(name, []), local_log)
    report_log.extend((name, line_no, old, new, status) for line_no, old, new, status in local_log)
    return text


# Coordinator decisions for the Rainer fail-loud stops (small pass 3):
# hand-crafted, meaning-preserving rewrites rather than a mechanical name
# swap. Colette's two remaining Sahar mentions (beyond the one the
# coordinator gave exact text for) use the SAME role-not-name terms
# established there ("your copywriter" for Rainer, "your pitch writer" for
# Sahar) for a single stable identity across her file -- June's addition,
# not dictated verbatim, logged like everything else. Applied before
# scrub_stray_rainer_names so these exact phrasings win.
RAINER_REWRITE_TABLE = {
    "kairo": [
        ("(Rainer-on-Codex)", "(the same review run on a different model family)"),
    ],
    "indira": [
        ("during Rainer sprint", "during a product sprint"),
    ],
    "colette": [
        (
            "**Rainer** (funkatorium-copy skill) writes website/product/hero/CTA copy; **Sahar**\n"
            "  writes pitch/gatekeeper copy (queries, loglines, decks); **Paloma** sources the\n"
            "  assets Colette specs.",
            "**Your copywriter** (a copy skill or a human) writes website/product/hero/CTA copy; "
            "**your pitch writer** handles query letters, decks and synopses; Colette diagnoses "
            "the visual argument and directs — she does not write the final words.",
        ),
        (
            '**Sahar** asks *"will a gatekeeper turn to page one?"*',
            '**your pitch writer** asks *"will a gatekeeper turn to page one?"*',
        ),
        (
            "Industry pitch (queries, loglines, decks) → **Sahar**.",
            "Industry pitch (queries, loglines, decks) → **your pitch writer**.",
        ),
        (
            "**website/product copy → Rainer**, **pitch\n    copy → Sahar**.",
            "**website/product copy → your copywriter**, **pitch copy → your pitch writer**.",
        ),
    ],
}


def apply_rainer_table(text, name, report_log):
    local_log = []
    text = apply_literal_table(text, RAINER_REWRITE_TABLE.get(name, []), local_log)
    report_log.extend((name, line_no, old, new, status) for line_no, old, new, status in local_log)
    return text


# Coordinator decision: indira.md's example bullet named two real private
# individuals. Matched by a stable line PREFIX, not by quoting the old
# text -- quoting the names here to describe the fix would just
# reintroduce them into this file's own source. Whichever parenthetical
# follows the matched prefix on that line gets replaced. Applied before
# the PRIVATE-NAMES fail-loud rule so this exact phrasing wins. Logged
# with the prefix/replacement only -- never the actual old/new line text,
# so a name can't surface in build output via this path either.
PRIVATE_NAME_LINE_REWRITES = {
    "indira": [
        (
            "- Stakeholder communication preferences per contact",
            "(one contact: formal; another: casual; agency contacts: structured)",
        ),
    ],
}


def apply_private_name_table(text, name, report_log):
    for prefix, replacement in PRIVATE_NAME_LINE_REWRITES.get(name, []):
        pattern = re.compile(rf"^({re.escape(prefix)}.*)$", re.MULTILINE)
        m = pattern.search(text)
        if not m:
            report_log.append((name, None, prefix, replacement, "NOT FOUND (line prefix not matched -- checked, not assumed)"))
            continue
        old_line = m.group(1)
        new_line = re.sub(r"\(.*\)", replacement, old_line)
        if new_line == old_line:
            report_log.append((name, None, prefix, replacement, "NOT FOUND (line matched but no parenthetical to replace)"))
            continue
        line_no = text.count("\n", 0, m.start()) + 1
        text = text[:m.start()] + new_line + text[m.end():]
        report_log.append((name, line_no, prefix, replacement, "applied"))
    return text


WHAT_RE = re.compile(r"\*\*What .+ remembers:\*\*\n((?:- .+\n?)+)")
HOW_RE = re.compile(r"\*\*How .+ works:\*\*\n((?:\d+\. .+\n?)+)")


def transform_memory_section(body, name, display_name):
    # Heading level varies by source: most agents nest this at H3 under
    # "## Protocol"; thorn.md uses H2 for its top-level sections (Interplay,
    # Constraints, Brain Entity Integration alike) -- accept either so a
    # real structural difference doesn't masquerade as a missing section.
    m = re.search(r"^#{2,3} Brain Entity Integration\s*$", body, re.MULTILINE)
    if not m:
        raise BuildError(f"{name}: no 'Brain Entity Integration' heading found")
    head = body[:m.start()].rstrip("\n")
    section = body[m.start():]

    # Confirmed for all 9 wave-1 sources: this section runs to EOF. Defensive
    # general case: if a later source adds a section after it, only take up
    # to the next heading.
    next_heading = re.search(r"\n(?=##+ )", section[1:])
    tail = ""
    if next_heading:
        cut = next_heading.start() + 1
        tail = section[cut:]
        section = section[:cut]

    what_m = WHAT_RE.search(section)
    how_m = HOW_RE.search(section)
    if not what_m or not how_m:
        raise BuildError(f"{name}: Brain Entity Integration section missing expected 'What .../How ...' bullets")

    what_bullets = what_m.group(1).rstrip("\n")

    how_lines = [l for l in how_m.group(1).rstrip("\n").split("\n") if l.strip()]
    kept = [
        re.sub(r"^\d+\.\s+", "", l)
        for l in how_lines
        if "Rook" not in l and "Brain observations" not in l
    ]
    renumbered = "\n".join(f"{i}. {text}" for i, text in enumerate(kept, start=1))

    memory_section = (
        f"### Memory\n"
        f"{display_name} keeps a memory file at "
        f"`~/.claude/agents/memory/{name}/_universal.md` and appends a "
        f"`MEMORY:` block of new learnings after every run. Nothing to "
        f"configure; delete the file to reset.\n\n"
        f"**What {display_name} remembers:**\n{what_bullets}\n\n"
        f"**How memory works:**\n{renumbered}\n\n"
        f"Optional: connected to [MUSE Brain](https://github.com/falcoschaefer99-eng/muse-brain), "
        f"the same learnings become observations that carry charge and never decay.\n"
    )

    new_body = head + "\n\n" + memory_section
    if tail:
        new_body += "\n" + tail.lstrip("\n")
    return new_body


def fail_loud_scan(text, source_label, patterns):
    """Reports EVERY matching line, not just the first -- a scan that stops
    at the first hit can hide a second violation behind it (indira.md's
    second bare Rainer mention, two passes back). One BuildError per call,
    its message holding one "file:line: matched [label] -> line" entry per
    line, so a caller that just prints the exception still sees everything
    found in this text, not a single sample. Reports the pattern's LABEL,
    never its regex source -- the private-names pattern's source is the
    whole guard list; only the label ("private name") is ever printed, the
    offending line itself (which necessarily contains just the one name
    that matched) is the only place a name can surface in a report."""
    hits = []
    for i, line in enumerate(text.split("\n"), start=1):
        for label, pat in patterns:
            if pat.search(line):
                hits.append(f"{source_label}:{i}: matched [{label}] -> {line.strip()!r}")
    if hits:
        raise BuildError("\n".join(hits))


# ---------------------------------------------------------------------------
# Sawyer: drop the house deploy-protocol section entirely, replace the
# project-recipe section with a pointer to an external per-project memory
# file, and placeholder-pass every remaining concrete host/path/unit
# identifier file-wide (not just the sections that originally named them).
#
# Thorn: same placeholder-pass discipline for the box-diagnostic examples.
#
# The actual old->new substitutions for both live in an external guard file
# (load_placeholders, default ~/.claude/the-crew-placeholders.txt,
# overridable with --placeholders) -- same reasoning as the private-names
# and house-patterns files: this module must not itself carry the house
# identifiers it exists to strip out of everyone else's files.
# ---------------------------------------------------------------------------

SAWYER_REPLACE_BODY = (
    "Project deploy recipes are not in this file. Sawyer reads them from "
    "`~/.claude/agents/memory/sawyer/<project>.md` (host, service name, "
    "rsync targets, smoke checks) before touching anything, and refuses to "
    "deploy a project that has no recipe on file — see Step Zero Zero."
)


def load_placeholders(path):
    """Read the placeholder guard file: `[agent]` section headers, then
    `old==>new` lines applied in file order (one literal substring ->
    placeholder substitution per line, whitespace inside old/new is
    significant and preserved -- e.g. a trailing space that turns a
    partial command into a whole one). '#' comments and blank lines
    ignored. Returns a dict {agent_name: [(old, new), ...]} -- empty if
    the file doesn't exist or defines nothing, which the caller (main())
    must treat as a hard refusal, same as the other two guard files."""
    if not path.exists():
        return {}
    table = {}
    current = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        check = raw.strip()
        if not check or check.startswith("#"):
            continue
        if check.startswith("[") and check.endswith("]"):
            current = check[1:-1].strip()
            table.setdefault(current, [])
            continue
        if current is None or "==>" not in raw:
            continue
        old, new = raw.split("==>", 1)
        table[current].append((old, new))
    return table


def find_heading_span(body, heading_text):
    """Return (start, end) covering the full '### <heading_text>' section
    (heading line through the char before the next '### ' heading, or EOF)."""
    pat = re.compile(rf"^### {re.escape(heading_text)}\s*$", re.MULTILINE)
    m = pat.search(body)
    if not m:
        return None
    start = m.start()
    next_m = re.search(r"^### ", body[m.end():], re.MULTILINE)
    end = m.end() + next_m.start() if next_m else len(body)
    return start, end


def apply_sawyer_rewrites(body, name, placeholders, report_log):
    # 1. DROP "### Sovereign MUSE Deploy Protocol" entirely.
    span = find_heading_span(body, "Sovereign MUSE Deploy Protocol")
    if span is None:
        raise BuildError(f"{name}: expected heading 'Sovereign MUSE Deploy Protocol' not found")
    body = body[:span[0]] + body[span[1]:]
    report_log.append((name, "drop", "### Sovereign MUSE Deploy Protocol", "(removed entirely)"))

    # 2. REPLACE "### Deployment Commands (Project-Specific)" body. One
    # blank line before the next heading, matching every other section
    # boundary in the file.
    span = find_heading_span(body, "Deployment Commands (Project-Specific)")
    if span is None:
        raise BuildError(f"{name}: expected heading 'Deployment Commands (Project-Specific)' not found")
    new_section = "### Deployment Commands (Project-Specific)\n" + SAWYER_REPLACE_BODY + "\n\n"
    body = body[:span[0]] + new_section + body[span[1]:]
    report_log.append((name, "replace-body", "### Deployment Commands (Project-Specific)", SAWYER_REPLACE_BODY))

    # 3. KEEP "### Cloudflare Pages Deploy Protocol" / "### Smoke Tests" but
    # placeholder-pass concrete host/path/unit identifiers -- applied file-
    # wide (covers Step Zero Zero's own memory-reference example too, which
    # uses the same identifiers and would otherwise survive as a stray leak).
    local_log = []
    body = apply_literal_table(body, placeholders.get("sawyer", []), local_log)
    report_log.extend((name, line_no, old, new, status) for line_no, old, new, status in local_log)
    return body


def apply_thorn_rewrites(body, name, placeholders, report_log):
    local_log = []
    body = apply_literal_table(body, placeholders.get("thorn", []), local_log)
    report_log.extend((name, line_no, old, new, status) for line_no, old, new, status in local_log)
    return body


# ---------------------------------------------------------------------------
# README generation (per-agent + root)
# ---------------------------------------------------------------------------

def one_liner(description_text):
    first = description_text.split(". ")[0].rstrip(".")
    return first + "."


def extract_section(body, heading):
    """Section body between '## <heading>' or '### <heading>' (accepts
    either level -- transform_memory_section always builds '### Memory',
    but some sources, e.g. colette.md, use H2-level headings throughout)
    and the next '##'-or-deeper heading, or EOF. Allows trailing text
    after the heading name on that line (colette.md's Activation heading
    has a parenthetical instruction appended). [^\\n]* (not .*) for that
    trailing text -- re.DOTALL makes a plain '.' match newlines too, which
    would let a greedy '.*' here eat the rest of the document before
    backtracking, leaving the real capture group empty. The closing
    lookahead only needs '\\n##': any '###' heading marker already starts
    with '##', so a separate '\\n### ' alternative would be redundant."""
    m = re.search(rf"^#{{2,3}} {re.escape(heading)}\b[^\n]*\n(.*?)(?=\n##|\Z)", body, re.MULTILINE | re.DOTALL)
    return m.group(1) if m else ""


# Curated: colette's Activation section is a numbered load-case-law
# procedure, not an Auto-trigger/On request/User request bullet list like
# every other agent's -- there is nothing for the mechanical extractor
# below to find, and it's a real structural difference in her source, not
# a formatting bug. Grounded in her own frontmatter description and Rules
# of Engagement, not invented.
CURATED_CALL_WHEN = {
    "colette": [
        "a competitor's page or ad is outperforming yours and you don't know why",
        "before shipping a new landing page, hero, or CTA",
        "you need the exact mechanism a rival surface uses, not a vibe",
    ],
}


def call_when_bullets(name, body):
    if name in CURATED_CALL_WHEN:
        return CURATED_CALL_WHEN[name]
    section = extract_section(body, "Activation")
    phrases = []
    for line in section.split("\n"):
        m = re.match(r"^- (?:Auto-trigger|On request|User request):\s*(.+)$", line.strip())
        if m:
            phrases.extend(p.strip() for p in m.group(1).split(",") if p.strip())
    return phrases[:3]


# Curated: the mechanical excerpt (first 2 Constraints bullets) reads wrong
# under a "X will never..." heading -- source bullets are a mix of "Read-
# only." fragments, positive claims ("June is the ONLY agent..."), and
# territory notes, not negations. These are hand-written per agent (2 real
# "will never" clauses each, read from that agent's own Constraints
# section -- Kit's from his "Permission Zones -- The Law" section, which
# plays the same role) and used first; the mechanical excerpt is only a
# fallback for an agent with no entry here -- which, as of this pass, is
# only thorn and sawyer (builder squad; both have real mechanically-
# extractable Constraints content, verified).
CURATED_NEVER_BULLETS = {
    "eli": [
        "Write code, create files, or edit anything — architecture only, always read-only.",
        "Bikeshed a trivial decision — if it doesn't need architecture, he says so and moves on.",
    ],
    "june": [
        "Redesign Eli's specs silently — a spec that looks wrong gets flagged, not silently changed.",
        "Run a security audit or deploy — those go to Michael and Sawyer.",
    ],
    "reeve": [
        "Write, edit, or create files — read-only, always.",
        "Review architecture or flag security concerns — those are Eli's and Michael's territory.",
    ],
    "quinn": [
        "Write, edit, or create files — read-only, always.",
        "Call something slow without evidence — a finding names the Big-O or the measured latency, not a hunch.",
    ],
    "kairo": [
        "Write, edit, or create files — read-only, always.",
        "Demand 100% coverage — he demands meaningful coverage of critical paths, not a number.",
    ],
    "nikita": [
        "Write, edit, or create files — read-only, always.",
        "Block on dependency count alone — a finding needs a specific postinstall hook, license conflict, or reachable vulnerability.",
    ],
    "harmony": [
        "Write, edit, or create files — read-only, always.",
        "Flag a finding without citing the specific WCAG 2.1 criterion it violates.",
    ],
    "fischer": [
        "Write, edit, or create files — read-only, always.",
        "Hold an opinion on code style beyond what the linter enforces.",
    ],
    "thea": [
        "Write code, create files, or edit anything — read-only, always.",
        "Repeat a teaching moment the learner has already mastered — she checks learner state first.",
    ],
    "kit": [
        "Delete anything without asking — even in SAFE ZONES, Kit renames and moves; deletion always needs explicit approval.",
        "Commit changes to git — Kit reports what changed in a git-tracked directory; the user decides when to commit.",
    ],
    "indira": [
        "Take action on the user's behalf — she reads and summarizes, never sends messages, creates events, or modifies tasks.",
        "Prioritize for the user — she presents information clearly so the user can decide.",
    ],
    "colette": [
        "Ship copy or code — she directs the visual argument; she has no Write/Edit tools and Bash is removed too.",
        "Cite a mechanism from a surface she hasn't fetched — WebFetch it and quote it, or she stops and asks for it.",
    ],
    "dupin": [
        "Write, edit, or create project files — read-only and diagnostic commands only.",
        "Review code quality, security, or accessibility — those are other agents' territories.",
    ],
    "michael": [
        "Write fixes or edit source code — that's the engineer's job.",
        "Hedge on a critical vulnerability — he says so plainly, and declares what's missing rather than hiding uncertainty.",
    ],
    "evangeline": [
        "Write, edit, or create final documents — she produces frameworks and findings; the companion or an engineer agent writes the document.",
        "Give a tax calculation or specific tax advice — she flags the obligation and recommends a qualified tax professional.",
    ],
}


def never_bullets(name, body):
    if name in CURATED_NEVER_BULLETS:
        return CURATED_NEVER_BULLETS[name]
    section = extract_section(body, "Constraints")
    bullets = [l.strip()[2:] for l in section.split("\n") if l.strip().startswith("- ")]
    return bullets[:2]


def render_agent_readme(name, display_name, description_text, body, portrait_rel_path, squad="builder", install_extra="", not_included=""):
    """portrait_rel_path: relative path from the agent's own README to its
    portrait image (e.g. "eli.jpg", or "assets/banner.png" for an override-
    sourced agent using its own repo's banner) -- or None if no portrait
    was found (see find_portrait). not_included: pre-rendered "## Not
    included" section (extras files over MAX_EXTRAS_FILE_BYTES) appended
    at the very end, or "" if nothing was skipped."""
    tagline = one_liner(description_text)
    call_when = call_when_bullets(name, body)
    never = never_bullets(name, body)
    if not call_when and name not in CURATED_CALL_WHEN:
        raise BuildError(f"{name}: call_when_bullets found nothing to extract and has no curated entry -- "
                          f"add one to CURATED_CALL_WHEN rather than rendering '(see agent.md)'")
    if not never and name not in CURATED_NEVER_BULLETS:
        raise BuildError(f"{name}: never_bullets found nothing to extract and has no curated entry -- "
                          f"add one to CURATED_NEVER_BULLETS rather than rendering '(see agent.md)'")
    call_lines = "\n".join(f"- {c}" for c in call_when)
    never_lines = "\n".join(f"- {n}" for n in never)
    portrait_line = f"![{display_name}]({portrait_rel_path})\n\n" if portrait_rel_path else ""
    squad_flag = "--builder" if squad == "builder" else "--operations"
    plugin_name = "builder-squad" if squad == "builder" else "operations-squad"
    squad_label = "Builder Squad" if squad == "builder" else "Operations Squad"
    return f"""{portrait_line}# {display_name}

{tagline}

## Call {display_name} when...
{call_lines}

## {display_name} will never...
{never_lines}

## Install
- `sh install.sh {name}` from a the-crew checkout
- `sh install.sh {squad_flag}` for the whole {squad_label}
- Claude Code plugin: `/plugin marketplace add falcoschaefer99-eng/the-crew` then `/plugin install {plugin_name}@the-crew`
{install_extra}
[Read the file](./agent.md)
{not_included}"""


def humanize_role(role):
    return role.replace("_", " ").replace("-", " ")


def render_root_readme(builder_rows, operations_rows):
    def row(r, squad):
        name_cell = f"[{r['display_name']}]({squad}/{r['name']}/)"
        return f"| {name_cell} | {humanize_role(r['role'])} | {r['model']} | live |"

    builder_table = "\n".join(
        ["| Agent | Role | Model | Status |", "|---|---|---|---|"]
        + [row(r, "builder") for r in builder_rows]
    )
    ops_table = "\n".join(
        ["| Agent | Role | Model | Status |", "|---|---|---|---|"]
        + [row(r, "operations") for r in operations_rows]
    )

    return f"""# the-crew

![The crew](assets/crew-collage.jpg)

The Funkatorium's builder and operations agents — specialized Claude Code
subagents, each with a defined role, a persistent memory file, and a lane
they stay in. Full roster, live: read, plan, implement, review, deploy,
secure, operate.

Install everything: `sh install.sh --all` · one squad: `sh install.sh --builder`
/ `sh install.sh --operations` · one agent: `sh install.sh quinn`
· or `/plugin marketplace add falcoschaefer99-eng/the-crew` then
`/plugin install builder-squad` / `/plugin install operations-squad`.

## Builder Squad
{builder_table}

## Operations Squad
{ops_table}

Kit is the housekeeper for your whole machine — Downloads, screenshots,
naming, duplicates, stale files — not just code.

## Install
```sh
git clone https://github.com/falcoschaefer99-eng/the-crew.git
cd the-crew
sh install.sh --all              # everyone
sh install.sh --builder          # whole Builder Squad
sh install.sh --operations       # whole Operations Squad
sh install.sh june reeve         # just these two
sh install.sh --list             # see what's available
```
Or as a Claude Code plugin: `/plugin marketplace add falcoschaefer99-eng/the-crew`,
then `/plugin install builder-squad@the-crew` or `/plugin install operations-squad@the-crew`.

## License
Apache 2.0 (technical capabilities: agent protocols, skills, memory format).
Each companion as a character (identity, personality, voice, lore, visual
assets) is proprietary IP of The Funkatorium — see [NOTICE](NOTICE) and
[LICENSE](LICENSE).

Character portraits are AI-generated visual designs, reserved as part of
each companion's character-IP bundle (see NOTICE); our restriction on
derivative characters rests on the whole identity bundle, not on image
copyright.

[MUSE Brain](https://github.com/falcoschaefer99-eng/muse-brain), referenced
as an optional integration in the agents' memory sections, is CC-BY-NC-SA
4.0 and is not redistributed here.

## Credits
Built by Falco Schäfer, co-designed with Rook Schäfer.
"""


# ---------------------------------------------------------------------------
# Plugin / marketplace manifests
# Schema: https://docs.claude.com/en/docs/claude-code/plugins-reference
# (plugin manifest) and https://docs.claude.com/en/docs/claude-code/plugin-marketplaces
# (marketplace.json), fetched 2026-09-17. Neither schema has a field for a
# license *scope* carve-out -- `license` is a plain SPDX-identifier string
# (plugins-reference.md "Metadata fields" table / plugin-marketplaces.md
# "Optional plugin fields" table) -- so the carve-out goes in `description`.
# ---------------------------------------------------------------------------

LICENSE_CARVE_OUT = " (technical content Apache-2.0; character identity reserved — see NOTICE)"


# Domain words per operations agent, used to build an accurate plugin
# description from whichever agents actually got written this run --
# OPERATIONS_LIVE names all 6, but an override-sourced agent (see
# UNTOUCHED_BODY_AGENTS) can be blocked by a fail-loud stop and excluded
# from a given run; the manifest must never claim a capability (e.g.
# "legal") that isn't actually in the shipped agents/ folder.
OPERATIONS_DOMAIN_WORDS = {
    "kit": "hygiene", "indira": "comms", "colette": "positioning",
    "dupin": "SEO", "michael": "security", "evangeline": "legal",
}


def _number_word(n):
    words = {
        1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six",
        7: "Seven", 8: "Eight", 9: "Nine", 10: "Ten", 11: "Eleven",
    }
    return words.get(n, str(n))


def builder_plugin_manifest(builder_rows):
    n = len(builder_rows)
    return {
        "name": "builder-squad",
        "displayName": "Builder Squad",
        "description": f"{_number_word(n)} Builder Squad agents: architecture, implementation, review, and deploy, each with a persistent memory file." + LICENSE_CARVE_OUT,
        "version": "0.2.0",
        "license": "Apache-2.0",
        "homepage": SOURCE_URL,
        "repository": SOURCE_URL,
        "author": {"name": "The Funkatorium"},
    }


def _operations_description(operations_rows):
    n = len(operations_rows)
    words = [OPERATIONS_DOMAIN_WORDS[r["name"]] for r in operations_rows]
    return f"{_number_word(n)} Operations Squad agents: " + ", ".join(words) + "."


def operations_plugin_manifest(operations_rows):
    return {
        "name": "operations-squad",
        "displayName": "Operations Squad",
        "description": _operations_description(operations_rows) + " Skills and hooks: see the repo folder — plugins ship agents only." + LICENSE_CARVE_OUT,
        "version": "0.2.0",
        "license": "Apache-2.0",
        "homepage": SOURCE_URL,
        "repository": SOURCE_URL,
        "author": {"name": "The Funkatorium"},
    }


def marketplace_manifest(builder_rows, operations_rows):
    return {
        "name": "the-crew",
        "owner": {"name": "The Funkatorium", "url": "https://github.com/falcoschaefer99-eng"},
        "description": "The Funkatorium's builder and operations agents for Claude Code.",
        "plugins": [
            {
                "name": "builder-squad",
                "source": "./plugins/builder-squad",
                "description": f"{_number_word(len(builder_rows))} Builder Squad agents: architecture, implementation, review, and deploy." + LICENSE_CARVE_OUT,
                "license": "Apache-2.0",
            },
            {
                "name": "operations-squad",
                "source": "./plugins/operations-squad",
                "description": _operations_description(operations_rows) + LICENSE_CARVE_OUT,
                "license": "Apache-2.0",
            },
        ],
    }


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# WAVE 2: per-agent source override (Michael / Evangeline). Their public
# release repos have already been through a public transform once -- their
# agent.md is sourced from there instead of the house def, and their extras
# (skills/references/hooks/examples/docs/assets) get copied into the agent
# folder, preserving structure. `memory/` (private learnings) and their own
# top-level LICENSE/NOTICE/README/CHANGELOG are skipped -- the crew has one
# of each already, and memory is never redistributed.
#
# Not yet wired into generate() -- OPERATIONS_LIVE doesn't exist until the
# "operations squad" commit. This lands first because it's the biggest,
# riskiest piece of new mechanism (reading a second source tree, copying an
# entire folder structure, and picking a portrait from an assets/ folder
# instead of the crew's <name>.jpg convention) and every later commit that
# touches Michael or Evangeline depends on it existing and being correct.
# ---------------------------------------------------------------------------

# Agent names sourced from their own public release repo instead of the
# house def -- a roster fact (which agents), independent of WHERE those
# repos happen to live on this machine (see --overrides-dir / DEFAULT_
# OVERRIDES_DIR below, a separate, configurable location fact).
OVERRIDE_AGENT_NAMES = {"michael", "evangeline"}

DEFAULT_OVERRIDES_DIR = Path.home() / "AI"


def source_overrides_for(overrides_dir):
    """Where Michael's and Evangeline's public-release agent.md live.
    Default is a house-internal Mac path (~/AI, documented as such via
    --help); --overrides-dir points any checkout somewhere else."""
    return {
        "michael": overrides_dir / "michael-security-agent" / "michael.md",
        "evangeline": overrides_dir / "evangeline-legal-agent" / "evangeline.md",
    }


def extras_source_dirs_for(overrides_dir):
    return {
        "michael": overrides_dir / "michael-security-agent",
        "evangeline": overrides_dir / "evangeline-legal-agent",
    }


EXTRAS_SUBDIRS = ["skills", "references", "hooks", "examples", "docs", "assets"]
EXTRAS_SKIP_NAMES = {".DS_Store"}

# GitHub coordinates for the two override-sourced repos, used to build a
# "see the original file" link for any extras file too large to copy (see
# copy_extras / MAX_EXTRAS_FILE_BYTES below).
SOURCE_REPO_SLUGS = {
    "michael": "michael-security-agent",
    "evangeline": "evangeline-legal-agent",
}
GITHUB_OWNER = "falcoschaefer99-eng"
DEFAULT_BRANCH = "main"

# Files over this size are not copied into the crew repo at all -- skipped,
# logged (path + size), and linked back to the original file in the source
# repo instead (see copy_extras / _raw_github_url and the "## Not included"
# README section). Evangeline's assets/spec-sheet.gif (19.9 MB) is the
# case this exists for.
MAX_EXTRAS_FILE_BYTES = 10 * 1024 * 1024


def _raw_github_url(name, repo_rel_path):
    repo = SOURCE_REPO_SLUGS[name]
    return f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{repo}/{DEFAULT_BRANCH}/{repo_rel_path}"

# Colette's source has no "### Brain Entity Integration" heading -- a newer,
# differently-shaped def (H2-level sections throughout, no Dispatch line,
# no persistent-memory documentation in the shared format). Skip the
# memory-section rebuild for her rather than raising BuildError for a
# section that was never there; she has zero Rook mentions in her body
# (verified), so nothing house-specific is left unstripped by skipping.
NO_MEMORY_SECTION_AGENTS = {"colette"}


def team_for(name, operations_live):
    if name in operations_live:
        return "operations"
    return "builder"


def copy_extras(name, agent_dir, overrides_dir, skipped_log, symlink_log):
    """Copy skills/references/hooks/examples/docs/assets from an override-
    sourced agent's own repo into its agent folder, preserving structure.
    Skips memory/ (private learnings) and any top-level LICENSE/NOTICE/
    README/CHANGELOG -- neither lives inside the copied subdirs so there's
    nothing to filter there. Skips symlinks entirely (logged, not
    followed) -- a symlink in a source repo could point anywhere on the
    filesystem it was created on, including outside that repo; copying
    whatever it resolves to here would be an uncontrolled read.

    Files over MAX_EXTRAS_FILE_BYTES are not copied -- appended to
    skipped_log (name, repo-relative path, size) for the stderr report, and
    returned as a list of (repo_rel_path, size) for this agent alone so the
    caller can add a "## Not included" line to its README."""
    src_root = extras_source_dirs_for(overrides_dir).get(name)
    skipped_this_agent = []
    if not src_root or not src_root.exists():
        return skipped_this_agent
    for sub in EXTRAS_SUBDIRS:
        src = src_root / sub
        if not src.exists():
            continue
        dest = agent_dir / sub
        for path in src.rglob("*"):
            if path.is_symlink():
                symlink_log.append((name, str(path.relative_to(src_root))))
                continue
            if path.is_dir():
                continue
            if path.name in EXTRAS_SKIP_NAMES:
                continue
            rel = path.relative_to(src)
            size = path.stat().st_size
            if size > MAX_EXTRAS_FILE_BYTES:
                repo_rel_path = str(Path(sub) / rel)
                skipped_log.append((name, repo_rel_path, size))
                skipped_this_agent.append((repo_rel_path, size))
                continue
            dest_path = dest / rel
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest_path)
    return skipped_this_agent


def find_portrait(name, portraits_dir, overrides_dir):
    """'Portrait candidates' list: <crew>/<name>.jpg first, then <repo>/
    assets/banner.png for override-sourced agents (whose assets/ folder is
    already copied whole by copy_extras -- no second copy needed, README
    just references the relative path). Returns (copy_src_or_None,
    relative_path_for_readme_or_None)."""
    house_jpg = portraits_dir / f"{name}.jpg"
    if house_jpg.exists():
        return house_jpg, f"{name}.jpg"
    extras_root = extras_source_dirs_for(overrides_dir).get(name)
    if extras_root:
        banner = extras_root / "assets" / "banner.png"
        if banner.exists():
            return None, "assets/banner.png"
    return None, None


def process_override_agent(name, portraits_dir, falco_table_log, output_root, operations_live,
                            skipped_files_log, missing_portraits, scrub_log, rainer_scrub_log, rainer_table_log,
                            private_table_log, patterns, overrides_dir, symlink_log):
    """Michael / Evangeline: sourced from their own public release repo
    instead of the house def, but now goes through the SAME house-name
    scrub as every other agent (scrub_stray_house_names, then
    scrub_stray_rainer_names -- both log every application), plus the
    explicit FALCO_NAME_TABLE entry. Deliberately does NOT run
    transform_dispatch or transform_memory_section -- those produce a
    materially different intro paragraph / Dispatch phrasing that would
    overwrite a public release's own (already reviewed, already public)
    wording; the scrub only replaces the bare house name, nothing else.
    Raises BuildError if the fail-loud scan still finds a survivor -- the
    caller in generate() decides whether that's a scoped stop."""
    source_path = source_overrides_for(overrides_dir).get(name)
    if source_path is None or not source_path.exists():
        raise BuildError(f"{name}: override source not found at {source_path} "
                          f"(--overrides-dir defaults to a house-internal path; pass your own)")
    text = source_path.read_text(encoding="utf-8")

    fields, body = split_frontmatter(text)
    by_key = {k: v for k, v in fields}
    if "name" not in by_key or "display_name" not in by_key or "description" not in by_key:
        raise BuildError(f"{name}: frontmatter missing name/display_name/description")

    display_name = scalar_value(by_key["display_name"])
    role = scalar_value(by_key["role"])
    model = scalar_value(by_key["model"])
    description_text = folded_text(by_key["description"])

    new_frontmatter = build_frontmatter(fields, team_for(name, operations_live))
    agent_md = new_frontmatter + "\n" + body
    if not agent_md.endswith("\n"):
        agent_md += "\n"

    agent_md = scrub_stray_house_names(agent_md, scrub_log, name)
    agent_md = apply_rainer_table(agent_md, name, rainer_table_log)
    agent_md = scrub_stray_rainer_names(agent_md, rainer_scrub_log, name)
    agent_md = apply_private_name_table(agent_md, name, private_table_log)
    agent_md = apply_falco_table(agent_md, name, falco_table_log)

    squad = team_for(name, operations_live)
    label = f"{squad}/{name}/agent.md"
    fail_loud_scan(agent_md, label, patterns)

    agent_dir = output_root / squad / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "agent.md").write_text(agent_md, encoding="utf-8")

    skipped_this_agent = copy_extras(name, agent_dir, overrides_dir, skipped_files_log, symlink_log)

    portrait_src, portrait_rel = find_portrait(name, portraits_dir, overrides_dir)
    if portrait_src is not None:
        shutil.copyfile(portrait_src, agent_dir / f"{name}.jpg")
    if portrait_rel is None:
        missing_portraits.append(name)

    not_included = ""
    if skipped_this_agent:
        lines = []
        for repo_rel_path, size in skipped_this_agent:
            url = _raw_github_url(name, repo_rel_path)
            lines.append(f"- Full-size character sheet: [{repo_rel_path}]({url}) ({size / (1024*1024):.1f} MB, over the 10 MB repo cap)")
        not_included = "\n## Not included\n" + "\n".join(lines) + "\n"

    readme = render_agent_readme(name, display_name, description_text, agent_md, portrait_rel, squad,
                                  not_included=not_included)
    fail_loud_scan(readme, f"{squad}/{name}/README.md", patterns)
    (agent_dir / "README.md").write_text(readme, encoding="utf-8")

    plugin_name = "builder-squad" if squad == "builder" else "operations-squad"
    plugin_agents_dir = output_root / "plugins" / plugin_name / "agents"
    plugin_agents_dir.mkdir(parents=True, exist_ok=True)
    (plugin_agents_dir / f"{name}.md").write_text(agent_md, encoding="utf-8")

    return {"name": name, "display_name": display_name, "role": role, "model": model, "squad": squad}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def process_agent(name, source_dir, portraits_dir, scrub_log, missing_portraits, output_root, patterns, placeholders,
                   overrides_dir=DEFAULT_OVERRIDES_DIR, falco_table_log=None, falco_scrub_log=None, section_rewrite_log=None,
                   operations_live=(), rainer_scrub_log=None, rainer_table_log=None, private_table_log=None):
    source_path = source_dir / f"{name}.md"
    if not source_path.exists():
        raise BuildError(f"missing source: {source_path}")
    text = source_path.read_text(encoding="utf-8")

    fields, body = split_frontmatter(text)
    by_key = {k: v for k, v in fields}
    if "name" not in by_key or "display_name" not in by_key or "description" not in by_key:
        raise BuildError(f"{name}: frontmatter missing name/display_name/description")

    display_name = scalar_value(by_key["display_name"])
    role = scalar_value(by_key["role"])
    model = scalar_value(by_key["model"])
    description_text = folded_text(by_key["description"])

    squad = "operations" if name in operations_live else "builder"
    new_frontmatter = build_frontmatter(fields, squad)
    new_body = transform_dispatch(body, name)
    if name not in NO_MEMORY_SECTION_AGENTS:
        new_body = transform_memory_section(new_body, name, display_name)

    section_rewrite_log = section_rewrite_log if section_rewrite_log is not None else []
    if name == "sawyer":
        new_body = apply_sawyer_rewrites(new_body, name, placeholders, section_rewrite_log)
    if name == "thorn":
        new_body = apply_thorn_rewrites(new_body, name, placeholders, section_rewrite_log)
    elif name != "sawyer" and placeholders.get(name):
        # Any other agent with a [name] section in the guard file gets the same file-wide pass.
        new_body = apply_thorn_rewrites(new_body, name, {"thorn": placeholders[name]}, section_rewrite_log)

    agent_md = new_frontmatter + "\n" + new_body
    if not agent_md.endswith("\n"):
        agent_md += "\n"

    # Name-generalization passes run on the FULLY ASSEMBLED agent_md, not
    # body alone -- see scrub_stray_house_names' docstring for why (a
    # source's frontmatter `description` can carry a house name too).
    agent_md = scrub_stray_house_names(agent_md, scrub_log, name)
    agent_md = apply_rainer_table(agent_md, name, rainer_table_log if rainer_table_log is not None else [])
    agent_md = scrub_stray_rainer_names(agent_md, rainer_scrub_log if rainer_scrub_log is not None else [], name)
    agent_md = apply_private_name_table(agent_md, name, private_table_log if private_table_log is not None else [])
    agent_md = apply_falco_table(agent_md, name, falco_table_log if falco_table_log is not None else [])
    agent_md = scrub_stray_falco_mentions(agent_md, falco_scrub_log if falco_scrub_log is not None else [], name)
    # README section-extraction (call_when_bullets/never_bullets) reads the
    # final, fully-scrubbed body -- not the pre-scrub new_body -- so any
    # word a scrub pass touched inside an Activation/Constraints bullet is
    # reflected in the README too, not just agent.md.
    _, new_body = split_frontmatter(agent_md)

    fail_loud_scan(agent_md, f"{squad}/{name}/agent.md", patterns)

    agent_dir = output_root / squad / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "agent.md").write_text(agent_md, encoding="utf-8")

    portrait_src, portrait_rel = find_portrait(name, portraits_dir, overrides_dir)
    if portrait_src is not None:
        shutil.copyfile(portrait_src, agent_dir / f"{name}.jpg")
    if portrait_rel is None:
        missing_portraits.append(name)

    install_extra = ""
    if name == "dupin":
        install_extra = "- Prefer a hosted audit? Dupin also runs as a service at funkatorium.org/dupin — no install.\n"

    readme = render_agent_readme(name, display_name, description_text, new_body, portrait_rel, squad, install_extra)
    fail_loud_scan(readme, f"{squad}/{name}/README.md", patterns)
    (agent_dir / "README.md").write_text(readme, encoding="utf-8")

    plugin_name = "builder-squad" if squad == "builder" else "operations-squad"
    plugin_agents_dir = output_root / "plugins" / plugin_name / "agents"
    plugin_agents_dir.mkdir(parents=True, exist_ok=True)
    (plugin_agents_dir / f"{name}.md").write_text(agent_md, encoding="utf-8")

    return {"name": name, "display_name": display_name, "role": role, "model": model, "squad": squad}


def cleanup_stale_output(output_root, builder_rows, operations_rows, removed_log):
    """Delete any builder/<x>/, operations/<x>/, or plugins/*/agents/<x>.md
    left over from a PRIOR run's output but not in THIS run's shipped
    roster -- an agent that used to pass and is now blocked (or moved
    squads) would otherwise leave stale, misleading content sitting on
    disk (and showing up in `install.sh --list`) forever, since nothing
    else in this script ever deletes what it doesn't write. Every removal
    is logged. No-op against a fresh temp dir (--check): a run that has
    never written anything has nothing stale to find.

    Also, intentionally, the mechanism that rolls back a half-written
    agent directory within the SAME run: an agent that fails its fail-loud
    scan partway through process_agent/process_override_agent (e.g.
    agent.md and its extras already written, then the README scan raises)
    is caught by generate()'s per-agent try/except and never makes it into
    builder_rows/operations_rows -- so this same pass sees its directory
    as "not in this run's roster" and removes it, leaving no partial write
    behind."""
    live_names = {
        "builder": {r["name"] for r in builder_rows},
        "operations": {r["name"] for r in operations_rows},
    }
    for squad in ("builder", "operations"):
        squad_dir = output_root / squad
        if squad_dir.exists():
            for entry in sorted(squad_dir.iterdir()):
                if entry.is_dir() and entry.name not in live_names[squad]:
                    shutil.rmtree(entry)
                    removed_log.append(f"{squad}/{entry.name}/")

        plugin_name = "builder-squad" if squad == "builder" else "operations-squad"
        agents_dir = output_root / "plugins" / plugin_name / "agents"
        if agents_dir.exists():
            for entry in sorted(agents_dir.iterdir()):
                if entry.is_file() and entry.stem not in live_names[squad]:
                    entry.unlink()
                    removed_log.append(f"plugins/{plugin_name}/agents/{entry.name}")


def generate(output_root, source_dir, portraits_dir, patterns, placeholders, overrides_dir):
    """Run the full transform, writing every generated file under
    output_root. Used both by the real writer (output_root=REPO_ROOT) and
    the --check dry run (output_root=a throwaway temp dir). patterns comes
    from build_fail_loud_patterns() -- the caller (main()) is responsible
    for refusing to run at all if the private-names guard file couldn't be
    loaded; generate() just uses whatever patterns it's handed."""
    scrub_log = []
    rainer_scrub_log = []
    rainer_table_log = []
    private_table_log = []
    falco_table_log = []
    falco_scrub_log = []
    section_rewrite_log = []
    skipped_files_log = []
    symlink_log = []
    blocked_log = []
    removed_log = []
    missing_portraits = []
    builder_rows = []
    operations_rows = []

    for name in BUILDER_LIVE:
        try:
            builder_rows.append(process_agent(name, source_dir, portraits_dir, scrub_log, missing_portraits, output_root,
                                               patterns, placeholders, overrides_dir, falco_table_log, falco_scrub_log, section_rewrite_log, OPERATIONS_LIVE,
                                               rainer_scrub_log, rainer_table_log, private_table_log))
        except BuildError as exc:
            # A genuine fail-loud stop -- not a silent strip: exclude just
            # this agent, keep building everyone else, report every line
            # the scan found (fail_loud_scan now reports all hits, not
            # just the first -- see its docstring).
            blocked_log.append((name, str(exc)))

    for name in OPERATIONS_LIVE:
        if name in OVERRIDE_AGENT_NAMES:
            try:
                operations_rows.append(process_override_agent(
                    name, portraits_dir, falco_table_log, output_root,
                    OPERATIONS_LIVE, skipped_files_log, missing_portraits,
                    scrub_log, rainer_scrub_log, rainer_table_log, private_table_log, patterns,
                    overrides_dir, symlink_log))
            except BuildError as exc:
                blocked_log.append((name, str(exc)))
        else:
            try:
                operations_rows.append(process_agent(name, source_dir, portraits_dir, scrub_log, missing_portraits, output_root,
                                                       patterns, placeholders, overrides_dir, falco_table_log, falco_scrub_log, section_rewrite_log, OPERATIONS_LIVE,
                                                       rainer_scrub_log, rainer_table_log, private_table_log))
            except BuildError as exc:
                blocked_log.append((name, str(exc)))

    cleanup_stale_output(output_root, builder_rows, operations_rows, removed_log)

    write_json(output_root / "plugins" / "builder-squad" / ".claude-plugin" / "plugin.json",
               builder_plugin_manifest(builder_rows))
    write_json(output_root / "plugins" / "operations-squad" / ".claude-plugin" / "plugin.json",
               operations_plugin_manifest(operations_rows))
    write_json(output_root / ".claude-plugin" / "marketplace.json", marketplace_manifest(builder_rows, operations_rows))

    readme = render_root_readme(builder_rows, operations_rows)
    (output_root / "README.md").write_text(readme, encoding="utf-8")

    return (builder_rows, operations_rows, scrub_log, rainer_scrub_log, rainer_table_log, private_table_log,
            falco_table_log, falco_scrub_log, section_rewrite_log, skipped_files_log, symlink_log, blocked_log,
            removed_log, missing_portraits)


def dirs_differ(a, b):
    """Recursively compare two directories. Returns a list of human-
    readable diff descriptions; empty means identical. Missing-vs-present
    counts as drift too (a deleted or newly-required generated file)."""
    if not a.exists() and not b.exists():
        return []
    if not a.exists() or not b.exists():
        return [f"{a} vs {b}: one side missing"]
    diffs = []
    cmp = filecmp.dircmp(a, b)
    diffs += [f"only in {a}: {name}" for name in cmp.left_only]
    diffs += [f"only in {b}: {name}" for name in cmp.right_only]
    diffs += [f"differs: {a / name} vs {b / name}" for name in cmp.diff_files]
    for sub in cmp.common_dirs:
        diffs += dirs_differ(a / sub, b / sub)
    return diffs


def report_side_channel(scrub_log, rainer_scrub_log, rainer_table_log, private_table_log, falco_table_log, falco_scrub_log,
                         section_rewrite_log, skipped_files_log, symlink_log, blocked_log, removed_log, missing_portraits):
    if scrub_log:
        print("Generic house-name scrub fired (bare 'Rook' survivors, logged not silently stripped):", file=sys.stderr)
        for source_name, before, after in scrub_log:
            print(f"  {source_name}.md: {before!r} -> {after!r}", file=sys.stderr)

    if private_table_log:
        print("Explicit private-name rewrite-table applied:", file=sys.stderr)
        for name, line_no, old, new, status in private_table_log:
            where = f"{name}.md:{line_no}" if line_no else f"{name}.md"
            print(f"  {where}: {old!r} -> {new!r} [{status}]", file=sys.stderr)

    if rainer_table_log:
        print("Explicit Rainer rewrite-table applied:", file=sys.stderr)
        for name, line_no, old, new, status in rainer_table_log:
            where = f"{name}.md:{line_no}" if line_no else f"{name}.md"
            print(f"  {where}: {old!r} -> {new!r} [{status}]", file=sys.stderr)

    if rainer_scrub_log:
        print("Rainer scrub fired (Dispatch/Interplay lines only, logged not silently stripped):", file=sys.stderr)
        for source_name, before, after in rainer_scrub_log:
            print(f"  {source_name}.md: {before!r} -> {after!r}", file=sys.stderr)

    if falco_table_log:
        print("Explicit Falco name-table applied:", file=sys.stderr)
        for name, line_no, old, new, status in falco_table_log:
            where = f"{name}.md:{line_no}" if line_no else f"{name}.md"
            print(f"  {where}: {old!r} -> {new!r} [{status}]", file=sys.stderr)

    if falco_scrub_log:
        print("Generic Falco scrub fired (bare 'Falco' survivors, logged not silently stripped):", file=sys.stderr)
        for source_name, before, after in falco_scrub_log:
            print(f"  {source_name}.md: {before!r} -> {after!r}", file=sys.stderr)

    if section_rewrite_log:
        print("Section-rewrite table applied (Sawyer/Thorn):", file=sys.stderr)
        for entry in section_rewrite_log:
            if entry[1] in ("drop", "replace-body"):
                name, action, heading, new_text = entry
                print(f"  {name}.md [{action}] {heading} -> {new_text!r}", file=sys.stderr)
            else:
                name, line_no, old, new, status = entry
                where = f"{name}.md:{line_no}" if line_no else f"{name}.md"
                print(f"  {where}: {old!r} -> {new!r} [{status}]", file=sys.stderr)

    if blocked_log:
        print("BLOCKED (fail-loud stop -- NOT shipped this run, excluded not stripped):", file=sys.stderr)
        for name, msg in blocked_log:
            for line in msg.split("\n"):
                print(f"  {name}: {line}", file=sys.stderr)

    if skipped_files_log:
        print("Extras files skipped (over the 10 MB cap -- not copied, linked in that agent's README instead):", file=sys.stderr)
        for name, repo_rel_path, size in skipped_files_log:
            print(f"  {name}: {repo_rel_path}: {size:,} bytes ({size / (1024*1024):.2f} MB)", file=sys.stderr)

    if symlink_log:
        print("Symlinks skipped in copied extras (not followed, not copied):", file=sys.stderr)
        for name, rel_path in symlink_log:
            print(f"  {name}: {rel_path}", file=sys.stderr)

    if removed_log:
        print("Stale output removed (not in this run's roster):", file=sys.stderr)
        for path in removed_log:
            print(f"  removed {path}", file=sys.stderr)

    if missing_portraits:
        print(f"No portrait found for: {', '.join(missing_portraits)} -- README generated without a portrait line.", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_dir", nargs="?", default=str(DEFAULT_SOURCE_DIR),
                         help="Directory of source agent .md files (default: ~/.claude/agents)")
    parser.add_argument("--portraits-dir", default=str(DEFAULT_PORTRAITS_DIR),
                         help="Directory of <name>.jpg portraits. Default is "
                              "house-internal (a Funkatorium Mac path) and "
                              "will be empty on any other machine -- pass "
                              "your own, or accept READMEs without a "
                              "portrait line.")
    parser.add_argument("--check", action="store_true",
                         help="Dry run: build into a temp dir, diff against "
                              "this tree, exit 1 on drift. Writes nothing.")
    parser.add_argument("--private-names", default=str(DEFAULT_PRIVATE_NAMES_FILE),
                         help="Path to the private-names guard file (one "
                              "name per line, '#' comments, blank lines "
                              "ignored). Default: ~/.claude/the-crew-"
                              "private-names.txt. Both the writer and "
                              "--check refuse to run (exit 2) if this file "
                              "is missing or empty -- see load_private_names.")
    parser.add_argument("--house-patterns", default=str(DEFAULT_HOUSE_PATTERNS_FILE),
                         help="Path to the house-infrastructure guard file "
                              "(`label<TAB>regex` per line, '#' comments, "
                              "blank lines ignored). Default: ~/.claude/"
                              "the-crew-house-patterns.txt. Both the writer "
                              "and --check refuse to run (exit 2) if this "
                              "file is missing or empty -- see "
                              "load_house_patterns.")
    parser.add_argument("--placeholders", default=str(DEFAULT_PLACEHOLDERS_FILE),
                         help="Path to the placeholder guard file (`[agent]` "
                              "sections, `old==>new` lines). Default: "
                              "~/.claude/the-crew-placeholders.txt. Both the "
                              "writer and --check refuse to run (exit 2) if "
                              "this file is missing or empty -- see "
                              "load_placeholders.")
    parser.add_argument("--overrides-dir", default=str(DEFAULT_OVERRIDES_DIR),
                         help="Directory containing michael-security-agent/ "
                              "and evangeline-legal-agent/ (each with its "
                              "own <name>.md and assets/). Default is "
                              "house-internal (~/AI on a Funkatorium Mac) "
                              "-- pass your own checkout root.")
    args = parser.parse_args()

    source_dir = Path(args.source_dir).expanduser()
    portraits_dir = Path(args.portraits_dir).expanduser()
    private_names_path = Path(args.private_names).expanduser()
    house_patterns_path = Path(args.house_patterns).expanduser()
    placeholders_path = Path(args.placeholders).expanduser()
    overrides_dir = Path(args.overrides_dir).expanduser()

    private_names = load_private_names(private_names_path)
    if not private_names:
        print("private-names guard file missing — refusing to build without it", file=sys.stderr)
        sys.exit(2)
    house_patterns = load_house_patterns(house_patterns_path)
    if not house_patterns:
        print("house-patterns guard file missing — refusing to build without it", file=sys.stderr)
        sys.exit(2)
    placeholders = load_placeholders(placeholders_path)
    if not placeholders:
        print("placeholders guard file missing — refusing to build without it", file=sys.stderr)
        sys.exit(2)
    patterns = build_fail_loud_patterns(private_names, house_patterns)

    if args.check:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            try:
                (builder_rows, operations_rows, scrub_log, rainer_scrub_log, rainer_table_log, private_table_log,
                 falco_table_log, falco_scrub_log, section_rewrite_log, skipped_files_log, symlink_log, blocked_log,
                 removed_log, missing_portraits) = generate(tmp_root, source_dir, portraits_dir, patterns, placeholders, overrides_dir)
            except BuildError as exc:
                print(f"BUILD FAILED (fail-loud rule fired): {exc}", file=sys.stderr)
                sys.exit(1)

            report_side_channel(scrub_log, rainer_scrub_log, rainer_table_log, private_table_log, falco_table_log,
                                 falco_scrub_log, section_rewrite_log, skipped_files_log, symlink_log, blocked_log,
                                 removed_log, missing_portraits)

            diffs = []
            diffs += dirs_differ(tmp_root / "builder", REPO_ROOT / "builder")
            diffs += dirs_differ(tmp_root / "operations", REPO_ROOT / "operations")
            diffs += dirs_differ(tmp_root / "plugins", REPO_ROOT / "plugins")
            diffs += dirs_differ(tmp_root / ".claude-plugin", REPO_ROOT / ".claude-plugin")
            if not filecmp.cmp(tmp_root / "README.md", REPO_ROOT / "README.md", shallow=False):
                diffs.append("differs: README.md")

            if diffs:
                print("DRIFT DETECTED (tree does not match a fresh generation from source):", file=sys.stderr)
                for d in diffs:
                    print(f"  {d}", file=sys.stderr)
                sys.exit(1)

        total = len(builder_rows) + len(operations_rows)
        print(f"{total} agents, 0 house references")
        if blocked_log:
            sys.exit(1)
    else:
        try:
            (builder_rows, operations_rows, scrub_log, rainer_scrub_log, rainer_table_log, private_table_log,
             falco_table_log, falco_scrub_log, section_rewrite_log, skipped_files_log, symlink_log, blocked_log,
             removed_log, missing_portraits) = generate(REPO_ROOT, source_dir, portraits_dir, patterns, placeholders, overrides_dir)
        except BuildError as exc:
            print(f"BUILD FAILED (fail-loud rule fired): {exc}", file=sys.stderr)
            sys.exit(1)

        report_side_channel(scrub_log, rainer_scrub_log, rainer_table_log, private_table_log, falco_table_log,
                             falco_scrub_log, section_rewrite_log, skipped_files_log, symlink_log, blocked_log,
                             removed_log, missing_portraits)
        total = len(builder_rows) + len(operations_rows)
        print(f"Wrote {total} agents ({len(builder_rows)} builder, {len(operations_rows)} operations). 0 house references.")
        if blocked_log:
            print(f"BLOCKED: {[n for n, _ in blocked_log]} not written this run -- see stderr above.", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
