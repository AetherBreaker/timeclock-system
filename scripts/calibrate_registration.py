r"""Calibration replay for registration's bars (`registration.py`): run in the image's Python with the patched libfprint.

Scores every press of every exported print against its own print's other presses and against every other person's
presses, then prints how candidate bars sort the known-bad prints from the healthy ones. Output is counts and
distributions only: no print ids, employee ids or names. The export and the score cache are biometric data: keep them in
a scratch directory and delete them afterwards.

  python calibrate_registration.py PRINTS OUT_DIR --stray PRINT_ID [--old PRINTS] [--scans SCANS]

PRINTS: one print per line, whitespace-separated: id, employee_id, finger, base64 FP3 template (the clocks' gallery).
--stray: the print whose single stray press resembles other people's prints. --old: replaced prints known to have drawn
near-collisions; scored against the gallery but never part of it. --scans: the scan log, one scan per line,
`|`-separated, exported by `SELECT extract(epoch FROM at), instance_id, result, coalesce(best_fingerprint_id::text, ''),
translate(encode(probe, 'base64'), E'\n', '') FROM fingerprint_scans WHERE probe IS NOT NULL ORDER BY at`; probes are
biometric data too.

The 2026-11-05 replay over 39 production prints and 2 replaced ones, at the defaults (detail points 20-120, centroid
within 100 px, agreement 41 by 1 of the other 4, resemblance 40): every press of the 7 weak prints and the stray press is
replaced, the 2 old prints lose their resembling presses to the own-stray rule instead of going to an admin; 15 of 155
healthy presses are asked again, no healthy print needs an admin or is flagged. That day's 97 attributed real scans (17
prints) favour the 1-partner rule: it re-asks presses that carry 1 of the 54 scans reaching 40, 2 partners at 41 would
lose 17 and an every-pair floor of 10 already 8, since presses with a single partner carry the most real matches.
"""

# Standard library imports
import argparse
import base64
import itertools
import json
import math
import statistics
import sys
from collections import Counter
from pathlib import Path

# Third party imports
import gi

gi.require_version("FPrint", "2.0")
# Third party imports
from gi.repository import FPrint, GLib

WIDTH, HEIGHT = 384, 290  # uru4000; minutiae are NIST coordinates (y up from the bottom edge)
GENUINE_BAR = 40  # the scan threshold: "agreeing" in the known-bad ground truth
WEAK_PRINT_AGREEING = 3  # ground truth: a print with this many or fewer agreeing presses is known-bad
WHOLE_PRINT_FAILS = 2  # more failing presses than this and the other print fails as a whole (case B)
MARGIN = 15  # the scan's match margin
RESEMBLANCE_BARS = (25, 30, 33, 36, 38, 40, 43)
CHOSEN = ((20, 120, 100, None, 41, 1), 40)  # (min, max, centre, edge, agreement bar, needed), resemblance bar
SCAN_WINDOW = 60  # seconds: a failed scan is the person's who matches next on the same clock within this


def presses_of(template: bytes) -> list[list[tuple[int, int, int]]]:
  """Decode an FP3 template's presses: (x, y, theta) per detail point."""
  variant = GLib.Variant.new_from_bytes(GLib.VariantType("(issbymsmsia{sv}v)"), GLib.Bytes(template[3:]), False)  # noqa: FBT003
  nbis = variant.get_child_value(9).get_variant().unpack()  # (a(aiaiai)): per press, its x, y and theta columns
  return [list(zip(x, y, t, strict=True)) for x, y, t in nbis[0]]


def load(path: Path) -> list[dict]:
  """Read a PRINTS file."""
  rows = []
  for line in path.read_text(encoding="utf-8").splitlines():
    if line.strip():
      print_id, employee_id, finger, template = line.split()
      rows.append({"id": print_id, "employee_id": int(employee_id), "finger": finger, "template": base64.b64decode(template)})
  return rows


def score(prints: list[dict], gallery: list[dict]) -> dict:
  """Own 5x5 matrices (both directions) and each press's scores against every other person's gallery print."""
  objects = {p["id"]: FPrint.Print.deserialize(p["template"]) for p in prints}
  result = {}
  for n, p in enumerate(prints, 1):
    fp = objects[p["id"]]
    count = len(presses_of(p["template"]))
    own = [list(fp.score_press(i, fp)) for i in range(count)]
    cross = {
      q["id"]: [list(fp.score_press(i, objects[q["id"]])) for i in range(count)]
      for q in gallery
      if q["employee_id"] != p["employee_id"]
    }
    result[p["id"]] = {"own": own, "cross": cross}
    print(f"scored {n}/{len(prints)}", file=sys.stderr)
  return result


