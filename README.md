# Release Date Tracker

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

## Usage

```bash
poetry install
cp .env.example .env   # fill in TMDB_API_KEY, TWITCH_CLIENT_ID/SECRET, OPENAI_API_KEY

# seed from a local file (local/seeds.json, gitignored) or live Notion
poetry run rdt seed sync

# resolve Tier-0 dates for watched entities
poetry run rdt pull

# show ranked best estimates
poetry run rdt show

# browse it interactively
poetry run rdt tui
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
- **enter** open the work card · **←/→** change state (auto-saves) · **esc** back
- **a** add a title — same syntax, pointed at TMDB/IGDB/Steam, with candidate selection
- **/** back to the query bar · **r** reload · **q** quit

A half-typed value is shown as what it is about to mean: `is:a` greys `ging` in after the
caret and the table already shows `is:aging`, tab walks that preview through the rest of
the candidates (`available`, …), and **→**, **space** or **enter** takes the one on
screen. Completions are scoped to what you typed, so `is:a` never offers `dated`.

The bucket keys rewrite the `is:` term in the query rather than keeping separate view
state, so what you see is always explained by the string in the bar.

## Privacy

Personal data stays out of git: `.env`, `local/` and the `*.db` are gitignored. The
committed Notion adapter is generic; your watchlist lives only in `local/seeds.json`
or behind your own `NOTION_TOKEN`.
