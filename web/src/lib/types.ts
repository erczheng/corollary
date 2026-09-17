/**
 * Shared types, label maps, and enum-ish constants for the mock data layer.
 *
 * Split out of `mockData.ts` so that production code can pull in the shapes
 * the eventual API will return without also bundling every seeded fixture —
 * `mockData.ts` holds the seeded PRNG and the generated data; this file
 * holds everything about their *shape*, including the label/class lookups
 * used to render them. Phase 2 swaps fixtures for TanStack Query hooks; the
 * types here are what both sides agree on.
 */

/** Which Alpaca account's keys are in use. Defined here rather than in the
 * UI store because it keys the fixtures below; the store imports it back.
 * In Phase 2 this keys the API request instead. */
export type AccountMode = 'paper' | 'cash'

export interface PricePoint {
  date: string // ISO date
  value: number
}

/** One point on a price series finer than a day.
 *
 * A second point type rather than a nullable field on `PricePoint`, and the
 * reason is load-bearing: `PricePoint.date` is a **day**, so seventy-eight
 * five-minute points would all share one `date` and a chart keyed on it
 * would draw them at the same x. That looks like working code and is not.
 *
 * `at` is a full ISO instant in UTC, stamped at the interval's **open**.
 * Render it in America/New_York — unlike a bare `YYYY-MM-DD` it carries its
 * own offset, so the date-only trap in format.ts#formatExpiry does not
 * apply here. */
export interface IntradayPoint {
  at: string // ISO instant, UTC
  value: number
}

export type ChartRange = '1D' | '1W' | '1M' | '3M' | 'YTD' | '1Y' | 'All'

export interface Recommendation {
  id: string
  symbol: string
  /** Strike or strike pair, pre-formatted for display: '$235', '$560/$555'. */
  strike: string
  /** Contract structure: 'Call', 'Put', 'Put Credit Spread'. */
  structure: string
  /** Calendar date (YYYY-MM-DD), not an instant — render with
   * format.ts#formatExpiry, which formats in UTC. Formatting this in ET
   * shifts it a day earlier. */
  expiry: string
  /** Setup class from the taxonomy. Drives the confidence base rate
   * (PRD.md §6.3) — not the same thing as the human-readable reason. */
  setup: string
  /** Why this surfaced: the scanner rule that matched, or the LLM's
   * annotation (PRD.md §6.1). */
  reason: string
  confidence: number | null // null renders as "—" — no base rate yet
  unvalidated: boolean
  origin: 'scanner' | 'llm'
}

export type ActivityStatus = 'filled' | 'rejected' | 'pending' | 'canceled'

export const ACTIVITY_STATUS_LABEL: Record<ActivityStatus, string> = {
  filled: 'Filled',
  rejected: 'Rejected',
  pending: 'Pending',
  canceled: 'Canceled',
}

export type ActivityAction = 'BTO' | 'STC' | 'STO' | 'BTC' | 'DEPOSIT' | 'WITHDRAWAL'

/** Order actions keep their standard options abbreviations — BTO/STC/STO/
 * BTC are the jargon, not shorthand to be expanded. Cash movements are
 * not orders and read as plain words. */
export const ACTIVITY_ACTION_LABEL: Record<ActivityAction, string> = {
  BTO: 'BTO',
  STC: 'STC',
  STO: 'STO',
  BTC: 'BTC',
  DEPOSIT: 'Deposit',
  WITHDRAWAL: 'Withdrawal',
}

/** Text color for a rendered activity status.
 *
 * Read by the `full` layout of ExecutionsTable — Activity's Status column.
 * The Dashboard's `summary` layout has no room for one and puts the status
 * on hover instead, so this map is live on one page of the two. Keeping it
 * here rather than beside either table is what lets the two agree.
 *
 * `rejected` takes `error`, not `bearish` — DESIGN.md assigns error to
 * "Rejections, failures", and a rejected order is a system/rule outcome
 * rather than a losing position. Both clear 6.1:1 on surface in light and
 * 10.9:1 in dark; verified. */
export const ACTIVITY_STATUS_CLASS: Record<ActivityStatus, string> = {
  filled: 'text-on-surface-variant',
  rejected: 'text-error',
  pending: 'text-caution',
  canceled: 'text-on-surface-variant',
}

export interface ActivityItem {
  id: string
  time: string // ISO datetime
  contract: string
  action: ActivityAction
  price: number | null
  quantity: number | null
  pnl: number | null
  /** Return on the closed trade as a percentage of cost basis. Null
   * wherever `pnl` is null — an opening fill has realized nothing yet, and
   * a rejection never will. Carried beside `pnl` rather than derived from
   * it: the dollar figure and the percentage answer different questions
   * (how much, how well), and the Activity header averages both
   * separately (PRD.md §8.2). */
  pnlPct: number | null
  /** Signed cash movement for DEPOSIT / WITHDRAWAL — positive in, negative
   * out. Null on trades, which report `pnl` instead. Kept separate from
   * `pnl` on purpose: money you moved into the account is not money the
   * account made, and summing the two would overstate performance. */
  amount: number | null
  status: ActivityStatus
  /** Why the order was refused, in the rule's own words. **The wire sends
   * `null`, not an absent key** — confirmed against `/api/activity` and
   * `/api/account/transfers` — so this is `string | null` as well as
   * optional. Narrowed to `?: string` a null read as "present", and every
   * `rejectionReason &&` guard downstream silently did the right thing for
   * the wrong reason. */
  rejectionReason?: string | null
}

