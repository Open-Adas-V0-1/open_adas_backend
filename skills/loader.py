"""Procedural memory: waku-style progressive disclosure over skills/{default,user,learned}.

Three levels, escalating cost:
  Level 1 (get_index)     — SKILL.md frontmatter only (name + description). Cheap, always run.
  Level 2 (match)         — keyword overlap between a query and each skill's name+description;
                             only the top-N matched skills' SKILL.md BODIES are read.
  Level 3 (get_patterns / get_syntax / get_error_help) — within a matched skill, SECTION-level
                             retrieval from its reference files (SYNTAX.md/PATTERNS.md/ERRORS.md).
                             A full reference file (300-500 lines) is never handed to the caller —
                             only the matching ## / ### section(s).

Matching throughout is transparent keyword/heading overlap — no embeddings, no LLM calls.

Source precedence when two skills share a `name`: user > learned > default (a user-uploaded
skill overrides a bundled default). All three directories are scanned every time — user/ and
learned/ are empty today but need no code change to start working once populated.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_SKILLS_ROOT = Path(__file__).resolve().parent

# Order = precedence order (first match for a given name wins).
_SOURCE_DIRS = (
    ("user", _SKILLS_ROOT / "user"),
    ("learned", _SKILLS_ROOT / "learned"),
    ("default", _SKILLS_ROOT / "default"),
)
_PRECEDENCE_RANK = {source: rank for rank, (source, _dir) in enumerate(_SOURCE_DIRS)}

_STOPWORDS = {
    "a", "an", "the", "of", "for", "to", "in", "on", "and", "or", "is", "are",
    "this", "that", "with", "by", "as", "be", "it", "its", "at", "from", "into",
    "i", "you", "your", "we", "not", "was", "were", "did", "mean", "here",
}

_HEADING_RE = re.compile(r"^(#{2,3})\s+(.*)$", re.MULTILINE)


def _normalize(word: str) -> str:
    """Naive stemming (strip a trailing plural 's') so 'requirement' overlaps
    'Requirements', 'constraint' overlaps 'Constraints', etc. Still a plain,
    transparent rule — not a real stemmer.
    """
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _tokenize(text: str) -> set[str]:
    return {
        _normalize(w)
        for w in re.findall(r"[a-zA-Z0-9_]+", text.lower())
        if len(w) > 1 and w not in _STOPWORDS
    }


# ---------------------------------------------------------------------------
# Level 1 — descriptions only
# ---------------------------------------------------------------------------

@dataclass
class SkillMeta:
    name: str
    description: str
    source: str  # "user" | "learned" | "default"
    dir: Path  # the skill's own directory (contains SKILL.md, references/)
    skill_md_path: Path


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Minimal frontmatter parser: top-level `key: value` scalar lines between the
    two `---` delimiters. Deliberately not a full YAML parser — SKILL.md frontmatter
    in practice is a handful of scalar keys (name, description, license); nested
    blocks (e.g. `metadata:`) are skipped, not needed by this loader.
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    frontmatter_block = text[3:end].strip("\n")
    body = text[end + 4:].lstrip("\n")

    meta: dict[str, str] = {}
    for line in frontmatter_block.splitlines():
        if not line or line[0] in " \t":
            continue  # skip nested/indented keys — not needed by this loader
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, body


def _scan_skill_dirs() -> list[SkillMeta]:
    found_by_name: dict[str, SkillMeta] = {}
    for source, base_dir in _SOURCE_DIRS:
        if not base_dir.exists():
            continue
        for skill_md in sorted(base_dir.glob("*/SKILL.md")):
            text = skill_md.read_text(encoding="utf-8")
            meta, _body = _parse_frontmatter(text)
            name = meta.get("name") or skill_md.parent.name
            if name in found_by_name:
                continue  # a higher-precedence source was scanned first (see _SOURCE_DIRS order)
            found_by_name[name] = SkillMeta(
                name=name,
                description=meta.get("description", ""),
                source=source,
                dir=skill_md.parent,
                skill_md_path=skill_md,
            )
    return list(found_by_name.values())


def _current_signature() -> tuple:
    """mtime signature over every SKILL.md across all three source dirs — cheap to
    compute, used to detect when the on-disk index needs a rescan.
    """
    sig = []
    for _source, base_dir in _SOURCE_DIRS:
        if not base_dir.exists():
            continue
        for skill_md in sorted(base_dir.glob("*/SKILL.md")):
            sig.append((str(skill_md), skill_md.stat().st_mtime_ns))
    return tuple(sig)


_index_cache: list[SkillMeta] | None = None
_index_signature: tuple | None = None


def get_index() -> list[SkillMeta]:
    """Level 1: cached descriptions (name + description) for every skill across
    user/ > learned/ > default/. Bodies are not read here. Automatically rescans
    when any SKILL.md's mtime signature changes.
    """
    global _index_cache, _index_signature
    sig = _current_signature()
    if _index_cache is None or sig != _index_signature:
        _index_cache = _scan_skill_dirs()
        _index_signature = sig
    return _index_cache


def reset_cache() -> None:
    """Test/dev helper: force the next get_index() call to rescan from disk."""
    global _index_cache, _index_signature
    _index_cache = None
    _index_signature = None


# ---------------------------------------------------------------------------
# Level 2 — match + load body
# ---------------------------------------------------------------------------

@dataclass
class MatchedSkill:
    meta: SkillMeta
    body: str
    score: int


def load_body(skill: SkillMeta) -> str:
    text = skill.skill_md_path.read_text(encoding="utf-8")
    _meta, body = _parse_frontmatter(text)
    return body


def match(query: str, max_skills: int = 2) -> list[MatchedSkill]:
    """Level 2: transparent keyword overlap between *query* and each skill's
    name+description. Only the top `max_skills` matches have their SKILL.md body
    read. Ties broken by source precedence (user > learned > default), then name.
    """
    query_tokens = _tokenize(query)
    scored: list[tuple[int, int, SkillMeta]] = []
    for skill in get_index():
        skill_tokens = _tokenize(skill.name.replace("-", " ").replace("_", " ")) | _tokenize(skill.description)
        overlap = len(query_tokens & skill_tokens)
        if overlap == 0:
            continue
        scored.append((overlap, _PRECEDENCE_RANK.get(skill.source, 99), skill))

    scored.sort(key=lambda t: (-t[0], t[1], t[2].name))
    top = scored[:max_skills]
    return [MatchedSkill(meta=s, body=load_body(s), score=score) for score, _rank, s in top]


# ---------------------------------------------------------------------------
# Level 3 — section-level retrieval from reference files
# ---------------------------------------------------------------------------

@dataclass
class Section:
    level: int
    heading: str
    text: str  # heading line + this section's own body, up to (not including) the next heading


def _parse_sections(markdown_text: str) -> list[Section]:
    headings = list(_HEADING_RE.finditer(markdown_text))
    sections: list[Section] = []
    for i, m in enumerate(headings):
        level = len(m.group(1))
        heading = m.group(2).strip()
        start = m.start()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(markdown_text)
        sections.append(Section(level=level, heading=heading, text=markdown_text[start:end].rstrip() + "\n"))
    return sections


def _score_section(section: Section, query_tokens: set[str]) -> int:
    heading_tokens = _tokenize(section.heading)
    body_tokens = _tokenize(section.text)
    # heading overlap counts more than body overlap — matching "by heading/keyword"
    return len(heading_tokens & query_tokens) * 3 + len(body_tokens & query_tokens)


def _select_sections(sections: list[Section], query: str, max_sections: int, min_score: int) -> list[Section]:
    query_tokens = _tokenize(query)
    scored = [(_score_section(s, query_tokens), s) for s in sections]
    scored = [(score, s) for score, s in scored if score >= min_score]
    # higher score first; among ties, prefer the more specific (deeper) heading —
    # smaller, more targeted section, in the spirit of "never load more than needed"
    scored.sort(key=lambda t: (-t[0], -t[1].level))
    return [s for _score, s in scored[:max_sections]]


def _resolve_skill_dir(skill_name: str | None) -> Path | None:
    index = get_index()
    if skill_name is not None:
        for skill in index:
            if skill.name == skill_name:
                return skill.dir
        return None
    return index[0].dir if index else None


def _read_reference(skill_name: str | None, filename: str) -> tuple[Path | None, str]:
    skill_dir = _resolve_skill_dir(skill_name)
    if skill_dir is None:
        return None, ""
    ref_path = skill_dir / "references" / filename
    if not ref_path.exists():
        return ref_path, ""
    return ref_path, ref_path.read_text(encoding="utf-8")


def get_syntax(query: str, skill_name: str | None = None, max_sections: int = 2) -> str:
    """Level 3: only the SYNTAX.md section(s) matching *query*, never the whole file."""
    _path, text = _read_reference(skill_name, "SYNTAX.md")
    if not text:
        return ""
    sections = _select_sections(_parse_sections(text), query, max_sections, min_score=2)
    return "\n".join(s.text for s in sections)


def get_patterns(query: str, skill_name: str | None = None, max_sections: int = 2) -> str:
    """Level 3: only the PATTERNS.md section(s) matching *query*, never the whole file."""
    _path, text = _read_reference(skill_name, "PATTERNS.md")
    if not text:
        return ""
    sections = _select_sections(_parse_sections(text), query, max_sections, min_score=2)
    return "\n".join(s.text for s in sections)


def get_error_help(diagnostics: list, skill_name: str | None = None) -> str:
    """Level 3: for each verify diagnostic, find the ERRORS.md section(s) whose
    heading/body overlaps its message, and return ONLY those matched fix sections
    (deduped) — not the whole file. Accepts dicts with a 'message' key, objects
    with a .message attribute, or plain strings.
    """
    _path, text = _read_reference(skill_name, "ERRORS.md")
    if not text:
        return ""
    all_sections = _parse_sections(text)

    matched: list[Section] = []
    seen_headings: set[str] = set()
    for diagnostic in diagnostics:
        if isinstance(diagnostic, dict):
            message = diagnostic.get("message", "")
        else:
            message = getattr(diagnostic, "message", str(diagnostic))
        for section in _select_sections(all_sections, message, max_sections=1, min_score=2):
            if section.heading not in seen_headings:
                seen_headings.add(section.heading)
                matched.append(section)

    return "\n".join(s.text for s in matched)
