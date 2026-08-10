"""
The money engine.

Two independent pots, sized off DIFFERENT headcounts because guests only buy
into one of them:

  team pot   = $5 x (full members who teed off), per side, so $10 each in total
  skat pot   = $10 x (everyone who teed off, guests included)

Worked through: 11 members and 1 guest puts $230 in the pot. $110 of it is team
money, $55 a side. The other $120 is the skat pot - $110 from the members plus
the guest's $10. Ten members and two guests: $220 in, $100 team ($50 a side),
$120 skats.

A guest is a player with too little history for a quota. They pay $10, play for
greens and skins, and are invisible to team play: their points don't count
toward their team's total, their (non-existent) quota isn't in the team's Xi,
and they can't collect if the team wins.

Because a guest's points never matter, they are usually not written down at all.
So whether somebody played is an EXPLICIT fact on the entry, never inferred from
whether points are present - a blank guest row means "played, nothing to record",
not "no-show".

A side is won by the team with the highest (team points minus team quota).
Ties split the side pot across every eligible player on every tied team.

The skat rule was validated against the Access report dated 18-Jul-26 and
reproduces the published skat money for all 44 members to the dollar.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from cartel_config import RULES, SIDES, HouseRules, Stake
from cartel_quota import team_side_quota


# --------------------------------------------------------------------------
# inputs
# --------------------------------------------------------------------------

@dataclass
class PlayerEntry:
    """One player's line on the scoresheet."""
    name: str
    team_no: int
    quota: int | None = None          # None means guest
    points_front: int | None = None
    points_back: int | None = None
    score: int | None = None
    greens: int = 0
    skins: int = 0
    is_guest: bool = False
    played: bool = True               # explicit: untick for a genuine no-show

    def __post_init__(self):
        # the two ways of saying "guest" must never disagree
        if self.quota is None:
            self.is_guest = True
        elif self.is_guest:
            self.quota = None

    @property
    def has_points(self) -> bool:
        return self.points_front is not None and self.points_back is not None

    @property
    def counts(self) -> bool:
        """
        In the round at all, so paying an ante and eligible for skats.

        A guest counts on the strength of `played` alone - nobody writes down a
        guest's points. A member needs points, because without them there is
        nothing to add to their team; a member marked played with no points is
        a data problem and gets flagged rather than silently absorbed.
        """
        if not self.played:
            return False
        return True if self.is_guest else self.has_points

    @property
    def plays_team(self) -> bool:
        return self.counts and not self.is_guest

    @property
    def points_total(self) -> int:
        return (self.points_front or 0) + (self.points_back or 0)

    @property
    def skats(self) -> int:
        return self.greens + self.skins

    def points_for(self, side: str) -> int:
        return (self.points_front if side == "front" else self.points_back) or 0

    def ante(self, stake=None) -> float:
        stake = stake or RULES.default_stake()
        return stake.ante_for(self.is_guest) if self.counts else 0.0


# --------------------------------------------------------------------------
# outputs
# --------------------------------------------------------------------------

@dataclass
class SideOutcome:
    side: str
    team_no: int
    players: list[str]          # eligible players only
    points: int
    quota: float
    net: float
    is_winner: bool
    payout_per_player: float


@dataclass
class PlayerPayout:
    name: str
    team_no: int
    is_guest: bool = False
    team_money: float = 0.0
    skat_money: float = 0.0
    greens: int = 0
    skins: int = 0
    ante: float = 0.0

    @property
    def total(self) -> float:
        return self.team_money + self.skat_money

    @property
    def net(self) -> float:
        return self.total - self.ante


@dataclass
class RoundResult:
    n_players: int              # everyone who teed off
    n_team_players: int         # full members who teed off
    n_guests: int
    pot_per_side: float
    skat_pot: float
    total_skats: int
    skat_value: float
    carry_in: float = 0.0
    stake: Stake = field(default_factory=lambda: RULES.default_stake())
    sides: list[SideOutcome] = field(default_factory=list)
    payouts: dict[str, PlayerPayout] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    carried_money: float = 0.0

    @property
    def total_collected(self) -> float:
        return sum(p.ante for p in self.payouts.values())

    @property
    def total_paid(self) -> float:
        return sum(p.total for p in self.payouts.values())

    def winners(self, side: str) -> list[SideOutcome]:
        return [s for s in self.sides if s.side == side and s.is_winner]

    def check_balance(self, tol: float = 0.01) -> None:
        """Every dollar in, plus anything carried in, comes out or carries on."""
        diff = (self.total_collected + self.carry_in) - self.total_paid - self.carried_money
        if abs(diff) > tol:
            raise AssertionError(
                f"Books don't balance: collected ${self.total_collected:.2f} "
                f"+ ${self.carry_in:.2f} carried in, paid ${self.total_paid:.2f}, "
                f"${self.carried_money:.2f} carried on (off by ${diff:.2f})"
            )


