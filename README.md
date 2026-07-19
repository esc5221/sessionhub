# sessionhub

Search everything you've ever done with your coding agents.

![sessionhub searching months of sessions for 'connection pool', then opening one](docs/hero-dark.png)

## Install

```bash
uv tool install git+https://github.com/esc5221/sessionhub
```

<sub>No uv? `curl -LsSf https://astral.sh/uv/install.sh | sh`. Python 3.11+.</sub>

## Set up

```bash
sessionhub setup
```

It finds your sessions, asks what this machine is for, and does the rest.
When it finishes, you can search.

## Use

```bash
sessionhub recent -d 3                 # last 3 days
sessionhub search "split brain"        # full text, across everything
sessionhub search --file src/auth.py   # sessions that touched a file
sessionhub list -p acme-api            # one project
sessionhub show 2c24cdad               # detail — 8 characters of the id is enough
sessionhub raw 2c24cdad                # the original transcript
```

Subagent and exec sessions are hidden by default. `-a` includes them.

## More than one machine

Sessions live on whichever machine you were typing on, so pick **one** to keep
the archive — a desktop that's usually on is ideal. `sessionhub setup` asks
which role a machine has:

```
keeps the archive      reads its own sessions on a timer, and can pull in
                       other machines' sessions over ssh

queries the archive    stays empty; sends every search to the machine that
                       has one
```

On a laptop, choose *queries the archive* and give it the other machine's ssh
alias. Searches then run there and you get the results — nothing is copied.

```bash
sessionhub search "..."       # answered by the archive machine
sessionhub --local recent     # this machine's own data instead
```

Both roles are set up the same way: install, `sessionhub setup`, answer.

## Claude Code

A skill ships with sessionhub, so you can ask instead of type:

> *find the session where we fixed the pgbouncer timeout*

`sessionhub setup` installs it for you. If you'd rather install it the way you
install any other skill, pick whichever fits:

```bash
# as a plugin, managed by Claude Code
/plugin marketplace add esc5221/sessionhub
/plugin install sessionhub@sessionhub

# or by hand, for yourself
sessionhub skill install                     # → ~/.claude/skills/sessionhub/

# or for one project, checked into its repo
cp -r plugins/sessionhub/skills/sessionhub .claude/skills/
```

Claude Code picks up a new skill straight away — no restart.

## Privacy

`hub.db` holds your sessions verbatim — code, keys, whatever was on screen.
Treat it like the repositories it came from. It never leaves your machines:
there is no telemetry and no service to phone home to. The included
`.gitignore` keeps the database and its raw files out of git.

## Details

<details>
<summary>Which project a session belongs to</summary>

Guessed from the directory you were working in. Container directories
(`code`, `work`, `repos`, `src`, …) are skipped in favour of the one that
names the project:

```
~/code/acme-api/services   →   acme-api / services
```

Keep repos somewhere unusual, or want a different answer? Add to
`~/.config/sessionhub/config.toml`:

```toml
[classification]
generic_components = ["sandbox"]     # more container dirs to skip
rules = [
  { pattern = "%/acme-monorepo%web%", project = "acme", subsystem = "web", priority = 10 },
  { pattern = "%/acme-monorepo%",     project = "acme" },
]
```

Highest priority wins. Rules you delete here are deleted from the database on
the next refresh.
</details>

<details>
<summary>Keeping it fresh</summary>

Setup installs a timer (launchd or systemd) that refreshes every 15 minutes.
`sessionhub run` refreshes now; `sessionhub status` shows the last refresh and
anything that failed to parse. A session in progress isn't fully on disk yet,
so the most recent minutes of work may be missing.
</details>

<details>
<summary>All the commands</summary>

```
setup                 guided first-run setup
recent list search    find sessions
show raw stats        read them
status                last refresh, errors
run                   refresh now
add-host <alias>      copy another machine's sessions INTO this archive
remote set <host>     send this machine's queries OUT to an archive elsewhere
remote install        write a short `shm` alias for the above
skill install         (re)install the Claude Code skill
service               install/uninstall the refresh timer
init                  create config non-interactively (setup does this for you)
uninstall [--purge]   remove the timer, optionally config and data
```
</details>

<details>
<summary>Config file</summary>

`~/.config/sessionhub/config.toml` — written by setup, edit if you like.
`$SESSIONHUB_CONFIG_DIR` and `$XDG_CONFIG_HOME` move it.

```toml
[data]
db_path = "~/.local/share/sessionhub/hub.db"
raw_dir = "~/.local/share/sessionhub/raw"

[schedule]
interval_minutes = 15

[sources.local]
label = "laptop"                       # name shown in results; default: hostname
claude = "~/.claude/projects"
codex_sessions = "~/.codex/sessions"
codex_state = "~/.codex/state_5.sqlite"

[[sources.remote]]                     # machines this archive pulls from
name = "workstation"
host = "workstation"                   # ssh alias
label = "workstation"

[remote_query]                         # the archive this machine queries
host = "workstation"
bin = "/abs/path/to/sessionhub"        # setup fills this in
```

`bin` exists because a non-interactive ssh shell has a minimal PATH that often
omits `~/.local/bin`. Setup detects the absolute path so you don't have to.
</details>

<details>
<summary>Update and uninstall</summary>

```bash
uv tool upgrade sessionhub       # pulls the latest commit

sessionhub uninstall --purge     # timer, config and database
uv tool uninstall sessionhub
```
</details>

![sessionhub in use](docs/demo.gif)

## Agents

Reading an agent's history means parsing whatever it writes to disk, so support
is per-agent:

```
Claude Code    ~/.claude/projects/*.jsonl
Codex          ~/.codex/sessions/**.jsonl  (+ the state sqlite, when present)
```

Want another one — Cursor, Aider, Gemini CLI, something in-house? Open an issue
with a sample transcript, or send a pull request. An adapter is one file in
`src/sessionhub/adapters/`: iterate the files, return a `ParsedSession` per
session. Nothing else in the pipeline needs to know the format.

## Development

```bash
uv run --extra dev pytest -q
uv run --extra dev ruff check .
```

The screenshots come from a generated demo archive, so they can be rebuilt and
contain nobody's real transcripts:

```bash
brew install charmbracelet/tap/freeze vhs
./scripts/capture.sh          # docs/*.png
vhs scripts/demo.tape         # docs/demo.gif
```

The skill lives in `src/sessionhub/skill_files/SKILL.md` and is mirrored into
`plugins/` for the Claude Code marketplace. After editing it, run
`scripts/sync_plugin_skill.py` — a test fails if the copies drift.

MIT
