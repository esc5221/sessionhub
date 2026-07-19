---
name: sessionhub
description: Search and read past AI coding sessions (Claude Code + Codex) collected by sessionhub across every machine you work on. Use when recalling what was done before — "how did I fix that last time", "what did I work on in project X last week", "find the session where we discussed Y", or when the raw transcript of an earlier session is needed. Triggers on 'sessionhub', 'shm', 'past session', 'previous session', 'earlier we', 'last time I'.
---

# sessionhub

Queries a local SQLite archive of your Claude Code and Codex sessions:
titles, projects, timing, changed files, and full-text search over transcripts.

Start with `search` when you know roughly what was said, `recent`/`list` when
you know roughly when it happened. Session IDs may be abbreviated to their
first 8 characters everywhere an id is accepted — that prefix is the first
column of every result listing.

## Commands

```
sessionhub search "split brain"     full-text search (multi-word is fine)
sessionhub search --tag NAME        by tag
sessionhub search --file PATH       sessions that touched a file path
sessionhub recent -d 3              last 3 days
sessionhub list -p myproject -n 20  filter by project
sessionhub list -m workstation      filter by machine
sessionhub show <id-prefix>         session detail (summary, files, tags)
sessionhub raw <id-prefix>          the conversation (trimmed digest)
sessionhub raw --full <id-prefix>   the untrimmed log, in place (may ssh)
sessionhub stats                    totals by source / machine / project
sessionhub status                   hub health: last sync, last ingest, errors
sessionhub tag <id-prefix> <tag>    tag a session for later recall
```

### Filters for `recent` / `list`

- `-d DAYS` time window, `-m MACHINE`, `-p PROJECT`, `-s SUBSYSTEM`, `-n LIMIT`
- By default only **interactive** sessions are shown. Subagent and exec
  sessions are hidden because they are numerous and rarely what a person means
  by "the session where…". Add `-a/--all` to include them, or
  `--origin {interactive,subagent,exec}` to select one kind.

## Typical flow

```
sessionhub search "auth refresh token"   # → note the 8-char id
sessionhub show 2c24cdad                 # → summary, files touched
sessionhub raw 2c24cdad                  # → only if the detail isn't enough
```

Prefer `show` over `raw`. `raw` prints the trimmed conversation (a digest);
read it only when the specific wording of an exchange matters. `raw --full`
opens the complete original log, fetching it from the origin machine over ssh
if that is where it lives.

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

## Troubleshooting

**A session from minutes ago is missing.** Ingestion runs on a timer (default
15 min). Force it: `sessionhub run` — or on a remote hub,
`sessionhub --local run` from that machine. Note the currently-running session
is not written to disk in full until it ends.

**`ssh HOST sessionhub` fails but interactive SSH works.** A non-interactive
SSH shell gets a minimal PATH that usually omits `~/.local/bin`. Configure the
absolute remote path once: `sessionhub remote set HOST --bin /abs/path/sessionhub`.
Check with `sessionhub remote status`.

**Search returns nothing for a phrase with punctuation.** Queries containing
non-word characters are auto-quoted as an FTS phrase; try fewer, plainer words.

**A machine's sessions are absent entirely.** `sessionhub status` lists the
configured hosts and their last successful sync. A machine only appears once
it has been added with `sessionhub add-host` on the hub and synced at least
once.
