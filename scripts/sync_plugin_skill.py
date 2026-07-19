#!/usr/bin/env python3
"""Copy the packaged skill into the plugin tree.

The skill exists in two places for two delivery routes:

  src/sessionhub/skill_files/SKILL.md      shipped inside the wheel, written by
                                           `sessionhub skill install`
  plugins/sessionhub/skills/sessionhub/    read by Claude Code's plugin
                                           marketplace, straight from git

The first is canonical. Run this after editing it; tests/test_skill.py fails
if the two ever drift.
"""

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src" / "sessionhub" / "skill_files" / "SKILL.md"
PLUGIN = ROOT / "plugins" / "sessionhub" / "skills" / "sessionhub" / "SKILL.md"

PLUGIN.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(SOURCE, PLUGIN)
print(f"synced {SOURCE.relative_to(ROOT)} -> {PLUGIN.relative_to(ROOT)}")
