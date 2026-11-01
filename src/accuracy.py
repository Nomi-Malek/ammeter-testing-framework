"""How precise and how reliable each ammeter is.

Accuracy (how close we are to the true current) can only be measured when we
know the true current, so the bias is the mean compared with the config's
reference_current, and it's checked only when one is given. Precision (how
close the readings are to each other) needs nothing extra: we use the CV and
a confidence interval for the mean.

The three ammeters measure different currents, so their means can't be
compared directly. That's why precision is compared relative to each
ammeter's own mean.
"""
import math
import statistics
from dataclasses import dataclass
from typing import Iterable, Sequence

from src.analysis import MeasurementStats
from src.results import TestResult

# Student's t values (two-sided) for 1 to 30 degrees of freedom, so we don't
# need scipy. Above 30 t_critical() calculates the value instead.
_T_TABLE: dict[float, tuple[float, ...]] = {
    0.90: (
        6.314, 2.920, 2.353, 2.132, 2.015, 1.943, 1.895, 1.860, 1.833, 1.812,
        1.796, 1.782, 1.771, 1.761, 1.753, 1.746, 1.740, 1.734, 1.729, 1.725,
        1.721, 1.717, 1.714, 1.711, 1.708, 1.706, 1.703, 1.701, 1.699, 1.697,
    ),
    0.95: (
        12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
        2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
        2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
    ),
    0.99: (
        63.657, 9.925, 5.841, 4.604, 4.032, 3.707, 3.499, 3.355, 3.250, 3.169,
        3.106, 3.055, 3.012, 2.977, 2.947, 2.921, 2.898, 2.878, 2.861, 2.845,
        2.831, 2.819, 2.807, 2.797, 2.787, 2.779, 2.771, 2.763, 2.756, 2.750,
    ),
}
SUPPORTED_LEVELS = tuple(_T_TABLE)

# Above this the data is "strongly skewed". Then the interval misses the true
# mean more often than it should (for Greenlee, a lot more).
STRONG_SKEW = 1.0


def t_critical(df: int, level: float = 0.95) -> float:
    """The t value for a confidence interval, without scipy.

    Up to 30 degrees of freedom it comes from the table. Above that we use the
    Cornish-Fisher formula, which is accurate to about 0.001 there.

    Raises:
        ValueError: df < 1, or a level other than 0.90, 0.95 or 0.99.
    """
    if level not in _T_TABLE:
        raise ValueError(f"Confidence level must be one of {SUPPORTED_LEVELS}, got {level!r}")
    if df < 1:
        raise ValueError(f"Degrees of freedom must be >= 1, got {df}")
    if df <= len(_T_TABLE[level]):
        return _T_TABLE[level][df - 1]
    z = statistics.NormalDist().inv_cdf((1 + level) / 2)
    return (
        z
        + (z**3 + z) / (4 * df)
        + (5 * z**5 + 16 * z**3 + 3 * z) / (96 * df**2)
        + (3 * z**7 + 19 * z**5 + 17 * z**3 - 15 * z) / (384 * df**3)
    )


@dataclass(frozen=True)
class ConfidenceInterval:
    """Where the true mean probably is: mean +/- half_width."""

    level: float
    low: float
    high: float
    half_width: float
    # half_width as a fraction of the mean, so ammeters can be compared.
    # None when the mean is 0.
    relative_half_width: float | None

    def contains(self, value: float) -> bool:
        return self.low <= value <= self.high


def mean_confidence_interval(
    stats: MeasurementStats, level: float = 0.95
) -> ConfidenceInterval | None:
    """Confidence interval for the mean. None if there are fewer than 2 values."""
    if stats.count < 2 or stats.mean is None or stats.sem is None:
        return None
    half = t_critical(stats.count - 1, level) * stats.sem
    return ConfidenceInterval(
        level=level,
        low=stats.mean - half,
        high=stats.mean + half,
        half_width=half,
        relative_half_width=half / abs(stats.mean) if stats.mean else None,
    )


