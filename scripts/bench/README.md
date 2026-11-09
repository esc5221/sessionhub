# bench

Two measurements for search changes, run against your own archive and your
own scenes. Scenes are real conversations, so they are never committed.

A scene is one moment someone went looking for an earlier session: when (`ts`),
from which session (`sid`), what they asked (`prompt`), what they typed into
`sessionhub search` (`searches`), and which sessions they then opened
(`opened`, the answer). See the docstring of `search_hit.py`.

| script | measures | LLM |
|---|---|---|
| `search_hit.py` | hit@k of `search` alone, with the request text or the typed searches as the query | none |
| `replay.py` | an agent given the request and the CLI (via `cutoff_cli.py`) — does it find the opened session, in how many turns and tokens | `claude -p` |

Both read the archive as it was at the scene's time: later sessions and the
scene's own session are hidden (`cutoff_cli.py` does this for the agent).

```sh
export SESSIONHUB_CONFIG_DIR=/path/to/a/copy/of/config   # never your live hub
python scripts/bench/search_hit.py --scenes scenes.jsonl --query searches --mode legacy
python scripts/bench/search_hit.py --scenes scenes.jsonl --query prompt --mode ranked
python scripts/bench/replay.py --scenes scenes.jsonl --out runs/before --model haiku
python scripts/bench/replay.py --out runs/before --summary
```

Compare two runs on the same scenes only, and resample by session (several
scenes often come from one session) before reading a difference as real.