# --------------------------------------------------------------------------
# the engine
# --------------------------------------------------------------------------

def score_round(
    entries: list[PlayerEntry],
    rules: HouseRules = RULES,
    carry_in: float = 0.0,
    stake: Stake | None = None,
) -> RoundResult:
    """
    Turn a filled-in scoresheet into money.

    `stake` is what THIS round cost, captured when it was prepared. Passing it
    in rather than reading today's rate is what lets the stake change without
    disturbing a round already played.
    """
    stake = stake or rules.default_stake()
    played = [e for e in entries if e.counts]
    no_shows = [e.name for e in entries if not e.played]
    missing_points = [e.name for e in entries
                      if e.played and not e.is_guest and not e.has_points]
    team_players = [e for e in played if e.plays_team]
    guests = [e for e in played if e.is_guest]

    if not played:
        raise ValueError("Nobody played - nothing to settle.")

    result = RoundResult(
        n_players=len(played),
        n_team_players=len(team_players),
        n_guests=len(guests),
        stake=stake,
        pot_per_side=stake.team_per_side * len(team_players),
        # The sum of what people actually PAID in: half of each member's ante
        # plus the whole of each guest's. Sizing it on a member's share times
        # everybody would credit a guest with money they never put in, the
        # moment the two antes stop being equal.
        skat_pot=(stake.skat_per_member * len(team_players)
                  + stake.guest_ante * len(guests) + carry_in),
        total_skats=0,
        skat_value=0.0,
        carry_in=carry_in,
    )

    for e in played:
        result.payouts[e.name] = PlayerPayout(
            name=e.name, team_no=e.team_no, is_guest=e.is_guest,
            greens=e.greens, skins=e.skins, ante=e.ante(stake),
        )

    if no_shows:
        result.warnings.append(
            f"Marked as not playing, so out of every pot and out of their team's "
            f"quota: {', '.join(no_shows)}"
        )
    if missing_points:
        result.warnings.append(
            f"Marked as playing but with no points recorded, so left out of the "
            f"round entirely: {', '.join(missing_points)}. Enter their points, or "
            f"untick Played if they were a no-show."
        )
    if guests:
        result.warnings.append(
            f"Playing as guest(s) for greens and skins only - "
            f"${stake.guest_ante:.0f} each into the skat pot, no team money either "
            f"way: {', '.join(g.name for g in guests)}"
        )

    _validate_teams(team_players, guests, rules, result)
    _settle_team_money(team_players, rules, result, stake)
    _settle_skat_money(played, rules, result, stake)

    result.check_balance()
    return result


def _validate_teams(team_players, guests, rules: HouseRules, result: RoundResult) -> None:
    teams: dict[int, list[PlayerEntry]] = {}
    for e in team_players:
        teams.setdefault(e.team_no, []).append(e)

    if not teams:
        result.warnings.append(
            "Nobody eligible for team play - the whole field is guests or no-shows, "
            "so there is no team money this round."
        )
        return
    if len(teams) < 2:
        result.warnings.append(
            "Only one team has eligible players, so there is nothing to win on "
            "either side. Their team money is returned."
        )

    for team_no, members in sorted(teams.items()):
        n_guests = sum(1 for g in guests if g.team_no == team_no)
        total = len(members) + n_guests
        if not (rules.min_team_size <= total <= rules.max_team_size):
            result.warnings.append(
                f"Team {team_no} played {total} player(s), outside the usual "
                f"{rules.min_team_size}-{rules.max_team_size}."
            )
        if n_guests:
            result.warnings.append(
                f"Team {team_no} is {len(members)} eligible player(s) plus {n_guests} "
                f"guest(s). Only the eligible ones count toward its points and quota."
            )

    sizes = {len(m) for m in teams.values()}
    if len(sizes) > 1:
        result.warnings.append(
            "Uneven teams this round ("
            + ", ".join(f"T{t}={len(m)}" for t, m in sorted(teams.items()))
            + "). Quotas scale with team size so the contest is fair, but the side pot "
            "splits per player, so a winning smaller team takes more each."
        )