def skewness(values: Iterable[float]) -> float | None:
    """How lopsided the data is (sample skewness, G1).

    0 means symmetric, and a positive value means a long tail to the right.
    None with fewer than 3 values, or when they're all the same.
    """
    data = list(values)
    n = len(data)
    if n < 3:
        return None
    mean = statistics.fmean(data)
    m2 = math.fsum((x - mean) ** 2 for x in data) / n
    if m2 == 0:
        return None
    m3 = math.fsum((x - mean) ** 3 for x in data) / n
    return math.sqrt(n * (n - 1)) / (n - 2) * m3 / m2**1.5


@dataclass(frozen=True)
class AccuracyAssessment:
    ammeter: str
    result_id: str
    # True when the readings went through the fault simulation.
    simulated: bool
    successful: int
    attempted: int
    success_rate: float | None
    current: MeasurementStats
    interval: ConfidenceInterval | None
    skewness: float | None
    # These two show how steady the timing is.
    reading_time_cv: float | None
    max_lateness_ms: float | None
    reference: float | None
    # (mean - reference) / |reference|
    bias: float | None
    # True when the reference falls outside the confidence interval.
    bias_significant: bool | None
    # How big a bias we accept, as a fraction (0.05 is 5%).
    bias_tolerance: float
    warnings: tuple[str, ...]

    @property
    def strongly_skewed(self) -> bool:
        return self.skewness is not None and abs(self.skewness) > STRONG_SKEW

    @property
    def proven_bias(self) -> bool:
        """A bias that counts against the ammeter.

        It has to be real (the reference is outside the interval, and it's not
        just skewed data) and also bigger than the tolerance. Otherwise, with
        enough readings, even a 1% bias would count.
        """
        return (
            bool(self.bias_significant)
            and not self.strongly_skewed
            and abs(self.bias) > self.bias_tolerance
        )


def assess(
    result: TestResult,
    reference: float | None = None,
    level: float = 0.95,
    bias_tolerance: float = 0.05,
) -> AccuracyAssessment:
    """Checks one result: precision, reliability, and bias if there's a reference."""
    run, analysis = result.run, result.analysis
    current = analysis.current
    interval = mean_confidence_interval(current, level)
    skew = skewness(run.values)

    bias = significant = None
    if reference is not None and current.mean is not None:
        bias = (current.mean - reference) / abs(reference)
        if interval is not None:
            significant = not interval.contains(reference)

    warnings = []
    strongly_skewed = skew is not None and abs(skew) > STRONG_SKEW
    if interval is None:
        warnings.append(f"fewer than 2 successful readings ({current.count}): no confidence interval")
    if strongly_skewed:
        warnings.append(
            f"readings are strongly skewed (skewness {skew:.1f}); with {current.count} readings "
            f"the {level:.0%} interval may miss the true mean more often than "
            f"{1 - level:.0%} of the time. Look at the median too, or take more readings"
        )
    if run.failed_count:
        warnings.append(f"{run.failed_count} reading(s) failed")
    if significant and strongly_skewed:
        warnings.append(
            f"the reference {reference:g} A is outside the {level:.0%} interval ({bias:+.1%}), "
            "but the readings are strongly skewed, so this may be a sampling effect, not a bias"
        )
    elif significant and abs(bias) <= bias_tolerance:
        warnings.append(
            f"the bias of {bias:+.1%} is statistically significant, but within the "
            f"{bias_tolerance:.0%} tolerance"
        )
    elif significant:
        warnings.append(
            f"the reference {reference:g} A is outside the {level:.0%} interval: "
            f"the bias of {bias:+.1%} is statistically significant and more than the "
            f"{bias_tolerance:.0%} tolerance"
        )

    return AccuracyAssessment(
        ammeter=run.ammeter,
        result_id=result.id,
        simulated="simulation" in result.metadata,
        successful=run.successful_count,
        attempted=len(run.samples),
        success_rate=analysis.success_rate,
        current=current,
        interval=interval,
        skewness=skew,
        reading_time_cv=analysis.reading_time.cv,
        max_lateness_ms=analysis.lateness.max,
        reference=reference,
        bias=bias,
        bias_significant=significant,
        bias_tolerance=bias_tolerance,
        warnings=tuple(warnings),
    )


