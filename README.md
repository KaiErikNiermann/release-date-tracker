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

Everything that can work without a key does — Steam, JustWatch, Wikidata and the whole tech
side need nothing. Keys widen coverage, and each is optional and independent:

| variable | unlocks | free? |
|---|---|---|
| `TMDB_API_KEY` | movies / TV — dates, credits, watch providers | yes |
| `TWITCH_CLIENT_ID` + `TWITCH_CLIENT_SECRET` | games, via IGDB | yes |
| `OPENAI_API_KEY` | the Tier-1 gap-filler for what Tier 0 leaves as TBA | paid |

The quickest way to set them is `s` in the TUI, which writes them for you. `rdt config set
TMDB_API_KEY=…` does the same headlessly, and `rdt doctor` says what is set, where each
value came from, and what the missing ones cost.

**Getting a TMDB key.** Create an account, then **Settings → API** and request a key; approval
is immediate for personal use. That page shows *two* credentials, and this is the one place
people get stuck: copy the **API Key (v3 auth)** — the short alphanumeric one — **not** the
API Read Access Token. `rdt` authenticates with the v3 key as a query parameter, so the v4
token (it starts `ey`, being a JWT) fails as an ordinary 401. `ctrl+t` in the settings screen
checks the key against TMDB and names that mistake specifically if you make it.

**Getting IGDB credentials.** IGDB authenticates through Twitch, so you need a Twitch account
with 2FA enabled, then [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps) →
**Register Your Application**. Any name and an OAuth redirect of `http://localhost` are fine;
the setting that matters is **Client Type: Confidential** — a Public app is never issued a
secret, and that is the usual reason people end up with an id and nothing to pair it with.
Then copy the Client ID and press **New Secret**. Free for non-commercial use, 4 requests a
second, and `rdt` handles the token exchange itself.

**Where they are stored.** `~/.config/rdt/config.toml`, mode 0600, written by the settings
screen and `rdt config set`. A real environment variable always wins over the file, so a
one-off `TMDB_API_KEY=… rdt rd dune` works and a CI runner is unaffected by whatever is on
your machine. A `~/.config/rdt/.env` (or one beside a checkout) is still read, below the
config file — `rdt config migrate` copies its keys up, and the TUI does that once on first
launch.

### Where your data lives

Paths follow the XDG base directories, so `rdt` finds the same tracker no matter which
directory you run it from. Every one is overridable:

| what | default (Linux) | override |
|---|---|---|
| tracker database | `~/.local/share/rdt/releases.db` | `RDT_DB_PATH` |
| learned platform map | `~/.local/share/rdt/platforms.db` | `RDT_PLATFORM_DB_PATH` |
| mined trend cache | `~/.cache/rdt/trends_cache.db` | `RDT_TREND_CACHE_PATH` |
| seeds | `~/.config/rdt/seeds.json` | `RDT_SEEDS_PATH` |
| settings | `~/.config/rdt/config.toml` | `RDT_CONFIG_FILE` |

macOS and Windows get their platform equivalents. A checkout that already has a
`data/releases.db` beside it keeps using it, so upgrading in place never strands an
existing tracker. `rdt doctor` prints what this machine resolved, along with which settings are overridden
and where each value came from.

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
| `platform:` / `on:`, `series:` | consumption platform, series; `on:netflix@us` scopes it to a market |
| `region:` / `in:` | a market something is streamable in at all |
| `year:` | any release year — `2026`, `2020..2026`, `>=2026`, `<2030` |
| `season:` `part:` | season / mid-season coords — set with `rdt add --season N [--part N]`, `rdt edit part`, or **s** in the add palette |
| `franchise:` / `continuity:` | season counted across a reboot's renumbering (see below) |

`is:` is a different axis from `state:`: the *bucket* `watched` spans
`watched|dropped|skipped`, while the *state* `watched` is only one of them.

