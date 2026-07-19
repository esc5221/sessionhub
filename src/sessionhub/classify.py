"""Project classification.

Two layers:
  1. `project_rules` table (SQL LIKE patterns, priority-ordered) — user rules
  2. path_component fallback — infer project from path shape. Auto-registers
     the inferred (pattern, project) pair into project_rules with source='auto'
     so future queries hit (1) directly.

Goal: user never has to hand-register rules for new folders.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Paths we never classify: these are roots that could belong to any project.
_GENERIC_ROOTS = {
    "/",
    "~",
    "/Users",
    "/home",
    "/tmp",
    "/var",
    "/opt",
}

# Directory names that are containers rather than projects. When a path is
# <home>/<container>/<project>/..., we want <project>.
#
#   ~/code/acme-api/services  ->  project "acme-api", subsystem "services"
#
# Extend this per-machine via config.toml rather than editing the source:
#
#   [classification]
#   generic_components = ["sandbox", "clients"]   # merged with the list below
#
DEFAULT_GENERIC_COMPONENTS = frozenset(
    {
        "projects",
        "proj",
        "work",
        "workspace",
        "repos",
        "repo",
        "code",
        "dev",
        "src",
        "git",
    }
)

# Second-level names that never make a useful subsystem label.
_NON_SUBSYSTEM = frozenset({"src", "dist", "build", "node_modules", "target"})


def _match_rule(
    conn: sqlite3.Connection, project_path: str
) -> tuple[str, str | None] | None:
    row = conn.execute(
        """
        SELECT project, subsystem FROM project_rules
        WHERE ? LIKE path_pattern
        ORDER BY priority DESC LIMIT 1
        """,
        (project_path,),
    ).fetchone()
    if row:
        return row[0], row[1]
    return None


def _infer_from_path(
    project_path: str,
    home: Path,
    generic_components: frozenset[str] | set[str],
) -> tuple[str, str | None, str] | None:
    """Return (project, subsystem, pattern_for_rule) or None.

    Uses the first "meaningful" path component under HOME.
    """
    try:
        p = Path(project_path)
    except Exception:
        return None

    # Skip obviously generic paths
    if project_path in _GENERIC_ROOTS or str(p).rstrip("/") == str(home).rstrip("/"):
        return None

    try:
        rel = p.relative_to(home)
    except ValueError:
        # outside HOME — use the first directory component as project
        parts = [x for x in p.parts if x not in ("/", "")]
        if not parts:
            return None
        first = parts[0]
        return first, None, f"%{first}%"

    parts = [x for x in rel.parts if x]
    if not parts:
        return None

    # Filter out generic wrapper directories
    meaningful = [x for x in parts if x not in generic_components]

    # If the path looks like <home>/<generic>/<project>/..., prefer <project>.
    # If every component is generic, fall back to the first.
    project = meaningful[0] if meaningful else parts[0]

    # subsystem: second meaningful component, if it adds information
    subsystem = None
    if len(meaningful) >= 2 and meaningful[1] != project:
        if meaningful[1] not in _NON_SUBSYSTEM:
            subsystem = meaningful[1]

    # Derive a rule pattern from the project component. Note that
    # "%/{name}/%" would miss a path ending in the component itself
    # (/x/y/name), so the trailing slash is left off deliberately.
    pattern = f"%/{project}%"

    return project, subsystem, pattern


def classify(
    conn: sqlite3.Connection,
    project_path: str | None,
    *,
    auto: bool = True,
    home: Path | None = None,
    generic_components: frozenset[str] | set[str] | None = None,
) -> tuple[str | None, str | None]:
    """Return (project, subsystem) for a session path.

    If auto is True, also registers a new auto-rule so subsequent sessions
    from the same project hit rule-matching directly.
    """
    if not project_path:
        return None, None

    # Layer 1: existing rules
    hit = _match_rule(conn, project_path)
    if hit:
        return hit

    if not auto:
        return None, None

    # Layer 2: path_component inference
    home = home or Path.home()
    generic = (
        DEFAULT_GENERIC_COMPONENTS
        if generic_components is None
        else DEFAULT_GENERIC_COMPONENTS | set(generic_components)
    )
    inferred = _infer_from_path(project_path, home, generic)
    if not inferred:
        return None, None

    project, subsystem, pattern = inferred

    # Auto-register so next time layer 1 hits
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO project_rules (path_pattern, project, subsystem, priority, source)
            VALUES (?, ?, ?, -5, 'auto')
            """,
            (pattern, project, subsystem),
        )
    except sqlite3.Error:
        pass

    return project, subsystem


def sync_config_rules(conn: sqlite3.Connection, rules) -> int:
    """Push `[classification] rules` from config.toml into project_rules.

    Config is the source of truth for source='config' rows: rules deleted from
    config.toml are removed from the table. Auto-inferred rows (source='auto')
    are left alone.

    An empty rule list is a no-op rather than a wipe — otherwise a DB seeded
    with 'config' rows by some earlier means would be emptied by any user who
    simply never defined rules in config.toml.

    Returns the number of rules written.
    """
    if not rules:
        return 0
    conn.execute("DELETE FROM project_rules WHERE source='config'")
    for r in rules:
        conn.execute(
            """
            INSERT INTO project_rules
              (path_pattern, project, subsystem, priority, source)
            VALUES (?, ?, ?, ?, 'config')
            """,
            (r.pattern, r.project, r.subsystem, r.priority),
        )
    conn.commit()
    return len(rules)