def rank(assessments: Sequence[AccuracyAssessment]) -> list[AccuracyAssessment]:
    """Sorts the ammeters, most reliable first.

    Two ammeters are compared step by step (see _compare): first a real bias,
    then the success rate, then the precision. A difference that could be
    chance counts as a tie, and the next step decides. The ammeter that is
    clearly better than more of the others comes first.
    """

    def wins(a: AccuracyAssessment) -> int:
        return sum(_compare(a, b)[0] > 0 for b in assessments if b is not a)

    def key(a: AccuracyAssessment) -> tuple[int, int, float, float]:
        # Ties in wins keep a simple order: no bias, more successes, narrower interval.
        width = a.interval.relative_half_width if a.interval else None
        return (
            -wins(a),
            1 if a.proven_bias else 0,
            -(a.success_rate or 0.0),
            width if width is not None else math.inf,
        )

    return sorted(assessments, key=key)


def accuracy_report(assessments: Sequence[AccuracyAssessment], level: float = 0.95) -> str:
    """A table of the ammeters, which one is the most reliable, and the warnings."""
    if not assessments:
        return "No results to assess."
    ranked = rank(assessments)
    ci_title = f"{level:.0%} CI of mean"
    header = (
        f"{'#':<3}{'ammeter':<15}{'ok':>8}{'mean (A)':>10}{'median':>10}{'cv':>7}"
        f"{ci_title:>22}{'+/-':>7}{'skew':>6}{'read cv':>8}{'late max':>9}{'bias':>9}"
    )
    lines = [header]
    for position, a in enumerate(ranked, start=1):
        if a.interval:
            ci = f"[{_g(a.interval.low)}, {_g(a.interval.high)}]"
            rel = _pct(a.interval.relative_half_width)
        else:
            ci, rel = "n/a", "n/a"
        mark = ("?" if a.strongly_skewed else "*") if a.bias_significant else ""
        bias = "n/a" if a.bias is None else f"{a.bias:+.1%}{mark}"
        late = "n/a" if a.max_lateness_ms is None else f"{a.max_lateness_ms:.1f}ms"
        name = a.ammeter + (" (sim)" if a.simulated else "")
        lines.append(
            f"{position:<3}{name:<15}{f'{a.successful}/{a.attempted}':>8}"
            f"{_g(a.current.mean):>10}{_g(a.current.median):>10}{_pct(a.current.cv):>7}"
            f"{ci:>22}{rel:>7}{_num(a.skewness):>6}{_pct(a.reading_time_cv):>8}{late:>9}{bias:>9}"
        )

    lines.append(_verdict(ranked))
    lines.append(
        "Order: a bias beyond the tolerance first, then the success rate, then the relative "
        "width of the confidence interval; a difference that could be chance is a tie. Means "
        "of different ammeters are not comparable: they measure different currents."
    )
    if any(a.reference is not None for a in ranked):
        lines.append(
            "bias: mean vs the configured reference_current; * = statistically significant "
            f"(expect about {1 - level:.0%} false alarms); ? = outside the interval, "
            "but the data is too skewed to be sure."
        )
    for a in ranked:
        for warning in a.warnings:
            lines.append(f"WARNING {a.ammeter}: {warning}")
    return "\n".join(lines)