A **reboot that restarts the count** carries two season numbers at once. "Daredevil: Born
Again" is season 1 of itself and the 4th of Daredevil's continuity, and TMDB models the two as
separate shows because that is what `/tv/{id}/season/{n}` has to resolve against. Say so once,
between the *series*, and the position is derived by walking:

```
rdt relate "Daredevil: Born Again" continues "Marvel's Daredevil" --after 3
```

`season:` keeps meaning the show's own number — it is what the puller depends on, and giving it
two meanings would make it ambiguous exactly where it must not be. `franchise:` is the second
ordering beside it. One link covers every season of the continuation, chains compose (Doctor
Who's 2024 → 2005 → 1963), and a chain that cannot be completed reports why instead of guessing
a number: a missing `--after` is not read as zero.

`on:netflix@us` and `on:netflix region:us` are **not** the same question. The second is a
conjunction of two independent row predicates — *has a Netflix edge* and *has a US edge* — so
it matches a show that is on Netflix in Germany and Paramount+ in the States. The `@` form
binds them to one edge, which is what you want when you are picking a VPN target. A platform
we hold no market for (a broadcast network) answers `on:showtime` but never `on:showtime@us`:
"we don't know where" is not "yes, in the US".

## TUI

`rdt tui` puts the same language in a live query bar over the same views.

- **type** to filter (instant — it filters an in-memory snapshot, not the database)
- **tab** / **shift+tab** walk the completions · **1/2/3** available / upcoming / watched
- **↓** into the list · **enter** open the work card · **←/→** change state (auto-saves) · **esc** back
- **e** on a card edits it — title, dates, who/where/what, notes
- **u** on a card refreshes it and drops you in that same form, with what moved marked
- **s** settings — keys, paths and preferences, with a check for the API keys
- **a** add a title — same syntax, pointed at TMDB/IGDB/Steam/Wikidata, with candidate
  selection. It moves like the browse screen: **↓** or **enter** into the candidates,
  **j/k** through them, **enter** adds, **e** reviews first, **esc** steps back to the bar. A
  search or an add in flight shows a spinner where its result will land
- **Sources on a card** — every work lists where its dates can be read, split into what the
  tool can refetch (`●`, and `u` re-pulls them) and what only a person can open (`○`, with
  the reason). The second list comes from Wikidata: given an id we already hold, one query
  finds the item carrying it and reads off IMDb, Metacritic, Rotten Tomatoes, the official
  site — and, for hardware, GSMArena and TechPowerUp, which decline automated extraction but
  are exactly where a human should look
- **adding a device** — a bare search covers movies, TV and games only. Wikidata (the tech
  source) label-matches *everything* at full score, so sweeping it by default would put a
  sand dune and a Klaus Schulze album above the film for `dune`. Instead, a query that reads
  as hardware (`Xiaomi Mix 4`, `RTX 5090`, `Steam Deck`) is retried as tech automatically
  when the media DBs come back empty; anything else takes an explicit `kind:tech` (`gadget`
  and `hardware` alias it), and the no-match line says so — it still names what was missed or
  never configured, since the freeform row is offered *underneath* that warning, not instead
  of it
- **adding one season** — a show adds as one unit by default, which is right for a limited
  series where a season number is meaningless. To track a single season, either say so
  (`yellowjackets season:2`, or just `yellowjackets season 2` — a trailing season phrase is
  read as a coordinate, shown back as `read as … + season:2`, and taken off the text the
  sources are searched with), or press **s** on a TV hit to list the show's seasons with
  their air dates and pick one. The whole-show row stays first either way, and a one-season
  show declines rather than opening a picker with nothing to choose. Either path lands the
  same row `rdt add --season 2` would
- **adding one part of a season** — **→** on a season opens it into the release blocks its
  episode air dates imply, and every level stays selectable: the whole show, a whole season, or
  one cut. Nothing is forced. No source models an intra-season split — TMDB carries Money
  Heist "Part 1-5" and Cobra Kai S6's three drops as plain numbered seasons — so this reads the
  one signal that *is* sourced, the gaps between episodes, measured against the season's own
  cadence rather than a fixed number of days. It proposes and shows its working; a season that
  did not split still opens, to a row saying so. The *word* a cut was sold under is only ever
  read back from what you typed (`arcane noxus act 1`), never guessed from outside — unstated,
  it reads as "Part", and the review form is where you change it
- **tracking something unannounced** — the last row of the candidate list is always *add it
  yourself*, because a search that found things can still have found the wrong things, and
  four of the nine kinds (book, music, podcast, comic) have no source to search at all. It
  only ever opens the review form, never adds outright, because every field on it is a guess
- **how much it guesses** — as much as it can defend, and it shows its working. Strongest
  evidence first: an explicit `kind:`/`year:`/`season:` is your own word and wins outright; a
  `season:` means a series; otherwise the kind is read off the matches that scored above the
  match floor *if they all agree* — search a film nobody has listed yet and the rest of its
  series comes back, which is real evidence about what you are adding. Failing that, a name
  that reads as hardware is tech, and `Steam Deck 2` additionally strips its generation marker,
  looks the family up and prefills from the lineage. Nothing at all, and it stays deliberately
  unclassified rather than confidently wrong. Every rung that fires prints a line saying so on
  the review form, so a wrong guess is visible and one **←→** from being fixed. Dates are never
  inferred — a franchise says nothing about when a new entry ships, so only `year:` fills that
- **/** or **shift+tab** back to the query bar · **ctrl+backspace** delete a word · **s** settings · **r** reload · **q** quit

A half-typed value is shown as what it is about to mean: `is:a` greys `ging` in after the
caret and the table already shows `is:aging`. Tab takes that offer into the bar and walks
on through the rest of the candidates (`available`, …), rewriting the term in place and
leaving the caret at the end of it, so a space carries straight on into the next filter;
**→**, **space** and **enter** also take what is on screen. Completions are scoped to what
you typed, so `is:a` never offers `dated`.

The bucket keys rewrite the `is:` term in the query rather than keeping separate view
state, so what you see is always explained by the string in the bar.

### Refreshing a card

**u** runs the same refresh `rdt refresh` runs for one work — Tier-0 sources *and* the
JustWatch offer scan — then opens the edit form rather than flashing a notification.

Nothing you typed can be lost to it. A pull only clears the providers that answered, and no
source is called `manual`, so a hand-authored date and a pulled one are separate rows that
both survive. What can happen is that yours stops being the one shown, because
`best_estimates` re-picks on every read and **precision dominates** — a typed `2026-Q4` loses
to a pulled `2026-10-15`. Meeting that in silence is confusing, so each date row carries the
other value and says which is live:

```
theatrical   2026-36        moved 2026-09-01 → 2026-10-15  ◀ more precise
physical                    new 2026-11-17
primary      2026-11-05
digital                     pulled 2027-01-20
```

`moved` is what this refresh changed — `new` when a channel gained its first date (a physical
release finally dated), `dropped` when a source stopped carrying one. `◀` marks the value the
card is showing, with the reason it won, read straight off the ranking key so the two cannot drift apart. Rows with
nothing to report stay blank, so the ones that moved are the ones you see.

There is no "always prefer mine" — if you disagree with the ranking, sharpen your own date
and it wins on its own merits.

### Reviewing before adding

**e** on a candidate opens it as a draft instead of writing it: title, kind, tech category
and a date, with the line it was inferred from stated underneath. **ctrl+s** adds it, **esc**
drops it, and nothing is written either way until you say so.

It matters most for an entry nobody has published yet. Category is inferred from the name, and
a product line can change category between generations — a handheld's successor need not be a
handheld — so the guess needs somewhere to be corrected. Correcting it is not cosmetic: the
category picks which sites the card's Sources section sends you to.

What is *never* inferred is a date. Successor cadence looks predictable and isn't — across
real predecessor chains, guessing the next release from the previous interval lands a median
of ~200 days out, and inside 30 days approximately never. The predecessor's own date is shown
as an anchor to type against instead.

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
