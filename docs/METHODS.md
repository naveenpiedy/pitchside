# How pitchside calculates things

Every formula behind the numbers, what slice of data each piece is estimated on, and a worked example with real IPL numbers. Examples come from the database as built on 2026-10-22 (IPL 2008–2026); they will drift slightly as new matches arrive.

**Contents**

1. [How data is sliced](#1-how-data-is-sliced)
2. [Counting rules](#2-counting-rules)
3. [Standard metrics](#3-standard-metrics)
4. [Net stats](#4-net-stats)
5. [Small-sample shrinkage](#5-small-sample-shrinkage-_shrunk)
6. [Win probability and WPA](#6-win-probability-and-wpa)
7. [Entry point](#7-entry-point)
8. [Archetypes](#8-archetypes)
9. [Team and venue numbers](#9-team-and-venue-numbers)
10. [Pre-match brief](#10-pre-match-brief)
11. [Summary: what is estimated where](#11-summary-what-is-estimated-where)

---

## 1. How data is sliced

There are two kinds of calculation, and the difference matters for reading any number.

**Models, fitted once per competition when data loads.** The net-stat baselines, the shrinkage priors and the win-probability models are each estimated from one competition's full history (all IPL seasons for the IPL) and stored in that competition's database. They are rebuilt on every load or sync, never per query. The IPL is judged against the IPL; a BBL, once loaded, against the BBL.

**Metrics, computed per query over whatever slice you ask for.** Every ball already carries its expectations (expected runs, expected dismissal, win probability before and after) from the models above. A metric is then a sum over the balls matching your filters:

```
metric(slice) = f( Σ over balls in slice of actual values,  Σ over the same balls of expected values )
```

So "net strike rate vs spin in the death overs, 2024–2026" sums actual runs and expected runs over exactly those balls. Nothing is re-fitted for the slice, and a "season" number is just the slice `season = 2024`. Any combination of filters (season, phase, opposition, venue, bowling type, entry point...) works the same way, for every metric in this document.

---

## 2. Counting rules

Applied when matches are loaded (`etl/parse.py`), the same for every metric:

| Rule | Detail |
|---|---|
| Ball faced (batter) | Every delivery except a wide. No-balls count as faced. |
| Legal ball (bowler, team) | Not a wide and not a no-ball. |
| Runs (batter) | Runs off the bat only. |
| Runs conceded (bowler) | Total runs minus byes, leg-byes and penalty runs (wides and no-balls count against the bowler). |
| Runs (team) | Everything, including all extras. |
| Batter dismissed (out) | Any dismissal except *retired hurt* / *retired not out*. Includes being run out as the non-striker. |
| Bowler wicket | Bowled, caught, caught and bowled, lbw, stumped, hit wicket. Not run outs. |
| Four / six | Boundaries only: a four that was all run (`non_boundary`) is not a four. |
| Super overs | Excluded from every statistic. |
| Phases (T20) | Powerplay overs 1–6, middle 7–15, death 16–20. Shortened innings keep these proportions; the powerplay length comes from Cricsheet's record when present. |

**Shortened matches.** A match cut short before the start (chase target over fewer than 20 overs, no DLS) gives both innings the reduced allocation, so "balls remaining" and phases are right. Each innings' real allocation is stored as `allocated_balls`.

---

## 3. Standard metrics

All are ratios of sums over the slice (never averages of per-match figures).

**Batting** (per batter, over balls faced):

```
strike_rate        = 100 × runs / balls
average            = runs / outs
dot_pct            = 100 × dot balls / balls          (faced, 0 off the bat)
boundary_pct       = 100 × (fours + sixes) / balls
balls_per_boundary = balls / (fours + sixes)
innings            = distinct (match, innings) the batter appeared in
```

**Bowling** (per bowler, over legal balls):

```
economy      = 6 × runs conceded / balls
average      = runs conceded / wickets
strike_rate  = balls / wickets
dot_pct      = 100 × legal balls with 0 runs conceded / balls
boundary_pct = 100 × (fours + sixes conceded) / balls
```

**Example (IPL 2026, all batters):** 26,151 runs off 16,727 balls with 878 outs, so strike rate = 100 × 26151 / 16727 = **156.34** and average = 26151 / 878 = **29.78**.

---

## 4. Net stats

Net stats compare each player with what an average player would have produced on **the same balls**: same competition, season, over, wickets down, innings, ground, and how settled the batter was. Positive always means better than average. The idea of judging players against an expectation for the same balls is inspired by the "true stats" of Jarrod Kimber and Good Areas; the model below is pitchside's own.

### 4.1 The expected value of each ball

For each ball and each of eight targets (batter: runs, dismissal, dot, boundary; bowler: runs conceded, wicket, dot, boundary), the expected value is:

```
E[ball] = level(season, over) × situation(era, phase, wickets down, innings) × settle(era, phase, balls faced)
          × venue(era, ground) × scale(season, over)
```

| Piece | What it captures | Estimated on |
|---|---|---|
| `level` | The competition's average rate for that over in that season. This is the era adjustment: 2010 balls are compared with 2010, 2026 with 2026. | All balls of that over number in that season (≈ 74 matches × 2 innings in a modern IPL season) |
| `situation` | How much more or less is produced at this many wickets down (each of 0–7, then 8+ together), setting or chasing. | All seasons in the same **rules era** and phase. IPL eras: before 2023 and 2023+ (Impact Player). |
| `settle` | How settled the striker is: balls 1–5 (new), 6–10 (getting in), 11+ (set). | Same rules era and phase. Stops at 11+ on purpose: batters who last 30+ balls are disproportionately the good ones, so a finer split would hide skill. |
| `venue` | How much more or less this ground produces than the league, so a batter based at a flat ground isn't credited with the pitch (and a bowler at a slow one isn't credited with it either). | All matches at that ground in the same rules era. Grounds are canonicalised through `aliases.toml`, so a renamed ground is one cell. Shrunk with a prior of 2,400 balls (≈ 10 matches), not 50: see below. |
| `scale` | A correction so that, within each season and over, expected totals equal actual totals exactly. | Each season–over |

Shortened innings are mapped onto the 20-over scale (`effective over = floor(over × 120 / allocated balls)`), so the last over of a 5-over game is compared with other last overs.

**Fitting.** The factors are estimated by iterative proportional fitting: starting with all factors at 1, the situation, settle and venue factors and the scales are re-estimated in turn (10 rounds) until expected matches actual on every margin. Each factor is

```
factor(cell) = (Σ actual + 50 × r̄) / (Σ predicted + 50 × r̄)        r̄ = Σ predicted / balls in the cell
```

where "predicted" uses the current values of everything else. The 50 is a prior of 50 balls at factor 1, so thin cells (8+ wickets down in the death in one era) can't swing wildly. The last step is always the rescale, which guarantees the zero-sum property below.

**Why the venue prior is 2,400 balls.** Half the balls at a ground are faced (and bowled) by the home side, so a ground's raw rate mixes the pitch with the home team's skill. Lightly shrunk, the venue factor would absorb that skill and erase it from the home players' net stats. With 2,400 pseudo-balls a ground keeps about half its observed effect after 10 matches and about 80% after 40. Example, IPL 2023+, batting runs: Chepauk 0.93, Ekana (Lucknow) 0.94, Wankhede 1.03, Chinnaswamy 1.02 (`pitchside baselines ipl` lists every ground).

**Worked example: one ball.** IPL 2024, match 1422124, chase, ball 15.2 (2nd ball of the 16th over, the death). Kohli has faced 44 balls (settled), team 119/4 needing 58 off 29. Expected runs off the bat on this ball:

```
level    (2024, over 16, runs per ball faced)   1.6186
situation (2023+, death, 4 down, chasing)      × 1.0893
settle    (2023+, death, 11+ balls)             × 1.0879
venue     (2023+, M Chinnaswamy Stadium)        × 1.0183
scale     (2024, over 16)                       × 0.9577
                                                = 1.8706 expected runs
```

He hit Harshal Patel for 4, so this ball's contribution to his net runs is 4 − 1.87 = **+2.13**. His expected chance of dismissal on that ball was 0.0506.

### 4.2 Batting net stats

Over any slice, with `exp_runs`, `exp_out`, `exp_dot`, `exp_bnd` the sums of the per-ball expectations on the batter's balls faced:

```
exp_sr            = 100 × exp_runs / balls
net_sr           = 100 × (runs − exp_runs) / balls                     = strike_rate − exp_sr
exp_avg           = exp_runs / exp_out
net_avg          = runs / outs − exp_runs / exp_out                    = average − exp_avg
net_runs         = runs − exp_runs
outs_saved        = exp_out − outs
net_dot_pct      = 100 × (exp_dot − dots) / balls                      (+ = fewer dots than expected)
net_boundary_pct = 100 × (fours + sixes − exp_bnd) / balls
```

The dismissal expectation per ball counts any dismissal on that ball (including a non-striker run-out), so that league-wide expected outs equal actual outs.

**Worked example: Kohli, IPL 2024 season** (slice: `player = Kohli, season = 2024`):

| | Actual | Expected | Net |
|---|---|---|---|
| Runs, balls | 741 off 479 | 778.6 | net_runs = 741 − 778.6 = **−37.6** |
| Strike rate | 100 × 741/479 = 154.70 | 100 × 778.6/479 = 162.54 | net_sr = **−7.84** |
| Outs | 12 | 24.0 | outs_saved = **+12.0** |
| Average | 741/12 = 61.75 | 778.6/24.0 = 32.51 | net_avg = **+29.24** |

Reading: he scored slower than an average batter would have on his balls (−7.8 per 100 balls), but was dismissed half as often as expected, so his average was 29 runs better than par. That's the classic anchor profile. Before the venue factor his net SR read −5.5: half his innings were at Chinnaswamy, and the extra runs that ground gives everyone were being credited to him.

### 4.3 Bowling net stats

Over the bowler's balls, with expectations summed over every delivery they bowled (`exp_runs`, `exp_wkts`, ...):

```
exp_economy       = 6 × exp_runs / balls
runs_saved        = exp_runs − runs
net_economy      = 6 × (exp_runs − runs) / balls                       = exp_economy − economy
net_wickets      = wickets − exp_wkts
net_avg          = exp_runs / exp_wkts − runs / wickets                (+ = cheaper wickets than expected)
net_sr           = balls / exp_wkts − balls / wickets                  (+ = quicker wickets than expected)
net_dot_pct      = 100 × (dots − exp_dot) / balls
net_boundary_pct = 100 × (exp_bnd − fours − sixes) / balls
```

**Worked example: Bumrah, IPL 2024 season:**

| | Actual | Expected | Net |
|---|---|---|---|
| Runs off 311 balls | 336 | 521.7 | runs_saved = **+185.7** |
| Economy | 6 × 336/311 = 6.48 | 6 × 521.7/311 = 10.06 | net_economy = **+3.58** runs/over |
| Wickets | 20 | 17.0 | net_wickets = **+3.0** |
| Average | 336/20 = 16.80 | 521.7/17.0 = 30.6 | net_avg = **+13.8** |
| Strike rate | 311/20 = 15.6 | 311/17.0 = 18.3 | net_sr = **+2.7** |

### 4.4 Properties you can rely on

- **Zero-sum.** Across all players in a season (and within every season–over), net runs, net outs, net dots and net boundaries sum to exactly zero, for batting and for bowling. An average player scores 0.
- **Flat by situation.** League-wide, net stats are about 0 for every phase, wickets-down value, setting/chasing, balls-faced bucket and entry point. By ground they are close to 0 but not exactly, because the venue factor is shrunk: since 2023 Chepauk and Ekana still read about −3.5 net SR (−12 without the factor). Batting positions 9–11 stay negative, because tailenders really are worse batters.
- **Era-aware.** Because `level` is per season, a 2010 batter and a 2026 batter are each measured against their own season.

Both properties are enforced by tests; `pitchside baselines ipl` shows the factors.

---

## 5. Small-sample shrinkage (`*_shrunk`)

A rate on a small sample is mostly noise. Shrunk metrics pull each player towards the average **under the same filters**, by `k` balls of evidence:

```
shrunk rate = (player's total + k × μ) / (player's balls + k)
μ = population total / population balls,  over the same slice without the player restriction
```

`k` is estimated per per-ball rate, once per competition at load, by the method of moments over **player-seasons with at least 60 balls**:

```
σ²  = per-ball variance of the rate (all balls)
S   = Σ nᵢ (pᵢ − p̄)²           over player-seasons i with nᵢ balls and rate pᵢ
τ²  = (S − (N − 1) σ²) / (Σ nᵢ − Σ nᵢ² / Σ nᵢ)      talent spread between players
k   = σ² / τ²                  clamped to [20, 5000]
```

`k` is the number of balls at which a player's own record and the average get equal weight. Current IPL values:

| Rate | k (balls) | | Rate | k (balls) |
|---|---|---|---|---|
| Batting runs per ball | 85 | | Bowling runs per ball | 93 |
| Batting dot rate | 83 | | Bowling dot rate | 70 |
| Batting boundary rate | 88 | | Bowling wicket rate | 1,417 |
| Batting dismissal rate | 2,387 | | Bowling net runs per ball | 277 |
| Batting net runs per ball | 177 | | Bowling net wickets per ball | 5,000 (cap) |
| Batting net dismissals per ball | 1,083 | | | |

Dismissal and wicket rates need over 1,000 balls to become half-reliable: in T20 they are mostly noise, which is a property of the data. Net wickets hit the cap: at season level, wicket-taking beyond what the situation explains is barely detectable.

**Which metrics.** Batting: `strike_rate_shrunk`, `average_shrunk` (shrunk runs per ball ÷ shrunk dismissals per ball), `dot_pct_shrunk`, `boundary_pct_shrunk`, `net_sr_shrunk`, `net_avg_shrunk`. Bowling: `economy_shrunk`, `average_shrunk`, `strike_rate_shrunk`, `dot_pct_shrunk`, `net_economy_shrunk`, `net_wickets_shrunk` (shrunk per-ball rate × balls). Net stats shrink the *difference from expectation*; the expectation itself (the player's context) is never shrunk.

**Worked example: Kohli, 2024.** Net runs −37.55 off 479 balls; the 2024 population net rate is 0 by construction; k = 177.2:

```
net_sr_shrunk = 100 × (−37.55 + 177.2 × 0) / (479 + 177.2) = −5.72     (raw −7.84)
```

**Small vs large sample, 2026:**

| Player | Balls | Net SR (raw) | Net SR (shrunk) |
|---|---|---|---|
| Vaibhav Suryavanshi | 327 | +71.89 | **+46.62** |
| Azmatullah Omarzai | 27 | +43.80 | **+5.79** |

Both had remarkable raw numbers; only one had enough balls to keep most of it.

With several competitions open, each competition's `k` is combined, weighted by its number of qualifying player-seasons.

---

## 6. Win probability and WPA

### 6.1 The models

Two logistic regressions per competition, fitted on all its seasons, give the batting side's chance of winning **before each ball**:

```
p = 1 / (1 + e^(−z)),   z = b₀ + Σ bⱼ xⱼ
```

`rpb` is the season's runs per legal ball (all innings), which puts every state in its era.

**First innings** (batting first):

| Feature | Formula |
|---|---|
| runs_ahead | (score − rpb × balls bowled) / (rpb × 120): runs ahead of the season's pace, in innings-lengths |
| wickets | wickets in hand / 10 |
| balls_left | balls left / 120 |
| wickets_x_balls, ahead_x_balls | products of the above |

**Chase:**

| Feature | Formula |
|---|---|
| difficulty | ln( (runs required + 0.5) / ((balls left + 0.5) × rpb) ): the required rate relative to the season's scoring rate, on a log scale |
| wickets | wickets in hand / 10 |
| log_balls | ln(balls left + 1) |
| difficulty_x_wickets, difficulty_x_balls, wickets_x_balls | products |

Outcomes: won 1, lost 0, tied 0.5 (even if a super over decided it); no-result matches are left out. Fitting: Newton's method on standardised features with a small ridge penalty (λ = 1). A chase whose target is already reached has p = 1.

**Current IPL chase coefficients:** intercept 0.521, difficulty −3.837, wickets 10.387, log_balls −0.948, difficulty×wickets −2.043, difficulty×balls −0.095, wickets×balls −1.263. Fit: Brier score 0.138 for chases and 0.220 for first innings. Predicted and actual win rates agree within about 0.02 in almost every tenth of probability (`pitchside winprob ipl`).

**Worked example: the same Kohli ball.** 2024, chase, 119/4 needing 58 off 29; 2024 rpb = 1.5934.

```
difficulty = ln(58.5 / (29.5 × 1.5934)) = 0.2188
wickets    = 6/10 = 0.6
log_balls  = ln(30) = 3.4012
z = 0.521 − 3.837(0.2188) + 10.387(0.6) − 0.948(3.4012)
    − 2.043(0.2188 × 0.6) − 0.095(0.2188 × 3.4012) − 1.263(0.6 × 3.4012) = −0.228
p = 1 / (1 + e^0.228) = 0.443
```

RCB's chance of winning before the ball: **44.3%**.

### 6.2 Win probability added (WPA)

```
WPA(ball) = p_after − p_before         from the batting side's point of view, in wins
```

`p_after` is the next ball's `p_before`. The end of the first innings hands over to 1 − (the chase's starting probability); the end of the chase is the result (1, 0, or 0.5 for a tie). So each innings' WPA sums exactly to its end minus its start.

**Credit:**

```
batter WPA = WPA of every ball they face (nothing on wides)
bowler WPA = −WPA of every ball they bowl
```

Kohli's four in the example moved RCB from 44.3% to 50.6%: **+0.063 WPA** for Kohli and −0.063 for Harshal Patel.

Fielding isn't credited, and a run-out is charged to the striker on that ball.

### 6.3 MVP and match impact

```
total_wpa     = Σ batting WPA + Σ bowling WPA    over the slice
wpa_per_match = total_wpa / matches played in the slice
```

**Example: 2026 MVP.** Bhuvneshwar Kumar 1.91 (0.62 batting + 1.29 bowling), Vaibhav Suryavanshi 1.57, Shreyas Iyer 1.32. A WPA of 1.0 means one full match swung from certain defeat to certain victory.

`match_impact` shows, for one match: the batting-first side's win probability at the end of each over, players' total WPA in that match, and the five balls with the largest |WPA|.

**Net stats vs WPA.** Net stats measure efficiency against an average player on the same balls. WPA measures effect on the result, so a single in the last over of a tight chase is worth far more than a six at 200/2.

---

## 7. Entry point

When a batter walked in, taken from the innings' batting order:

```
entry_balls   = legal balls bowled in the innings before their first appearance
entry_over    = entry_balls // 6 + 1
entry_wickets = wickets down when they arrived
entry_phase   = opener (entry_balls = 0) | powerplay (over ≤ 6) | middle (≤ 15) | death
entry_bucket  = 00 opener | 01-06 | 07-10 | 11-15 | 16-20
```

Both openers have `entry_balls = 0`, so a number 3 out in the first over isn't counted as an opener. In bowling tables the same fields describe the batter being bowled to.

---

## 8. Archetypes

Rule-based, from these features over the slice (default minimums: 200 balls batting, 240 bowling):

**Batters.** Role from where they bat:

| Role | Rule |
|---|---|
| opener | arrived at ball 0 in at least half their innings |
| finisher | at least 40% of balls faced in the death, or average entry from over 13 |
| top order | average entry over ≤ 6 |
| middle order | otherwise |

Style, from `net_sr_shrunk` and balls per innings:

| Style | Rule |
|---|---|
| aggressor (Powerplay enforcer, Top-order / Middle-overs aggressor, Finisher) | net_sr_shrunk ≥ +8 |
| anchor (Anchor opener, Top-order / Middle-order anchor) | net_sr_shrunk ≤ −5 **and** ≥ 18 balls per innings (slow but short isn't an anchor) |
| finisher role, not aggressive | Lower-order hitter (net_sr_shrunk > −5) or Lower-order batter |
| otherwise | Opener / Top-order accumulator / Middle-order accumulator |

**Bowlers.** Role from type and share of balls by phase, checked in this order (first match wins):

| Archetype | Rule |
|---|---|
| All-phase seamer | pace; ≥ 30% of balls in the powerplay and ≥ 30% at the death |
| Death specialist | pace; ≥ 35% at the death |
| New-ball seamer | pace; ≥ 45% in the powerplay |
| Middle-overs seamer | other pace bowlers |
| Powerplay spinner | ≥ 30% in the powerplay |
| Death-overs spinner | ≥ 20% at the death |
| Middle-overs spinner | other spinners |

Tagged *(economical)* if `net_economy_shrunk ≥ +0.5`, *(expensive)* if ≤ −0.5. The thresholds are constants in `src/pitchside/stats/archetypes.py`.

---

## 9. Team and venue numbers

**Team batting and bowling** (all runs including extras, all dismissals):

```
run_rate         = 6 × runs / legal balls      (batting side: scored; bowling side: conceded)
average          = runs / wickets
balls_per_wicket = legal balls / wickets
team wpa         = Σ WPA from that side's point of view
```

**Results** (one row per match per team):

```
win_pct = won / (won + lost)               (no results excluded; super-over results count as won/lost)
```

`tied` counts ties in normal play; the super over then decides won/lost.

**Venue summary** (one row per match, from the side batting first):

```
avg_1st_innings      = mean first-innings total, full-length innings only (120 balls, no DLS)
median_1st_innings   = same, median
chasing_win_pct      = chases won / decided matches
toss_winner_win_pct  = matches won by the toss winner / decided matches
toss_chose_field_pct = matches where the toss winner bowled first / matches
```

**Example: M Chinnaswamy Stadium, 2023–2026:** 24 matches, average first innings 194.5 (median 189, highest 287), chasing win 54.2%, and every toss winner chose to field, which is why the toss and chase percentages are identical.

**Head to head** combines the results above, filtered `team = A, opposition = B`, with both sides' phase run rates and the top run-scorers and wicket-takers in those matches.

---

## 10. Pre-match brief

Composed entirely from the pieces above; nothing new is modelled.

| Term | Definition |
|---|---|
| Recent | The last 3 seasons either team played, unless you pass a `season` filter |
| Squad | Everyone who batted or bowled for the team in its last 10 matches (Cricsheet has no announced squads) |
| Key batters | Top 4 of the squad by `net_runs` over the recent window (minimum 30 balls) |
| Key bowlers | Top 4 by `runs_saved`, same window. Ranked by value above average rather than WPA, because over a few seasons WPA is too noisy. |
| Style matchup | The six most-used batters of one side: balls, strike rate and `net_sr_shrunk` against pace and against spin, beside the other side's share of spin (% of its squad's recent balls) |
| Head to heads | Batter v bowler pairings between the two squads with at least 12 balls, all seasons; the 8 with the most balls, each way |
| Phase profile | Team batting run rate and bowling economy per phase, minus the league's run rate for the same phase and window |

---

## 11. Summary: what is estimated where

| Quantity | Estimated on | When |
|---|---|---|
| Per-ball expectation: level | One competition, one season, one over number | At load |
| Per-ball expectation: situation and settle factors | One competition, one rules era, one phase | At load |
| Per-ball expectation: venue factor | One competition, one rules era, one ground | At load |
| Shrinkage `k` | One competition, all player-seasons with 60+ balls | At load |
| Win-probability model | One competition, all seasons; era context via season runs per ball | At load |
| Shrinkage population rate `μ` | Your slice (same filters, all players) | Per query |
| Every metric (standard, net, shrunk, WPA) | Your slice: sums over the balls matching your filters | Per query |
| Sample flags | Your slice: `low` < 30 balls, `small` < 120 | Per query |

Source files: `src/pitchside/etl/parse.py` (counting rules), `model/baselines.py` (net-stat expectations), `model/shrinkage.py` (priors), `model/winprob.py` (win probability), `stats/splits.py` (metric formulas), `stats/archetypes.py`, `stats/teams.py`, `stats/impact.py`, `stats/brief.py`.