def _settle_team_money(team_players, rules: HouseRules, result: RoundResult,
                       stake: Stake) -> None:
    teams: dict[int, list[PlayerEntry]] = {}
    for e in team_players:
        teams.setdefault(e.team_no, []).append(e)

    if not teams:
        return

    # Nobody to play against: hand the team money back rather than inventing a winner.
    if len(teams) == 1:
        only = next(iter(teams.values()))
        for side in SIDES:
            pts = sum(m.points_for(side) for m in only)
            quota = team_side_quota([m.quota for m in only])
            result.sides.append(SideOutcome(
                side=side, team_no=only[0].team_no, players=[m.name for m in only],
                points=pts, quota=quota, net=pts - quota, is_winner=False,
                payout_per_player=0.0,
            ))
        for m in only:
            result.payouts[m.name].team_money += 2 * stake.team_per_side
        return

    for side in SIDES:
        rows = []
        for team_no, members in sorted(teams.items()):
            pts = sum(m.points_for(side) for m in members)
            quota = team_side_quota([m.quota for m in members])
            rows.append({
                "team_no": team_no,
                "players": [m.name for m in members],
                "points": pts,
                "quota": quota,
                "net": pts - quota,
            })

        best = max(r["net"] for r in rows)
        winning = [r for r in rows if abs(r["net"] - best) < 1e-9]
        n_winning_players = sum(len(r["players"]) for r in winning)
        per_player = result.pot_per_side / n_winning_players

        if len(winning) > 1:
            result.warnings.append(
                f"{side.title()} side tied at {best:+.1f} between teams "
                f"{', '.join(str(r['team_no']) for r in winning)} - "
                f"${result.pot_per_side:.2f} split {n_winning_players} ways."
            )

        for r in rows:
            is_winner = r in winning
            result.sides.append(SideOutcome(
                side=side, team_no=r["team_no"], players=r["players"],
                points=r["points"], quota=r["quota"], net=r["net"],
                is_winner=is_winner,
                payout_per_player=per_player if is_winner else 0.0,
            ))
            if is_winner:
                for name in r["players"]:
                    result.payouts[name].team_money += per_player


def _settle_skat_money(played, rules: HouseRules, result: RoundResult,
                       stake: Stake) -> None:
    total_skats = sum(e.skats for e in played)
    result.total_skats = total_skats

    if total_skats == 0:
        result.skat_value = 0.0
        if rules.no_skat_policy == "carry":
            result.carried_money += result.skat_pot
            result.warnings.append(
                f"No greens and no skins this round - ${result.skat_pot:.2f} carried "
                f"forward to the next round."
            )
        else:
            # Refund what each player actually put into the skat pot: half a
            # member's ante, the whole of a guest's. A flat share would hand a
            # guest money they never staked.
            for e in played:
                result.payouts[e.name].skat_money += (
                    stake.guest_ante if e.is_guest else stake.skat_per_member)
            result.warnings.append(
                f"No greens and no skins this round - everyone refunded what they "
                f"put in (${stake.skat_per_member:,.2f} a member, "
                f"${stake.guest_ante:,.2f} a guest)."
            )
        return

    result.skat_value = result.skat_pot / total_skats
    for e in played:
        if e.skats:
            result.payouts[e.name].skat_money += e.skats * result.skat_value


# --------------------------------------------------------------------------
# entry checks
# --------------------------------------------------------------------------

# Hard limits: values outside these cannot be typed at all. Set well beyond
# anything ever recorded, so they only ever catch a slipped keystroke.
HARD_LIMITS = {
    "points": (0, 40),
    "score": (50, 140),
    "greens": (0, RULES.par_threes + 1),
    # One skin per hole, so a single player could theoretically win all 18.
    # Vanishingly unlikely, but a limit set from the historical trend rather
    # than from what the course allows will one day block a legitimate entry.
    "skins": (0, RULES.holes),
}

