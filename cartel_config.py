"""
Cartel house rules, in one place.

Every number the group could ever argue about lives here, not scattered
through the code. Change it here and the whole app follows.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, asdict


# The split of a member's ante is fixed by the house rules and never varies:
# a quarter to the front, a quarter to the back, a half to greens and skins.
# Only the SIZE of the ante changes.
FRONT_SHARE = 0.25
BACK_SHARE = 0.25
SKAT_SHARE = 0.50


@dataclass(frozen=True)
class Stake:
    """
    What a round costs. Stored per round, so changing the stake never disturbs
    a round already played.

    The split is derived, not configured. Three numbers that must be kept
    consistent by hand is three chances to get it wrong; one number that drives
    the rest cannot disagree with itself.
    """
    member_ante: float = 20.00
    guest_ante: float = 10.00

    @property
    def team_per_side(self) -> float:
        """A quarter of the member ante, to each of the front and the back."""
        return self.member_ante * FRONT_SHARE

    @property
    def team_per_member(self) -> float:
        return self.member_ante * (FRONT_SHARE + BACK_SHARE)

    @property
    def skat_per_member(self) -> float:
        """Half the member ante goes to greens and skins."""
        return self.member_ante * SKAT_SHARE

    def ante_for(self, is_guest: bool) -> float:
        return self.guest_ante if is_guest else self.member_ante

    def validate(self) -> None:
        if self.member_ante <= 0:
            raise ValueError("The member ante must be more than nothing.")
        if self.guest_ante <= 0:
            raise ValueError(
                "The guest ante must be more than nothing - a guest who pays "
                "nothing and can still win skats is a giveaway.")
        if self.guest_ante < self.member_ante / 2 - 1e-9:
            raise ValueError(
                f"House rule: a guest never pays less than half the member ante. "
                f"${self.guest_ante:,.2f} is under half of ${self.member_ante:,.2f}.")

    def describe(self) -> str:
        return (f"${self.member_ante:,.2f} a member "
                f"(${self.team_per_side:,.2f} a side, "
                f"${self.skat_per_member:,.2f} skats), "
                f"${self.guest_ante:,.2f} a guest")


# What a round cost before the app existed. Every imported round is settled at
# this rate, because that is what the group was actually playing for.
LEGACY_STAKE = Stake(member_ante=20.00, guest_ante=10.00)


@dataclass(frozen=True)
class HouseRules:
    # ---- money ----
    # The DEFAULT stake, used when a round carries none of its own. The live
    # figure lives in the database and is set on the Health tab.
    ante_per_player: float = 20.00
    guest_ante: float = 10.00

    # ---- guests ----
    # A player with too little history to have a meaningful quota plays for
    # greens and skins only: their whole ante goes to the skat pot, they are
    # not part of any team's quota, and they cannot win team money.
    guest_min_rounds: int = 3             # fewer rounds than this -> guest

    # ---- quota ----
    quota_window: int = 5                 # average the N most recent rounds
    quota_rounding: str = "half_up"       # "half_up" (Excel-style) | "half_even" | "none"

    # ---- standings ranking ----
    # Set by the Cartel Stats Admin, Aug 2026. A rank on $ per round is only
    # meaningful over a decent sample of recent play: two rounds, one of them
    # two years old, produced a 2nd place that told nobody anything.
    rank_min_rounds: int = 5          # rounds needed to appear in the ranking
    rank_window_months: int = 12      # ...within this many months of the last round

    # ---- results entry ----
    # Reading the paper scoresheet from a photo. Switched OFF by decision of the
    # Cartel Stats Admin, Aug 2026: it saves perhaps ninety seconds of typing and
    # gives up control over who writes what and how, which is a poor trade on the
    # one step where an error becomes somebody's money.
    #
    # This is the deciding switch. Even with an API key present the panel stays
    # hidden, so it cannot be turned on by accident or by anyone else.
    photo_prefill_enabled: bool = False

    # ---- what the course makes possible ----
    # Limits should reflect what CAN happen, not what has happened. One skin per
    # hole means a single player could theoretically take all 18; the field
    # collectively can never take more than 18 either. Greens are closest-to-pin
    # on a par 3, so the ceiling is the number of par 3s.
    holes: int = 18
    par_threes: int = 4          # 4 on both the North and the South

    # ---- teams ----
    min_team_size: int = 2
    max_team_size: int = 5

    # ---- edge cases ----
    # What to do with the skat pot when a round produces zero greens and skins.
    no_skat_policy: str = "carry"         # "carry" | "refund"

    def default_stake(self) -> Stake:
        return Stake(member_ante=self.ante_per_player, guest_ante=self.guest_ante)

    def validate(self) -> None:
        self.default_stake().validate()
        if self.rank_min_rounds < 1:
            raise ValueError("rank_min_rounds must be at least 1")
        if self.rank_window_months < 1:
            raise ValueError("rank_window_months must be at least 1")
        if self.quota_window < 1:
            raise ValueError("quota_window must be at least 1")
        if self.guest_min_rounds < 0:
            raise ValueError("guest_min_rounds can't be negative")
        if self.quota_rounding not in ("half_up", "half_even", "none"):
            raise ValueError(f"unknown quota_rounding: {self.quota_rounding}")
        if self.no_skat_policy not in ("carry", "refund"):
            raise ValueError(f"unknown no_skat_policy: {self.no_skat_policy}")

    def as_dict(self) -> dict:
        return asdict(self)


RULES = HouseRules()
RULES.validate()

DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cartel.db")


def db_path() -> str:
    """
    Where the SQLite file lives, read fresh each time.

    Resolved at call time rather than import time so that setting CARTEL_DB
    after the package is imported still works - which is how the tests drive it,
    and how anyone launching the app from a wrapper script would expect it to
    behave.
    """
    return os.environ.get("CARTEL_DB") or DEFAULT_DB_PATH


# Kept for callers that want a plain value; prefer db_path() where the
# environment might change.
DB_PATH = db_path()

TEE_CODES = {"B": 1, "W": 3, "W-G": 4, "G": 5}
SIDES = ("front", "back")
