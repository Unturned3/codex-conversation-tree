"""Textual's POSIX driver with a shorter, bounded input/shutdown poll."""

from codecs import getincrementaldecoder
import os
import selectors

from textual._parser import ParseError
from textual._xterm_parser import XTermParser
from textual.drivers.linux_driver import LinuxDriver


class ResponsiveLinuxDriver(LinuxDriver):
    """Keep Textual's terminal setup/cleanup, avoid its 100 ms shutdown waits.

    Textual 8.2.8 polls at 100 ms and performs another timed select after
    unregistering stdin. Only the input loop is replaced; parsing, signals,
    terminal restoration, and writer-thread shutdown stay with Textual.
    """

    def run_input_thread(self) -> None:
        parser = XTermParser(self._debug)
        decode = getincrementaldecoder("utf-8")().decode
        with selectors.SelectSelector() as selector:
            selector.register(self.fileno, selectors.EVENT_READ)
            try:
                while not self.exit_event.is_set():
                    ready = selector.select(0.01)
                    if self.exit_event.is_set():
                        break
                    if ready:
                        data = os.read(self.fileno, 4096)
                        if not data:
                            break
                        text = decode(data)
                        # A partial UTF-8 character is not end-of-input.
                        if text:
                            for event in parser.feed(text):
                                self.process_message(event)
                    for event in parser.tick():
                        self.process_message(event)
            finally:
                try:
                    for _ in parser.feed(""):
                        pass
                except (EOFError, ParseError):
                    pass
