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
```

## Privacy

Personal data stays out of git: `.env`, `local/` and the `*.db` are gitignored. The
committed Notion adapter is generic; your watchlist lives only in `local/seeds.json`
or behind your own `NOTION_TOKEN`.