export interface ActivityStats {
  /** Null where the account has no trade of that kind yet — an average
   * over zero trades is not zero, it is unknown, and rendering it as
   * $0.00 would claim a result that does not exist. */
  avgWin: number | null
  avgWinPct: number | null
  avgLoss: number | null
  avgLossPct: number | null
  lifetimePnl: number
  wins: number
  losses: number
  /** How many closings the figures above are missing. 0 on a complete
   * ledger.
   *
   * An adjusted root reaching an OPEXC/OPASN branch is refused rather
   * than guessed: a real GME1 delivers 100 GME *plus* 10 GME.WS while
   * `multiplier` and `size` both report 100, so there is no honest
   * dollar figure to book, and inventing one would suppress a Phase 6
   * `max_daily_loss_pct` halt that should have fired. The contracts
   * still left the book, so the lifetime figure is genuinely incomplete
   * and the page says so beside the cards.
   *
   * This counts closings that produced no realized trade — an
   * arithmetic fact about two tables. It deliberately does not claim
   * each one's *cause*: the rejection rule is logged by the ledger and
   * no Phase 2 table stores it. */
  notBooked: number
  /** The contracts behind `notBooked`, OCC-spelled and sorted. A bare
   * count cannot be reconciled by hand; a symbol can be looked up in the
   * log and in the broker's own history. */
  notBookedSymbols: string[]
}

export type OrderType = 'market' | 'limit' | 'stop' | 'stop_limit'

/** The four order sides, in the jargon. Defined here rather than in
 * orders.ts because `WorkingOrder` below needs it and orders.ts already
 * imports this file — putting it there would make the two circular. */
export type OrderSide = 'BTO' | 'STC' | 'STO' | 'BTC'

export const ORDER_TYPE_LABEL: Record<OrderType, string> = {
  market: 'Market',
  limit: 'Limit',
  stop: 'Stop',
  stop_limit: 'Stop-Limit',
}

/** Day and GTC are the only two an option order accepts — IOC, FOK, OPG
 * and CLS are all rejected. Verified against Alpaca's order-type matrix. */
export type TimeInForce = 'day' | 'gtc'

export const TIME_IN_FORCE_LABEL: Record<TimeInForce, string> = {
  day: 'Day',
  gtc: 'GTC',
}

/** Where an attached exit physically lives. The difference is whether it
 * survives Corollary being down: a broker-side OCO fires regardless, a
 * Corollary-managed exit does not exist while the engine is stopped. The
 * row says which, because that is not a detail. */
export type ExitHolder = 'broker' | 'corollary'

export interface PositionLeg {
  /** OCC format: underlying + YYMMDD + C/P + 8-digit strike ×1000. */
  symbol: string
  strike: number
  right: 'call' | 'put'
  side: 'long' | 'short'
  /** Alpaca requires leg ratios in simplest form — the GCD across a
   * multi-leg order's ratios must be 1, or the order is rejected. */
  ratio: number
}

/** A manual exit attached to a position, modelled on Alpaca's
 * `order_class: oco`: a take-profit limit paired with a stop, where
 * supplying `stopLimitPrice` makes the stop leg a stop-limit.
 *
 * A position holds at most one of these. Editing replaces it in place —
 * cancelling and re-submitting would leave a window with no exit on the
 * position at all, which is the window a fast market runs through. */
export interface AttachedExit {
  takeProfit: number
  stopPrice: number
  stopLimitPrice: number | null
  timeInForce: TimeInForce
  heldBy: ExitHolder
}

/** The exits a strategy manages on its own positions (PRD.md §5.1). These
 * stop applying the moment the position is detached. */
export interface ManagedExit {
  profitTargetPct: number
  stopLossPct: number
  timeStopDte: number
}

export interface Position {
  id: string
  symbol: string
  contract: string
  /** The **contract's** last traded price, not the underlying's — it sits
   * between `bid` and `ask`, which is what anyone reading a row of three
   * price columns already assumes. It held the underlying's price until
   * the Open Positions rework; one row carrying two instruments with
   * nothing in the names to say so was a bug waiting for a chart. */
  last: number
  /** The underlying's price. Read by the payoff curve and nothing else. */
  underlying: number
  costBasis: number
  value: number
  quantity: number
  pnl: number
  pnlPct: number
  bid: number
  ask: number
  /** Which side the position is on. Determines how it closes — a long is
   * sold to close at the bid, a short is bought to close at the ask — so
   * Flatten cannot generate a correct execution without it. */
  direction: 'long' | 'short'
  /** More than one leg means multi-leg, which Alpaca will only accept as a
   * limit order. Order-type availability is derived from this, never
   * hardcoded per position. */
  legs: PositionLeg[]
  /** Calendar date (YYYY-MM-DD), not an instant — the single most
   * time-sensitive fact about an option, and until now it existed only
   * inside the contract string and the legs' OCC symbols, where nothing
   * could count it down. Render with `formatExpiry`, which formats in UTC;
   * formatting a date-only value in ET shows the previous day. */
  expiry: string
  /** Which strategy manages this position, or null once detached. */
  strategyId: string | null
  /** Which strategy opened it, or null where nothing here opened it.
   *
   * Never cleared once set, so detaching is reversible: without this,
   * reattaching could only guess, and the obvious guess — whichever
   * strategy happens to be active now — is wrong whenever you've switched
   * strategies since the position was opened.
   *
   * **Null on every live position**, and the server says so: a broker
   * position Corollary did not open has no strategy behind it, and
   * `PositionOut.opened_by_strategy_id` is `str | None` rather than carry a
   * made-up id that resolves to nothing. Widened here to match, which the
   * server schema explicitly asked the frontend dispatch to do. */
  openedByStrategyId: string | null
  managedExit: ManagedExit | null
  attachedExit: AttachedExit | null
  /** Position value over the life of the position. Starts at `costBasis`
   * and ends at `value` — see buildValueHistory. */
  valueHistory: PricePoint[]
}

