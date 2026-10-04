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
| ↑ / ↓ | Move between visible conversations, including while searching |
| ← / → | Collapse / expand; move to parent / first child, including while searching |
| Page Up / Page Down | Navigate a page at a time |
| Enter | Open the selected conversation, including while searching |
| Any printable character | Start or continue searching titles, directories, and IDs |
| Backspace / Delete | Remove the last query character (including the Mac Delete key) |
| Ctrl+L | Clear search and return to the tree |
| Ctrl+A | Toggle archived conversations |
| Ctrl+R | Refresh from disk |
| Esc in search | Clear search and return to the tree |
| Esc in tree with no search, or Ctrl+C anywhere | Quit |

Search is case-insensitive and matches all space-separated terms. Exact substrings always
match. Otherwise, RapidFuzz checks title and directory words (including prefixes) with a
small typo budget: no errors for 1–2 characters, one for 3–7, and two for 8 or more.
An inserted, missing, or replaced character counts as one error, as does swapping adjacent
letters. For example, `refersh tok` matches “Refresh authentication tokens”. Session IDs
and query terms containing punctuation keep literal substring matching.

Matching conversations retain
their ancestors in the tree. Missing parents are dimmed placeholders. Roots and siblings are
sorted by their own most recent activity; fuzzy search does not reorder branches. An exact
match is preferred when initially selecting a result, and an existing matching selection
is preserved as you continue typing. Mouse selection highlights an item; Enter launches it.
Letters such as `q`, `r`, `a`, and `hjkl`, as well as `/` and spaces, are search text.
The search banner is a plain display; keyboard focus always stays on the tree.
Typing appends to the query and Backspace/Delete removes its last character.

The original exact matcher remains in `codex_tree.py` as `exact_matches`, with a commented
alternative assignment next to `matches_session` for easily restoring the old behavior.

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