# Soft limits: plausible but unusual. Entry is allowed, but must be confirmed.
# Chosen against 2,982 real entries so they fire on the rare thing and not on
# an ordinary bad nine.
SOFT_POINTS_LOW = 1       # 0 happens - six times on record, all with 99+ scores
SOFT_POINTS_HIGH = 27     # 28+ has happened five times in four years
SOFT_SCORE_LOW = 65
SOFT_SCORE_HIGH = 109     # 100-109 is a regular bad day; 110+ is worth a look
SOFT_SKINS_HIGH = 10      # possible up to 18, but 11+ warrants reading the sheet again


@dataclass
class EntryQuery:
    """One thing worth a second look before the round is posted."""
    name: str
    field: str
    value: object
    message: str


def check_entries(entries: list[PlayerEntry]) -> list[EntryQuery]:
    """
    Flag entries that are possible but unusual.

    Nothing here blocks a round. Every one of these has a legitimate reading -
    a 0 on a side is a blow-up, 28 points is a career nine - so the answer is
    to ask, not to refuse. The two real errors in four years of history were
    both a points total typed into the Score column, which the score check
    catches.
    """
    queries: list[EntryQuery] = []
    for e in entries:
        if not e.counts or e.is_guest:
            continue          # a guest's points are never recorded

        for side, value in (("front", e.points_front), ("back", e.points_back)):
            if value is None:
                continue
            if value < SOFT_POINTS_LOW:
                queries.append(EntryQuery(
                    e.name, f"{side} points", value,
                    f"{value} points on the {side}. Possible on a blow-up, but "
                    f"check it isn't a blank or a misread."))
            elif value > SOFT_POINTS_HIGH:
                queries.append(EntryQuery(
                    e.name, f"{side} points", value,
                    f"{value} points on the {side} is exceptional - only five "
                    f"rounds over {SOFT_POINTS_HIGH} in four years. Confirm it."))

        if e.score is not None and e.score > 0:
            if e.score < SOFT_SCORE_LOW:
                queries.append(EntryQuery(
                    e.name, "score", e.score,
                    f"A score of {e.score} is very low. The two wrong entries in "
                    f"the whole history were both a points total typed into the "
                    f"Score column - is that what happened?"))
            elif e.score > SOFT_SCORE_HIGH:
                queries.append(EntryQuery(
                    e.name, "score", e.score,
                    f"A score of {e.score} is high enough to be worth confirming."))

            total = e.points_total
            if total >= 30 and e.score > 95:
                queries.append(EntryQuery(
                    e.name, "score", e.score,
                    f"{total} points against a score of {e.score} don't hang "
                    f"together - a good day usually means a low score."))
            elif 0 < total <= 12 and e.score < 80:
                queries.append(EntryQuery(
                    e.name, "score", e.score,
                    f"{total} points against a score of {e.score} don't hang "
                    f"together - a low score usually means points."))

        if e.skins > SOFT_SKINS_HIGH:
            queries.append(EntryQuery(
                e.name, "skins", e.skins,
                f"{e.skins} skins is possible - there are {RULES.holes} holes - but "
                f"nothing near it has ever been recorded. Check every player's "
                f"skins on the sheet, not just this one: a misread column usually "
                f"shows up in more than one row."))

    queries.extend(_check_the_field(entries))
    return queries


def _check_the_field(entries: list[PlayerEntry]) -> list[EntryQuery]:
    """
    Checks that only make sense across the whole round.

    These are the strong ones. A player's own total is rarely wrong on its own,
    but the field's total is bounded by arithmetic: one skin per hole, one green
    per par 3. Nobody can win a hole two people also won.
    """
    played = [e for e in entries if e.counts]
    if not played:
        return []

    out: list[EntryQuery] = []
    field_skins = sum(e.skins for e in played)
    field_greens = sum(e.greens for e in played)

    if field_skins > RULES.holes:
        out.append(EntryQuery(
            "The whole field", "skins", field_skins,
            f"{field_skins} skins across the field, but there are only "
            f"{RULES.holes} holes and a hole yields at most one skin. At least "
            f"{field_skins - RULES.holes} of these cannot be right."))

    if field_greens > RULES.par_threes:
        out.append(EntryQuery(
            "The whole field", "greens", field_greens,
            f"{field_greens} greens across the field, but the course has "
            f"{RULES.par_threes} par 3s and each yields one green. Either a green "
            f"is recorded twice, or this course has more par 3s than the app "
            f"expects (set par_threes in the house rules)."))
    return out
