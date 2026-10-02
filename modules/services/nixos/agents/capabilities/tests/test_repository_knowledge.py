"""Published contract and real pinned skill tools in isolated writable profiles.

This proves tool behavior and publication, not future model compliance.
"""

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SKILLS = Path(__file__).parents[2] / "skills"


def respect_unix_modes():
    """Drop DAC bypass only in the disposable probe, including root CI builds."""
    if sys.platform != "linux" or os.geteuid() != 0:
        return

    class Header(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]

    class Capabilities(ctypes.Structure):
        _fields_ = [
            ("effective", ctypes.c_uint32),
            ("permitted", ctypes.c_uint32),
            ("inheritable", ctypes.c_uint32),
        ]

    libc = ctypes.CDLL(None, use_errno=True)
    header = Header(0x20080522, 0)  # Linux capability ABI v3, current thread.
    capabilities = (Capabilities * 2)()
    if libc.capget(ctypes.byref(header), capabilities) != 0:
        raise OSError(ctypes.get_errno(), "capget failed")
    mask = (1 << 1) | (1 << 2)  # CAP_DAC_OVERRIDE and CAP_DAC_READ_SEARCH.
    for field in ("effective", "permitted", "inheritable"):
        setattr(capabilities[0], field, getattr(capabilities[0], field) & ~mask)
    # Lowering our own sets needs no CAP_SETPCAP; changing the bounding set
    # would fail in restricted Nix builders. No exec follows this reduction.
    if libc.capset(ctypes.byref(header), capabilities) != 0:
        raise OSError(ctypes.get_errno(), "capset failed")


def native_probe():
    respect_unix_modes()
    sys.path.insert(0, os.environ["HERMES_UPSTREAM_SOURCE"])
    from tools.skills_tool import skills_list, skill_view
    from tools.skill_manager_tool import skill_manage

    home = Path(os.environ["HERMES_HOME"])

    def call(action, name="widget-procedures", **kwargs):
        return json.loads(skill_manage(action, name, **kwargs))

    body = "---\nname: widget-procedures\ndescription: Confirmed widget workflow from AGENTS.md.\n---\nRead the repository AGENTS.md before edits.\n"
    initial = json.loads(skills_list())
    assert initial["success"]
    assert not (home / "skills/widget-procedures").exists()
    created = call("create", content=body)
    assert created["success"] and not created.get("staged"), created
    assert Path(created["skill_md"]).is_relative_to(home / "skills")
    viewed = json.loads(skill_view("widget-procedures", preprocess=False))
    assert viewed["success"] and viewed["content"] == body, viewed
    duplicate = call("create", content=body)
    assert not duplicate["success"], duplicate
    updated = call(
        "patch",
        old_string="Read the repository AGENTS.md before edits.",
        new_string="Read the repository AGENTS.md before edits. Confirmed fixture procedure: invoke ./test-widget.",
    )
    assert updated["success"], updated
    assert len(list((home / "skills").rglob("widget-procedures/SKILL.md"))) == 1
    assert (
        "./test-widget" in json.loads(skill_view("widget-procedures", preprocess=False))["content"]
    )
    # Native identifiers cannot address another profile or escape the skills root.
    for foreign in ["../foreign", str(home.parent / "other/skills/foreign")]:
        assert not call("create", name=foreign, content=body)["success"]
    assert not call("patch", name="foreign-procedures", old_string="x", new_string="y")["success"]
    managed = home.parent.parent / "managed/managed-procedure"
    assert not os.access(managed, os.W_OK), "probe must respect directory mode bits"
    assert not os.access(managed / "SKILL.md", os.W_OK), "managed file must be read-only"
    assert json.loads(skill_view("managed-procedure", preprocess=False))["success"]
    try:
        result = call(
            "patch",
            name="managed-procedure",
            old_string="Managed invariant",
            new_string="Untrusted override",
        )
        assert not result["success"], result
    except PermissionError:
        pass
    assert "Managed invariant" in (managed / "SKILL.md").read_text()
    assert not call("create", name="managed-procedure", content=body)["success"]
    # Read-only OS boundary, not a mocked tool result. Restore permissions for cleanup.
    target = home / "skills/widget-procedures"
    target.chmod(0o500)
    (target / "SKILL.md").chmod(0o400)
    try:
        try:
            result = call("patch", old_string="./test-widget", new_string="./unconfirmed")
            assert not result["success"], result
        except PermissionError:
            pass
        assert "./unconfirmed" not in (target / "SKILL.md").read_text()
    finally:
        target.chmod(0o700)
        (target / "SKILL.md").chmod(0o600)
    print("native discovery, read, create, reuse, patch and ownership boundaries passed")


class RepositoryKnowledgeTests(unittest.TestCase):
    def test_published_contract_and_generic_consumers(self):
        text = (SKILLS / "repository-knowledge/SKILL.md").read_text()
        for required in [
            "skills_list",
            "skill_view",
            "skill_manage",
            "AGENTS.md",
            "duplicate",
            "ownership",
            "unwritable",
            "blocker",
            "private skill",
            "deployment authorization",
        ]:
            self.assertIn(required, text)
        for name in ["repository-knowledge", "forgejo-pr-lifecycle", "nix-maintenance-protocol"]:
            body = (SKILLS / name / "SKILL.md").read_text()
            for forbidden in [
                "nixos/nix-config",
                "git.wrzalek.com",
                "Vikunja project: 110",
                "Sandro",
                "refs/heads/master",
            ]:
                self.assertNotIn(forbidden, body)
            if name != "repository-knowledge":
                self.assertIn("repository-knowledge", body)
        foundation = (SKILLS.parent / "capabilities/foundation-packages.nix").read_text()
        self.assertIn('mkAgentSkill "repository-knowledge"', foundation)
        self.assertEqual(foundation.count("requires = [ repositoryKnowledge.id ];"), 2)

    def test_native_tools_own_profile_only(self):
        source = os.environ.get("HERMES_UPSTREAM_SOURCE")
        self.assertTrue(source, "pinned Hermes source is required")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "profiles/builder"
            other = root / "profiles/auditor/skills/foreign-procedures"
            home.mkdir(parents=True)
            other.mkdir(parents=True)
            (other / "SKILL.md").write_text(
                "---\nname: foreign-procedures\ndescription: Foreign.\n---\nx\n"
            )
            before = (other / "SKILL.md").read_bytes()
            managed = root / "managed/managed-procedure"
            managed.mkdir(parents=True)
            (managed / "SKILL.md").write_text(
                "---\nname: managed-procedure\ndescription: Managed.\n---\nManaged invariant\n"
            )
            managed.chmod(0o500)
            (managed / "SKILL.md").chmod(0o400)
            (home / "config.yaml").write_text(
                json.dumps({"skills": {"external_dirs": [str(managed.parent)]}})
            )
            env = dict(os.environ, HERMES_HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
            proc = subprocess.run(
                [sys.executable, __file__, "native-probe"],
                env=env,
                text=True,
                capture_output=True,
                timeout=90,
            )
            managed.chmod(0o700)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual((other / "SKILL.md").read_bytes(), before)


if __name__ == "__main__":
    if sys.argv[1:] == ["native-probe"]:
        native_probe()
    else:
        unittest.main()