def stats(values: list[float]) -> str:
  """Count, extremes and percentiles, on one line."""
  if not values:
    return "none"
  v = sorted(values)
  pick = lambda q: v[min(len(v) - 1, int(q * (len(v) - 1) + 0.5))]
  return f"n={len(v)} min={v[0]:.0f} p5={pick(0.05):.0f} p25={pick(0.25):.0f} med={pick(0.5):.0f} p75={pick(0.75):.0f} p95={pick(0.95):.0f} max={v[-1]:.0f}"


def features(p: dict, scores: dict) -> list[dict]:
  """Per press: detail points, centroid offset, edge margin, own agreement (best first) and best other-person score."""
  own = scores["own"]
  out = []
  for i, minutiae in enumerate(presses_of(p["template"])):
    xs, ys = [m[0] for m in minutiae] or [0], [m[1] for m in minutiae] or [0]
    others = [max(own[i][j], own[j][i]) for j in range(len(own)) if j != i]
    out.append(
      {
        "n": len(minutiae),
        "centre": math.hypot(statistics.fmean(xs) - WIDTH / 2, statistics.fmean(ys) - HEIGHT / 2),
        "edge": min(*xs, *ys, WIDTH - 1 - max(xs), HEIGHT - 1 - max(ys)),
        "agree": sorted(others, reverse=True),  # best first
        "cross": max((s for q in scores["cross"].values() for s in q[i]), default=0),
      }
    )
  return out


def flagged(f: dict, bars: tuple) -> bool:
  """Whether a press fails the press checks `bars` (min, max, centre, edge, agreement bar, needed); None skips one."""
  low, high, centre, edge, agree_bar, needed = bars
  return (
    f["n"] < low
    or (high is not None and f["n"] > high)
    or (centre is not None and f["centre"] > centre)
    or (edge is not None and f["edge"] < edge)
    or sum(s >= agree_bar for s in f["agree"]) < needed
  )


