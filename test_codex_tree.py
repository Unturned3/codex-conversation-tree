import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from textual import events
from textual.widgets import Input

from codex_tree import Catalog, Picker, Session, SessionTree, load_catalog, resume_command, visible_sessions


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def write_session(self, sid, parent=None, archived=False, **extra):
        directory = self.home / ("archived_sessions" if archived else "sessions") / "2026/10/02"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"rollout-{sid}.jsonl"
        meta = dict(id=sid, forked_from_id=parent, cwd=str(self.home), timestamp="2026-10-02T10:00:00Z", **extra)
        path.write_text(json.dumps(dict(type="session_meta", payload=meta)) + "\n" + "not parsed transcript\n")
        return path

    def test_realistic_forks_index_database_and_subagents(self):
        self.write_session("root")
        self.write_session("child", "root")
        self.write_session("deep", "child")
        self.write_session("agent", source={"subagent": {"other": "guardian"}}, parent_thread_id="root")
        self.write_session("archived", "root", archived=True)
        (self.home / "session_index.jsonl").write_text("\n".join(json.dumps(row) for row in [
            dict(id="deep", thread_name="Refresh failure", updated_at="2026-10-02T12:00:00Z"),
            dict(id="deep", thread_name="Stale title", updated_at="2026-10-02T11:00:00Z"),
        ]) + "\n{partial")
        with sqlite3.connect(self.home / "state_5.sqlite") as conn:
            conn.execute("CREATE TABLE threads (id TEXT, title TEXT)")
            conn.execute("INSERT INTO threads VALUES ('root', 'Authentication')")
        c = load_catalog(self.home)
        self.assertEqual(c.sessions["root"].title, "Authentication")
        self.assertEqual(c.sessions["deep"].title, "Refresh failure")
        self.assertIsNone(c.sessions["agent"].parent_id)
        self.assertEqual(visible_sessions(c)[1], {"root", "child", "deep"})
        self.assertEqual(visible_sessions(c, "refresh"), ({"root", "child", "deep"}, {"deep"}))
        self.assertIn("agent", visible_sessions(c, include_subagents=True)[1])
        self.assertIn("archived", visible_sessions(c, include_archived=True)[1])
        self.assertEqual(len(c.warnings), 1)

    def test_missing_and_archived_ancestors_remain_visible(self):
        self.write_session("child", "missing")
        self.write_session("archived-root", archived=True)
        self.write_session("active-child", "archived-root")
        c = load_catalog(self.home)
        self.assertIsNone(c.sessions["missing"].path)
        visible, matches = visible_sessions(c)
        self.assertEqual(visible, {"missing", "child", "archived-root", "active-child"})
        self.assertEqual(matches, {"child", "active-child"})

    def test_corruption_and_cycles_do_not_hide_or_hang_sessions(self):
        self.write_session("a", "b")
        self.write_session("b", "a")
        self.write_session("self", "self")
        path = self.write_session("bad")
        path.write_text('{"partial":')
        c = load_catalog(self.home)
        self.assertEqual(len(c.warnings), 3)
        self.assertEqual(visible_sessions(c)[1], {"a", "b", "self"})
        for sid in c.sessions:
            seen = set()
            while sid:
                self.assertNotIn(sid, seen)
                seen.add(sid)
                sid = c.sessions[sid].parent_id

    def test_launch_uses_argument_list_and_saved_directory(self):
        session = Session("session-id", cwd=str(self.home), path=self.write_session("session-id"))
        self.assertEqual(resume_command(session), ["codex", "resume", "--cd", str(self.home), "session-id"])
        session.cwd = str(self.home / "missing")
        with self.assertRaisesRegex(ValueError, "directory"):
            resume_command(session)
        with self.assertRaisesRegex(ValueError, "missing locally"):
            resume_command(Session("missing"))
        session.archived = True
        with self.assertRaisesRegex(ValueError, "archived"):
            resume_command(session)