export interface UnderlyingQuote {
  symbol: string
  price: number
  /** The **vendor's** observation timestamp for `price` — the quote, print
   * or daily bar the number came from, never a clock on this side of the
   * wire. ISO-8601 with a `Z`, aware UTC, straight from the server.
   *
   * Decision 18's rule 1: one quote map, two writers (this poll and the
   * websocket), and without a stamp the last arrival wins — which on a
   * 400ms poll racing a push means the screen flickers backwards in time.
   * `quotes.ts` merges on it. */
  at: string
  /** Yesterday's close. The daily change is measured from here, not from
   * the first point of the series — a 60-session chart's left edge is two
   * months ago and "today" measured against it is not today.
   *
   * **Null when the feed carried no prior daily bar** — a newly listed name,
   * or one that did not trade the previous session. Anchoring a change to a
   * close nobody measured invents the entire move, so this stays null and
   * the chart's readout says so instead. */
  previousClose: number | null
  /** Daily closes, oldest first, ending at today's live price. Served when
   * the request asks for `timeframe=1D` — the default — and **empty at every
   * other timeframe**, where `intraday` carries the series instead. */
  history: PricePoint[]
  /** The same series at a resolution finer than a day: served when
   * `timeframe` is `1Min`, `5Min`, `15Min` or `1H`, and empty at `1D`.
   *
   * **Never populated at the same time as `history`.** Whichever is
   * non-empty is the answer, which is how the response states its own
   * resolution. Both empty means the window held no session at all — `1D`
   * asked on a Sunday — and the honest answer there is nothing to draw. */
  intraday: IntradayPoint[]
}

/** An order that has been placed and hasn't filled.
 *
 * Only non-market orders appear here: a market order fills, it does not
 * sit and work. Until this existed the terminal had no concept of "an
 * order I placed that hasn't happened yet", which made Activity a record
 * of the past rather than the ledger of record it claims to be.
 *
 * Attached exits are deliberately *not* modelled here. They live on the
 * position, which is where they are edited and cancelled, and duplicating
 * them into a second list would give the same thing two homes that could
 * disagree. */
export interface WorkingOrder {
  id: string
  /** The position this order acts on, or null for an order that opens a
   * new one — at that point there is no position yet, which is the whole
   * difference between the two. Exactly one of this and `contractKey` is
   * set. */
  positionId: string | null
  /** The contract an *opening* order rests against, null once it belongs
   * to a position. A closing order fills from its position mark; an
   * opening one has no position to mark, so it fills from the chain. */
  contractKey: string | null
  /** Denormalised for display, so the list renders without resolving the
   * position — which may have been closed out from under it. */
  contract: string
  side: OrderSide
  orderType: Exclude<OrderType, 'market'>
  quantity: number
  limitPrice: number | null
  stopPrice: number | null
  timeInForce: TimeInForce
  placedAt: string
  /** The pending row this order wrote to the activity feed. Cancelling
   * flips that row to `canceled` rather than appending a second one — the
   * order had one life and the ledger should show it once. */
  activityId: string
}

export type Sentiment = 'bullish' | 'bearish' | 'neutral' | 'unclassified'

/** Which of PRD.md §9's three tiers produced a label.
 *
 * Carried on the item because the tiers do not have equal standing and the
 * feed should not pretend they do: tier 1 is a score the vendor shipped,
 * tier 2 is a deterministic pattern match on a high-signal event, and tier
 * 3 is the LLM. Settings reports accuracy *per tier*, so a reader comparing
 * that table against a headline has to know which row the headline belongs
 * to.
 *
 * It is also the only honest account of `unclassified`: tiers 1 and 2
 * always publish a direction, so an unlabelled item is always tier 3
 * falling below its confidence threshold. Silence beats a wrong label. */
export const SENTIMENT_LABEL: Record<Sentiment, string> = {
  bullish: 'Bullish',
  bearish: 'Bearish',
  neutral: 'Neutral',
  unclassified: 'Unclassified',
}

/** Four characters, for the feed's Sentiment column.
 *
 * A dense ledger has no room for "Unclassified" spelled out on every row,
 * and these are the abbreviations a tape actually uses. The full word stays
 * reachable — the cell carries `SENTIMENT_LABEL` as its title, and the
 * filter dropdown names them in full. */
export const SENTIMENT_SHORT: Record<Sentiment, string> = {
  bullish: 'BULL',
  bearish: 'BEAR',
  neutral: 'NEUT',
  unclassified: 'UNCL',
}

/** Text colour for a sentiment label in a dense row.
 *
 * The parallel of `ACTIVITY_STATUS_CLASS`, and deliberately the same shape:
 * ExecutionsTable already established that a status-like value in a packed
 * table is small coloured text rather than a pill, because a chip per row
 * sets the row height for the sake of a two-word label. Chips stay for
 * places with room to breathe.
 *
 * `unclassified` takes `caution`, never `error` — the LLM declining to
 * commit is the system working as PRD.md §9 specifies, not a failure. And
 * `neutral` the token is 4.27:1 and below the text floor, so a neutral
 * label reads in `on-surface-variant` instead. */
export const SENTIMENT_CLASS: Record<Sentiment, string> = {
  bullish: 'text-bullish',
  bearish: 'text-bearish',
  neutral: 'text-on-surface-variant',
  unclassified: 'text-caution',
}

export type SentimentTier = 'provider' | 'rules' | 'llm'

export const SENTIMENT_TIER_LABEL: Record<SentimentTier, string> = {
  provider: 'Provider',
  rules: 'Rules',
  llm: 'LLM',
}

export const SENTIMENT_TIER_DETAIL: Record<SentimentTier, string> = {
  provider: 'Tier 1 — a score shipped by Finnhub or Alpaca, published as-is.',
  rules: 'Tier 2 — a deterministic headline pattern for a high-signal event.',
  llm: 'Tier 3 — the LLM, batched 20 headlines per call and cached by article ID. Publishes a direction only above its confidence threshold.',
}

