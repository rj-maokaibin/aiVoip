from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DtmfSource:
    source: str
    digits: str | None
    available: bool = True


@dataclass(frozen=True)
class DtmfFusionResult:
    consensus: str | None
    status: str
    mismatches: tuple[dict, ...]
    sources: dict[str, str | None]


class DtmfFusion:
    @staticmethod
    def fuse(*sources: DtmfSource) -> DtmfFusionResult:
        values = {s.source: s.digits for s in sources if s.available}
        non_null = [(k, v) for k, v in values.items() if v is not None]
        if not non_null:
            return DtmfFusionResult(None, "NO_SIGNAL", (), values)
        counts: dict[str, int] = {}
        for _, value in non_null:
            counts[value] = counts.get(value, 0) + 1
        consensus = max(counts.items(), key=lambda x: (x[1], len(x[0])))[0]
        mismatches = []
        for source, digits in non_null:
            if digits == consensus:
                continue
            first = 0
            limit = min(len(digits), len(consensus))
            while first < limit and digits[first] == consensus[first]:
                first += 1
            mismatches.append({
                "source": source,
                "digits": digits,
                "consensus": consensus,
                "first_divergence_index": first,
            })
        status = "CONSISTENT" if not mismatches else "DIVERGENT"
        return DtmfFusionResult(consensus, status, tuple(mismatches), values)
