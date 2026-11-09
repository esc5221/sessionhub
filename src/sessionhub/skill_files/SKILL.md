---
name: sessionhub
description: Search and read past AI coding sessions (Claude Code + Codex) collected by sessionhub across every machine you work on. Use when recalling what was done before — "how did I fix that last time", "what did I work on in project X last week", "find the session where we discussed Y", or when the raw transcript of an earlier session is needed. Triggers on 'sessionhub', 'shm', 'past session', 'previous session', 'earlier we', 'last time I'.
---

# sessionhub

Queries a SQLite archive of your Claude Code and Codex sessions: titles,
projects, timing, changed files, and full-text search. `search` covers each
session's title, summary, first message, project, changed files and tags, and
also the **conversation text** (the same trimmed digest `raw` prints). Tool
calls and their output are not kept, so a word that only ever appeared inside a
command or a tool result is not searchable.

Start with `search` when you know roughly what was said, `recent`/`list` when
you know roughly when it happened. Session IDs may be abbreviated to their
first 8 characters everywhere an id is accepted — that prefix is the first
column of every result listing.

## Commands

```
sessionhub search relay pace cap    ranked full-text search; best match first
sessionhub search --tag NAME        by tag
sessionhub search --file PATH       sessions that touched a file path
sessionhub search X --any "Y Z"     merge another phrasing of the same need
sessionhub search X --since 2026-10-01 --before 2026-11-01
sessionhub search "그거 다시" --expand  a small model adds search queries (one model call)
sessionhub recent -d 3              last 3 days
sessionhub list -p myproject -n 20  filter by project
sessionhub list -m workstation      filter by machine
sessionhub show <id-prefix>         session detail, with a card per episode (asked/did/result/decided/files)
sessionhub about NAME                what is recorded about a repo/service/person, newest first
sessionhub stream NAME               a line of work over time, and where it stood last
sessionhub raw <id-prefix>          the conversation (trimmed digest)
sessionhub raw --full <id-prefix>   the untrimmed log, in place (may ssh)
sessionhub stats                    totals by source / machine / project
sessionhub status                   hub health: last sync, last ingest, errors
sessionhub doctor                   which physical machine each label is; flags duplicates
sessionhub tag <id-prefix> <tag>    tag a session for later recall
```

### How `search` ranks

Every word is optional: sessions with more of the words, and rarer ones, come
first, and a typo or an extra word no longer empties the list. The best match
is on top, not the newest (`--newest` orders by time, `--all-words` keeps only
sessions with every word). Under each result is the line of the conversation
that matched best (`↳ …`). Use the names, paths and error text you saw in this
conversation as search words — they find more than a paraphrase of the request.
`--json` prints machine-readable results.

`--expand` helps when all you have is the person's vague wording ("do that thing
again"): from the bare request it found the right session in the top five 41%
of the time against 23% without. When you already have exact names, paths or
error text, search with those and leave `--expand` off — mixing in guessed
queries pushed precise matches down.

### Filters for `recent` / `list`

- `-d DAYS` time window, `-m MACHINE`, `-p PROJECT`, `-s SUBSYSTEM`, `-n LIMIT`
- Listings print `project/subsystem` as one word (e.g. `turing/mathking`).
  `-p` accepts the project, the subsystem, or the whole `project/subsystem`,
  ignoring case; `-s` is the subsystem alone. A name that exists nowhere prints
  suggestions instead of a bare "no sessions found".
- `-m` takes a machine **label** — a name from the hub's config, not
  necessarily the machine's hostname. `sessionhub doctor` shows which machine
  each label is.
- By default only **interactive** sessions are shown. Subagent and exec
  sessions are hidden because they are numerous and rarely what a person means
  by "the session where…". Add `-a/--all` to include them, or
  `--origin {interactive,subagent,exec}` to select one kind.

## Typical flow

```
sessionhub search "auth refresh token"   # → note the 8-char id
sessionhub show 2c24cdad                 # → episode cards: what was asked, done, decided
sessionhub raw 2c24cdad                  # → only if the detail isn't enough
```

