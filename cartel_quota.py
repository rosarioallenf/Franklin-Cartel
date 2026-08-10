"""
Points quota, and the guest test.

Quota is the rounded average of a player's most recent N rounds. Validated
against the report dated 18-Jul-26: this rule reproduces the published quota
for all 44 members, exactly.

A player with fewer than `guest_min_rounds` rounds on file has no meaningful
quota, so they don't get one. They play as a guest: greens and skins only.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, ROUND_HALF_EVEN

from cartel_config import RULES, HouseRules


def _round(value: float, mode: str) -> int:
    if mode == "none":
        return int(value)
    rounding = ROUND_HALF_UP if mode == "half_up" else ROUND_HALF_EVEN
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=rounding))


@dataclass
class QuotaResult:
    name: str
    quota: int | None          # None for a guest
    rounds_available: int      # total rounds on file, not just the window
    rounds_used: int           # how many went into the average
    raw_average: float | None
    is_guest: bool
    note: str = ""
    provisional_quota: int | None = None   # a guest's quota-so-far, for display
    provisional_rounds: int = 0            # how many scored rounds that rests on

    @property
    def short_history(self) -> bool:
        """Has a quota, but from fewer than a full window of rounds."""
        return not self.is_guest and self.rounds_used < RULES.quota_window

    @property
    def needs_review(self) -> bool:
        return self.is_guest or self.short_history

    @property
    def display(self) -> str:
        return "guest" if self.is_guest else str(self.quota)


def compute_quota(
    name: str,
    recent_points: list[int],
    rules: HouseRules = RULES,
    rounds_available: int | None = None,
) -> QuotaResult:
    """
    recent_points: that player's total points (front + back) for their most
    recent rounds, MOST RECENT FIRST.

    rounds_available: their total rounds on file. Defaults to len(recent_points),
    which is right when the caller passed everything; pass it explicitly when
    you only fetched the window.
    """
    available = rounds_available if rounds_available is not None else len(recent_points)

    if available < rules.guest_min_rounds:
        # Work out the quota-so-far purely so the roster can show progress. It is
        # never applied - promotion happens on its own once the rounds are there.
        window = recent_points[: rules.quota_window]
        provisional = _round(sum(window) / len(window), rules.quota_rounding) if window else None
        note = (f"Only {available} round(s) on file, fewer than the "
                f"{rules.guest_min_rounds} needed for a quota. Plays as a guest: "
                f"${rules.guest_ante:.0f} in, greens and skins only, no team money "
                f"either way.")
        if provisional is not None:
            remaining = rules.guest_min_rounds - available
            note += (f" {available} scored round(s) so far, averaging {provisional} - "
                     f"{remaining} more and they play as a full member automatically.")
        else:
            note += (" No points recorded for them at all, so there is nothing to "
                     "base a quota on - write their points down next time.")
        return QuotaResult(
            name, None, available, 0, None, True, note,
            provisional_quota=provisional, provisional_rounds=len(window),
        )

    window = recent_points[: rules.quota_window]
    if not window:
        raise ValueError(
            f"{name} has {available} rounds on file but no points were supplied."
        )

    avg = sum(window) / len(window)
    quota = _round(avg, rules.quota_rounding)

    note = ""
    if len(window) < rules.quota_window:
        note = (f"Quota from {len(window)} round(s) rather than a full "
                f"{rules.quota_window}.")

    return QuotaResult(name, quota, available, len(window), avg, False, note)


def team_side_quota(player_quotas: list[int | None]) -> float:
    """
    The team's quota for ONE side: the sum of its members' quotas, halved.
    This is the 'Team Points/side' figure on the paper scoresheet.

    Guests (quota None) are dropped, and so are players who didn't tee off —
    the caller passes only those who count. On Round 60, Team 1 was posted with
    four names but Casey Kennedy is a guest who never played, so the quota was
    35+27+27 = 89 -> 44.5, exactly what the sheet shows.
    """
    real = [q for q in player_quotas if q is not None]
    return sum(real) / 2.0
