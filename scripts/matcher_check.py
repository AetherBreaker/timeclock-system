"""A clock's matcher check: registration's scoring on threads equals scoring on one, and what a matcher thread costs.

Runs inside a clock's app container, on its gallery (spec §Tests, level 2), with glibc's thread-stack cache off as the
app runs (`__main__.run_app`):

  docker cp scripts/matcher_check.py <app>:/tmp/ && docker exec -u <app uid> \
    -e GLIBC_TUNABLES=glibc.pthread.stack_cache_size=0 <app> /app/.venv/bin/python /tmp/matcher_check.py

Each of the first `--prints` prints' first press is scored against every one of those prints, as a registration's
gallery jobs do, split into 2, 4 and 8 chunks. bozorth3's score of a print against itself can depend on what its
thread matched before (upstream behaviour, unpatched libfprint included), so one serial pass in another order is no
reference: each chunk is scored alone, one at a time on a fresh thread, then all at once on their own threads, and
every score must be equal. R15: it prints only counts, timings, resident memory and pass/fail, never a score or a
template.
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


def main() -> None:  # noqa: C901  # one check, its steps in order
  """Score each chunk alone and all chunks at once, compare, and measure a parked matcher thread's memory."""
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

  def score(chunks: list[list[tuple[int, int]]], *, at_once: bool) -> tuple[dict[tuple[int, int], tuple[int, ...]], float]:
    matching, results, lock = FprintMatching(), {}, threading.Lock()

    def work(chunk: list[tuple[int, int]]) -> None:
      cancel = matching.new_cancel()
      out = {(p, o): matching.score(templates[p], 0, templates[o], cancel) for p, o in chunk}
      with lock:
        results.update(out)

    threads = [threading.Thread(target=work, args=(c,), name=f"matcher-{k}") for k, c in enumerate(chunks)]
    t0 = time.monotonic()
    for t in threads:
      t.start()
      if not at_once:
        t.join()
    for t in threads:
      t.join()
    return results, time.monotonic() - t0

  _, one = score([pairs], at_once=False)
  print(f"1 thread: {one:.2f} s")
  for n in (2, 4, 8):
    chunks = [pairs[k::n] for k in range(n)]
    alone, _ = score(chunks, at_once=False)
    together, took = score(chunks, at_once=True)
    print(f"{n} threads: {took:.2f} s ({one / took:.2f}x), equal: {together == alone}")

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