Prefer `show` over `raw`. When the hub has run `enrich`, `show` lists each
episode of the session (one topic each) with its time, what was asked, what was
done, how it ended, what was decided and which files changed — usually enough
to act on without reading the conversation. Search results then also show the
best-matching episode under each session (`↳ episode: …`).

`about NAME` and `stream NAME` are built from the same episodes when you ask,
so every line keeps its session and date; `--before TIME` reads them as of an
earlier moment. Use `about` when a short name in the request ("bf", "the
relay") needs resolving, `stream` to see where a line of work last stood. `raw` prints the trimmed conversation (a digest);
read it only when the specific wording of an exchange matters. `raw --full`
opens the complete original log, fetching it from the origin machine over ssh
if that is where it lives.

## Recall hook (optional)

`sessionhub hook install` prints a Claude Code `UserPromptSubmit` hook (add
`--write` to put it in `~/.claude/settings.json`, with a backup). On the first
request of a session, or one with a recall cue ("last time", "that one",
"저번에", "그거"), it searches past episodes and adds up to four dated cards in
front of the request. The cards are leads, not facts: they say what was true
on their date. `[recall]` in config sets `mode = cue | always | off`; the hook
logs each decision to `recall-hook.log` and never blocks a request.

## Remote hubs

The archive often lives on an always-on machine rather than the laptop. If
`[remote_query]` is configured, the read-only commands above transparently run
there over SSH — no flags needed. Otherwise:

```
sessionhub --remote HOST search "..."   # one-off against another machine
sessionhub --local recent -d 1          # force the local DB
shm search "..."                        # shim, if `remote install` was run
```

Only queries are forwarded. `sync`, `ingest`, `run`, `service`, and
`uninstall` always act on the local machine, so they cannot disturb a remote
hub by accident.

## Do not reconfigure the hub from an agent session

`add-host`, `rm-host`, `remote set/unset`, `service`, `init`, `sync`, `ingest`,
`run`, `enrich` and `uninstall` change configuration, schedulers or archive contents on
the machine they run on. If something cannot be found, **report what you
searched and stop** — do not add hosts, edit `config.toml`, or touch ssh keys to
"fix" it. An empty result almost never means a machine is not collected (see
below). Ask the user before changing any setup.

## Troubleshooting

**A session from minutes ago is missing.** Ingestion runs on a timer (default
15 min). Force it: `sessionhub run` — or on a remote hub,
`sessionhub --local run` from that machine. Note the currently-running session
is not written to disk in full until it ends.

**`ssh HOST sessionhub` fails but interactive SSH works.** A non-interactive
SSH shell gets a minimal PATH that usually omits `~/.local/bin`. Configure the
absolute remote path once: `sessionhub remote set HOST --bin /abs/path/sessionhub`.
Check with `sessionhub remote status`.

**Search returns nothing.** The message lists what was searched. Try other
words — names, file paths, error text; punctuation such as `mathking-cs` is
searched as text. Korean words match without their particles (`고객센터를`
finds `고객센터에서`); installing the optional `sessionhub[ko]` extra uses a
morphological analyser for better stems. If the message says the conversation
index is behind, the hub has not run since the upgrade — `sessionhub run` on
the hub builds it once (about 20 s per 9,000 sessions). A word that appeared
only in a tool call or tool output is not in the digest at all.

**`--local` shows 0 sessions.** A machine set up as a client keeps no archive
of its own; its queries are forwarded to the hub named in `[remote_query]`.
Its empty local database is normal. Drop `--local`.

**A machine's sessions seem to be missing.** First run `sessionhub doctor`:
it lists each label with the hostname and machine id behind it, how many
sessions each has, and the last sync. A label is only a name in the config —
the laptop you are typing on may be collected under a different name (for
example `macpro`) than its hostname. `sessionhub status` shows the last sync per
host. A machine only appears once it has been added with `sessionhub add-host`
on the hub and synced at least once.

**The same machine appears under two labels.** `doctor` reports it. `add-host`
refuses to add a machine whose id is already collected (override with `--force`
only if that is truly intended).