def main() -> None:  # noqa: C901, PLR0912, PLR0915
  """Score (or load the cached scores), then print the distributions and the replays."""
  parser = argparse.ArgumentParser()
  parser.add_argument("prints", type=Path)
  parser.add_argument("out", type=Path)
  parser.add_argument("--stray", required=True)
  parser.add_argument("--old", type=Path)
  parser.add_argument("--scans", type=Path)
  args = parser.parse_args()

  gallery = load(args.prints)
  old = load(args.old) if args.old else []
  cache = args.out / "scores.json"
  if cache.exists():
    scores = json.loads(cache.read_text())
  else:
    scores = score(gallery + old, gallery)
    cache.write_text(json.dumps(scores))

  feats = {p["id"]: features(p, scores[p["id"]]) for p in gallery + old}
  # Known-bad prints (the spec's ground truth) and, per print, its known-bad presses.
  bad: dict[str, list[int]] = {}
  for p in gallery:
    agreeing = [i for i, f in enumerate(feats[p["id"]]) if f["agree"] and f["agree"][0] >= GENUINE_BAR]
    if len(agreeing) <= WEAK_PRINT_AGREEING:
      bad[p["id"]] = [i for i in range(len(feats[p["id"]])) if i not in agreeing]
  stray = feats[args.stray]
  bad[args.stray] = [max(range(len(stray)), key=lambda i: stray[i]["cross"])]
  for p in old:
    bad[p["id"]] = list(range(len(feats[p["id"]])))
  healthy = [f for p in gallery if p["id"] not in bad for f in feats[p["id"]]]
  bad_presses = [feats[pid][i] for pid, idx in bad.items() for i in idx]
  print(
    f"prints: {len(gallery)} in the gallery, {len(old)} old; known-bad prints {len(bad)}, presses {len(bad_presses)}; healthy presses {len(healthy)}"
  )

  print("\n== press features: healthy | known-bad")
  for key, get in (
    ("detail points", lambda f: f["n"]),
    ("centroid offset (px)", lambda f: f["centre"]),
    ("edge margin (px)", lambda f: f["edge"]),
    ("best own agreement", lambda f: (f["agree"][0:] or [0])[0]),
    ("2nd own agreement", lambda f: (f["agree"][1:] or [0])[0]),
    ("4th own agreement", lambda f: (f["agree"][3:] or [0])[0]),
    ("best other-person score", lambda f: f["cross"]),
  ):
    print(f"{key}:\n  healthy  {stats([get(f) for f in healthy])}\n  bad      {stats([get(f) for f in bad_presses])}")

  # Resemblance hits at or above the lowest candidate bar: hits[new print][other print] = [(new press, their press, score)].
  hits: dict[str, dict[str, list[tuple[int, int, int]]]] = {}
  for pid, sc in scores.items():
    for qid, rows in sc["cross"].items():
      found = [(i, j, s) for i, row in enumerate(rows) for j, s in enumerate(row) if s >= min(RESEMBLANCE_BARS)]
      if found:
        hits.setdefault(pid, {})[qid] = found
  old_ids = {p["id"] for p in old}
  kinds = {pid: "old" if pid in old_ids else "stray" if pid == args.stray else "weak" for pid in bad}

  def simulate(quality: tuple, bar: int, *, lone_their_press_is_b: bool, relative: bool = False) -> dict:
    """The registration's decision, every print registering anew against the rest: press checks, then cases A/B/C.

    `relative` also counts a pair scoring within MARGIN of either press's best own agreement (a margin refusal to come).
    """
    fails = {pid: {i for i, f in enumerate(fs) if flagged(f, quality)} for pid, fs in feats.items()}
    reask = {pid: set(f) for pid, f in fails.items()}
    c_raised, b_flags = set(), set()
    for pid, by_q in hits.items():
      for qid, found in by_q.items():
        pairs = [  # failing presses are replaced first
          (i, j)
          for i, j, s in found
          if i not in fails[pid]
          and (s >= bar or (relative and s >= min(feats[pid][i]["agree"][0], feats[qid][j]["agree"][0]) - MARGIN))
        ]
        if not pairs:
          continue
        healthy = [(i, j) for i, j in pairs if j not in fails[qid]]  # as `Session.add`: pairs on their stray presses don't count
        new, theirs = {i for i, _ in healthy}, {j for _, j in healthy}
        if len(fails[qid]) > WHOLE_PRINT_FAILS or not healthy or (lone_their_press_is_b and len(theirs) == 1 and len(new) > 1):
          b_flags.add(qid)
        elif len(new) == 1:
          reask[pid] |= new
        else:  # as `Session.add`: resembling presses agreeing with too few of the other kept presses are its own strays
          own, rest = scores[pid]["own"], set(range(len(feats[pid]))) - fails[pid] - new
          strays = {i for i in new if rest and sum(max(own[i][j], own[j][i]) >= quality[4] for j in rest) < quality[5]}
          if strays:
            reask[pid] |= strays
          else:
            c_raised.add(pid)
    caught = {(pid, i) for pid, idx in bad.items() for i in idx if i in reask[pid] or pid in c_raised}
    return {
      "per_bad": sorted(
        (
          kinds[pid],
          len(idx),
          sum(i in fails[pid] for i in idx),
          sum(i in reask[pid] - fails[pid] for i in idx),
          pid in c_raised,
          pid in b_flags,
        )
        for pid, idx in bad.items()
      ),
      "bad_presses": len(caught),
      "bad_prints": sum(all((pid, i) in caught for i in idx) for pid, idx in bad.items()),
      "missed": sorted({kinds[pid] for pid, idx in bad.items() for i in idx if (pid, i) not in caught}),
      "reasked": sum(len(reask[p["id"]]) for p in gallery if p["id"] not in bad),
      "healthy_c": sum(p["id"] in c_raised for p in gallery if p["id"] not in bad),
      "b_on_healthy": len(b_flags - set(bad)),
      "b_on_bad": len(b_flags & set(bad)),
    }

  def show(r: dict) -> str:
    return (
      f"bad presses {r['bad_presses']}/{total_bad} prints {r['bad_prints']}/{len(bad)} missed {r['missed']}  "
      f"healthy re-asked {r['reasked']} C {r['healthy_c']}  B flags healthy {r['b_on_healthy']} bad {r['b_on_bad']}"
    )

  total_bad = sum(len(idx) for idx in bad.values())
  quality_grid = list(itertools.product((0, 15, 20, 25, 30), (None, 80, 100), (None, 60, 80, 100), (None, 5), (41, 45, 50), (1, 2)))
  for lone in (False, True):
    print(f"\n== full decision; several new presses on one healthy press of theirs counts as {'B' if lone else 'C'}")
    print(f"   (known-bad: {total_bad} presses in {len(bad)} prints; healthy: {len(healthy)} presses)")
    results = []
    for quality, bar in itertools.product(quality_grid, RESEMBLANCE_BARS):
      r = simulate(quality, bar, lone_their_press_is_b=lone)
      results.append(((r["bad_presses"], -r["reasked"], -r["healthy_c"], -r["b_on_healthy"]), quality, bar, r))
    results.sort(key=lambda x: x[0], reverse=True)
    shown = set()
    for key, quality, bar, r in results:
      if key in shown or len(shown) >= 15:  # noqa: PLR2004
        continue
      shown.add(key)
      low, high, centre, edge, agree_bar, needed = quality
      print(f"{show(r)}  | min {low} max {high} centre {centre} edge {edge} agree {needed}x>={agree_bar} resemblance {bar}")
  print("\n== named press checks by resemblance bar (B only on their failing presses), absolute | absolute or within 15")
  for name, quality in (
    ("agreement 1x>=41 only", (0, None, None, None, 41, 1)),
    ("+ min 20 max 120 centre 100", (20, 120, 100, None, 41, 1)),
  ):
    print(f"  {name}")
    for bar in RESEMBLANCE_BARS:
      for relative in (False, True):
        print(
          f"    bar {bar}{' +15 rule' if relative else '         '}: {show(simulate(quality, bar, lone_their_press_is_b=False, relative=relative))}"
        )

  print(f"\n== chosen {CHOSEN}: per known-bad print (kind, bad presses, failing press checks, case A, raises C, flagged B)")
  for row in simulate(*CHOSEN, lone_their_press_is_b=False)["per_bad"]:
    print(f"  {row}")

  print("\n== resemblance: best score against another person's press, per healthy press")
  genuine = {p["id"]: statistics.median(s for f in feats[p["id"]] for s in f["agree"]) for p in gallery if p["id"] not in bad}
  for bar in RESEMBLANCE_BARS:
    print(f"  >= {bar}: {sum(f['cross'] >= bar for f in healthy)}/{len(healthy)} healthy presses")
  relative = sum(f["cross"] >= genuine[p["id"]] - MARGIN for p in gallery if p["id"] in genuine for f in feats[p["id"]])
  print(f"  within 15 of the print's median own score: {relative}/{len(healthy)}")
  print(f"  median own score per healthy print: {stats(list(genuine.values()))}")
  print(f"  the stray press's best other-person score: {max(stray[i]['cross'] for i in bad[args.stray])}")
  for p in old:
    print(f"  an old print's presses, best other-person score each: {[round(f['cross']) for f in feats[p['id']]]}")
  if args.scans:
    real_scans(args.scans, args.out, gallery, scores)


