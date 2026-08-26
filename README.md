# Release Date Tracker

[![CI](https://github.com/KaiErikNiermann/release-date-tracker/actions/workflows/ci.yml/badge.svg)](https://github.com/KaiErikNiermann/release-date-tracker/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)

Free-first aggregation of **concrete** release dates — theatrical, **digital/VOD**,
physical, per-storefront and per-retailer — plus **locations** and **prices**, for
movies, TV, games and tech. Built to kill the "google a release date → ten clickbait
articles → no actual date" loop.

## Approach

Three tiers, cheapest first:

1. **Tier 0 — structured APIs (free, no NLP).** Most of what you want is already
   structured data:
   - **TMDB** `release_dates` gives per-country release *types* including a dedicated
     **Digital** type (the thing most movie DBs never report), plus watch providers.
   - **IGDB** `release_dates` gives platform + region + a precision category
     (exact / quarter / year / TBA).
   - **Steam** `appdetails` gives release date + `price_overview` per region.
2. **Tier 1 — gap-filler (cheap LLM).** Only for what Tier 0 leaves as TBA (unannounced
   digital dates, tech hardware, rumors): targeted search → boilerplate strip → local
   date-candidate filtering → batched structured extraction.
3. **Tier 2 — prediction (free).** e.g. model the theatrical→digital gap per distributor
   to estimate a digital date before any announcement. *(planned)*

Every claim is a sourced **observation** (channel, region, date+precision, optional
price, certainty, source URL + exact quote). A **confidence scorer** weighs source trust,
corroboration, recency and stance to produce a ranked **best estimate** per
(entity, channel, region) — not a single fake-precise date.

## Data model

- `Entity` — a tracked title (`MediaKind`: movie/tv/game/tech/…) + discovered external IDs.
- `ReleaseObservation` — one sourced claim. `ReleaseChannel` spans theatrical/digital/
  physical/steam/psn/best_buy/…; `region` is the geography; `Money` is price in minor units.
- `BestEstimate` — derived pick over observations.

Stored in SQLite with idempotent, resume-safe upserts.

## Install

Requires **Python 3.13+**. Nothing is published to PyPI yet — install from the repo:

```bash
curl -fsSL https://raw.githubusercontent.com/KaiErikNiermann/release-date-tracker/main/install.sh | bash
```

On Windows use the `uv` or `pipx` line below — `install.sh` needs a POSIX shell (WSL and
Git Bash both count).

The script prefers `uv`, falls back to `pipx`, and otherwise builds a private venv and
links a shim into `~/.local/bin` — so `rdt` never lands in your system site-packages.
Equivalent one-liners if you already have a preference:

```bash
uv tool install   git+https://github.com/KaiErikNiermann/release-date-tracker.git
pipx install      git+https://github.com/KaiErikNiermann/release-date-tracker.git
```

Or from a checkout:

```bash
git clone https://github.com/KaiErikNiermann/release-date-tracker.git
cd release-date-tracker
just dev          # poetry install + git hooks
just tui
```

### API keys

Every source that can work without a key does. Keys only widen coverage, and each is
optional and independent:

| variable | unlocks | free? |
|---|---|---|
| `TMDB_API_KEY` | movies / TV — dates, credits, watch providers | yes |
| `TWITCH_CLIENT_ID` + `TWITCH_CLIENT_SECRET` | games, via IGDB | yes |
| `OPENAI_API_KEY` | the Tier-1 gap-filler for what Tier 0 leaves as TBA | paid |

Put them in `~/.config/rdt/.env` (or a `.env` beside a checkout, which takes precedence).
`.env.example` lists every variable with a comment.

### Where your data lives

Paths follow the XDG base directories, so `rdt` finds the same tracker no matter which
directory you run it from. Every one is overridable:

| what | default (Linux) | override |
|---|---|---|
| tracker database | `~/.local/share/rdt/releases.db` | `RDT_DB_PATH` |
| learned platform map | `~/.local/share/rdt/platforms.db` | `RDT_PLATFORM_DB_PATH` |
| mined trend cache | `~/.cache/rdt/trends_cache.db` | `RDT_TREND_CACHE_PATH` |
| seeds | `~/.config/rdt/seeds.json` | `RDT_SEEDS_PATH` |

macOS and Windows get their platform equivalents. A checkout that already has a
`data/releases.db` beside it keeps using it, so upgrading in place never strands an
existing tracker. `just paths` prints what this machine resolved.

## Usage

```bash
rdt --version

# seed from a local file or live Notion
rdt seed sync

# resolve Tier-0 dates for tracked entities
rdt pull

# show ranked best estimates
rdt show

# browse it interactively
rdt tui
```

## Querying

One query language, shared by the CLI and the TUI — `query.py` owns the parser and the
predicates, so the two frontends cannot disagree about what a query means.

A bare (or quoted) word matches the title and its aliases; everything else is
`field:value`. Terms are AND-ed, comma-separated values within a term are OR-ed, and a
leading `-` (or `!`) negates.

```bash
rdt find 'kind:movie is:available'
rdt find 'cast:"Alan Ritchson" year:2026'
rdt find 'genre:horror -is:watched'
rdt upcoming 'tag:"body horror" year:2026..2028'
```

| field | matches |
|---|---|
| *(bare)* / `name:` | title + aliases |
| `kind:` | `movie tv game tech book …` (`tv-show`, `film`, `show` alias; `kind:anime` becomes `origin:anime`) |
| `state:` | `unset want watching watched dropped skipped` |
| `is:` | `available upcoming watched` · `blocked notes dated tba confirmed speculative predicted fresh aging stale` |
| `tag:` | any descriptor; `genre:` `theme:` `mood:` `style:` `origin:` narrow it |
| `director:` `cast:` `writer:` `studio:` `network:` … | one credit role each (derived from `CreditRole`, so a new role is a new field) |
| `person:` | any credit, any role |
| `platform:` / `on:`, `series:` | consumption platform, series |
| `year:` | any release year — `2026`, `2020..2026`, `>=2026`, `<2030` |
| `season:` `part:` | season / mid-season coords |

`is:` is a different axis from `state:`: the *bucket* `watched` spans
`watched|dropped|skipped`, while the *state* `watched` is only one of them.

## TUI

`rdt tui` puts the same language in a live query bar over the same views.

- **type** to filter (instant — it filters an in-memory snapshot, not the database)
- **tab** / **shift+tab** walk the completions · **1/2/3** available / upcoming / watched
- **↓** into the list · **enter** open the work card · **←/→** change state (auto-saves) · **esc** back
- **e** on a card edits it — title, dates, who/where/what, notes
- **a** add a title — same syntax, pointed at TMDB/IGDB/Steam, with candidate selection.
  It moves like the browse screen: **↓** or **enter** into the candidates, **j/k** through
  them, **enter** adds, **esc** steps back to the bar. A search or an add in flight shows a
  spinner where its result will land
- **/** or **shift+tab** back to the query bar · **ctrl+backspace** delete a word · **r** reload · **q** quit

A half-typed value is shown as what it is about to mean: `is:a` greys `ging` in after the
caret and the table already shows `is:aging`. Tab takes that offer into the bar and walks
on through the rest of the candidates (`available`, …), rewriting the term in place and
leaving the caret at the end of it, so a space carries straight on into the next filter;
**→**, **space** and **enter** also take what is on screen. Completions are scoped to what
you typed, so `is:a` never offers `dated`.

The bucket keys rewrite the `is:` term in the query rather than keeping separate view
state, so what you see is always explained by the string in the bar.

### Editing a card

`e` opens the card writable and `esc` puts you back on it, so a change can be looked at
where it will be read. Nothing is staged: a field commits when you leave it, the way the
state toggle already does. Every write goes through the same functions `rdt edit …` calls.

- **↓/↑** move between fields · **tab** takes the completion offered, tab again walks on
- **filling the empty row at the end of a section adds**; **emptying a row removes** what it
  pointed at. No add key, no delete key — except on the note log, where **d** drops an entry
- Names complete against the role you picked, so a `director` box offers directors and a
  `studio` box offers studios (and stores one as a company, not a person). Anything you
  type is accepted, and becomes a candidate itself once it is in the graph
- Dates are EDTF, the same literal `rdt edit date` takes: `2026`, `2026-09`, `2026-Q3`,
  `2026-09-18`, `2027..2029` for a window, trailing `~` for approximate and `?` for unsure.
  The field holds only what *you* wrote — a pulled date shows as the placeholder, so it is
  never frozen into a hand-authored one by accident, and emptying the field gives it back

A hand-authored date carries no confidence figure, because it cannot mean anything: the
resolver recomputes every score from certainty and tier on read, so a number typed in would
be overwritten before it could be displayed. The card shows such a date as `manual` plus the
stance the EDTF qualifier already stated.

## Privacy

Nothing leaves your machine except the API calls that resolve a date. There is no
account, no telemetry and no sync: the tracker is a SQLite file in your XDG data dir,
and `.env`, `local/` and every `*.db` are gitignored so a checkout cannot commit them by
accident. The Notion adapter is generic — your watchlist lives only in your own seeds
file or behind your own `NOTION_TOKEN`.

## Development

`just` drives everything; `just` on its own lists the recipes.

```bash
just dev        # install deps, wire up the pre-push hook
just check      # lint + format check + strict pyright + tests — exactly what CI runs
just test -k tui
just complexity # radon, flags anything below grade B
```

CI runs the suite on **Linux, macOS and Windows** across 3.13 and 3.14, lints and
type-checks once on Linux, and separately builds the wheel and installs it into a clean
venv — the packaging failure a test run from a checkout cannot catch.
`tests/test_cross_platform.py` holds what actually differs by OS: where the paths
anchor, whether the database survives a path with spaces and non-ASCII, and whether text
round-trips when the platform's default encoding is not UTF-8.

Releases are cut by tag: `just release patch` bumps, tags and pushes, and the workflow
builds the artifacts and writes the notes.

## License

[MIT](LICENSE) © Kai Niermann