export interface NewsItem {
  id: string
  time: string
  /** A ticker, or `MARKET` for a story about no single name. */
  ticker: string
  headline: string
  sentiment: Sentiment
  publisher: string
  sector: string
  /** Which tier of PRD.md §9 produced `sentiment`. */
  tier: SentimentTier
}

/** The sector `MARKET` items are filed under. Macro is a sector in the
 * feed's sense — "what is this story about" — even though it is not one in
 * the GICS sense the consensus panel uses. */
export const MACRO_SECTOR = 'Macro'

export interface SentimentComponent {
  name: string
  description: string
  /** 0-100, z-scored on a trailing window and rescaled. */
  score: number
}

export interface SocialAttentionItem {
  ticker: string
  /** Messages in the last session. */
  mentions: number
  /** The 30-day average this session is measured against. */
  baselineMentions: number
  /** Messages carrying a user-applied bull/bear label — roughly 30-50% of
   * them, per PRD.md §8.3. Sentiment is aggregated over these and nothing
   * else, and the count is displayed so that a direction drawn from eighty
   * messages is visibly not the same claim as one drawn from two thousand. */
  labeledCount: number
  sampleSize: number
  sentiment: Sentiment
}

export interface SectorConsensus {
  sector: string
  etf: string
  /** The largest constituent, which the roll-up is weighted toward. */
  leader: string
  /** Percentages of covering analysts. The three sum to 100. */
  buy: number
  hold: number
  sell: number
  asOf: string
}

export type CalendarEventType = 'earnings' | 'economic' | 'central-bank' | 'dividend' | 'geopolitical'

export const CALENDAR_TYPE_LABEL: Record<CalendarEventType, string> = {
  earnings: 'Earnings',
  economic: 'Economic',
  'central-bank': 'Central bank',
  dividend: 'Dividend',
  geopolitical: 'Geopolitical',
}

export interface CalendarEvent {
  id: string
  /** The **Eastern** session the event falls on, as a calendar date.
   *
   * Carried separately from `at` rather than derived from it, because
   * deriving it is the bug: `2026-08-12T00:00:00Z` is midnight UTC, which
   * is 8pm ET on **August 11**, so an ex-dividend date stored as an instant
   * groups under the day before the one it is. Grouping runs off this
   * field, and `news.test.ts` pins that every timed event's ET date matches
   * it — the same trap `formatExpiry` documents, one screen over. */
  date: string
  /** The scheduled instant, or `null` when the event is a date rather than
   * a time.
   *
   * An ex-dividend date and a trade-council session have no 8:30am; they
   * are properties of a day. Storing a placeholder midnight for them is how
   * a row ends up reading "7:00 PM" for something that never had a time,
   * and on the wrong day at that. */
  at: string | null
  type: CalendarEventType
  title: string
  ticker?: string
}

export interface OptionContract {
  symbol: string
  strike: number
  /** YYYY-MM-DD. A calendar date, not an instant — render it with
   * formatExpiry, which parses UTC. Formatting it in ET shows the
   * previous day. */
  expiration: string
  type: 'call' | 'put'
  /** The last print, else the session's close, else the quote mid. **Null on
   * a contract that has never traded and has no quote** — 26 of 100 on the
   * recorded NVDA page. Not guaranteed to sit inside `[bid, ask]`: a print is
   * a fact about the past and a spread is a fact about now. (A *position's*
   * `last` is a different number with a different invariant.) */
  last: number | null
  /** Yesterday's settle. Carried rather than derived so that `change` stays
   * anchored while `last` moves: a poll that re-marks the contract updates
   * the price and the change follows from this, instead of the two drifting
   * apart into a percentage measured against nothing. */
  previousClose: number | null
  change: number | null
  changePct: number | null
  /** **Null is not a zero bid.** Alpaca documents `bp: 0` as *"the security
   * has no active bid"*, and it is 49 of 100 contracts on a real NVDA page.
   * A mid taken from an invented bid is half the ask, and that mid is the
   * input to a derived IV — an invented number under the word IV. */
  bid: number | null
  ask: number | null
  /** Contracts traded this session. Null before the first print. */
  volume: number | null
  /** **Null means unavailable, and 0 means nobody holds one** — two
   * different facts, so the column renders them differently and never as a
   * blank. Decision 15: OPRA is paywalled on Basic and the one free
   * publisher forbids automated retrieval, so this arrives only where the
   * vendor happened to supply it. The *"highest open interest"* screen is
   * removed rather than ranking on nulls. */
  openInterest: number | null
  /** Implied volatility, vendor-solved where Alpaca's own Black-Scholes
   * succeeded and derived locally where it did not. Null where neither
   * worked. Which of the two a number came from arrives as `ivSource`,
   * which this interface deliberately does **not** declare — it is a
   * documented server-side addition (`tests/api/test_schema_contract.py`),
   * read structurally by `ivSourceOf` in `markets.ts`. */
  iv: number | null
}

export interface ChainSpec {
  symbol: string
  baseIv: number
  /** Scales volume and open interest. SPY trades orders of magnitude more
   * contracts than MSFT, and a chain where every name is equally busy
   * makes "most volume" meaningless. */
  liquidity: number
  seed: number
}

/** Whether the session a figure was measured over had finished.
 *
 * The Volume column means two things and has to say which: during a session
 * it is *traded so far today*, and outside one it is *traded last session* --
 * because a blank column at the weekend answers nobody. A reader who cannot
 * tell them apart compares a partial day against a full one and concludes a
 * stock is quiet when it is mid-morning.
 *
 * The server decides this, from the market calendar. Do not re-derive it from
 * the clock here: `new Date()` is browser-local and every session boundary in
 * this app is a New York one, half-days included. */
export type SessionState = 'in_progress' | 'completed'

