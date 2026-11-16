# Registration matching on a thread-safe matcher

Approved by the owner on 2026-11-16.

## Why

Registering a finger is slow at the clock. Measured on the test clock with the real reader, the reader itself is fast:
it opens in 0.1 s and waits for a finger 0.02 s after a capture starts. The time goes to matching, which runs on the
scanner thread, the same thread that captures:

- **Before the first press**, `Scanner._start_session` compares every other person's registered presses with each
  other: five matcher calls per print, about 57 ms each on the clocks' CPUs, so roughly 12 s at today's gallery size.
  The result is cached in memory only, so the first registration after every app restart pays all of it.
- **After every press**, `Scanner._press` scores the press against every other person's print and against the session's
  earlier presses before the reader re-arms: about 2 s per press.

One thread does all of it because the matcher isn't thread-safe. NIST's bozorth3, inside libfprint, keeps its working
tables in global arrays (20 in `nbis/bozorth3/bz_gbls.c`, 8 more file-level in `nbis/bozorth3/bozorth3.c`), and every
score is two steps over them: `bozorth_probe_init` fills them from the probe, then `bozorth_to_gallery` reads them for
each comparison. Two threads matching at once overwrite each other's tables and return wrong scores, silently.

## What the owner decided

1. **The matcher is made thread-safe, and registration matching runs on threads** rather than helper processes, so the
   number of matchers isn't tied to the CPU count.
2. **The reader only collects presses.** Each press's comparisons run in the background, and their results update the
   registration as they come back. When a press has to be replaced, the total needed goes up, and the reader keeps
   collecting until five acceptable presses are confirmed.
3. **The start-of-registration pass goes.** Registration only cares whether the new person's presses resemble an
   existing print. Another print's own agreement (how well its five presses agree with each other) matters only for a
   print a new press resembles: it decides whether that print is carelessly registered (case B, flagged for
   re-registering) rather than the new press being at fault. So it's computed for those prints only, when a press first
   resembles them.
4. **After the fifth press, while results are still coming back,** the screen says the presses are being checked, and
   the reader stays armed without asking for more. It asks again only when a press has to be replaced.
5. **An intermittent matcher failure doesn't fail the registration.** Failures are retried; only a count of failures
   past a threshold ends the registration.
6. **The tests are very thorough,** the change's memory use is measured, and the clock caps the number of matcher
   threads so matching can't consume too much memory.

## Goals

- The registration screen is ready for the first press as soon as the reader re-arms (about a second), at every
  registration, including the first after a restart.
- A press never waits on matching before the reader re-arms for the next one.
- Scores are identical to today's, bit for bit, whichever thread computes them and however many run at once.
- Registration's rules stay as they are: the press checks, the agreement bar and count, the resemblance bar, cases A, B
  and C, the give-up count, the admin's forced save and the too-similar review.
- Memory outside a registration stays as it is today.

## Non-goals

- Punch scans (identify) keep running on the scanner thread.
- Nothing is stored on disk; with the start pass gone, there's nothing worth persisting.

## Anti-goals

- A wrong score from a matcher shared between threads. This is the failure that matters most: it would be silent.
- A registration that fails because one matcher call failed once.
- Presses captured after the registration has its verdict counting toward anything.
- The reader pausing for matching at any point in a registration.

## Approaches considered

- **Store each print's self-agreement on the clock**, computed once per print. Rejected: the self-agreement isn't needed
  up front at all (decision 3), and it leaves the 2 s per press on the reader.
- **Helper processes**, each with its own copy of the matcher's globals: no C change, and a matcher crash loses only a
  helper. Rejected by the owner in favour of threads, so the matcher count isn't bound to processes and cores.
- **A per-call context struct** in place of the globals, the textbook fix: it touches most of the matcher's 2,500
  lines. Rejected: thread-local storage gives each thread its own tables with declaration changes only.
- **Thread-local tables and a capped thread pool** (chosen).

## The design

### The thread-safe matcher (timeclock-extras)

- A fifth libfprint patch makes every mutable table bozorth3 keeps thread-local (C11 `_Thread_local`): the arrays in
  `bz_gbls.c` and their `extern` declarations in `nbis/include/bozorth.h`, and the file-level arrays in `bozorth3.c`
  (`rot`, `rtp`, `ct`, `gct`, `ctt`, `ctp`, `yy`, `sct`). Read-only tables stay as they are. No logic changes.