def real_scans(path: Path, out: Path, gallery: list[dict], scores: dict) -> None:  # noqa: PLR0915
  """Score the scan log's real scans against each registered press: which presses carry them, what each rule would cost."""
  rows = [
    dict(zip(("at", "clock", "result", "best", "probe"), line.split("|"), strict=True))
    for line in path.read_text(encoding="utf-8").splitlines()
    if line.strip()
  ]
  ids = {p["id"] for p in gallery}
  owner: list[int | None] = []  # the scan whose print is the person's: a match itself, a failure the next match
  for k, r in enumerate(rows):
    m = next(
      (
        m
        for m in range(k, len(rows))
        if rows[m]["clock"] == r["clock"] and rows[m]["result"] == "matched" and float(rows[m]["at"]) - float(r["at"]) <= SCAN_WINDOW
      ),
      None,
    )
    owner.append(m if m is not None and rows[m]["best"] in ids else None)
  cache = out / "scan_scores.json"
  if cache.exists():
    cached = json.loads(cache.read_text())
  else:
    probes = [FPrint.Print.deserialize(base64.b64decode(r["probe"])) for r in rows]
    prints = {p["id"]: FPrint.Print.deserialize(p["template"]) for p in gallery}
    both = lambda a, b: max(probes[a].score_press(0, probes[b])[0], probes[b].score_press(0, probes[a])[0])
    matched = [k for k, m in enumerate(owner) if m == k]
    cached = {
      "presses": [  # each attributed scan against each press of its person's print, both directions
        None
        if m is None
        else [
          max(s, prints[rows[m]["best"]].score_press(j, probes[k])[0])
          for j, s in enumerate(probes[k].score_press(0, prints[rows[m]["best"]]))
        ]
        for k, m in enumerate(owner)
      ],
      "next": [None if m is None or m == k else both(k, m) for k, m in enumerate(owner)],
      "same": [both(a, b) for a, b in itertools.combinations(matched, 2) if rows[a]["best"] == rows[b]["best"]],
    }
    cache.write_text(json.dumps(cached))
  presses, nxt = cached["presses"], cached["next"]
  pair = lambda pid, i, j: max(scores[pid]["own"][i][j], scores[pid]["own"][j][i])
  count = lambda pid: len(scores[pid]["own"])
  partners = lambda pid, bar: [sum(pair(pid, i, j) >= bar for j in range(count(pid)) if j != i) for i in range(count(pid))]

  def connected(pid: str, bar: int) -> set[int]:
    """The presses outside the largest group that pairs at or above `bar` link."""
    groups = [{i} for i in range(count(pid))]
    for i, j in itertools.combinations(range(count(pid)), 2):
      a, b = next(g for g in groups if i in g), next(g for g in groups if j in g)
      if a is not b and pair(pid, i, j) >= bar:
        a |= b
        groups.remove(b)
    return set(range(count(pid))) - max(groups, key=len)

  def floor(pid: str, low: int) -> set[int]:
    """The presses dropped, worst pair first, until every remaining pair scores at least `low`."""
    keep = set(range(count(pid)))
    while any(pair(pid, i, j) < low for i, j in itertools.combinations(keep, 2)):
      keep.remove(min(keep, key=lambda i: min(pair(pid, i, j) for j in keep - {i})))
    return set(range(count(pid))) - keep

  bar = CHOSEN[0][4]
  attributed = [k for k, m in enumerate(owner) if m is not None]
  failed = [k for k in attributed if owner[k] != k]
  of = lambda k: rows[owner[k]]["best"]
  print(
    f"\n== real scans: {len(rows)}; {len(attributed)} attributed to {len({of(k) for k in attributed})} prints, {len(failed)} of them failed"
    f" (each linked to the next match on its clock within {SCAN_WINDOW} s)"
  )
  print(
    f"  failed, under {GENUINE_BAR} against the print and the person's next scan (a poor press): "
    f"{sum(nxt[k] < GENUINE_BAR and max(presses[k]) < GENUINE_BAR for k in failed)}; matching the next scan but not the print "
    f"(a part of the finger the registration lacks): {sum(nxt[k] >= GENUINE_BAR > max(presses[k]) for k in failed)}"
  )
  print(f"  one person's matched scans against each other: {stats(cached['same'])}")
  reach = [k for k in attributed if max(presses[k]) >= GENUINE_BAR]
  carried = Counter(partners(of(k), bar)[presses[k].index(max(presses[k]))] for k in reach)
  everyone = Counter(c for pid in {of(k) for k in attributed} for c in partners(pid, bar))
  print(
    f"  scans reaching {GENUINE_BAR}, by the partners at {bar} of the press they score best on: {dict(sorted(carried.items()))};"
    f" those prints' presses: {dict(sorted(everyone.items()))}"
  )
  rules = [
    (f"{n} partner(s) at {b}", lambda pid, b=b, n=n: {i for i, c in enumerate(partners(pid, b)) if c < n})
    for b in (35, bar, 45, 50, 60)
    for n in (1, 2, 3)
  ]
  rules.append(
    (
      f"1 partner at {bar}, all connected at {bar}",
      lambda pid: {i for i, c in enumerate(partners(pid, bar)) if not c} | connected(pid, bar),
    )
  )
  rules += [(f"every pair at {low}", lambda pid, low=low: floor(pid, low)) for low in (10, 20, 30)]
  total = sum(count(p["id"]) for p in gallery)
  print(
    f"  per rule: presses re-asked of {total}, prints touched, scans of {len(reach)} reaching {GENUINE_BAR} only on a re-asked press"
  )
  for name, rule in rules:
    drops = {p["id"]: rule(p["id"]) for p in gallery}
    lost = sum(max((s for j, s in enumerate(presses[k]) if j not in drops[of(k)]), default=0) < GENUINE_BAR for k in reach)
    print(f"    {name:34} re-asked {sum(map(len, drops.values())):3}  prints {sum(map(bool, drops.values())):2}  lost {lost}")


if __name__ == "__main__":
  main()
