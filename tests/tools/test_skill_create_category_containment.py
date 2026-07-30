"""Skill WRITES must not escape the configured skills roots.

Regression: a vendored Orca source tree was mounted at
``~/.hermes/skills/orchestration``, so an agent-created skill landed inside
that checkout and tripped three harness runtime audits as an unexpected
runtime skill. Reads must stay unrestricted — only writes are gated.

Implementation notes:
- ``_find_skill`` uses ``Path.rglob()`` which does NOT follow symlinks in
  Python <=3.12, so vendor skills under a symlinked category are not
  reachable through the normal patch/edit/write_file API (they return
  "not found" before reaching any write path).
- The containment guard in ``_atomic_write_text`` / ``_assert_within_skills_roots``
  is therefore defense-in-depth for ``create`` (which constructs the target
  path directly without ``_find_skill``) and for any future code path that
  might follow symlinks.

Runnable standalone (no pytest needed):
    venv/bin/python tests/tools/test_skill_create_category_containment.py
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="skill-containment-")
os.environ["HERMES_HOME"] = str(Path(_TMP) / ".hermes")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools import skill_manager_tool as smt  # noqa: E402

SKILL_MD = """---
name: {name}
description: Probe skill used to verify skills-root containment.
---

Body text so frontmatter validation passes.
"""


def _vendor_root() -> Path:
    """A fake vendored skill source checkout, outside every skills root."""
    return Path(_TMP) / ".local/share/agent-skill-sources/stablyai-orca/skills/orchestration"


def _link_vendor_category() -> Path:
    """Mount the vendor tree at <skills>/orchestration, as on the real host."""
    skills = smt._skills_dir()
    skills.mkdir(parents=True, exist_ok=True)
    vendor = _vendor_root()
    vendor.mkdir(parents=True, exist_ok=True)
    link = skills / "orchestration"
    if not link.exists():
        link.symlink_to(vendor)
    return vendor


def _create(name, category=None):
    return smt._create_skill(name=name, content=SKILL_MD.format(name=name), category=category)


# ── create path (the real attack surface) ─────────────────────────────────────

def test_vendor_symlink_category_is_rejected_on_create():
    """create() resolves the category path directly — must be blocked."""
    vendor = _link_vendor_category()

    result = _create("vendor-probe", "orchestration")

    assert result["success"] is False, result
    assert str(vendor.resolve()) in result["error"], result["error"]
    assert "vendored skill source tree" in result["error"], result["error"]
    assert not (vendor / "vendor-probe").exists(), "create wrote into the vendor tree"


def test_no_empty_dir_left_in_vendor_tree():
    """mkdir must not run before the containment check."""
    vendor = _link_vendor_category()

    smt._create_skill(name="mkdir-probe", content=SKILL_MD.format(name="mkdir-probe"),
                      category="orchestration")

    assert list(vendor.iterdir()) == [], "vendor tree has a leftover directory"


# ── _atomic_write_text guard (defense-in-depth) ────────────────────────────────

def test_assert_within_skills_roots_raises_on_vendor_path():
    """_assert_within_skills_roots is the shared rejection primitive."""
    vendor = _link_vendor_category()
    victim = vendor / "some-skill" / "SKILL.md"

    raised = False
    try:
        smt._assert_within_skills_roots(victim)
    except smt._SkillPathOutsideRoots as exc:
        raised = True
        assert str(vendor.resolve()) in str(exc)
        assert "vendored skill source tree" in str(exc)
    assert raised, "_SkillPathOutsideRoots was not raised for vendor path"


def test_assert_within_skills_roots_passes_for_owned_path():
    skills = smt._skills_dir()
    skills.mkdir(parents=True, exist_ok=True)
    ok_path = skills / "productivity" / "my-skill" / "SKILL.md"
    smt._assert_within_skills_roots(ok_path)  # must not raise


# ── reads and legitimate categories still work ─────────────────────────────────

def test_vendor_skill_not_findable_via_api():
    """_find_skill uses rglob (no followlinks in <=3.12) — vendor skills are
    invisible to the write API, giving first-layer protection before the guard."""
    vendor = _link_vendor_category()
    phantom = vendor / "vendor-resident"
    phantom.mkdir(parents=True, exist_ok=True)
    (phantom / "SKILL.md").write_text(SKILL_MD.format(name="vendor-resident"), encoding="utf-8")

    found = smt._find_skill("vendor-resident")

    assert found is None, f"_find_skill unexpectedly found a vendor skill: {found}"


def test_hermes_owned_real_category_succeeds():
    result = _create("owned-probe", "productivity")

    assert result["success"] is True, result
    assert (smt._skills_dir() / "productivity" / "owned-probe" / "SKILL.md").is_file()


def test_symlink_category_inside_skills_root_is_allowed():
    """No over-blocking: a symlink that resolves inside a root is fine."""
    skills = smt._skills_dir()
    real = skills / "_real_category"
    real.mkdir(parents=True, exist_ok=True)
    link = skills / "linked"
    if not link.exists():
        link.symlink_to(real)

    result = _create("linked-probe", "linked")

    assert result["success"] is True, result
    assert (real / "linked-probe" / "SKILL.md").is_file()


def test_external_dir_skill_remains_writable():
    """skills.external_dirs are declared roots — writes there must still work."""
    external = Path(_TMP) / "shared-skills"
    external.mkdir(parents=True, exist_ok=True)
    resident = external / "external-resident"
    resident.mkdir(parents=True, exist_ok=True)
    (resident / "SKILL.md").write_text(SKILL_MD.format(name="external-resident"), encoding="utf-8")

    import agent.skill_utils as su
    orig = su.get_all_skills_dirs
    su.get_all_skills_dirs = lambda: [smt._skills_dir(), external]
    try:
        result = smt._patch_skill(
            "external-resident", old_string="Body text", new_string="Edited body",
            file_path=None
        )
    finally:
        su.get_all_skills_dirs = orig

    assert result["success"] is True, result
    assert "Edited body" in (resident / "SKILL.md").read_text(encoding="utf-8")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(tests)}/{len(tests)} PASS")
