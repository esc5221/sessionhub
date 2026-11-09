"""search --expand: let a small model rewrite a request as search queries.

People ask for past work the way they talk ("do it like last time with that
relay thing"); sessions contain the names, paths and commands that were used.
On real recall scenes, cards found through model-written queries held the
needed facts about twice as often as cards found from the request's own words
(24% vs 14% of facts), and an agent given them picked the right first action
more often (.825 vs .763). A small model (haiku) wrote queries nearly as good
as a large one (93% of the facts covered).

The queries are merged with the original by reciprocal rank (search --any), so
expansion can add sessions but the plain match is never lost.
"""

from __future__ import annotations

from sessionhub import llm as llm_mod

PROMPT = """You turn a developer's request to their coding agent into search queries over their past agent sessions.
Guess which earlier session they mean and write queries made of words that session would actually contain.
- Spell out abbreviations and nicknames next to the short form (e.g. "bf" -> "browser-fleet bfr").
- Prefer proper nouns, file names, commands, service names, error strings. Leave out generic verbs, filler and pronouns.
- If the request mixes languages, or a term has a common spelling in another language, include both.
- Use the end of the current conversation, when given, to resolve "this", "that", "last time".
Output JSON only: {"queries": ["3-6 words", "3-6 words", "3-6 words", "up to 5 queries"]}"""


def queries(cfg, request: str, context: str | None = None, *, timeout: int = 60, limit: int = 5) -> list[str]:
    text = (f"End of the current conversation:\n{context[-1500:]}\n\n" if context else "") + f"Request:\n{request[:2500]}"
    data = llm_mod.parse_json(llm_mod.call(cfg, PROMPT, text, timeout=timeout))
    out = []
    for q in data.get("queries", []):
        if isinstance(q, str) and q.strip() and q.strip() not in out:
            out.append(q.strip())
    return out[:limit]
