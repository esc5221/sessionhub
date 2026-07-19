"""The skill ships two ways; both copies must stay in step."""

import json
from pathlib import Path

from sessionhub import skill

ROOT = Path(__file__).resolve().parent.parent
PACKAGED = ROOT / "src" / "sessionhub" / "skill_files" / "SKILL.md"
PLUGIN = ROOT / "plugins" / "sessionhub" / "skills" / "sessionhub" / "SKILL.md"
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN_MANIFEST = ROOT / "plugins" / "sessionhub" / ".claude-plugin" / "plugin.json"


def frontmatter(text: str) -> dict:
    assert text.startswith("---\n"), "SKILL.md must open with YAML frontmatter"
    body = text.split("---\n", 2)[1]
    out = {}
    key = None
    for line in body.splitlines():
        if line and not line.startswith((" ", "\t")) and ":" in line:
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip()
        elif key and line.strip():  # folded continuation
            out[key] += " " + line.strip()
    return out


# --- the two copies ---

def test_plugin_copy_matches_the_packaged_one():
    assert PLUGIN.exists(), "run scripts/sync_plugin_skill.py"
    assert PLUGIN.read_text() == PACKAGED.read_text(), (
        "skill copies have drifted — run scripts/sync_plugin_skill.py"
    )


def test_skill_is_installed_from_the_packaged_copy(tmp_path):
    dest = skill.install(tmp_path, force=True)
    assert (dest / "SKILL.md").read_text() == PACKAGED.read_text()


def test_install_refuses_to_clobber_without_force(tmp_path):
    assert skill.install(tmp_path) is not None
    assert skill.install(tmp_path) is None


# --- frontmatter Claude Code reads ---

def test_frontmatter_has_a_name_and_a_description():
    fm = frontmatter(PACKAGED.read_text())
    assert fm["name"] == "sessionhub"
    assert len(fm["description"]) > 40


def test_description_stays_within_the_listing_budget():
    """name + description share a 1,536 character budget in the skill list."""
    fm = frontmatter(PACKAGED.read_text())
    assert len(fm["name"]) + len(fm["description"]) < 1536


# --- marketplace wiring ---

def test_marketplace_points_at_the_plugin_directory():
    data = json.loads(MARKETPLACE.read_text())
    entry = data["plugins"][0]
    assert data["name"] == "sessionhub"
    source = (MARKETPLACE.parent.parent / entry["source"]).resolve()
    assert source == (ROOT / "plugins" / "sessionhub").resolve()
    assert source.is_dir()


def test_plugin_manifest_names_the_same_plugin():
    marketplace = json.loads(MARKETPLACE.read_text())
    manifest = json.loads(PLUGIN_MANIFEST.read_text())
    assert manifest["name"] == marketplace["plugins"][0]["name"]


def test_plugin_skill_sits_where_claude_code_looks():
    assert PLUGIN.parent.parent.name == "skills"
    assert PLUGIN.parent.name == "sessionhub"


# --- multi-agent install ---

def test_detect_targets_finds_present_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".codex").mkdir()
    names = [n for n, _ in skill.detect_targets()]
    assert names == ["Claude Code", "Codex"]


def test_detect_targets_skips_absent_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".codex").mkdir()          # only Codex present
    targets = skill.detect_targets()
    assert [n for n, _ in targets] == ["Codex"]
    assert targets[0][1] == tmp_path / ".codex" / "skills"


def test_detect_targets_empty_when_no_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert skill.detect_targets() == []


def test_install_all_writes_into_each_target(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".codex").mkdir()
    results = list(skill.install_all())
    assert {n for n, _ in results} == {"Claude Code", "Codex"}
    assert (tmp_path / ".claude" / "skills" / "sessionhub" / "SKILL.md").exists()
    assert (tmp_path / ".codex" / "skills" / "sessionhub" / "SKILL.md").exists()