- The patch's audit is part of the work: every `static` or global in `nbis/bozorth3/` and in libfprint's bozorth3 glue
  (`fpi-print.c`, and our `identify-best-match` and `registration-presses` patches) is listed with its verdict
  (thread-local, read-only, or unused by libfprint, such as `bz_io.c`'s command-line state). mindtct, which extracts
  minutiae during a capture, only runs on the scanner thread and isn't touched.
- **The matcher stops itself when asked** (owner: a matcher thread must still see its shutdown flag while otherwise
  hung). A thread can't be killed safely: a thread stopped from outside, by a kill or by a signal handler jumping out,
  can leave the allocator's or GLib's locks held for good. So the score call takes a `GCancellable`, the GLib object
  the reader's calls already take: `fp_print_score_press` gains a `cancellable` argument, set as a thread-local
  "current cancellable" for the call, and bozorth3's outer loops (and any loop whose length the input decides) check it
  with `g_cancellable_is_cancelled`, which is safe from any thread. A cancelled call returns at once with
  `G_IO_ERROR_CANCELLED`, leaving the thread's tables to be filled afresh by the next call's `bozorth_probe_init`.
- It ships as `timeclock-libfprint` 1.94.100.post4 on SFTPyPI, built and released by timeclock-extras' existing
  workflow; the clock pins it.

### Matcher threads (timeclock-system)

- Registration matching runs on a pool of matcher threads. The pool starts with a registration session and shuts down
  when it ends, so each thread's copy of the matcher tables (allocated by the C runtime when a thread first matches) is
  freed when its thread exits, and memory outside a registration is as today.
- **The cap.** The number of matcher threads is fixed in code, set from the measured memory per matcher thread (see
  Tests) so all matcher threads together stay under **256 MB**. It doesn't follow the CPU count. A thread that ignores
  its cancellation (the last resort below) still counts toward the cap until it returns.
- **Every job carries its own cancellable,** cancelled from outside the thread: by the job's deadline timer, by the end
  of its registration session, and by the app shutting down. Between jobs a matcher thread checks the pool's stop flag
  and exits when it's set, so a shutdown never waits on more than the calls in flight, which their cancellables end.
- Each thread deserializes the templates it needs into its own `FpPrint` objects, so no libfprint object is shared
  between threads. Calls cross the GIL boundary through PyGObject; that PyGObject releases the GIL for the duration of
  a matcher call is proven by the tests, not assumed.
- Jobs a press needs:
  - **Against the gallery:** the press scored against each other person's print (all its presses), split across the
    threads so one press's gallery scoring runs in parallel.
  - **Against the session:** the press against each earlier press of this registration, both directions, keeping the
    higher, as `Press.vs_session` holds today.
  - **A resembled print's own agreement:** for each other print the press scores at least the resemblance bar against,
    if not already known this session: its presses decoded and scored against each other (five calls).

### The scanner in a registration

- `register` no longer computes anything up front. `RegisterGallery` carries only the other prints' identities (print,
  employee, finger); no presses, no agreement. The resemblance bar reaches the scanner with the other settings it
  already takes (`set_gallery`).
- In REGISTER the scanner thread only captures: after each capture (and the existing finger-off pause) it re-arms at
  once. Each capture is posted at once as a capture event (so the screen can count it) and handed to the pool with its
  press number.
- A press's `Pressed` event is posted once all its jobs are back, in capture order: a later press that finishes first
  waits for the earlier ones, so the session sees presses in the order they were made, as today.
- `Press` gains the resembled prints' `OtherPrint` data (presses and agreement) for every other print it resembles,
  which the session uses in place of today's up-front `others`. The session keeps the gallery's order for ties.

### The session

- `Session` no longer takes every other print's presses and agreement up front, and no longer computes `failing` for
  every print at the start. It learns each resembled print from the presses that resemble it, and computes that print's
  failing presses the first time it appears. Everything else in `Session.add` is unchanged.

### The screen

- Captured presses count toward the screen's "n of total", with total five plus the presses known so far to need
  replacing.
- After the fifth press, while any press's results are still out, the screen shows a checking message in place of the
  "Press your finger" prompt. The reader stays armed. If the verdict needs a replacement, the prompt (with the drop's
  advice, as today) comes back with the higher total.
- A press made during checking anyway is captured and judged like any other: it's next in line if a replacement is
  needed.
- "Save anyway (admin)" shows only while no press's results are out, so `best()` and `resembled` see every press made.

### Session boundaries

