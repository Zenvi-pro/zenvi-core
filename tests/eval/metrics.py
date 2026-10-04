"""Pure scoring functions for the eval: how close a list of found times is to the known answer."""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple


def match_times(found: Sequence[float], truth: Sequence[float], tolerance: float) -> Tuple[List[Tuple[float, float]], List[float], List[float]]:
    """Pair each true time with the nearest unused found time inside *tolerance*.

    Returns (pairs as (found, truth), unmatched found, unmatched truth). Each found time serves at most one true time.
    """
    free = sorted(float(f) for f in found)
    pairs: List[Tuple[float, float]] = []
    missed: List[float] = []
    for t in sorted(float(x) for x in truth):
        best: Optional[int] = None
        for i, f in enumerate(free):
            if abs(f - t) <= tolerance and (best is None or abs(f - t) < abs(free[best] - t)):
                best = i
        if best is None:
            missed.append(t)
        else:
            pairs.append((free.pop(best), t))
    return pairs, free, missed


def prf(found: Sequence[float], truth: Sequence[float], tolerance: float) -> Dict[str, float]:
    """Precision, recall and F1 of found times against true times, plus the mean error of the matched ones."""
    pairs, extra, missed = match_times(found, truth, tolerance)
    tp = len(pairs)
    precision = tp / (tp + len(extra)) if (tp + len(extra)) else 1.0
    recall = tp / (tp + len(missed)) if (tp + len(missed)) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    err = sum(abs(f - t) for f, t in pairs) / tp if tp else float("nan")
    return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4), "mean_error": round(err, 4) if tp else None,
            "found": len(found), "truth": len(truth), "extra": len(extra), "missed": len(missed)}


def combine(*results: Dict[str, float]) -> Dict[str, float]:
    """Pool several prf results (counts add up) into one."""
    tp = sum(r["truth"] - r["missed"] for r in results)
    extra = sum(r["extra"] for r in results)
    missed = sum(r["missed"] for r in results)
    precision = tp / (tp + extra) if (tp + extra) else 1.0
    recall = tp / (tp + missed) if (tp + missed) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4), "extra": extra, "missed": missed,
            "truth": sum(r["truth"] for r in results), "found": sum(r["found"] for r in results)}


def edge_errors(found_spans: Sequence[Sequence[float]], true_spans: Sequence[Sequence[float]], max_gap: float = 1.0) -> Dict[str, Optional[float]]:
    """Start and end error (seconds) of each true span against the nearest found span that overlaps it or lies within *max_gap*."""
    starts, ends, missed = [], [], 0
    for ts, te in true_spans:
        near = [s for s in found_spans if s[1] >= ts - max_gap and s[0] <= te + max_gap]
        if not near:
            missed += 1
            continue
        best = min(near, key=lambda s: abs(s[0] - ts) + abs(s[1] - te))
        starts.append(abs(best[0] - ts))
        ends.append(abs(best[1] - te))
    def mean(xs):
        return round(sum(xs) / len(xs), 4) if xs else None
    return {"start_mae": mean(starts), "end_mae": mean(ends), "start_max": round(max(starts), 4) if starts else None,
            "end_max": round(max(ends), 4) if ends else None, "missed": missed, "truth": len(true_spans)}


def precision_at_k(ranked_ids: Sequence[str], relevant: Sequence[str], k: int) -> float:
    top = list(ranked_ids)[:k]
    return round(sum(1 for r in top if r in set(relevant)) / k, 4) if k else 0.0


def recall_at_k(ranked_ids: Sequence[str], relevant: Sequence[str], k: int) -> float:
    rel = set(relevant)
    return round(len(rel & set(list(ranked_ids)[:k])) / len(rel), 4) if rel else 1.0


def confusion(pred: Sequence[bool], truth: Sequence[bool]) -> Dict[str, float]:
    """Precision and recall of a yes/no flag."""
    tp = sum(1 for p, t in zip(pred, truth) if p and t)
    fp = sum(1 for p, t in zip(pred, truth) if p and not t)
    fn = sum(1 for p, t in zip(pred, truth) if not p and t)
    return {"precision": round(tp / (tp + fp), 4) if (tp + fp) else 1.0, "recall": round(tp / (tp + fn), 4) if (tp + fn) else 1.0,
            "tp": tp, "fp": fp, "fn": fn}


def rank_agreement(values: Sequence[float], expected_order: Sequence[int]) -> float:
    """Share of pairs whose order in *values* matches *expected_order* (0 = lowest expected): 1.0 is a perfect ordering."""
    n = len(values)
    pairs = agree = 0
    for i in range(n):
        for j in range(i + 1, n):
            if expected_order[i] == expected_order[j]:
                continue
            pairs += 1
            if (values[i] - values[j]) * (expected_order[i] - expected_order[j]) > 0:
                agree += 1
    return round(agree / pairs, 4) if pairs else 1.0