export interface StockQuote {
  symbol: string
  name: string
  price: number
  /** The **vendor's** observation timestamp for `price`, exactly as on
   * {@link UnderlyingQuote.at} and from the same server-side helper. This is
   * the payload the shared quote map is polled from, so this is the stamp
   * decision 18's rule 2 orders a poll against a push on. */
  at: string
  /** Yesterday's close — the **basis**, and the only form the daily move
   * takes on this wire. Rule 4 derives `change` and `changePct` from it at
   * read time (`changeOf` / `changePctOf` in `quotes.ts`), because two
   * writers storing a price and a change independently is how a row reports
   * +1.2% beside a price that is down.
   *
   * It is served rather than recovered as `price - change` for a second
   * reason, measured: every money field crosses the wire as an IEEE double,
   * so that subtraction is a third rounding on two already-rounded numbers.
   * On a penny-wide quote the mid lands on a half-cent routinely, and the
   * recovered close then renders a different cent than the server's own —
   * including `+$0.00` in bullish green for a move that really happened.
   *
   * **Null where there is no previous daily bar.** Never 0 — unchanged and
   * unknown are different facts in a column of dollars. */
  previousClose: number | null
  /** Shares traded over the session named by `volumeSession` and
   * `volumeDate`. **Null, not 0**: a zero claims the symbol did not trade. */
  volume: number | null
  /** Which of the two things `volume` means. Null exactly when `volume` is,
   * which is a symbol with no daily bar anywhere in the window. */
  volumeSession: SessionState | null
  /** The trading date `volume` covers, so the column can name the day rather
   * than say "last session". ISO date, parsed as UTC midnight like every
   * other date-only value here — see `formatExpiry`.
   *
   * Per row rather than per table: in the first minutes of a session one
   * symbol can have today's bar while another has not printed yet. */
  volumeDate: string | null
  /** Average daily share volume. Carried per name rather than drawn from
   * one range, because a uniform draw made COST as busy as NVDA and turned
   * "most active" into a reshuffle of the same list.
   *
   * It is also the denominator of relative volume, which is what "trending
   * now" actually means: 4x its usual volume is a stock something is
   * happening to, where raw volume only ever finds the same mega caps. */
  avgVolume: number | null
  /** Billions of dollars, or **null for a fund**. An ETF has no market
   * capitalisation. Rendering that as 0 would sort SPY below every real
   * company and read as a fund worth nothing, so the column shows an em
   * dash and the ranking sorts nulls last rather than treating them as
   * zero. */
  marketCap: number | null
}

/** Which of the one quote map's two writers put a price there.
 *
 * Client-side only, and deliberately absent from every REST model: which
 * endpoint a row arrived on is something the caller knows at the call site,
 * and a server-asserted `'poll'` would be a second copy of that fact to
 * disagree with. */
export type QuoteSource = 'stream' | 'poll'

/** One symbol's live price, merged from both writers — decision 18.
 *
 * **This is the live map's entry type, and the live map is empty on a cold
 * start.** A symbol with no entry is a symbol with no live quote; the caller
 * renders whatever its own query row says and never a fixture. (`underlyings`
 * is the Phase 1 fixture map, pre-seeded with invented prices and moved by
 * the mock random walks. It is not this and the two must not be confused —
 * writing real quotes into it would leave every never-polled symbol
 * rendering an invented price indistinguishable from a real one.)
 *
 * `price`, `at` and `source` travel together and describe one observation:
 * `at` is when the vendor saw *this* price and `source` is which writer
 * carried it. The remaining fields are the **poll's** alone — the stream
 * carries a price and nothing else — so a stream write leaves them exactly
 * as it found them.
 *
 * Nothing derived is stored here. See `changeOf` / `changePctOf`. */
export interface LiveQuote {
  symbol: string
  price: number
  /** The vendor's observation timestamp for `price`. The merge orders on
   * this and on nothing else — never on arrival order, never on a clock
   * read on this side of the wire. */
  at: string
  source: QuoteSource
  /** Yesterday's close, and the only basis there is for a change. **Null
   * means there is no basis to measure a move from** — a newly listed name,
   * or one that did not trade the previous session — and the selectors
   * return null rather than a 0.00 that would claim the price was
   * unchanged. Poll-only. */
  previousClose: number | null
  /** Shares traded over the session named by `volumeSession`. Poll-only. */
  volume: number | null
  volumeSession: SessionState | null
  volumeDate: string | null
  /** The denominator of relative volume. Poll-only. */
  avgVolume: number | null
  /** Null for a fund, never 0. Poll-only. */
  marketCap: number | null
}

export type StrategyStatus = 'draft' | 'backtest' | 'paper' | 'active' | 'retired'

export interface Strategy {
  id: string
  name: string
  version: number
  status: StrategyStatus
  backtest: { winRate: number; profitFactor: number; maxDrawdown: number; trades: number }
  /** Closed live trades **this strategy** placed — all of them validated, by
   * construction. A strategy trades its own setups, and a setup match is what
   * *makes* a trade validated (§6.2).
   *
   * This field carried a note asking for a validated/unvalidated split to be
   * added here. It should not be: the unvalidated bucket is LLM-originated
   * trades with **no setup match**, which is precisely why they cannot be
   * attributed to a strategy. They live account-wide in `LLM_ORIGINATION`
   * and nowhere else, exactly as §6.4's "their own P&L bucket" describes.
   *
   * So the Dashboard's figure was already validated-only. What it was missing
   * was *saying so* — §8.1 wants the exclusion named, and the count comes
   * from `LLM_ORIGINATION.unvalidated`, not from here. */
  live: { winRate: number; profitFactor: number; maxDrawdown: number; trades: number } | null
}

export type ChatRole = 'user' | 'assistant'