class PickerTests(unittest.IsolatedAsyncioTestCase):
    def catalog(self, directory):
        return Catalog({s.id: s for s in [
            Session("root", title="Authentication", cwd=directory, path=Path("root.jsonl")),
            Session("child", "root", "Cookie sessions", directory, path=Path("child.jsonl")),
            Session("deep", "child", "Refresh failure", directory, path=Path("deep.jsonl")),
            Session("other", title="Other conversation", cwd=directory, path=Path("other.jsonl")),
        ]})

    async def test_navigation_search_and_enter_resume_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Picker(Path(directory), catalog=self.catalog(directory))
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                self.assertEqual(app.theme, "ansi-light")
                tree = app.query_one(SessionTree)
                tree.move_cursor(app.nodes["root"])
                await pilot.press("left")
                self.assertFalse(app.nodes["root"].is_expanded)
                await pilot.press("right", "right")
                self.assertEqual(tree.cursor_node.data.id, "child")
                await pilot.press("left")
                self.assertFalse(app.nodes["child"].is_expanded)
                await pilot.press(*list("refresh"))
                await pilot.pause()
                self.assertEqual(app.matches, {"deep"})
                self.assertEqual(set(app.nodes), {"root", "child", "deep"})
                self.assertEqual(tree.cursor_node.data.id, "deep")
                await pilot.press("escape")
                await pilot.pause()
                self.assertIs(app.focused, tree)
                self.assertEqual(app.search_query, "")
                self.assertIsNone(app.return_value)
                await pilot.press(*list("refresh"))
                await pilot.pause()
                self.assertIs(app.focused, tree)
                self.assertFalse(app.query(Input))
                # Typing never transfers focus away from tree navigation.
                await pilot.press("up")
                self.assertEqual(tree.cursor_node.data.id, "child")
                await pilot.press("down")
                self.assertEqual(tree.cursor_node.data.id, "deep")
                await pilot.press("left", "left", "left")
                self.assertEqual(tree.cursor_node.data.id, "root")
                await pilot.press("right", "right")
                self.assertEqual(tree.cursor_node.data.id, "child")
                self.assertIs(app.focused, tree)
                self.assertEqual(app.search_query, "refresh")
                # Enter opens the highlighted ancestor directly from search.
                await pilot.press("enter")
            self.assertEqual(app.return_value.id, "child")

    async def test_empty_results_and_missing_cwd_do_not_launch(self):
        app = Picker(Path("/"), catalog=self.catalog("/does-not-exist-codex-test"))
        async with app.run_test(size=(60, 18)) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            self.assertIsNone(app.return_value)
            await pilot.press(*list("no-match"))
            await pilot.pause()
            self.assertFalse(app.matches)
            await pilot.press("enter", "enter")
            self.assertIsNone(app.return_value)
            await pilot.press("escape")

    async def test_printable_keys_search_and_control_shortcuts_work_in_search(self):
        app = Picker(Path("/"), catalog=self.catalog("/tmp"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press(*list("qrahjkl"), "slash", "space", "x")
            tree = app.query_one(SessionTree)
            self.assertEqual(app.search_query, "qrahjkl/ x")
            self.assertIs(app.focused, tree)
            await pilot.press("backspace")
            self.assertEqual(app.search_query, "qrahjkl/ ")
            await pilot.press("ctrl+a")
            self.assertTrue(app.include_archived)
            self.assertEqual(app.search_query, "qrahjkl/ ")
            with patch.object(app, "action_reload") as refresh:
                await pilot.press("ctrl+r")
                refresh.assert_called_once()
            await pilot.press("ctrl+l")
            self.assertEqual(app.search_query, "")
            self.assertIsInstance(app.focused, SessionTree)
            # Space starts a query too; it no longer toggles a branch.
            await pilot.press("space", "q")
            self.assertEqual(app.search_query, " q")
            with patch.object(app, "exit", wraps=app.exit) as quit_app:
                await pilot.press("ctrl+c")
                quit_app.assert_called_once()

    async def test_search_banner_typing_paste_and_delete_keep_tree_focus(self):
        app = Picker(Path("/"), catalog=self.catalog("/tmp"))
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one(SessionTree)
            for character in "Az09[]!? /é":
                app.post_message(events.Key(character, character))
                await pilot.pause()
                self.assertIs(app.focused, tree)
            self.assertEqual(app.search_query, "Az09[]!? /é")
            await pilot.press("delete", "backspace")
            self.assertEqual(app.search_query, "Az09[]!? ")
            app.post_message(events.Paste("pasted text"))
            await pilot.pause()
            self.assertEqual(app.search_query, "Az09[]!? pasted text")
            self.assertIs(app.focused, tree)
            await pilot.click("#search")
            await pilot.press("down", "up", "left", "right")
            self.assertIs(app.focused, tree)
            await pilot.press("escape", "backspace", "delete")
            self.assertEqual(app.search_query, "")
            await pilot.press("ctrl+c")

    async def test_batched_terminal_keys_keep_text_delete_navigation_order(self):
        from textual._xterm_parser import XTermParser

        app = Picker(Path("/"), catalog=self.catalog("/tmp"), print_id=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            # A terminal read may contain typing, Mac Delete, an arrow, and Enter.
            # Don't let Pilot's per-key pauses hide event ordering problems.
            for event in XTermParser().feed("refreshz\x7f\x1b[A\r"):
                app.post_message(event)
            await pilot.pause()
        self.assertEqual(app.search_query, "refresh")
        self.assertIsNotNone(app.return_value)
        self.assertEqual(app.return_value.id, "child")

    async def test_print_id_can_select_session_with_unavailable_cwd(self):
        app = Picker(Path("/"), catalog=self.catalog("/does-not-exist-codex-test"), print_id=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
        self.assertIsNotNone(app.return_value)


@unittest.skipUnless(os.name == "posix", "POSIX terminal driver")
class TerminalInputTests(unittest.TestCase):
    def test_fragmented_input_and_idle_shutdown(self):
        from terminal_driver import ResponsiveLinuxDriver

        read_fd, write_fd = os.pipe()
        driver = ResponsiveLinuxDriver.__new__(ResponsiveLinuxDriver)
        driver.fileno = read_fd
        driver._debug = False
        driver.exit_event = threading.Event()
        events = []
        errors = []
        driver.process_message = events.append

        def run():
            try:
                driver.run_input_thread()
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=run)
        worker.start()
        try:
            # Reads may split both terminal escape sequences and UTF-8 characters.
            for part in (b"\x1b[", b"B", b"\xc3", b"\xa9"):
                os.write(write_fd, part)
                time.sleep(0.003)
            deadline = time.monotonic() + 2
            while len(events) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual([event.key for event in events], ["down", "é"])
            os.write(write_fd, b"\x1b")
            deadline = time.monotonic() + 2
            while len(events) < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual([event.key for event in events], ["down", "é", "escape"])
            driver.exit_event.set()
            worker.join(timeout=0.5)
            self.assertFalse(worker.is_alive(), "Idle input thread did not stop")
            self.assertFalse(errors)
        finally:
            driver.exit_event.set()
            worker.join(timeout=1)
            os.close(read_fd)
            os.close(write_fd)


if __name__ == "__main__":
    unittest.main()
