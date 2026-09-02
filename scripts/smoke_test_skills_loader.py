"""Unit tests for skills/loader.py: waku-style progressive disclosure over
skills/{default,user,learned}.

Run: python -m scripts.smoke_test_skills_loader
"""
import shutil
from pathlib import Path

from skills import loader

SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"


# ---------------------------------------------------------------------------
# Test 1: Level 1 — descriptions scanned, bodies NOT loaded
# ---------------------------------------------------------------------------
def test_level1_descriptions_only():
    print("\n--- Test 1: Level 1 (descriptions scanned, no bodies loaded) ---")
    loader.reset_cache()
    index = loader.get_index()

    assert len(index) >= 1, "expected at least the bundled sysmlv2-skill"
    sysml_skill = next((s for s in index if s.name == "sysmlv2-skill"), None)
    assert sysml_skill is not None, "expected 'sysmlv2-skill' in the index"
    assert sysml_skill.source == "default"
    assert "SysML v2" in sysml_skill.description
    assert not hasattr(sysml_skill, "body"), "Level 1 SkillMeta must not carry a body field at all"

    print(f"index has {len(index)} skill(s): {[(s.name, s.source) for s in index]}")
    print(f"sysmlv2-skill description: {sysml_skill.description[:80]}...")
    print("assert OK: SkillMeta carries name+description only, no body field")
    print("Test 1 PASSED")


# ---------------------------------------------------------------------------
# Test 2: Level 2 — match() finds the relevant skill, loads its body; an
# unrelated query does not match.
# ---------------------------------------------------------------------------
def test_level2_match():
    print("\n--- Test 2: Level 2 (keyword-overlap match, body loaded only for matches) ---")
    loader.reset_cache()

    relevant = loader.match("SysML v2 generate a braking requirement")
    assert len(relevant) == 1
    assert relevant[0].meta.name == "sysmlv2-skill"
    assert len(relevant[0].body) > 500, "matched skill's SKILL.md body should be loaded"
    assert relevant[0].score > 0
    print(f"match('SysML v2 generate a braking requirement') -> "
          f"{relevant[0].meta.name} (score={relevant[0].score}, body={len(relevant[0].body)} chars)")

    unrelated = loader.match("how do I bake a chocolate cake")
    assert unrelated == [], f"expected no match for an unrelated query, got {[m.meta.name for m in unrelated]}"
    print(f"match('how do I bake a chocolate cake') -> {unrelated} (correctly empty)")

    print("assert OK: relevant query matches and loads the body; unrelated query matches nothing")
    print("Test 2 PASSED")


# ---------------------------------------------------------------------------
# Test 3: Level 3 — section-level retrieval, not whole-file
# ---------------------------------------------------------------------------
def test_level3_section_retrieval():
    print("\n--- Test 3: Level 3 (section-level retrieval from reference files) ---")
    loader.reset_cache()

    full_errors_len = (SKILLS_ROOT / "default" / "sysml_v2" / "references" / "ERRORS.md").stat().st_size
    full_patterns_len = (SKILLS_ROOT / "default" / "sysml_v2" / "references" / "PATTERNS.md").stat().st_size

    diagnostics = [
        {
            "message": "Unknown keyword 'requirment'. Did you mean 'requirement'?",
            "line": 8, "column": 5, "severity": "Error",
        }
    ]
    error_help = loader.get_error_help(diagnostics)
    assert error_help, "expected a matched ERRORS.md section for the daltskin-style diagnostic"
    assert len(error_help) < full_errors_len, "must return a SLICE of ERRORS.md, not the whole file"
    assert "## Error:" in error_help
    other_error_headings = [
        "Couldn't resolve reference to Classifier",
        "Multiplicity must be literal Integer",
        "Feature must have a type",
    ]
    for heading in other_error_headings:
        assert heading not in error_help, f"unrelated section {heading!r} leaked into the result"
    print(f"get_error_help(...) -> {len(error_help)} chars (full ERRORS.md is {full_errors_len} chars)")
    print(error_help)

    patterns = loader.get_patterns("requirement")
    assert patterns, "expected a matched PATTERNS.md section for 'requirement'"
    assert len(patterns) < full_patterns_len, "must return a SLICE of PATTERNS.md, not the whole file"
    assert "Requirements" in patterns
    unrelated_pattern_headings = ["Pattern: Cloud Resource Modeling", "Pattern: Namespaces and Visibility"]
    for heading in unrelated_pattern_headings:
        assert heading not in patterns, f"unrelated section {heading!r} leaked into the result"
    print(f"\nget_patterns('requirement') -> {len(patterns)} chars (full PATTERNS.md is {full_patterns_len} chars)")

    print("\nassert OK: both helpers returned section-level slices only, never the whole reference file")
    print("Test 3 PASSED")


# ---------------------------------------------------------------------------
# Test 4: precedence — user/ overrides default/ for a same-named skill
# ---------------------------------------------------------------------------
def test_precedence_user_overrides_default():
    print("\n--- Test 4: precedence (user/ overrides default/ for a same-named skill) ---")
    dummy_dir = SKILLS_ROOT / "user" / "sysml_v2"
    dummy_dir.mkdir(parents=True, exist_ok=True)
    (dummy_dir / "SKILL.md").write_text(
        "---\n"
        "name: sysmlv2-skill\n"
        "description: DUMMY user-uploaded override of the bundled SysML v2 skill.\n"
        "---\n\n"
        "# Dummy override body\n",
        encoding="utf-8",
    )

    try:
        loader.reset_cache()
        index = loader.get_index()
        matches = [s for s in index if s.name == "sysmlv2-skill"]
        assert len(matches) == 1, "same-named skills across sources must be deduped, not both listed"
        winner = matches[0]
        print(f"winning source for 'sysmlv2-skill': {winner.source!r}, description={winner.description!r}")
        assert winner.source == "user", "user/ must win over default/ for the same skill name"
        assert winner.description.startswith("DUMMY"), "the USER skill's description must be the one kept"
        print("assert OK: user/ correctly overrode default/ for the conflicting skill name")
    finally:
        shutil.rmtree(dummy_dir)
        loader.reset_cache()
        # sanity: after removing the override, default/ is back to winning
        index = loader.get_index()
        winner = next(s for s in index if s.name == "sysmlv2-skill")
        assert winner.source == "default"
        print("cleanup OK: dummy user skill removed, default/ resumed winning")

    print("Test 4 PASSED")


def main() -> None:
    test_level1_descriptions_only()
    test_level2_match()
    test_level3_section_retrieval()
    test_precedence_user_overrides_default()
    print("\n=== SKILLS LOADER TEST SUITE PASSED ===")


if __name__ == "__main__":
    main()
