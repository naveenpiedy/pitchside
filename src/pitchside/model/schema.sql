-- Core tables for one competition database (db/<competition>.duckdb).
-- Loaded from Cricsheet JSON by pitchside.etl.load; derived tables live in derive.py.

CREATE TABLE IF NOT EXISTS matches (
    match_id        VARCHAR PRIMARY KEY,
    competition     VARCHAR NOT NULL,
    event_name      VARCHAR,
    season          VARCHAR NOT NULL,   -- as Cricsheet labels it, e.g. '2007/08'
    season_year     INTEGER,            -- year the season was played (derived)
    match_date      DATE NOT NULL,
    match_number    INTEGER,
    stage           VARCHAR,            -- NULL for league matches, else 'Final', 'Qualifier 1', ...
    match_type      VARCHAR,
    gender          VARCHAR,
    overs           INTEGER,
    balls_per_over  INTEGER,
    venue           VARCHAR,
    city            VARCHAR,
    team1           VARCHAR,
    team2           VARCHAR,
    toss_winner     VARCHAR,
    toss_decision   VARCHAR,
    result          VARCHAR NOT NULL,   -- 'win', 'tie', 'no result', 'draw'
    winner          VARCHAR,            -- includes super-over / eliminator winner
    win_by_runs     INTEGER,
    win_by_wickets  INTEGER,
    method          VARCHAR,            -- e.g. 'D/L'
    player_of_match VARCHAR[],
    data_version    VARCHAR,
    revision        INTEGER NOT NULL,
    source_file     VARCHAR
);

CREATE TABLE IF NOT EXISTS match_players (
    match_id    VARCHAR NOT NULL,
    team        VARCHAR NOT NULL,
    player_id   VARCHAR NOT NULL,
    player_name VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS innings (
    match_id       VARCHAR NOT NULL,
    innings_no     INTEGER NOT NULL,    -- 1, 2; super overs are 3+
    batting_team   VARCHAR NOT NULL,
    bowling_team   VARCHAR NOT NULL,
    is_super_over  BOOLEAN NOT NULL,
    target_runs    INTEGER,
    target_overs   DOUBLE,
    runs           INTEGER NOT NULL,
    wickets        INTEGER NOT NULL,
    legal_balls    INTEGER NOT NULL,
    extras         INTEGER NOT NULL,
    absent_hurt    VARCHAR[],
    PRIMARY KEY (match_id, innings_no)
);

-- One row per delivery, with ball state captured *before* the ball.
CREATE TABLE IF NOT EXISTS deliveries (
    match_id            VARCHAR NOT NULL,
    innings_no          INTEGER NOT NULL,
    seq                 INTEGER NOT NULL,   -- delivery order within the innings, 1-based
    over_no             INTEGER NOT NULL,   -- 0-based
    ball_in_over        INTEGER NOT NULL,   -- 1-based, counts illegal deliveries
    is_legal            BOOLEAN NOT NULL,   -- not a wide or no-ball
    legal_balls_before  INTEGER NOT NULL,   -- legal balls bowled in the innings before this one
    phase               VARCHAR NOT NULL,   -- 'powerplay', 'middle', 'death'
    batter_id           VARCHAR NOT NULL,
    non_striker_id      VARCHAR NOT NULL,
    bowler_id           VARCHAR NOT NULL,
    batting_position    INTEGER NOT NULL,   -- striker's position in the order
    runs_batter         INTEGER NOT NULL,
    runs_extras         INTEGER NOT NULL,
    runs_total          INTEGER NOT NULL,
    wides               INTEGER NOT NULL,
    noballs             INTEGER NOT NULL,
    byes                INTEGER NOT NULL,
    legbyes             INTEGER NOT NULL,
    penalty             INTEGER NOT NULL,
    bowler_runs         INTEGER NOT NULL,   -- runs charged to the bowler
    is_ball_faced       BOOLEAN NOT NULL,   -- counts in the batter's balls faced (not a wide)
    is_four             BOOLEAN NOT NULL,
    is_six              BOOLEAN NOT NULL,
    is_wicket           BOOLEAN NOT NULL,   -- any dismissal on this ball (excludes retired hurt)
    is_bowler_wicket    BOOLEAN NOT NULL,   -- dismissal credited to the bowler
    is_batter_out       BOOLEAN NOT NULL,   -- the striker was the one dismissed
    score_before        INTEGER NOT NULL,   -- team score before this ball
    wickets_before      INTEGER NOT NULL,
    batter_runs_before  INTEGER NOT NULL,   -- striker's own score before this ball
    batter_balls_before INTEGER NOT NULL,
    balls_remaining     INTEGER NOT NULL,   -- legal balls left in the innings allocation
    runs_required       INTEGER,            -- chases only
    PRIMARY KEY (match_id, innings_no, seq)
);

CREATE TABLE IF NOT EXISTS wickets (
    match_id        VARCHAR NOT NULL,
    innings_no      INTEGER NOT NULL,
    seq             INTEGER NOT NULL,
    player_out_id   VARCHAR NOT NULL,
    kind            VARCHAR NOT NULL,
    bowler_id       VARCHAR NOT NULL,       -- bowler of the ball, credited or not
    bowler_credit   BOOLEAN NOT NULL,
    counts_as_out   BOOLEAN NOT NULL,       -- false for 'retired hurt' / 'retired not out'
    fielders        VARCHAR[],
    fielder_ids     VARCHAR[],
    score_at_fall   INTEGER NOT NULL,       -- team score after this ball
    wicket_number   INTEGER,                -- NULL when it doesn't count as out
    legal_balls_at_fall INTEGER NOT NULL
);

-- Order each batter came in, and the state of the innings when they did.
CREATE TABLE IF NOT EXISTS batting_order (
    match_id           VARCHAR NOT NULL,
    innings_no         INTEGER NOT NULL,
    player_id          VARCHAR NOT NULL,
    position           INTEGER NOT NULL,
    entry_legal_balls  INTEGER NOT NULL,
    entry_score        INTEGER NOT NULL,
    entry_wickets      INTEGER NOT NULL,
    PRIMARY KEY (match_id, innings_no, player_id)
);

-- Substitutions, including the IPL impact player (reason = 'impact_player').
CREATE TABLE IF NOT EXISTS replacements (
    match_id      VARCHAR NOT NULL,
    innings_no    INTEGER NOT NULL,
    seq           INTEGER NOT NULL,
    kind          VARCHAR NOT NULL,   -- 'match' or 'role'
    team          VARCHAR,
    player_in_id  VARCHAR,
    player_out_id VARCHAR,
    reason        VARCHAR,
    role          VARCHAR
);

CREATE TABLE IF NOT EXISTS players (
    player_id VARCHAR PRIMARY KEY,
    name      VARCHAR NOT NULL       -- most recent name used in this competition
);

CREATE TABLE IF NOT EXISTS meta (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR
);

CREATE TABLE IF NOT EXISTS ingest_log (
    ingested_at     TIMESTAMP NOT NULL,
    source          VARCHAR NOT NULL,
    matches_added   INTEGER NOT NULL,
    matches_updated INTEGER NOT NULL,
    data_version    INTEGER NOT NULL
);