/** A strategy the assistant offered.
 *
 * Carries no YAML body: the declarative document, its JSON Schema and the
 * indicator whitelist are Phase 4, and a proposal that printed rules the
 * validator has never seen would be inviting someone to trust them. `rules`
 * is prose describing intent, which is all a proposal can honestly be before
 * the schema exists to check it against.
 *
 * Accepting one lands a `draft` — never anything further along. PRD.md §5.3's
 * promotion gate is what moves a strategy past that, and the whole point of
 * the gate is that nothing skips it. */
export interface StrategyProposal {
  name: string
  summary: string
  rules: string[]
}

export interface ChatMessage {
  id: string
  role: ChatRole
  text: string
  at: string
  proposal: StrategyProposal | null
}

/** One scripted reply.
 *
 * **This is a shell, not a model.** Phase 1 is mock data and the LLM layer is
 * Phase 4, so replies are matched from a fixed table and the panel says so on
 * screen. A chat that answered plausibly while being scripted would be the
 * most misleading thing in the app — everything else here is visibly a
 * fixture, but a convincing sentence reads as a considered answer.
 *
 * Keyword-routed rather than sequential so a question gets a relevant reply,
 * and deterministic either way: same input, same output, no PRNG. */
export interface ChatScript {
  /** Tested against the user's message, lowercased. */
  match: RegExp
  reply: string
  proposal: StrategyProposal | null
}

/** A conversation you have finished with.
 *
 * The transcript is stored **verbatim rather than replayed** through
 * `CHAT_SCRIPT`. An archived conversation is a record of what was actually
 * said at the time; regenerating the replies from today's script would
 * quietly rewrite history the next time the table changes, which is the one
 * thing a history is supposed to be proof against.
 *
 * `title` is derived once, on archive, and stored — see `conversationTitle`
 * in `research.ts`. Deriving it at render time would be re-deriving a
 * constant on every paint for no benefit.
 */
export interface ArchivedChat {
  id: string
  title: string
  /** When the conversation was archived, not when it started. */
  at: string
  messages: ChatMessage[]
}

export interface Trend {
  changePct: number
  comparedTo: string
}

/** Everything that belongs to the *account* rather than to the strategy.
 * Paper and Cash are two different accounts holding different money, so the
 * Paper/Cash toggle has to move all of it — rendering paper's balance, or
 * paper's positions, while Cash is live misreports real money.
 *
 * Win rate is deliberately absent: it's a property of the selected
 * strategy, and it comes off STRATEGIES.live (see the Dashboard).
 *
 * Positions and activity are keyed here as of the Activity page. They were
 * previously a single shared set, which meant Flatten in either mode
 * cleared both books — fine while nothing rendered a per-account position
 * list, wrong the moment Activity did. Note the tradeoff PRD.md §3 calls
 * out: Activity has no Paper/Cash switch on it, so the header carries a
 * read-only account badge to say which book is on screen.
 *
 * The balance fields landed here with the Account page rather than becoming
 * a second account-keyed map beside this one. They are account-scoped for
 * exactly the reason everything else here is, and a parallel
 * `ACCOUNT_SUMMARY[mode]` would be a second place to forget to update. */
export interface AccountSnapshot {
  portfolioHistory: PricePoint[]
  volume24h: number
  balanceTrend: Trend
  volumeTrend: Trend
  positions: Position[]
  activity: ActivityItem[]
  workingOrders: WorkingOrder[]
  /** Total cash in the account, whatever its settlement state.
   *
   * There was a `settled`/`unsettled` split here, removed deliberately:
   * **Alpaca's account endpoint publishes no settlement breakdown**, so both
   * numbers would have been derived from activity dates with nothing to
   * validate them against. See PRD.md §8.6 before adding it back. */
  cash: number
  /** What the broker will let you spend. Paper is a margin account, so this
   * is 2× cash; the Cash account has no margin, so it is *less* than cash.
   * The gap is whatever the broker is holding — the account endpoint reports
   * the figure, never its composition. */
  buyingPower: number
  /** Always ≤ `buyingPower`: **options are not marginable**, so on the
   * margin account this is cash rather than twice it. Sizing an option
   * order against equity buying power overstates capacity by 2× — which is
   * why the two are separate fields instead of one number the caller
   * halves.
   *
   * This is the figure that actually binds an options trader on a margin
   * account, which is why its absence would matter and the settlement split's
   * does not. */
  optionsBuyingPower: number
}

export const ACCOUNT_LABEL: Record<AccountMode, string> = {
  paper: 'Paper',
  cash: 'Cash',
}

/** One of the five ceilings in CLAUDE.md rule 4 / PRD.md §4.
 *
 * `min` and `max` are the editable range, not the enforced one — the engine
 * enforces (rule 4) and this range only stops the *field* from accepting a
 * value no ceiling could sensibly take. They are per-limit rather than one
 * shared 0–100 because `max_concurrent_positions` is a count and the rest
 * are percentages, and a count of 100 concurrent option positions on this
 * account is not a limit, it is the absence of one. */
/** The five keys, as a union rather than a bare string.
 *
 * Both order tickets look a ceiling up by key. A typo in that lookup returns
 * "no limit configured", which is indistinguishable from a real absence at
 * runtime — so it is worth making it a compile error instead. */
export type RiskLimitKey =
  | 'max_risk_per_trade_pct'
  | 'max_daily_loss_pct'
  | 'max_concurrent_positions'
  | 'max_exposure_per_underlying'
  | 'max_net_directional_pct'

export interface RiskLimit {
  key: RiskLimitKey
  label: string
  /** The configured ceiling, or **null when no ceiling is configured**.
   *
   * Nullable because the server declares it nullable (`JsonMoney | None` in
   * `schemas.py`), and the nullability is the point rather than an
   * oversight. Null is not zero, not the shipped default, and not an omitted
   * row — the key is still served, because a page cannot say "no ceiling
   * configured" about a row it never received.
   *
   * Never substitute a number for it. The two order tickets once did this
   * lookup themselves with different fallbacks, `?? 7` in one and `?? 0` in
   * the other, so one reported a ceiling nobody had set and the other
   * reported every trade as over-limit. `riskLimitFor` returns
   * `number | null` for the same reason. */
  value: number | null
  unit: '%' | 'count'
  min: number
  max: number
  /** What this ceiling actually constrains, in one line. The Settings page
   * renders it beside the field: "25%" tells you nothing about whether it
   * is measured against equity or against the position. */
  help: string
}

