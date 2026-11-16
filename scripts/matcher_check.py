"""A clock's matcher check: registration's scoring on threads equals scoring on one, and what a matcher thread costs.

Runs inside a clock's app container, on its gallery (spec §Tests, level 2), with glibc's thread-stack cache off as the
app runs (`__main__.run_app`):

  docker cp scripts/matcher_check.py <app>:/tmp/ && docker exec -e GLIBC_TUNABLES=glibc.pthread.stack_cache_size=0 \
    <app> /app/.venv/bin/python /tmp/matcher_check.py

Each of the first `--prints` prints' first press is scored against every one of those prints, as a registration's
gallery jobs do: on one thread, then split across 2, 4 and 8. R15: it prints only counts, timings, resident memory
and pass/fail, never a score or a template.
"""

# Standard library imports
import argparse
import os
import threading
import time

# First party imports
from timeclock_system.fprint_reader import FprintMatching
from timeclock_system.settings import ClockSettings
from timeclock_system.store import LocalStore


def rss_mb() -> float:
  """This process's resident memory."""
  with open("/proc/self/status", encoding="ascii") as f:
    return next(int(line.split()[1]) / 1024 for line in f if line.startswith("VmRSS:"))


def main() -> None:
  """Score on 1, 2, 4 and 8 threads, compare, and measure a parked matcher thread's memory."""
  parser = argparse.ArgumentParser(description="Check registration matching on threads against one thread.")
  parser.add_argument("--prints", type=int, default=20, help="how many of the gallery's prints to use")
  args = parser.parse_args()
  print(f"GLIBC_TUNABLES={os.environ.get('GLIBC_TUNABLES', '(unset)')}")

  store = LocalStore(ClockSettings.get_settings().store_path)
  try:
    templates = [f.template.get_secret_value() for f in store.fingerprints()][: args.prints]
  finally:
    store.close()
  pairs = [(p, o) for p in range(len(templates)) for o in range(len(templates))]
  print(f"prints: {len(templates)}, scored pairs: {len(pairs)}")

  def score_all(n_threads: int) -> tuple[dict[tuple[int, int], tuple[int, ...]], float]:
    matching, results, lock = FprintMatching(), {}, threading.Lock()

    def work(chunk: list[tuple[int, int]]) -> None:
      cancel = matching.new_cancel()
      out = {(p, o): matching.score(templates[p], 0, templates[o], cancel) for p, o in chunk}
      with lock:
        results.update(out)

    t0 = time.monotonic()
    threads = [threading.Thread(target=work, args=(pairs[k::n_threads],), name=f"matcher-{k}") for k in range(n_threads)]
    for t in threads:
      t.start()
    for t in threads:
      t.join()
    return results, time.monotonic() - t0

  serial, one = score_all(1)
  print(f"1 thread: {one:.2f} s")
  for n in (2, 4, 8):
    scores, took = score_all(n)
    print(f"{n} threads: {took:.2f} s ({one / took:.2f}x), equal: {scores == serial}")

  n, matching = 4, FprintMatching()
  before = rss_mb()
  parked, release = threading.Barrier(n + 1), threading.Event()

  def match_then_park() -> None:
    cancel = matching.new_cancel()
    for o in range(len(templates)):
      matching.score(templates[0], 0, templates[o], cancel)
    parked.wait()
    release.wait()

  threads = [threading.Thread(target=match_then_park) for _ in range(n)]
  for t in threads:
    t.start()
  parked.wait()
  during = rss_mb()
  release.set()
  for t in threads:
    t.join()
  after = rss_mb()
  print(f"memory: {before:.1f} MB before, {(during - before) / n:.1f} MB per matcher thread, {after - before:+.1f} MB after")


if __name__ == "__main__":
  main()
