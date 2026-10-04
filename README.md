# Codex conversation tree

Browse local Codex conversations as a tree of forks, then press Enter to resume one.

Requires [uv](https://docs.astral.sh/uv/) and `codex` on your PATH. From this directory:

```sh
./codex-tree
```

The launcher uses `uv.lock` and installs dependencies into the local `.venv` on first run.
It also works when invoked by its full path from another directory.

| Key | Action |
| --- | --- |
| ↑ / ↓ or k / j | Move between visible conversations |
| ← / → or h / l | Collapse / expand; move to parent / first child |
| Space | Toggle the selected branch |
| Enter | Resume the selected conversation, including parents |
| / | Search titles, directories, and session IDs |
| Enter in search | Return focus to the tree |
| Ctrl+L | Clear search and return to the tree |
| a | Toggle archived conversations |
| r | Refresh from disk |
| Esc in search | Clear search and return to the tree |
| Esc in tree, q, or Ctrl+C | Quit |

Search is case-insensitive and matches all space-separated terms. Matching conversations retain
their ancestors in the tree. Missing parents are dimmed placeholders. Roots and siblings are
sorted by their own most recent activity. Mouse selection highlights an item; Enter launches it.

```sh
./codex-tree --codex-home /path/to/.codex
./codex-tree --include-archived
./codex-tree --include-subagents
./codex-tree --print-id
```

The default theme is `ansi-light`. Choose another theme per launch, or set
`CODEX_TREE_THEME` in your shell configuration to keep your preference:

```sh
./codex-tree --theme catppuccin-latte
./codex-tree --theme solarized-light
./codex-tree --theme textual-dark
./codex-tree --list-themes
export CODEX_TREE_THEME=catppuccin-latte
```

`--theme` takes precedence over the environment variable. For finer styling, edit
`Picker.CSS` in `codex_tree.py`; its colors use Textual theme variables such as
`$surface`, `$text-muted`, and `$primary-muted`.

On POSIX terminals, a small Textual driver subclass reduces input polling and
shutdown waits while retaining Textual's terminal cleanup. Bare Escape uses a
25 ms sequence timeout by default; an existing `ESCDELAY` setting is respected.
For a slow remote terminal you can increase it, e.g. `ESCDELAY=100 ./codex-tree`.

By default, data comes from `$CODEX_HOME`, or `~/.codex`. The picker reads the first
`session_meta` record of each JSONL file under `sessions/` and `archived_sessions/`.
`payload.id` identifies a conversation; `payload.forked_from_id` identifies its immediate
parent. It builds the hierarchy from those edges, not `session_id` or subagent
`parent_thread_id`. Titles come from the newest entry in `session_index.jsonl`, falling back
to the `threads` table in the newest readable `state_*.sqlite` database. SQLite is opened
read-only. Full conversation transcripts are not loaded.

These are Codex's internal storage formats, verified locally against CLI 0.160.0. Malformed
files are skipped with a warning; ancestry cycles are broken so the picker remains usable.
Spawned subagents are hidden by default. Archived or hidden sessions can still appear as
ancestors of visible conversations. Archived sessions must be unarchived in Codex before
resuming. Only sessions stored on this machine are available.

After the TUI closes, the launcher replaces itself with
`codex resume --cd SAVED_DIRECTORY SESSION_ID`, preserving the selected Codex home and
normal Codex permission settings. If the saved directory is unavailable, the picker stays
open and explains why. `--print-id` selects an ID without requiring that directory or launching
Codex. The picker does not edit, delete, archive, or fork conversations.

Run the loader and headless keyboard tests with:

```sh
uv run --locked python -m unittest -v
```