/** Which kind of setting an audit row describes.
 *
 * PRD.md §4 asks for an audit log on the five risk limits. Feed and
 * notification changes are in the same log rather than in logs of their
 * own, because all three answer the same question at 3pm on a bad day —
 * "did someone change something first?" — and three separate logs is three
 * places to look.
 *
 * Feed changes belong here specifically: switching historical equity bars
 * from SIP to IEX silently reinterprets every `min_avg_volume` in every
 * strategy YAML, since IEX is ~2.5% of US volume (CLAUDE.md). Nothing about
 * that is visible in the strategy document afterwards. */
export type AuditCategory = 'risk' | 'feed' | 'notification'

export interface AuditLogEntry {
  id: string
  time: string
  category: AuditCategory
  /** The stored field key — `max_risk_per_trade_pct`, not "Max risk per
   * trade". Resolved to a label for display by `auditFieldLabel` in
   * settings.ts, so the log stays a record of what changed rather than of
   * how it was worded on the day. */
  field: string
  previousValue: string
  newValue: string
}

/** The eight routable events of PRD.md §10.
 *
 * A key, not the display string. The routing matrix, the severity map and
 * the emitted notifications all have to agree about which event this is,
 * and agreeing on `'engine_error'` survives someone rewording the label —
 * which the table in PRD.md §10 has already done once, curly apostrophe
 * included. */
export type NotificationEvent =
  | 'order_filled'
  | 'order_rejected'
  | 'stop_loss_hit'
  | 'daily_loss_halt'
  | 'engine_error'
  | 'price_alert'
  | 'recommendations_ready'
  | 'strategy_promotion'

export const NOTIFICATION_EVENT_LABEL: Record<NotificationEvent, string> = {
  order_filled: 'Order filled',
  order_rejected: 'Order rejected',
  stop_loss_hit: 'Stop loss hit',
  daily_loss_halt: 'Daily loss halt',
  engine_error: 'Engine error / dead-man’s switch',
  price_alert: 'Price alert on a recommended trade',
  recommendations_ready: 'New recommendations ready',
  strategy_promotion: 'Strategy promotion eligible',
}

/** Routing per channel.
 *
 * These are **defaults**, not invariants. PRD.md §10's table describes the
 * shipped state; Settings may change any cell, including a bell one. That
 * is a deliberate decision — see the critical-event confirm in
 * `isCriticalEvent` (settings.ts), which is what stops a rule-9 dead-man's
 * switch alert from being routed silently to nowhere. */
export interface NotificationRoute {
  event: NotificationEvent
  bell: boolean
  discord: boolean
}

/** One delivered bell notification.
 *
 * `event` carries the type and the title comes off
 * `NOTIFICATION_EVENT_LABEL`, so a notification never stores its own
 * heading — two copies of "Order filled" is two things to reword and one to
 * forget.
 *
 * `account` is the book the event happened in, or **null** for an event
 * that belongs to no book: an engine error is not paper's or cash's, and
 * hiding it because the other account is selected would hide the one class
 * of event you most need to see. Everything else is account-scoped for the
 * same reason positions are — a paper fill announcing itself while Cash is
 * live misreports which money moved. */
export interface Notification {
  id: string
  time: string
  event: NotificationEvent
  /** The specifics: which contract, what price, which rule rejected it.
   * The event label says what kind of thing happened; this says what
   * happened. */
  detail: string
  read: boolean
  account: AccountMode | null
}

export interface DataSourceStatus {
  name: string
  status: 'connected' | 'degraded' | 'disconnected'
  detail: string
}

/** Which Alpaca data plan the account is on.
 *
 * This is a *fact* about the account, not a preference — you cannot select
 * your way onto OPRA. It is here because it gates which feed values are
 * legal, and because the numbers it determines (equity stream symbols,
 * option stream quotes, requests per minute) are the reason this app has a
 * 400ms stream scoped to open positions *and* a separate 2s poll across
 * everything else. */
export type DataPlan = 'basic' | 'algo_trader_plus'

export interface DataPlanCaps {
  label: string
  monthlyUsd: number
  /** The **equity** websocket's symbol cap, or null for unlimited.
   *
   * Deliberately named for the stream it governs: the websocket cap is *two*
   * budgets and not one, and `optionStreamQuotes` carries the other. Anything
   * rendering either must say which stream it means — "capped at 30 symbols"
   * was on three screens before `56e7041`, and it was describing a pool that
   * does not exist. */
  streamSymbols: number | null
  /** The **option** websocket's quote budget. Never null: unlimited is an
   * equities-only sentinel, and the paid plan raises options from 200 to
   * 1,000 rather than removing the ceiling (CLAUDE.md's Alpaca specifics;
   * decision 17 of the Phase 2 design). Every leg of every open position
   * spends one quote, which is why a book of positions streams and a page of
   * chains is polled. */
  optionStreamQuotes: number
  reqPerMin: number
}

export const DATA_PLANS: Record<DataPlan, DataPlanCaps> = {
  basic: {
    label: 'Basic (free)',
    monthlyUsd: 0,
    streamSymbols: 30,
    optionStreamQuotes: 200,
    reqPerMin: 200,
  },
  algo_trader_plus: {
    label: 'Algo Trader Plus',
    monthlyUsd: 99,
    streamSymbols: null,
    optionStreamQuotes: 1000,
    reqPerMin: 10_000,
  },
}

