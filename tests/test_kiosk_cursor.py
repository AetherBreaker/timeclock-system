# Standard library imports
import json
import re
import shutil
import subprocess

# Third party imports
import pytest

# First party imports
from timeclock_system.ui import page

NODE = shutil.which("node")
MINUTE = 60_000

# The page's cursor script against a stub DOM with a fake clock. Each step is an event (`["pointermove", "mouse"]`)
# or `["wait", ms]`; the output is whether the cursor is hidden at load and after each step.
_HARNESS = """
let now = 0, nextId = 1;
const timers = new Map(), classes = new Set(), listeners = {};
globalThis.setTimeout = (fn, ms) => { const id = nextId++; timers.set(id, [now + ms, fn]); return id; };
globalThis.clearTimeout = id => timers.delete(id);
globalThis.document = {
  documentElement: { classList: { add: c => classes.add(c), remove: c => classes.delete(c) } },
  addEventListener: (type, fn) => (listeners[type] ??= []).push(fn),
};
globalThis.location = { search: SEARCH };
eval(SCRIPT);
const hidden = [classes.has("sft-hide-cursor")];
for (const [kind, arg] of STEPS) {
  if (kind === "wait") {
    now += arg;
    for (const [id, [at, fn]] of [...timers]) if (at <= now) { timers.delete(id); fn(); }
  } else (listeners[kind] ?? []).forEach(fn => fn({ pointerType: arg }));
  hidden.push(classes.has("sft-hide-cursor"));
}
console.log(JSON.stringify(hidden));
"""


def cursor_hidden(search: str, steps: list[tuple[str, str | int]]) -> list[bool]:
  [script] = re.findall(r"<script>(.*?)</script>", page.KIOSK_CURSOR, re.DOTALL)
  program = _HARNESS.replace("SEARCH", json.dumps(search)).replace("SCRIPT", json.dumps(script)).replace("STEPS", json.dumps(steps))
  assert NODE is not None
  done = subprocess.run([NODE, "-"], input=program, capture_output=True, text=True, check=True, timeout=30)
  return json.loads(done.stdout)


needs_node = pytest.mark.skipif(NODE is None, reason="the cursor script runs under Node")


@needs_node
def test_the_kiosk_hides_the_cursor_until_a_mouse_moves_and_again_a_minute_later() -> None:
  steps: list[tuple[str, str | int]] = [("pointermove", "mouse"), ("wait", MINUTE - 1), ("wait", 1)]
  assert cursor_hidden("?kiosk", steps) == [True, False, False, True]


@needs_node
def test_each_mouse_movement_restarts_the_minute() -> None:
  steps: list[tuple[str, str | int]] = [
    ("pointermove", "mouse"),
    ("wait", 30_000),
    ("pointermove", "mouse"),
    ("wait", 59_000),
    ("wait", 1_000),
  ]
  assert cursor_hidden("?kiosk", steps) == [True, False, False, False, False, True]


@needs_node
def test_touch_never_shows_the_cursor_and_hides_a_shown_one() -> None:
  steps: list[tuple[str, str | int]] = [
    ("pointermove", "touch"),
    ("pointerdown", "touch"),
    ("pointermove", "pen"),
    ("pointermove", "mouse"),
    ("pointerdown", "touch"),
    ("wait", MINUTE),  # the hide already happened; the cancelled timer changes nothing
  ]
  assert cursor_hidden("?kiosk", steps) == [True, True, True, True, False, True, True]


@needs_node
def test_other_browsers_always_show_the_cursor() -> None:
  steps: list[tuple[str, str | int]] = [("pointerdown", "touch"), ("pointermove", "mouse"), ("wait", MINUTE)]
  assert cursor_hidden("", steps) == [False, False, False, False]