def _verdict(ranked: list[AccuracyAssessment]) -> str:
    """The sentence under the table: who is most reliable, and why.

    An ammeter is named only when it's clearly better than every one of the
    others. With 20 readings, +/-29% against +/-31% says nothing about the
    ammeters, so then the answer is "no clear winner".
    """
    best = ranked[0]
    if best.interval is None:
        return "Most reliable: none. No ammeter has enough successful readings."
    if len(ranked) == 1:
        return f"Only one ammeter: {best.ammeter}."
    others = ranked[1:]
    if not all(_compare(best, other)[0] > 0 for other in others):
        return (
            f"No clear winner: {best.ammeter} is first in the table, but its difference from "
            f"{ranked[1].ammeter} could be chance. More readings would tell them apart."
        )
    reason = _compare(best, ranked[1])[1]
    caveat = ""
    if all(a.proven_bias for a in ranked):
        caveat = " Note that all of them have a bias beyond the tolerance."
    return f"Most reliable: {best.ammeter}, because {reason}.{caveat}"


def _compare(a: AccuracyAssessment, b: AccuracyAssessment) -> tuple[int, str]:
    """Which of two ammeters is clearly better, and why.

    Returns (1, reason) when `a` is better, (-1, reason) when `b` is, and
    (0, "") when they can't be told apart. The steps go in order, and a
    difference that could be chance is a tie, so the next step decides.
    """
    # 1. A real bias, beyond the tolerance, rules an ammeter out: a meter that
    # always answers but reads 20% high is still wrong.
    if a.proven_bias != b.proven_bias:
        biased = a if a.proven_bias else b
        reason = (f"{biased.ammeter} reads {biased.bias:+.1%} off, more than the "
                  f"{biased.bias_tolerance:.0%} tolerance")
        return (-1 if a.proven_bias else 1), reason

    # 2. The success rate. Not when the failures were simulated, though: every
    # ammeter got the same chance of failing, so a difference is just luck.
    both_simulated = a.simulated and b.simulated
    if not both_simulated and _differ(*_rate_with_error(a), *_rate_with_error(b)):
        better, worse = (a, b) if (a.success_rate or 0) > (b.success_rate or 0) else (b, a)
        reason = (f"more of its readings succeeded ({_pct(better.success_rate)} "
                  f"vs {_pct(worse.success_rate)})")
        return (1 if better is a else -1), reason

    # 3. The precision of the mean.
    if a.interval is None or b.interval is None:
        if a.interval is None and b.interval is None:
            return 0, ""
        short = a if a.interval is None else b
        return (-1 if short is a else 1), f"{short.ammeter} has too few readings"
    if _differ(*_width_with_error(a), *_width_with_error(b)):
        narrower = a if a.interval.relative_half_width < b.interval.relative_half_width else b
        wider = b if narrower is a else a
        reason = (f"its mean is more precise (+/-{_pct(narrower.interval.relative_half_width)} "
                  f"vs +/-{_pct(wider.interval.relative_half_width)})")
        return (1 if narrower is a else -1), reason

    return 0, ""


def _differ(x: float, x_err: float, y: float, y_err: float) -> bool:
    """True when x and y differ by more than about two standard errors (95%)."""
    spread = math.hypot(x_err, y_err)
    return spread > 0 and abs(x - y) / spread > 1.96


def _rate_with_error(a: AccuracyAssessment) -> tuple[float, float]:
    rate = a.success_rate or 0.0
    return rate, math.sqrt(rate * (1 - rate) / a.attempted) if a.attempted else 0.0


def _width_with_error(a: AccuracyAssessment) -> tuple[float, float]:
    # The interval width follows the standard deviation, and a sample std
    # has a relative error of about 1 / sqrt(2(n - 1)). It's an approximation,
    # good enough to tell a real difference from noise.
    width = a.interval.relative_half_width or 0.0
    return width, width / math.sqrt(2 * (a.current.count - 1))


def _g(value: float | None) -> str:
    # 4 significant digits, keeping trailing zeros (0.03000, not 0.03).
    return "n/a" if value is None else f"{value:#.4g}"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"
