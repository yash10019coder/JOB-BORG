"""Measure the reworded-question matcher on the checked-in labelled pairs.

    manage.py question_match_eval                 # at the configured threshold
    manage.py question_match_eval --sweep         # recall / false merges per threshold
    manage.py question_match_eval --min-score 0.6 --json

No database, no network. Fails (non-zero) when the fixture is empty or malformed, or
when any near-miss pair is matched at the chosen threshold. Recall is reported, not
gated here (the test suite gates it).

The pairs prove only the classes they name. ``tuning`` is what thresholds and intent
wording are tuned on; ``held_out`` and ``fresh`` are only ever checked.
"""
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.accounts import question_matching as qm

FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "question_pairs.json"
SWEEP = (0.3, 0.4, 0.5, 0.6, 0.7, 0.8)
HALVES = ("tuning", "held_out", "fresh")


def load_pairs(path=FIXTURE):
    return json.loads(Path(path).read_text())


def check_pairs(data):
    """Raise ``ValueError`` for a missing half, an empty list or a malformed pair."""
    for half in HALVES:
        if half not in data:
            raise ValueError(f"missing half: {half}")
        for kind in ("reworded", "near_miss"):
            pairs = data[half].get(kind)
            if not pairs:
                raise ValueError(f"no pairs: {half}.{kind}")
            for pair in pairs:
                if not all(isinstance(pair.get(key), str) and pair[key].strip() for key in ("label", "a", "b")):
                    raise ValueError(f"malformed pair in {half}.{kind}: {pair!r}")


def evaluate(data, min_score):
    """``{half: {"recall": (hit, total), "missed": [...], "false_merges": [...]}}``."""
    out = {}
    for half in HALVES:
        reworded = data[half]["reworded"]
        misses = [p["label"] for p in reworded if not qm.match(p["a"], p["b"], min_score=min_score).matched]
        merges = [
            p["label"] for p in data[half]["near_miss"]
            if qm.match(p["a"], p["b"], min_score=min_score).matched
        ]
        out[half] = {
            "recall": (len(reworded) - len(misses), len(reworded)),
            "missed": misses,
            "false_merges": merges,
        }
    return out


def default_min_score():
    return getattr(settings, "QUESTION_CANDIDATE_MIN_SCORE", qm.DEFAULT_MIN_SCORE)


class Command(BaseCommand):
    help = "Recall and false-merge counts for the reworded-question matcher on the labelled pairs."

    def add_arguments(self, parser):
        parser.add_argument("--min-score", type=float, default=None, help="Trigram threshold (default: the setting).")
        parser.add_argument("--sweep", action="store_true", help="Also print recall/false merges per threshold.")
        parser.add_argument("--json", action="store_true", help="Print the result as JSON.")

    def handle(self, *args, **options):
        try:
            data = load_pairs()
            check_pairs(data)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            raise CommandError(f"labelled pairs unusable: {exc}")
        min_score = options["min_score"] if options["min_score"] is not None else default_min_score()
        result = evaluate(data, min_score)
        sweep = {t: evaluate(data, t) for t in SWEEP} if options["sweep"] else None

        if options["json"]:
            self.stdout.write(json.dumps({"min_score": min_score, "result": result}, indent=2, sort_keys=True))
        else:
            self.stdout.write(f"Threshold {min_score}")
            for half, row in result.items():
                hit, total = row["recall"]
                self.stdout.write(
                    f"  {half:9} recall {hit}/{total}  false merges {len(row['false_merges'])}"
                )
                for label in row["missed"]:
                    self.stdout.write(f"    missed: {label}")
                for label in row["false_merges"]:
                    self.stdout.write(f"    FALSE MERGE: {label}")
            if sweep:
                self.stdout.write("Sweep (recall hit/total, false merges) per threshold")
                for t, res in sweep.items():
                    hits = sum(r["recall"][0] for r in res.values())
                    total = sum(r["recall"][1] for r in res.values())
                    merges = sum(len(r["false_merges"]) for r in res.values())
                    self.stdout.write(f"  {t}: recall {hits}/{total}  false merges {merges}")
        if any(row["false_merges"] for row in result.values()):
            raise CommandError("a near-miss pair was matched")