- Every job is tagged with its registration session. A merge (a verdict reached) or a mode change ends the session: its
  outstanding jobs are dropped, their failures stop counting, its pool shuts down, and a late result is discarded, so
  nothing from one registration lands in the next or fails a registration whose merge is already running.
- The 30 s press timeout (`enroll_idle_timeout`) doesn't run out while results are out: it restarts when the last
  outstanding result is posted.

### Failures

- A job that raises is retried. A job not back within **15 s** is cancelled through its cancellable; the matcher
  returns within milliseconds, its thread is free again, the job counts as a failure and is retried.
- **Last resort:** a call still not back 5 s after its cancellation (stuck somewhere the matcher doesn't check, which
  the patch's audit should rule out) has its thread abandoned. The abandoned thread still counts toward the cap until it
  returns, it's logged, and the job is retried on another thread.
- A registration ends with "Registration failed, try again" (today's `EnrollFailed("error")` path) when its failures
  reach **3**. That ending sends one alert ("Fingerprint matching kept failing"). A retried failure sends none, and a
  registration ended by the reader still sends none, as today. Each failure is logged, without biometric detail (R15).
- A crash inside the matcher's C code now takes the app down, where a helper process would have contained it; the
  container restarts it, as for any crash today.

## Tests

The matcher's correctness under threads is the whole risk, so it's tested at three levels, and nothing ships until all
three pass.

1. **timeclock-extras, in CI, on synthetic prints** (no real fingerprint data anywhere in the repo or its logs):
   - Templates built from seeded random minutiae, deserialized through libfprint as the clock's templates are.
   - **Equivalence:** a large set of press-against-print scores computed on one thread, then the same set computed from
     many threads at once, in shuffled orders, over repeated rounds; every score must match exactly. This also runs on
     an unpatched build to show the test catches the race (it must fail there).
   - **The GIL:** the same work timed on one thread and on several; the threads must run in parallel.
   - **Memory:** resident memory measured with no matcher thread, after each additional matcher thread has matched,
     and after the threads exit; this gives the per-thread cost the cap is set from, and shows it's released.
   - **Loading:** a fresh process loads the patched libfprint through PyGObject and matches from a new thread, so
     thread-local storage works when the library is loaded at run time.
   - **Cancellation:** a call cancelled before it starts and one cancelled mid-match (a deliberately large synthetic
     print, cancelled from another thread) both return `G_IO_ERROR_CANCELLED` within a few milliseconds; the same
     thread's next calls then score exactly as an uncancelled run, so a cancel never leaves a table half-filled into a
     later score. Cancelling one thread's call leaves the others' scores untouched.
2. **The dev clock, on its real gallery** (scores and templates never printed or logged; only counts, timings and
   pass/fail): the equivalence run on real templates against the clock's own CPU, scaling with 1 to the cap's threads,
   and resident memory during and after a run.
3. **timeclock-system:** the scanner, session and screen changes test-first with an in-process stand-in for the
   matcher, including: out-of-order job completion still posting presses in capture order; the checking state; a
   replacement raising the total; presses after a verdict discarded; jobs from an ended session dropped and their
   cancellables cancelled; retried failures, the threshold of 3 and its alert; a hung job cancelled at its deadline and
   its thread reused; a job ignoring its cancellation abandoned while the cap still holds; the pool's shutdown with
   calls in flight; the forced-save button hidden while results are out; and a resembled print's agreement computed
   once per session.

Rollout: the dev clock first (the repo's practice for device-facing changes), a registration there, then both
production clocks.

## My calls (not the owner's)

- The 256 MB budget for all matcher threads, and the cap derived from the measured per-thread cost.
- The pool living only for a registration, so the tables' memory is released between registrations.
- The threshold of 3 failures, the 15 s job deadline and the 5 s grace after a cancel before a thread is abandoned; one
  alert only when matching itself keeps failing.
- Cancellation through a `GCancellable` checked in the matcher's loops, in place of a deadline the matcher keeps itself:
  it covers the deadline, a session's end and shutdown with one mechanism, the one the reader already uses.
- A press made during checking is judged like any other; the forced-save button hidden while results are out.
- The dev clock before production.

## Left to the builder

- Whether the pool is `concurrent.futures.ThreadPoolExecutor` or a small pool of its own (an abandoned thread must
  still count toward the cap).
- How the scanner's `post` receives results from the pool.
- The checking message's exact wording, in `text.py` beside `REGISTERING`.
