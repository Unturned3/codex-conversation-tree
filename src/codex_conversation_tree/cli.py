"""Read-only local Codex session browser. Run with codex-tree."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys

# Bare Escape needs a short timeout to distinguish it from terminal sequences.
# Honor an explicit terminal/user setting, especially on slower remote links.
os.environ.setdefault("ESCDELAY", "25")

from rich.text import Text
from rapidfuzz.distance import DamerauLevenshtein
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual import events
from textual.theme import BUILTIN_THEMES
from textual.widgets import Footer, Header, Static, Tree
from textual.widgets.tree import TreeNode


def clean(value: object) -> str:
    """Session labels are literal text, never terminal controls or markup."""
    return " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(value or "")).split())


def timestamp(value: object) -> float:
    try:
        if isinstance(value, (int, float)):
            return float(value)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0.0


def age(value: float) -> str:
    if not value:
        return "unknown date"
    seconds = max(0, datetime.now(timezone.utc).timestamp() - value)
    for limit, divisor, suffix in ((60, 1, "s"), (3600, 60, "m"), (86400, 3600, "h"), (604800, 86400, "d")):
        if seconds < limit:
            return f"{int(seconds / divisor)}{suffix} ago"
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d")


@dataclass
class Session:
    id: str
    parent_id: str | None = None
    title: str = ""
    cwd: str = ""
    updated: float = 0
    path: Path | None = None
    archived: bool = False
    subagent: bool = False
    search_text: str = field(init=False, repr=False)
    search_words: tuple[str, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.search_text = f"{self.title} {self.cwd} {self.id}".casefold()
        # Cache normalized words once per load. IDs deliberately stay literal.
        self.search_words = tuple(dict.fromkeys(re.findall(r"[^\W_]+", f"{self.title} {self.cwd}".casefold())))

    @property
    def label(self) -> str:
        return clean(self.title) or f"Untitled · {self.id[:8]}"


@dataclass
class Catalog:
    sessions: dict[str, Session] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def read_database(home: Path, warnings: list[str]) -> dict[str, dict]:
    """Optional enrichment only; ancestry always comes from session_meta."""
    databases = sorted(home.glob("state_*.sqlite"), key=lambda p: int(p.stem.split("_")[-1]) if p.stem.split("_")[-1].isdigit() else -1, reverse=True)
    for path in databases:
        try:
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as conn:
                conn.row_factory = sqlite3.Row
                columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
                wanted = [c for c in ("id", "name", "title", "first_user_message", "updated_at", "archived") if c in columns]
                if "id" not in wanted:
                    continue
                return {row["id"]: dict(row) for row in conn.execute("SELECT " + ",".join(wanted) + " FROM threads")}
        except (OSError, sqlite3.Error):
            warnings.append(f"Could not read {path.name}; using session files and index.")
    return {}


def load_catalog(home: Path) -> Catalog:
    catalog = Catalog()
    index: dict[str, dict] = {}
    try:
        with (home / "session_index.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                    if isinstance(item, dict) and isinstance(item.get("id"), str):
                        previous = index.get(item["id"], {})
                        if timestamp(item.get("updated_at")) >= timestamp(previous.get("updated_at")):
                            index[item["id"]] = item
                except ValueError:
                    catalog.warnings.append("Skipped an invalid session index entry.")
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError):
        catalog.warnings.append("Could not read the session index.")
    database = read_database(home, catalog.warnings)
    # Active copies win if an archive operation left a duplicate behind.
    for directory, archived in (("archived_sessions", True), ("sessions", False)):
        for path in sorted((home / directory).rglob("*.jsonl")):
            try:
                with path.open(encoding="utf-8") as stream:
                    row = json.loads(stream.readline(4 * 1024 * 1024))
                if not isinstance(row, dict) or row.get("type") != "session_meta":
                    raise ValueError("missing session_meta")
                meta = row.get("payload")
                if not isinstance(meta, dict) or not isinstance(meta.get("id"), str) or not meta["id"]:
                    raise ValueError("missing session id")
                sid = meta["id"]
                idx, db = index.get(sid, {}), database.get(sid, {})
                parent = meta.get("forked_from_id")
                source = meta.get("source")
                catalog.sessions[sid] = Session(
                    id=sid,
                    parent_id=parent if isinstance(parent, str) and parent else None,
                    title=clean(idx.get("thread_name") or db.get("name") or db.get("title") or db.get("first_user_message")),
                    cwd=meta.get("cwd") if isinstance(meta.get("cwd"), str) else "",
                    updated=max(timestamp(idx.get("updated_at")), timestamp(db.get("updated_at"))) or path.stat().st_mtime or timestamp(meta.get("timestamp")),
                    path=path,
                    archived=archived or bool(db.get("archived")),
                    subagent=isinstance(source, dict) and "subagent" in source,
                )
            except (OSError, ValueError, UnicodeError):
                catalog.warnings.append(f"Skipped unreadable session: {path.name}")
    # Keep disconnected children visible without guessing their ancestry.
    for session in list(catalog.sessions.values()):
        if session.parent_id and session.parent_id not in catalog.sessions:
            catalog.sessions[session.parent_id] = Session(session.parent_id, title="Missing parent · " + session.parent_id[:8])
    # Corrupt metadata must not hang either rendering or ancestor filtering.
    done: set[str] = set()
    for sid in catalog.sessions:
        chain: set[str] = set()
        current: str | None = sid
        while current and current not in done:
            chain.add(current)
            session = catalog.sessions[current]
            if session.parent_id in chain:
                catalog.warnings.append(f"Broken ancestry cycle at {current[:8]}.")
                session.parent_id = None
                break
            current = session.parent_id
        done.update(chain)
    return catalog


def exact_matches(session: Session, terms: list[str]) -> bool:
    """Original search: every query term must be a literal substring."""
    return all(term in session.search_text for term in terms)


@lru_cache(maxsize=32768)
def fuzzy_word_matches(term: str, word: str) -> bool:
    budget = 0 if len(term) < 3 else 1 if len(term) < 8 else 2
    if not budget or not term.isalnum():
        return False
    # Compare only plausible prefix lengths; the untyped ending isn't a typo.
    # Damerau-Levenshtein counts an adjacent-letter swap as one error.
    for length in range(max(1, len(term) - budget), min(len(word), len(term) + budget) + 1):
        if DamerauLevenshtein.distance(term, word[:length], score_cutoff=budget) <= budget:
            return True
    return False


def fuzzy_matches(session: Session, terms: list[str]) -> bool:
    return all(
        term in session.search_text
        or any(fuzzy_word_matches(term, word) for word in session.search_words)
        for term in terms
    )


matches_session = fuzzy_matches
# Original exact-substring behavior, retained as an inactive alternative.
# To restore it, replace the assignment above with:
# matches_session = exact_matches


def visible_sessions(catalog: Catalog, query: str = "", include_archived: bool = False, include_subagents: bool = False) -> tuple[set[str], set[str]]:
    terms = query.casefold().split()
    matches = {
        s.id for s in catalog.sessions.values()
        if s.path is not None
        and (include_archived or not s.archived)
        and (include_subagents or not s.subagent)
        and matches_session(s, terms)
    }
    visible = set(matches)
    for sid in matches:
        parent = catalog.sessions[sid].parent_id
        while parent and parent not in visible:
            visible.add(parent)
            parent = catalog.sessions[parent].parent_id
    return visible, matches


def resume_command(session: Session, executable: str = "codex") -> list[str]:
    if session.path is None:
        raise ValueError("This parent session is missing locally.")
    if session.archived:
        raise ValueError("This session is archived. Unarchive it in Codex before resuming.")
    if not session.cwd or not Path(session.cwd).is_dir():
        raise ValueError("The saved working directory is unavailable: " + clean(session.cwd))
    return [executable, "resume", "--cd", session.cwd, session.id]


class SessionTree(Tree[Session], inherit_bindings=False):
    # The tree keeps focus even while the user edits the search query.
    # Printable keys (including space and hjkl) always belong to search.
    BINDINGS = []

    def action_branch_left(self) -> None:
        node = self.cursor_node
        if node and node.is_expanded and node.children:
            node.collapse()
        elif node and node.parent and node.parent is not self.root:
            self.move_cursor(node.parent)

    def action_branch_right(self) -> None:
        node = self.cursor_node
        if node and node.children:
            if not node.is_expanded:
                node.expand()
            else:
                self.move_cursor(node.children[0])


class Picker(App[Session | None]):
    TITLE = "Codex conversations"
    AUTO_FOCUS = "#tree"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { background: $surface; }
    #search { margin: 0 1; padding: 0 1; height: 3; border: round $primary-muted; overflow: hidden; }
    #status {
        height: auto; max-height: 3; margin: 0 1; padding: 0 1;
        color: $text; background: $panel; text-style: bold;
        border-bottom: solid $primary-muted;
    }
    #tree { height: 1fr; margin: 0 1; padding: 0 1; }
    #details { height: 6; padding: 0 2; border-top: solid $primary-muted; overflow-y: auto; }
    """
    BINDINGS = [
        Binding("up", "navigate('cursor_up')", "Up", show=False, priority=True),
        Binding("down", "navigate('cursor_down')", "Down", show=False, priority=True),
        Binding("left", "navigate('branch_left')", "Collapse", show=False, priority=True),
        Binding("right", "navigate('branch_right')", "Expand", show=False, priority=True),
        Binding("pageup", "navigate('page_up')", "Page up", show=False, priority=True),
        Binding("pagedown", "navigate('page_down')", "Page down", show=False, priority=True),
        Binding("enter", "resume", "Open", key_display="Enter", priority=True),
        Binding("escape", "escape", "Back / quit", priority=True),
        Binding("ctrl+l", "clear_search", "Clear search", show=False, priority=True),
        Binding("backspace,delete", "search_backspace", "Delete character", show=False, priority=True),
        Binding("ctrl+r", "reload", "Refresh", priority=True),
        Binding("ctrl+a", "archives", "Archived", priority=True),
        Binding("ctrl+c", "quit", "Quit", priority=True),
    ]

    def __init__(self, home: Path, *, include_archived: bool = False, include_subagents: bool = False, print_id: bool = False, theme: str = "ansi-light", catalog: Catalog | None = None):
        super().__init__()
        self.theme = theme
        self.home = home
        self.include_archived = include_archived
        self.include_subagents = include_subagents
        self.print_id = print_id
        self.catalog = catalog or Catalog()
        self.preloaded = catalog is not None
        self.nodes: dict[str, TreeNode[Session]] = {}
        self.matches: set[str] = set()
        self.expanded: set[str] | None = None
        self.filtering = False
        self.search_query = ""

    def get_driver_class(self):
        driver = super().get_driver_class()
        if os.name == "posix":
            from .terminal_driver import LinuxDriver, ResponsiveLinuxDriver

            if driver is LinuxDriver:
                return ResponsiveLinuxDriver
        return driver

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Type to search · arrow keys navigate · Enter opens", id="search", markup=False)
        yield Static("Loading conversations…", id="status", markup=False)
        yield SessionTree("Conversations", id="tree")
        yield Static("", id="details", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        tree = self.query_one(SessionTree)
        tree.show_root = False
        tree.auto_expand = False
        tree.guide_depth = 3
        tree.focus()
        if self.preloaded:
            self.rebuild()
        else:
            self.action_reload()

    def action_reload(self) -> None:
        self.run_worker(self.reload_catalog(), group="catalog", exclusive=True)

    async def reload_catalog(self) -> None:
        self.query_one("#status", Static).update("Loading conversations…")
        self.catalog = await asyncio.to_thread(load_catalog, self.home)
        self.rebuild()
        if self.catalog.warnings:
            self.notify("\n".join(self.catalog.warnings[:4]), title="Session loading", severity="warning", timeout=8)

    def rebuild(self) -> None:
        tree = self.query_one(SessionTree)
        selected = tree.cursor_node.data.id if tree.cursor_node and tree.cursor_node.data else None
        query = self.search_query.strip()
        starting_search = bool(query) and not self.filtering
        if not self.filtering and self.nodes:
            self.expanded = {sid for sid, node in self.nodes.items() if node.is_expanded}
        self.filtering = bool(query)
        visible, self.matches = visible_sessions(self.catalog, query, self.include_archived, self.include_subagents)
        children: dict[str | None, list[Session]] = {}
        for sid in visible:
            session = self.catalog.sessions[sid]
            children.setdefault(session.parent_id, []).append(session)
        for siblings in children.values():
            siblings.sort(key=lambda s: (-s.updated, s.id))
        tree.move_cursor(None)
        tree.clear()
        self.nodes = {}
        stack = [(tree.root, s) for s in reversed(children.get(None, []))]
        while stack:
            parent, session = stack.pop()
            label = Text(session.label, style="bold" if query and session.id in self.matches else "")
            if session.path is None:
                label.stylize("dim italic")
            else:
                label.append("  " + age(session.updated), style="dim")
            if session.archived:
                label.append("  [archived]", style="yellow")
            if session.subagent:
                label.append("  [subagent]", style="dim")
            if session.id not in self.matches:
                label.stylize("dim")
            descendants = children.get(session.id, [])
            node = parent.add(label, session, expand=bool(query) or self.expanded is None or session.id in self.expanded, allow_expand=bool(descendants))
            self.nodes[session.id] = node
            stack.extend((node, child) for child in reversed(descendants))
        tree.root.expand()
        exact = {
            sid for sid in self.matches
            if exact_matches(self.catalog.sessions[sid], query.casefold().split())
        }
        target = self.nodes.get(selected)
        if target is None or (query and selected not in self.matches) or (starting_search and exact and selected not in exact):
            preferred = exact or self.matches
            target = next((node for sid, node in self.nodes.items() if sid in preferred), None)
        if target:
            ancestor = target.parent
            while ancestor:
                ancestor.expand()
                ancestor = ancestor.parent
            # Resolve visible lines now so the next key can navigate immediately,
            # even when typing and arrows arrive in the same terminal read.
            tree.get_node_at_line(0)
            tree.move_cursor(target)
            self.show_details(target.data)
        else:
            self.query_one("#details", Static).update("No matching conversations. Clear the search, show archived sessions, or check --codex-home.")
        suffix = " · archived shown" if self.include_archived else ""
        issues = f" · {len(self.catalog.warnings)} loading warnings" if self.catalog.warnings else ""
        self.query_one("#status", Static).update(f"{len(self.matches)} conversations{suffix}{issues}")

    def show_details(self, session: Session) -> None:
        state = "Missing locally" if session.path is None else "Archived" if session.archived else ""
        suffix = f"   ·   {state}" if state else ""
        self.query_one("#details", Static).update(
            f"{session.label}\nDirectory: {clean(session.cwd) or '—'}\nSession: {clean(session.id)}\nParent: {clean(session.parent_id) or '—'}{suffix}"
        )

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        if event.node.data:
            self.show_details(event.node.data)

    def action_resume(self) -> None:
        node = self.query_one(SessionTree).cursor_node
        session = node.data if node else None
        if session is None:
            return
        try:
            if self.print_id and session.path is not None:
                self.exit(session)
                return
            resume_command(session)
        except ValueError as error:
            self.notify(str(error), severity="warning")
            return
        self.exit(session)

    async def on_event(self, event: events.Event) -> None:
        # Handle text at the same dispatch stage as priority key bindings.
        # Bubbling printable keys through widgets can otherwise reorder them
        # after a following Backspace or arrow key from the same input batch.
        if isinstance(event, events.Key) and not event.is_forwarded and event.is_printable:
            self.set_search(self.search_query + (event.character or ""))
            return
        if isinstance(event, events.Paste) and not event.is_forwarded:
            self.set_search(self.search_query + clean(event.text))
            return
        await super().on_event(event)

    def set_search(self, query: str) -> None:
        self.query_one(SessionTree).focus()
        if query == self.search_query:
            return
        self.search_query = query
        banner = Text("Search: ", style="bold")
        banner.append(query, style="not bold")
        self.query_one("#search", Static).update(
            banner if query else "Type to search · arrow keys navigate · Enter opens"
        )
        self.rebuild()

    def action_search_backspace(self) -> None:
        self.set_search(self.search_query[:-1])

    async def action_navigate(self, action: str) -> None:
        tree = self.query_one(SessionTree)
        tree.get_node_at_line(0)
        await tree.run_action(action)

    def action_escape(self) -> None:
        if self.search_query:
            self.action_clear_search()
        else:
            self.exit()

    def action_clear_search(self) -> None:
        self.set_search("")

    def action_archives(self) -> None:
        self.include_archived = not self.include_archived
        self.rebuild()


def main() -> int:
    parser = argparse.ArgumentParser(prog="codex-tree", description=__doc__)
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser(), help="Codex data directory (default: $CODEX_HOME or ~/.codex)")
    parser.add_argument("--include-archived", action="store_true", help="Show archived sessions (read-only)")
    parser.add_argument("--include-subagents", action="store_true", help="Also show spawned subagent sessions")
    parser.add_argument("--print-id", action="store_true", help="Print the selected session ID instead of launching Codex")
    parser.add_argument("--theme", default=os.environ.get("CODEX_TREE_THEME", "ansi-light"), metavar="NAME", help="Textual theme (default: $CODEX_TREE_THEME or ansi-light)")
    parser.add_argument("--list-themes", action="store_true", help="List available themes and exit")
    args = parser.parse_args()
    if args.list_themes:
        for name, theme in sorted(BUILTIN_THEMES.items()):
            print(f"{name:24} {'dark' if theme.dark else 'light'}")
        return 0
    if args.theme not in BUILTIN_THEMES:
        parser.error(f"Unknown theme: {args.theme!r}. Use --list-themes to see available themes.")
    home = args.codex_home.expanduser().resolve()
    if not home.is_dir():
        parser.error(f"Codex directory does not exist: {home}")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("Run the picker in an interactive terminal.")
    executable = shutil.which("codex")
    if not args.print_id and executable is None:
        parser.error("codex was not found on PATH")
    session = Picker(home, include_archived=args.include_archived, include_subagents=args.include_subagents, print_id=args.print_id, theme=args.theme).run()
    if session is None:
        return 0
    if args.print_id:
        print(session.id)
        return 0
    try:
        command = resume_command(session, executable or "codex")
        environment = dict(os.environ, CODEX_HOME=str(home))
        # Textual has restored the terminal before we replace the picker process.
        os.execvpe(command[0], command, environment)
    except (OSError, ValueError) as error:
        print(f"Could not resume session: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
