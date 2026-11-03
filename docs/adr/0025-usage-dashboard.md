# ADR-0025 — A usage dashboard drawn from each workspace's own numbers

- **Status:** Accepted
- **Date:** 2026-10-22

## Context

#27 asks for charts of queries, tokens and cost per tenant over time, with a date-range filter, so
that "an admin can view usage and cost trends per tenant". ADR-0012 already records the numbers: one
`UsageRecord` per answer that reached the model, with estimated tokens and a cost baked in at the
price of the day, behind both isolation layers. What it serves is a single total for a window
(`GET /api/usage`), and a trend needs the window broken down.

Three things had to be settled before drawing anything.

**Whose numbers, and who sees them.** There are no roles: every member of a tenant is equal, and
`/api/usage` already answers any of them with their own tenant's spend. "Per tenant" could also be
read as an operator's view across tenants, but that needs an identity the auth model does not have
(every user belongs to one tenant's realm) and a read that row-level security exists to refuse.

**What a day is.** A row has an instant. A day depends on a timezone, and the server's and the
reader's need not agree.

**What counts.** A refusal never reaches the model, so ADR-0012 neither charges nor records it. A
chart labelled "queries" would undercount the questions people asked, leaving out every one that
nothing in their documents could answer.

## Decision

**The dashboard shows each workspace its own usage, to anyone signed in to it, day by day in UTC.**

- **Tenant-scoped, like everything else.** The page reads through a new endpoint that uses the
  tenant-scoped manager, so a workspace can only ever chart its own spend, and it is open to every
  member, as `/api/usage` is. With one role, "an admin" is whoever is signed in. An operator view
  across tenants is a decision of its own, needing a role and a path past row-level security, and
  is not made here.
- **`GET /api/usage/daily?start=&end=`** returns every UTC day of the range with its answers,
  input and output tokens and estimated cost, **zeros included**, and the range's totals.
  - Calendar dates only (`YYYY-MM-DD`), both ends included, defaulting to the 30 days ending today.
  - One grouped statement produces every day, and the totals are summed from those days in
    `Decimal`, so they cannot disagree with the days they summarise.
  - The range is checked before anything is read. A malformed date, an inverted range, a start in
    the future or a range longer than 366 days is a 400. An end in the future is moved back to today
    and the response says so: the days after today have had no usage yet, and reporting them as
    zeros would describe days that have not happened.
  - Zeros are in the response, not filled in by the browser, so a day has one definition, the API's.
- **UTC days, said on the page.** A stored row has an instant and no reader, so the API counts in
  UTC and the page says "Days are UTC". The frontend formats each day as the UTC calendar day it is;
  parsed naively, `2026-10-22` is midnight UTC, which a reader in California would see as the 21st.
- **"Answers", not "queries".** The charts count what the usage rows record, answers that reached
  the model, and the empty state says a refusal costs nothing and is not counted.
- **Server-rendered, with the range in the URL.** `/usage?start=…&end=…` is rendered by a Server
  Component that calls Django with the session's token (ADR-0013 §3); the filter is five preset
  links and a plain GET form. Every view is a link that can be shared, reloaded or gone back to, and
  the page needs no client-side data fetching.
- **Line charts drawn as SVG, without a charting library.** One measure per chart, so nothing
  shares an axis it does not belong on: answers, tokens (input and output, with a legend), and
  estimated cost. Each axis runs from zero to the smallest round number at or above the peak. The
  charts are Client Components only for their readout: pointing anywhere over a chart snaps a
  crosshair to the nearest day and lists its values, and the arrow keys do the same once the chart
  has focus. Every value is also in a table of every day, and each drawing has a one-sentence
  summary for a screen reader.
- **The money stays exact.** The browser never adds up money. The totals come from the API, every
  amount is printed from the API's decimal string, exactly below a dollar ($0.0105, not $0.01) and
  to the cent above it, and the numbers the charts parse only place the line.
- **Chart colours are their own tokens.** `--series-1` and `--series-2` (blue, then orange) join
  the design system in both themes. The accent still means "grounded and cited" and nothing else
  (ADR-0014), and status colours still mean state. The pair was checked for separation under
  red-green colour blindness and for 3:1 against `--surface` in both themes, and `tokens.test.ts`
  now holds them to the 3:1.

### Rejected alternatives

- **A charting library** (Recharts, Chart.js, visx). Three line charts do not need one, and each of
  them would ship tens of kilobytes of client JavaScript, draw in its own colours unless taught the
  tokens, and render on the client what the server can render once.
- **Fetching in the browser through the BFF proxy.** It would need client state for what the URL
  already holds, and a loading state for a page that can arrive complete.
- **Days in the reader's timezone.** A `tz` parameter would make one range total differently for
  two readers of the same workspace, and the cost has to be one number. UTC, stated, is the honest
  default; a timezone setting per workspace is the way to change it later.
- **Weekly buckets for long ranges.** A line carries 366 points legibly, so a second granularity,
  and the question of when to switch, buys nothing yet.

## Consequences

- An admin, meaning any member today, can see the trend of their workspace's answers, tokens and
  estimated cost for any range up to a year, and the page reports the same estimates ADR-0012
  records, with the same limits: estimated tokens, and answer generation only, since embedding cost
  is still unmetered.
- A reader far from UTC sees an evening's answers on the next or previous day's point. The page says
  the days are UTC, and the table lists each day by its UTC date.
- One more read on the `read` throttle per page view, and one grouped statement over the
  `(tenant, created_at)` index per view. Rows still accumulate unrolled, as ADR-0012 notes.
- When roles arrive, the usage endpoints and this page are the first place a member/admin split
  would apply, since cost is the one thing here a workspace might not show every member.
- A third series on a chart needs its colour checked again, not a third hue picked by eye.