export type FeedKey = 'options' | 'stockHistorical' | 'stockRealtime'

/** A feed setting, mirroring one env var read only inside
 * `data/providers/alpaca.py`. Feed names are configuration and never
 * literals (CLAUDE.md), which is exactly why they are selectable here:
 * upgrading the plan sets all three and changes nothing else in the code. */
export interface DataFeed {
  key: FeedKey
  envVar: string
  label: string
  value: string
  help: string
}

export interface SentimentAccuracy {
  source: string
  tier: string
  accuracy1h: number
  accuracy1d: number
}

/** Presence of one credential, and nothing else about it.
 *
 * There is no `value`, no `masked`, and no last-four, deliberately: rule 6
 * says the UI never renders a key, and `PK••••4F2A` renders four characters
 * of one. Presence is the entire answer the page is allowed to give, and it
 * happens to be the only one worth having — you cannot fix a wrong key by
 * squinting at its suffix.
 *
 * `optional` is what keeps the page from crying wolf: the live keys are
 * *meant* to be absent until Phase 7, so their absence renders as a plain
 * fact rather than a warning. */
export interface ApiKeyPresence {
  envVar: string
  purpose: string
  present: boolean
  optional: boolean
}

/* -------------------------------------------------------------------------
 * Wire responses
 *
 * Shapes that exist only at the API boundary — an envelope, a stated error,
 * a balances read — rather than things the pages model in their own right.
 * They live here with the rest of the contract so there is one home for
 * "what the server sends", the same reason `mockData.ts` was split in the
 * first place.
 *
 * None of these are in `tests/api/test_schema_contract.py`'s `MIRRORED` map
 * yet, which is why they can be added at all: that test pins the twenty
 * interfaces and ten unions above field-for-field against
 * `corollary/api/schemas.py`, and renaming one of those breaks a Python
 * test. These were written from the same file by hand and match it as of
 * step 7; adding them to `MIRRORED` later is a Python-side change and needs
 * no edit here.
 * ---------------------------------------------------------------------- */

/** One page of a long list.
 *
 * `total` is **rows matching the query**, not rows on this page, and the
 * Activity header cards are folded over every trade server-side rather than
 * over `items` — a lifetime figure computed from a window means something
 * narrower than the word. `page` is **zero-based**, which `usePagination`
 * (1-based, client-side) is not; the two are different objects. */
export interface Page<ItemT> {
  items: ItemT[]
  total: number
  page: number
  pageSize: number
  hasMore: boolean
}

/** The stated condition behind every non-2xx, including FastAPI's own 404
 * and 422. `code` is stable and machine-readable — branch on it, never on
 * the prose, which is written for a human and may be reworded. */
export interface ApiErrorBody {
  code: string
  message: string
}

/** How the broker classifies the account's margin. `pdt` is a pattern-day-
 * trader account at 4× intraday; `cash` has no leverage at all. */
export type MarginClassName = 'cash' | 'reg_t' | 'pdt' | 'unknown'

export interface MarginSummary {
  multiplier: number
  marginClass: MarginClassName
  label: string
  note: string
}

/** `GET /api/account` — balances, margin class and options entitlement for
 * one book.
 *
 * `derivedEquity`, `equityReconciles` and `equityDifference` are the
 * server's own arithmetic against the broker's `equity`, not something to
 * recompute in the browser: client-side money arithmetic is display-only,
 * and a second derivation that disagreed would have no way to say which one
 * is right.
 *
 * `cashAccountAvailable` is what the account toggle reads. When it is false
 * the Cash control disables with `cashAccountUnavailableReason` stated —
 * the same condition that answers `?account=cash` with a 409 rather than
 * with paper's numbers under the other book's name. */
export interface AccountResponse {
  account: AccountMode
  status: string
  currency: string
  cash: number
  equity: number
  lastEquity: number
  dayChange: number
  balanceTrend: Trend | null
  buyingPower: number
  optionsBuyingPower: number | null
  longMarketValue: number
  shortMarketValue: number
  netPositionValue: number
  grossPositionValue: number | null
  derivedEquity: number
  equityReconciles: boolean
  equityDifference: number
  margin: MarginSummary
  optionsApprovedLevel: number | null
  optionsTradingLevel: number | null
  tradingBlocked: boolean
  accountBlocked: boolean
  transfersBlocked: boolean
  cashAccountAvailable: boolean
  cashAccountUnavailableReason: string | null
  missingLiveCredentialEnvVars: string[]
}

/** One point on the broker's own equity curve. Every figure is nullable:
 * Alpaca returns nulls for sessions it has no record of, and a zero there
 * would draw a crash that never happened. */
export interface EquityCurvePoint {
  at: string // ISO datetime
  equity: number | null
  profitLoss: number | null
  profitLossPct: number | null
}

/** `GET /api/account/history` — Alpaca's equity curve, with t₀ marked.
 *
 * `t0` is when Corollary first ran, written once and never rewritten, and
 * `pointsBeforeT0` is how much of this curve predates it. `null` there is
 * *unknown*, not zero: zero would claim the whole curve as Corollary's,
 * which beside a strategy win rate is a claim about who made the money. */
export interface PortfolioHistoryResponse {
  account: AccountMode
  period: string
  timeframe: string
  baseValue: number | null
  baseValueAsof: string | null // ISO date
  points: EquityCurvePoint[]
  t0: string | null // ISO datetime
  pointsBeforeT0: number | null
}

/** `GET /api/engine/state` — the halt state and the start marker.
 *
 * `halted` is the cold-start default: the engine comes up halted until its
 * opening snapshot succeeds, and rule 9 requires an explicit human resume
 * out of any halt. Nothing in the client may call resume automatically. */
export interface EngineStateResponse {
  halted: boolean
  haltedReason: string | null
  haltedAt: string | null // ISO datetime
  t0: string | null // ISO datetime
}
