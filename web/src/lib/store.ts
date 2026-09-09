import { create } from 'zustand'
import {
  ACCOUNT_SNAPSHOTS,
  API_KEYS,
  AUDIT_LOG,
  CHAIN_SPEC_BY_SYMBOL,
  CHAT_HISTORY,
  CONTRACT_MULTIPLIER,
  CURRENT_PLAN,
  DATA_FEEDS,
  MARKET_QUOTES,
  NEWS_INCOMING,
  NEWS_ITEMS,
  NOTIFICATIONS,
  NOTIFICATION_ROUTES,
  OPTION_CHAIN,
  RISK_LIMITS,
  STRATEGIES,
  chainDte,
  halfSpread,
  moneyness,
  mulberry32,
  priceContract,
  surfaceIv,
  type UnderlyingQuote,
  type AccountMode,
  type ActivityItem,
  type ApiKeyPresence,
  type ArchivedChat,
  type ChatMessage,
  type AttachedExit,
  type AuditLogEntry,
  type DataFeed,
  type DataPlan,
  type FeedKey,
  type ManagedExit,
  type NewsItem,
  type Notification,
  type NotificationEvent,
  type NotificationRoute,
  type OptionContract,
  type Position,
  type PricePoint,
  type RiskLimit,
  type RiskLimitKey,
  type Strategy,
  type StrategyProposal,
  type StrategyStatus,
  type WorkingOrder,
} from './mockData'
import {
  auditEntry,
  feedOptionsFor,
  notificationAuditField,
  validateRiskLimit,
  type NotificationChannel,
} from './settings'
import { buildNotification, routedTo } from './notifications'
import { loadTheme, saveTheme, type Theme } from './theme'
import {
  archiveChat,
  canTransition,
  chatReply,
  proposalToStrategy,
  type Dispositions,
} from './research'
import {
  estimate,
  estimateOpen,
  exitTrigger,
  isWorkingOrderType,
  occSymbol,
  orderWouldFill,
  type OpenDraft,
  type OrderDraft,
} from './orders'
import { formatExpiry, formatUsd } from './format'

// Defined in `theme.ts`, which owns reading and writing it. Re-exported so
// the store stays the single import site for UI state types.
export type { Theme } from './theme'
export type ExecutionMode = 'manual' | 'auto'
export type { AccountMode }

interface UIState {
  theme: Theme
  /** Which Alpaca account keys are in use. Paper is the only mode that may
   * come up on a cold start (CLAUDE.md rule 5) — this store is never
   * persisted to storage, so a reload always resets here. */
  accountMode: AccountMode
  /** Whether the engine may place orders unattended. */
  executionMode: ExecutionMode
  paletteOpen: boolean
  /** Which strategy is active — only one at a time in v1 (PRD.md §5.2). */
  activeStrategyId: string
  /** Halt stops new entries only; existing positions keep their managed
   * exits. This is UI-side display state for Phase 1 mock data — the real
   * halt is enforced by the engine, never trusted from the client
   * (CLAUDE.md rule 4). */
  isHalted: boolean
  /** Positions and activity live here for Phase 1 only, so that Flatten and
   * Close have something real to act on. Both are server state and move to
   * TanStack Query in Phase 2 — do not grow this into a client-side
   * position ledger.
   *
   * Keyed by account: Paper and Cash are different accounts holding
   * different money, and only one account's keys are in use at a time. Read
   * these as `openPositions[accountMode]`, never as a merged book. */
  openPositions: Record<AccountMode, Position[]>
  activity: Record<AccountMode, ActivityItem[]>
  /** Orders that have been placed and haven't filled. Only non-market
   * orders land here — a market order fills immediately. Attached exits
   * are not duplicated in here; they live on the position. */
  workingOrders: Record<AccountMode, WorkingOrder[]>
  /** Live underlying quotes, keyed by symbol.
   *
   * In the store rather than read straight from the fixture because the
   * tick moves them — a page that streams contract prices while the stock
   * behind them sits frozen is only half live, and the payoff chart's
   * "now" marker would never move. Keyed by symbol because two positions
   * can share an underlying and must never disagree about its price. */
  underlyings: Record<string, UnderlyingQuote>
  /** The live option chain behind the Markets page.
   *
   * Held here rather than read straight from the fixture for the same
   * reason `underlyings` is: the poll moves it. A chain frozen under a
   * moving stock is a screen that looks live and is not.
   *
   * Re-priced from the underlying rather than walked contract by contract —
   * see `pollMarkets`. */
  chain: OptionContract[]
  /** When the last market snapshot arrived, or null before the first one.
   * Separate from `lastTickAt` on purpose: the position stream and the
   * market poll are different feeds with different cadences and different
   * limits, and one status pill covering both would report the wrong thing
   * on whichever page it was not describing. */
  lastPollAt: string | null
  /** The published news corpus, newest first.
   *
   * A **third** feed, and the bar for adding one is that it stands in for a
   * different Alpaca mechanism with different limits — this is the news
   * endpoint, which is neither the 30-symbol websocket nor the snapshot
   * poll, and it is not account-scoped because a headline is not owned by
   * whichever keys are loaded. It also fails differently: silence on a
   * price stream means something broke, while silence here means nothing
   * happened, and one status pill covering both would report the wrong
   * thing on whichever page it was not describing. */
  newsFeed: NewsItem[]
  /** How much of `NEWS_INCOMING` has been released. Held so a session
   * replays identically rather than depending on how long the tab was
   * open. */
  newsReleased: number
  /** When the last news poll completed, or null before the first one.
   * Advances on every poll, including one that returns nothing — see
   * `pollNews`. */
  lastNewsAt: string | null
  /** One news poll. Phase 2 swaps this for the Alpaca news request and
   * nothing downstream changes, because the store already treats a poll as
   * "we asked" rather than "a timer fired". */
  pollNews: () => void
  /** One market snapshot — the whole quoted universe, not just the symbols
   * behind open positions.
   *
   * This is the *poll*, not the stream, and the distinction is a real one
   * from CLAUDE.md: the websocket is capped at 30 symbols on the Basic
   * plan, so it is spent on open positions, while snapshot requests are
   * bounded by a 200/min budget instead and can cover a whole page of
   * chains. Phase 2 swaps this for those requests and nothing downstream
   * changes. */
  pollMarkets: (elapsedMs?: number) => void
  /** Opens a position from a chain row — the Markets ticket.
   *
   * Distinct from `submitPositionOrder`, which acts on something you
   * already hold. Nothing here decides whether the order is *allowed*: in
   * Phase 2 that is `RiskManager.approve()` and nowhere else (CLAUDE.md
   * rule 1), and this becomes the call that happens after it returns. */
  submitOpenOrder: (contract: OptionContract, draft: OpenDraft) => void
  toggleTheme: () => void
  setAccountMode: (mode: AccountMode) => void
  setExecutionMode: (mode: ExecutionMode) => void
  setActiveStrategyId: (id: string) => void
  openPalette: () => void
  closePalette: () => void
  /** Halt and Flatten are distinct actions and never merged (CLAUDE.md
   * rule 7). Halt stops new entries and leaves every open position alone.
   * Flatten closes them all, then halts. If these two ever have the same
   * body again, one of them is wrong. */
  halt: () => void
  resume: () => void
  flatten: () => void
  /** Close or add to one position — the per-row ticket on Activity
   * (PRD.md §8.2). Distinct from Flatten in the same way Flatten is
   * distinct from Halt: acting on a single position is not a book-wide
   * event and must never halt the engine.
   *
   * A close of the full quantity removes the position; a partial close
   * scales what remains. Either way it clears any attached exit, so a
   * position never carries a closing order that no longer describes it. */
  submitPositionOrder: (id: string, draft: OrderDraft) => void
  /** Attaches a manual exit, or edits the existing one in place.
   *
   * Not `attachExit` plus `cancelExit`: the gap between a cancel and its
   * replacement is a window with no exit on the position at all, and the
   * name is what stops a caller from opening that window by hand.
   *
   * Attaching also **detaches the position from its strategy**. Two exit
   * regimes on one position double-close when a cancel races a fill, so
   * there is only ever one party responsible for closing a position. */
  upsertExit: (id: string, exit: AttachedExit) => void
  /** Removes an attached exit outright — a deliberate act, not half of an
   * edit. The position stays detached; reattaching is its own action. */
  cancelExit: (id: string) => void
  detachFromStrategy: (id: string) => void
  /** Hands the position back to **the strategy that opened it**, which
   * means dropping any manual exit — the strategy's own rules resume, and
   * both cannot run. Deliberately takes no strategy argument: reattaching
   * to whichever strategy happens to be active now would silently move the
   * position to a different set of exit rules than it was opened under. */
  reattachToStrategy: (id: string) => void
  /** Cancels a working order and flips its pending row in the ledger to
   * `canceled`, rather than appending a second row — the order had one
   * life and the feed should show it once. */
  cancelWorkingOrder: (id: string) => void
  /** When the last price update arrived, or null before the first one. The
   * header reads this to say whether the page is actually live rather than
   * merely claiming to be. */
  lastTickAt: string | null
  /** One price update.
   *
   * Stands in for the Alpaca WebSocket, which Phase 2 puts in its place.
   * Only the active account ticks: `accountMode` is which keys are in use,
   * so the other book has no stream behind it. CLAUDE.md also caps the
   * stream at 30 symbols on the Basic plan and scopes it to open
   * positions — which is what this does, one symbol per position.
   *
   * This is the mock **broker**, not the risk manager. Fills and exit
   * triggers are simulated here because there is no market to get them
   * from; nothing in this function decides whether an order is *allowed*.
   * That stays with `RiskManager.approve()` in Phase 2.
   *
   * `elapsedMs` is how much time the tick covers, which scales how far
   * prices move. Frequency and volatility stay independent that way. */
  tick: (elapsedMs?: number) => void

  // -------------------------------------------------------------------- //
  // Settings (PRD.md §8.7)
  // -------------------------------------------------------------------- //

  /** The five ceilings of CLAUDE.md rule 4, editable from Settings.
   *
   * Here rather than read from the fixture because **both order tickets
   * quote this number**, and a ceiling that Settings can move has to be a
   * single value they both read. `ChainOrderTicket` previously captured it
   * in a module-level const, which meant an edited limit would never reach
   * it for the life of the tab.
   *
   * This is still display state. The engine enforces (rule 4) and never
   * trusts a limit that arrived from the client — Phase 2 makes editing
   * these a server call, and this becomes the cache of what it returned. */
  riskLimits: RiskLimit[]
  /** Every configuration change, newest first, with its previous value.
   *
   * PRD.md §4 requires this for the risk limits. Feed and routing changes
   * are in the same log because they answer the same question on a bad day:
   * did something change before this started happening? */
  auditLog: AuditLogEntry[]
  notificationRoutes: NotificationRoute[]
  dataFeeds: DataFeed[]
  /** Which credentials the environment supplied — presence only, never a
   * value (CLAUDE.md rule 6).
   *
   * In the store rather than read from the fixture because presence has
   * consequences elsewhere on the page: routing events to Discord with no
   * webhook configured delivers nothing, and Settings says so. Held here so
   * that warning is reachable and testable rather than a branch that cannot
   * render until Phase 2 supplies a real answer. */
  apiKeys: ApiKeyPresence[]
  /** Which Alpaca plan is in force. A fact about the account rather than a
   * preference — it gates which feed values are legal. */
  dataPlan: DataPlan
  /** The bell feed. Not account-keyed like `activity` is: each notification
   * carries its own `account`, because an engine fault belongs to no book
   * and has to appear in both. */
  notifications: Notification[]
  /** Edits one ceiling and records it.
   *
   * Refuses a value that fails `validateRiskLimit`, and refuses a no-op —
   * an audit log full of `7 → 7` is a log nobody will read on the day it
   * matters. */
  setRiskLimit: (key: RiskLimitKey, value: number) => void
  /** Toggles one cell of the routing matrix. Every cell is editable,
   * including a bell one: PRD.md §10's table is the shipped default, not an
   * invariant. The confirm that guards silencing a critical event lives in
   * the Settings UI, since it is a question for a person. */
  setNotificationRoute: (
    event: NotificationEvent,
    channel: NotificationChannel,
    enabled: boolean,
  ) => void
  /** Changes one feed. Refuses a value the current plan cannot serve —
   * requesting `opra` on Basic returns an auth error rather than data, so
   * storing it would break the data layer to no purpose. */
  setDataFeed: (key: FeedKey, value: string) => void
  /** Marks the notifications **visible in the current book** as read.
   *
   * Scoped on purpose: opening the bell in Paper must not clear an unread
   * Cash notification you have never seen. Account-less events are cleared,
   * since those were on screen. */
  markNotificationsRead: () => void
  dismissNotification: (id: string) => void

  // -------------------------------------------------------------------- //
  // Research (PRD.md §8.5)
  // -------------------------------------------------------------------- //

  /** The strategy list, mutable from Research.
   *
   * In the store rather than read from the fixture because Research renames,
   * promotes, retires and deletes them — and because **three other places
   * read a strategy** (the Dashboard's dropdown and win-rate card, and each
   * position row's "managed by" line). A rename that reached Research and not
   * the position rows would leave two names for one strategy on screen. */
  strategies: Strategy[]
  /** The Research chat transcript. Empty on a cold start, which is the
   * designed empty state rather than an accident. */
  chat: ChatMessage[]
  /** Conversations you have finished with, newest first.
   *
   * The live transcript is deliberately *not* in here. A history lists the
   * conversations you could go back to, and the one already on screen is not
   * one of them — keeping it in both places would need the two copies to
   * agree on every keystroke.
   *
   * Seeded from `CHAT_HISTORY` so the menu has contents on a cold start
   * without disturbing the empty transcript that opens beside it. */
  chatHistory: ArchivedChat[]
  /** Files the current transcript away and clears the chat.
   *
   * A no-op on an untouched transcript — `archiveChat` returns null for an
   * empty one, and filing blank rows is how a history stops being worth
   * opening. */
  newChat: () => void
  /** Switches to an archived conversation.
   *
   * Symmetrical with `newChat`: whatever is on screen is filed first, so
   * switching away never discards a transcript. The one being opened leaves
   * the history and becomes the live chat, which keeps the invariant above —
   * a conversation is either current or archived, never both. */
  openChat: (id: string) => void
  /** What has been done with each recommendation, keyed by id.
   *
   * Here rather than in Dashboard's local state, which is where dismissal
   * used to live. Both the Dashboard panel and Research's full table act on
   * the same candidate set, and two components each holding their own idea of
   * what was dismissed would disagree the moment you dismissed on one and
   * looked at the other. */
  dispositions: Dispositions
  /** Marks a recommendation executed.
   *
   * Phase 1 records the intent and nothing else — §11 puts manual execution
   * from Recommended Trades in **Phase 6**, behind the risk manager. Creating
   * an order here would be claiming something reached a broker. */
  executeRecommendation: (id: string) => void
  dismissRecommendation: (id: string) => void
  /** Restores dismissed rows — the scanner rebuilds its candidate set and
   * does not remember what you waved off (§6.5).
   *
   * Leaves executed rows alone. Those are not dismissals; you acted on them,
   * and a refresh that reset them would discard intent. */
  refreshRecommendations: () => void
  /** Appends the message and its scripted reply.
   *
   * The reply comes from `chatReply`, which is a lookup table, not a model.
   * Phase 4 replaces this call and the transcript shape does not change. */
  sendChatMessage: (text: string) => void
  /** Accepts a proposed strategy as a **draft**.
   *
   * Never anything further along: §5.3's promotion gate is what moves a
   * strategy past draft, and the LLM proposing one buys no exemption from it. */
  acceptProposal: (proposal: StrategyProposal) => void
  renameStrategy: (id: string, name: string) => void
  /** Moves a strategy along §5.2's lifecycle.
   *
   * Refuses a transition `canTransition` disallows, so nothing skips a stage —
   * the stages are the evidence, and a draft promoted straight to active has
   * no record for the gate to measure.
   *
   * Promoting one to `active` demotes the incumbent to `paper`, because §5.2
   * allows exactly one active strategy in v1. That demotion is deliberately
   * *not* an offered transition — it is a consequence the store applies, and
   * `paper` is where the incumbent can resume from without losing its record. */
  setStrategyStatus: (id: string, status: StrategyStatus) => void
  /** Deletes a strategy.
   *
   * Refuses to delete the **active** one: retire it first. Deleting the
   * running strategy would leave the engine with nothing managing the
   * positions it opened, and the Dashboard reading a strategy that no longer
   * exists. */
  deleteStrategy: (id: string) => void
}

/** A long is sold to close, a short is bought to close — and each crosses
 * the spread in the opposite direction.
 *
 * `idPrefix` records *why* the position closed: `act-flat-` for a book-wide
 * Flatten, `act-close-` for a single row closed from Activity. Same fill,
 * different decision, and the audit trail should be able to tell them
 * apart. */
function closeExecution(position: Position, at: string, idPrefix: string): ActivityItem {
  const long = position.direction === 'long'
  return {
    id: `${idPrefix}${position.id}`,
    time: at,
    contract: `${position.symbol} ${position.contract}`,
    action: long ? 'STC' : 'BTC',
    price: long ? position.bid : position.ask,
    quantity: position.quantity,
    pnl: position.pnl,
    pnlPct: position.pnlPct,
    amount: null, // a close reports P&L, not a cash movement
    status: 'filled',
  }
}

const round2 = (n: number) => Math.round(n * 100) / 100

/** One bell entry, or null when the routing matrix says this event does not
 * go to the bell.
 *
 * **The gate is applied here, at emission, and nowhere else.** Unchecking
 * "Order filled" in Settings stops future fills from arriving; it does not
 * retroactively erase the fills already in the panel. Those happened, and the
 * record of what happened is not a preference — filtering at read time would
 * let a settings change rewrite history. */
function bellEntry(
  routes: NotificationRoute[],
  event: NotificationEvent,
  key: string,
  detail: string,
  account: AccountMode,
  at: string,
): Notification | null {
  return routedTo(routes, event, 'bell')
    ? buildNotification(event, key, detail, account, at)
    : null
}

/** What a closed position reads as in the bell.
 *
 * P&L runs through `formatUsd` with `signed`, so a loss carries an explicit
 * minus rather than relying on a colour the panel does not apply to this
 * string. DESIGN.md requires the sign textually on every P&L value — a
 * notification read in a screenshot or in grayscale has to still say which
 * way the trade went. */
function exitDetail(position: Position, price: number, reason: string): string {
  return `${position.symbol} ${position.contract} ×${position.quantity} closed at ${formatUsd(price)} — ${reason}. ${formatUsd(position.pnl, { signed: true })}.`
}

/** The exits from PRD.md §5.1's example strategy. Phase 2 reads these off
 * the strategy document itself; Phase 1 has one shape for all of them. */
const DEFAULT_MANAGED_EXIT: ManagedExit = { profitTargetPct: 50, stopLossPct: 200, timeStopDte: 2 }

/** Keeps the value chart honest after the position changes size. Without
 * this the series still ends at the old value and the chart contradicts
 * the row it expands from — the one thing the fixture invariant exists to
 * prevent, undone at runtime. */
function withValuePoint(position: Position, at: string, value: number): PricePoint[] {
  const date = at.slice(0, 10)
  const history = position.valueHistory
  const last = history[history.length - 1]
  return last.date === date ? [...history.slice(0, -1), { date, value }] : [...history, { date, value }]
}

/** Returns the position that remains after selling `quantity` of it, or
 * null when the whole thing is gone.
 *
 * A partial close scales cost basis, value and P&L by what's left, so the
 * row keeps satisfying `value === last × quantity × 100`. It does not
 * scale `pnlPct`: closing half a position doesn't change how well the
 * trade did, only how much of it you still hold. */
function closeQuantity(position: Position, quantity: number, at: string): Position | null {
  if (quantity >= position.quantity) return null

  const remaining = position.quantity - quantity
  const fraction = remaining / position.quantity
  const value = round2(position.value * fraction)

  return {
    ...position,
    quantity: remaining,
    costBasis: round2(position.costBasis * fraction),
    value,
    pnl: round2(position.pnl * fraction),
    pnlPct: position.pnlPct,
    // The old exit was written for the old size and no longer describes
    // this position. One position, one closing order, always current.
    attachedExit: null,
    valueHistory: withValuePoint(position, at, value),
  }
}

function addQuantity(position: Position, quantity: number, price: number, at: string): Position {
  const newQuantity = position.quantity + quantity
  const costBasis = round2(position.costBasis + price * quantity * CONTRACT_MULTIPLIER)
  const value = round2(position.last * newQuantity * CONTRACT_MULTIPLIER)
  // A long gains as value rises above what it paid; a short as value falls
  // below what it took in.
  const pnl = round2(position.direction === 'long' ? value - costBasis : costBasis - value)

  return {
    ...position,
    quantity: newQuantity,
    costBasis,
    value,
    pnl,
    pnlPct: costBasis === 0 ? 0 : round2((pnl / costBasis) * 100),
    valueHistory: withValuePoint(position, at, value),
  }
}

/** Identifies a contract across a re-priced chain. The array is rebuilt
 * every poll, so a resting order cannot hold a reference to the object it
 * was placed against — it holds this instead. */
export function contractKey(c: {
  symbol: string
  expiration: string
  strike: number
  type: 'call' | 'put'
}): string {
  return `${c.symbol}-${c.expiration}-${c.strike}-${c.type}`
}

/** `$230 Call Aug 21` — the shape every contract is written in elsewhere,
 * so one bought from the chain reads identically in the ledger. The symbol
 * is not included: `Position` carries it separately and the activity feed
 * joins the two. */
export function contractLabel(c: {
  strike: number
  type: 'call' | 'put'
  expiration: string
}): string {
  return `$${c.strike} ${c.type === 'call' ? 'Call' : 'Put'} ${formatExpiry(c.expiration)}`
}

/** Builds the position an opening fill creates.
 *
 * A short's cost basis is the credit taken in, and its P&L runs the other
 * way — it gains as the contract cheapens. Both are zero at the instant of
 * the fill, which is the point: a position that opens showing a profit has
 * been marked against the wrong side of the spread. */
function openedPosition(
  contract: OptionContract,
  opts: {
    side: 'BTO' | 'STO'
    quantity: number
    price: number
    at: string
    strategyId: string
    underlying: number
  },
): Position {
  const { side, quantity, price, at, underlying } = opts
  const direction = side === 'BTO' ? 'long' : 'short'
  const costBasis = round2(price * quantity * CONTRACT_MULTIPLIER)
  const value = round2(contract.last * quantity * CONTRACT_MULTIPLIER)
  const pnl = round2(direction === 'long' ? value - costBasis : costBasis - value)

  return {
    id: `pos-open-${contractKey(contract)}-${at}`,
    symbol: contract.symbol,
    contract: contractLabel(contract),
    last: contract.last,
    underlying,
    costBasis,
    value,
    quantity,
    pnl,
    pnlPct: costBasis === 0 ? 0 : round2((pnl / costBasis) * 100),
    bid: contract.bid,
    ask: contract.ask,
    direction,
    legs: [
      {
        symbol: occSymbol(contract.symbol, contract.expiration, contract.type, contract.strike),
        strike: contract.strike,
        right: contract.type,
        side: direction,
        ratio: 1,
      },
    ],
    expiry: contract.expiration,
    // Opened by hand from the chain, so no strategy manages it and there is
    // no managed exit to inherit. Attaching one is a deliberate act on the
    // Activity row, the same as it is for any detached position.
    strategyId: null,
    openedByStrategyId: opts.strategyId,
    managedExit: null,
    attachedExit: null,
    // One point: the position starts where it opened. The chart fills in
    // from there as the mark moves.
    valueHistory: [{ date: at.slice(0, 10), value: costBasis }],
  }
}

/** The tick's price stream. Seeded, so a session replays identically and a
 * screenshot taken twice looks the same — the rule the fixtures already
 * follow, extended to the thing that moves them. */
const priceStream = mulberry32(20261101)

/** The poll draws from its own stream. Sharing one with the position tick
 * would make the Activity page's replay depend on whether you had visited
 * Markets first, which is exactly the flake seeding exists to prevent. */
const marketStream = mulberry32(20261102)

/** How far apart released headlines are stamped, on the fixture's clock.
 *
 * Four minutes, so a run of arrivals reads as a plausible afternoon rather
 * than a burst at one timestamp. Unrelated to the poll interval, which is
 * how often the terminal *asks* — the two would only coincide if news
 * arrived exactly when you looked for it. */
const RELEASE_GAP_MS = 4 * 60 * 1_000

/* Volatility is stated **per second**, not per tick, and scaled by how
 * long the tick actually covered.
 *
 * This is what keeps update frequency and price volatility independent.
 * Per-tick figures tie them together: raising the rate five times would
 * move prices the same distance five times as often, so a stream that was
 * only meant to feel more responsive would also become five times as
 * volatile and run the fixtures away from their starting values within a
 * minute. Change TICK_MS freely; these stay put. */
const CONTRACT_VOLATILITY_PER_SECOND = 0.018
const UNDERLYING_VOLATILITY_PER_SECOND = 0.003

/** The poll's own figure, and much calmer than the stream's.
 *
 * The stream's 0.3%/s is a legibility choice: Activity shows a handful of
 * positions and needs visible movement so a resting exit is reachable
 * without waiting all afternoon. A screener is the opposite problem —
 * scanned across 180 rows for what is unusual, and at 0.3%/s a two-second
 * snapshot moves SPY 0.85%, which put a deep ITM call at +15% on the day
 * and −0.7% two seconds later. Nothing is learnable from a table that
 * jumps like that, and worse, nothing in it is *true*: real quotes do not
 * do this.
 *
 * At 0.06%/s a snapshot moves SPY about a third of a point and an ATM
 * contract a couple of cents — visible, and believable. */
const POLL_UNDERLYING_VOLATILITY_PER_SECOND = 0.0006

/** A tick with no argument is assumed to cover a second — the shape tests
 * use, where the interval is not in play. */
const DEFAULT_TICK_MS = 1_000

/** Re-marks a position at a new contract price, carrying bid, ask, value
 * and P&L with it so the row stays internally consistent — the same
 * invariants `orders.test.ts` asserts about the fixtures. */
function remark(position: Position, price: number, at: string): Position {
  const half = round2((position.ask - position.bid) / 2)
  const last = round2(Math.max(price, 0.01))
  const value = round2(last * position.quantity * CONTRACT_MULTIPLIER)
  const pnl = round2(position.direction === 'long' ? value - position.costBasis : position.costBasis - value)

  return {
    ...position,
    last,
    bid: round2(Math.max(last - half, 0.01)),
    ask: round2(last + half),
    value,
    pnl,
    pnlPct: position.costBasis === 0 ? 0 : round2((pnl / position.costBasis) * 100),
    valueHistory: withValuePoint(position, at, value),
  }
}

/** Applies a change to one position in the active account's book, leaving
 * the other account untouched. Returns state unchanged if the id belongs
 * to a book whose credentials aren't loaded. */
function updatePosition(s: UIState, id: string, fn: (p: Position) => Position): Partial<UIState> {
  const mode = s.accountMode
  if (!s.openPositions[mode].some((p) => p.id === id)) return s
  return {
    openPositions: {
      ...s.openPositions,
      [mode]: s.openPositions[mode].map((p) => (p.id === id ? fn(p) : p)),
    },
  }
}

export const useUIStore = create<UIState>((set) => ({
  // The stored preference, not a constant — this is the one piece of state
  // that survives a reload. See `theme.ts` for why it is the only one.
  theme: loadTheme(),
  accountMode: 'paper',
  executionMode: 'manual',
  paletteOpen: false,
  activeStrategyId: 'strat-1',
  isHalted: false,
  openPositions: {
    paper: ACCOUNT_SNAPSHOTS.paper.positions,
    cash: ACCOUNT_SNAPSHOTS.cash.positions,
  },
  activity: {
    paper: ACCOUNT_SNAPSHOTS.paper.activity,
    cash: ACCOUNT_SNAPSHOTS.cash.activity,
  },
  workingOrders: {
    paper: ACCOUNT_SNAPSHOTS.paper.workingOrders,
    cash: ACCOUNT_SNAPSHOTS.cash.workingOrders,
  },
  underlyings: MARKET_QUOTES,
  chain: OPTION_CHAIN,
  lastPollAt: null,
  newsFeed: NEWS_ITEMS,
  newsReleased: 0,
  lastNewsAt: null,
  pollNews: () =>
    set((s) => {
      const at = new Date().toISOString()

      // The first poll is the initial fetch: it returns the corpus that is
      // already loaded and nothing newer. Releasing an arrival here instead
      // would mean the top of the feed is always a headline that landed
      // after you opened the page and before you could read it, which is
      // not how news arrives — and it would make the newest row on a cold
      // open the one row that was not there a moment ago.
      if (s.lastNewsAt === null) return { lastNewsAt: at }

      // The reserve is finite, and running dry is not an error. On this
      // page a poll that returns nothing is a *successful* poll — no news
      // is the ordinary state of a news feed — so `lastNewsAt` advances
      // either way and the pill keeps reporting a working connection.
      // A price stream saying the same thing would mean something broken;
      // that difference is why this is its own feed and not a branch of
      // `pollMarkets`.
      if (s.newsReleased >= NEWS_INCOMING.length) return { lastNewsAt: at }

      const item = NEWS_INCOMING[s.newsReleased]
      // Stamped against the *fixture's* clock, not the wall clock.
      //
      // MARKET_TODAY is 2026-08-07 and the machine's clock is not, so a
      // released headline stamped `new Date()` would sort months above a
      // corpus it belongs in the middle of, and the feed's newest row would
      // sit alone at the top of an empty day. `lastNewsAt` above is real
      // time because staleness is a real-time question; an article's
      // timestamp is a claim about when it was published.
      const previous = s.newsFeed[0]
      const published = new Date(Date.parse(previous.time) + RELEASE_GAP_MS).toISOString()

      return {
        newsFeed: [{ ...item, time: published }, ...s.newsFeed],
        newsReleased: s.newsReleased + 1,
        lastNewsAt: at,
      }
    }),
  toggleTheme: () =>
    set((s) => {
      const theme = s.theme === 'light' ? 'dark' : 'light'
      saveTheme(theme)
      return { theme }
    }),
  setAccountMode: (accountMode) => set({ accountMode }),
  setExecutionMode: (executionMode) => set({ executionMode }),
  setActiveStrategyId: (activeStrategyId) => set({ activeStrategyId }),
  openPalette: () => set({ paletteOpen: true }),
  closePalette: () => set({ paletteOpen: false }),
  halt: () => set({ isHalted: true }),
  resume: () => set({ isHalted: false }),
  flatten: () =>
    set((s) => {
      // Only the active account's book is touched. That is not a partial
      // flatten: `accountMode` is which Alpaca keys are in use, so the
      // engine only ever holds the positions in that account. The other
      // book belongs to credentials that aren't loaded.
      const mode = s.accountMode
      // Every close is an execution and shows up in the activity feed, the
      // same as it would coming back from the broker.
      const at = new Date().toISOString()
      return {
        openPositions: { ...s.openPositions, [mode]: [] },
        activity: {
          ...s.activity,
          [mode]: [...s.openPositions[mode].map((p) => closeExecution(p, at, 'act-flat-')), ...s.activity[mode]],
        },
        // Flatten closes everything, so every working order in this book is
        // now an order against a position that no longer exists. Leaving
        // them would show orders that can never fill.
        workingOrders: { ...s.workingOrders, [mode]: [] },
        isHalted: true,
      }
    }),
  submitPositionOrder: (id, draft) =>
    set((s) => {
      const mode = s.accountMode
      const position = s.openPositions[mode].find((p) => p.id === id)
      if (!position) return s

      const at = new Date().toISOString()
      const { pricePerContract, side } = estimate(position, draft)
      const quantity = Math.min(draft.quantity, draft.mode === 'close' ? position.quantity : draft.quantity)
      const contract = `${position.symbol} ${position.contract}`

      // A market order fills. Anything else sits and works until it does,
      // which is the whole reason working orders exist — a terminal that
      // fills every order instantly cannot show you an order you regret.
      if (isWorkingOrderType(draft.orderType)) {
        const activityId = `act-${draft.mode}-${position.id}-${at}`
        const pending: ActivityItem = {
          id: activityId,
          time: at,
          contract,
          action: side,
          price: draft.limitPrice ?? draft.stopPrice,
          quantity,
          // Nothing has happened yet, so there is nothing to report.
          pnl: null,
          pnlPct: null,
          amount: null,
          status: 'pending',
        }
        const order: WorkingOrder = {
          id: `wo-${position.id}-${at}`,
          positionId: position.id,
          contractKey: null,
          contract,
          side,
          orderType: draft.orderType,
          quantity,
          limitPrice: draft.limitPrice,
          stopPrice: draft.stopPrice,
          timeInForce: draft.timeInForce,
          placedAt: at,
          activityId,
        }
        return {
          workingOrders: { ...s.workingOrders, [mode]: [order, ...s.workingOrders[mode]] },
          activity: { ...s.activity, [mode]: [pending, ...s.activity[mode]] },
        }
      }

      const fill: ActivityItem = {
        id: `act-${draft.mode}-${position.id}-${at}`,
        time: at,
        contract,
        action: side,
        price: pricePerContract,
        quantity,
        // An opening fill has realized nothing; only a close reports P&L.
        pnl: draft.mode === 'close' ? round2(position.pnl * (quantity / position.quantity)) : null,
        pnlPct: draft.mode === 'close' ? position.pnlPct : null,
        amount: null,
        status: 'filled',
      }

      const next =
        draft.mode === 'close'
          ? closeQuantity(position, quantity, at)
          : addQuantity(position, quantity, pricePerContract, at)
      const closedOut = next === null

      // Note what this does *not* do: it never sets isHalted. Acting on one
      // position is not a book-wide event (CLAUDE.md rule 7).
      return {
        openPositions: {
          ...s.openPositions,
          [mode]: closedOut
            ? s.openPositions[mode].filter((p) => p.id !== id)
            : s.openPositions[mode].map((p) => (p.id === id ? next : p)),
        },
        activity: { ...s.activity, [mode]: [fill, ...s.activity[mode]] },
        // A working order against a position that no longer exists is an
        // order that can never fill. Closing out takes its orders with it.
        workingOrders: closedOut
          ? { ...s.workingOrders, [mode]: s.workingOrders[mode].filter((o) => o.positionId !== id) }
          : s.workingOrders,
      }
    }),
  lastTickAt: null,
  tick: (elapsedMs = DEFAULT_TICK_MS) =>
    set((s) => {
      const mode = s.accountMode
      const at = new Date().toISOString()
      const seconds = elapsedMs / 1_000
      const filled: ActivityItem[] = []
      const closedIds = new Set<string>()
      const consumedOrderIds = new Set<string>()
      /** Bell entries this tick produced.
       *
       * The bell reports what happened **while you were not looking** — the
       * mock broker acting on its own. Deliberately nothing is emitted for
       * halt, flatten, or a market order you just submitted: you were on
       * screen for those, Activity records them, and a notification telling
       * you about your own click is the kind of noise that gets a bell
       * ignored. `daily_loss_halt` and `engine_error` come from the engine in
       * Phase 2 and exist here only as seeds. */
      const notified: Notification[] = []

      // The stocks behind the positions this account holds — one symbol
      // per position, which is what keeps the subscription inside the
      // 30-symbol cap the Basic plan imposes (CLAUDE.md).
      const streamed = new Set(s.openPositions[mode].map((p) => p.symbol))
      const underlyings: Record<string, UnderlyingQuote> = { ...s.underlyings }
      for (const symbol of streamed) {
        const quote = underlyings[symbol]
        if (!quote) continue
        const move = (priceStream() - 0.5) * 2 * UNDERLYING_VOLATILITY_PER_SECOND * seconds
        const price = round2(quote.price * (1 + move))
        const change = round2(price - quote.previousClose)
        underlyings[symbol] = {
          ...quote,
          price,
          change,
          changePct: round2((change / quote.previousClose) * 100),
          // Today's point *is* today's price so far, so it moves rather
          // than a new daily close being appended every two seconds.
          history: [...quote.history.slice(0, -1), { date: quote.history[quote.history.length - 1].date, value: price }],
        }
      }

      const positions = s.openPositions[mode].map((position) => {
        // Brisk for a stock, ordinary for an option — enough movement that
        // a resting order is reachable without waiting all afternoon to
        // see the feature work.
        const drift = (priceStream() - 0.5) * 2 * CONTRACT_VOLATILITY_PER_SECOND * seconds
        const marked = {
          ...remark(position, position.last * (1 + drift), at),
          // Kept in step with the quote rather than drifting on its own:
          // two positions on the same stock must agree about its price.
          underlying: underlyings[position.symbol]?.price ?? position.underlying,
        }

        const exit = marked.attachedExit
        const trigger = exit ? exitTrigger(marked, exit, marked.last) : null
        if (exit && trigger) {
          const price = trigger === 'take_profit' ? exit.takeProfit : (exit.stopLimitPrice ?? exit.stopPrice)
          filled.push({
            id: `act-exit-${marked.id}-${at}`,
            time: at,
            contract: `${marked.symbol} ${marked.contract}`,
            action: marked.direction === 'long' ? 'STC' : 'BTC',
            price,
            quantity: marked.quantity,
            pnl: marked.pnl,
            pnlPct: marked.pnlPct,
            amount: null,
            status: 'filled',
          })
          closedIds.add(marked.id)

          // A take-profit is a fill; a stop is a stop. They are separate
          // events in PRD.md §10 and separately routable, so the same exit
          // firing for two different reasons must not collapse into one
          // notification type — and a stop is a `warning`, never an `error`.
          const takeProfit = trigger === 'take_profit'
          const entry = bellEntry(
            s.notificationRoutes,
            takeProfit ? 'order_filled' : 'stop_loss_hit',
            marked.id,
            exitDetail(marked, price, takeProfit ? 'take profit' : 'stop loss'),
            mode,
            at,
          )
          if (entry) notified.push(entry)
          return marked
        }

        return marked
      })

      // Working orders fill against the price their own position just
      // reached. An order on a position that closed out this same tick can
      // no longer fill, so it is dropped rather than matched.
      const byId = new Map(positions.map((p) => [p.id, p]))
      const remainingOrders = s.workingOrders[mode].filter((order) => {
        // An opening order names a contract, not a position, and fills
        // from the market poll instead — the feed that is actually
        // printing it. The stream carries position marks only.
        if (order.positionId === null) return true
        const position = byId.get(order.positionId)
        if (!position || closedIds.has(position.id)) return false
        if (!orderWouldFill(order, position.last)) return true

        consumedOrderIds.add(order.id)
        if (order.side === 'STC' || order.side === 'BTC') closedIds.add(position.id)

        const fillPrice = order.limitPrice ?? order.stopPrice ?? position.last
        const entry = bellEntry(
          s.notificationRoutes,
          'order_filled',
          order.id,
          `${order.contract} ×${order.quantity} ${order.side} filled at ${formatUsd(fillPrice)}.`,
          mode,
          at,
        )
        if (entry) notified.push(entry)
        return false
      })

      if (filled.length === 0 && consumedOrderIds.size === 0) {
        return {
          openPositions: { ...s.openPositions, [mode]: positions },
          underlyings,
          lastTickAt: at,
        }
      }

      // A working order that filled turns its pending ledger row into a
      // fill, rather than writing a second row beside it.
      const activity = s.activity[mode].map((a) => {
        const order = s.workingOrders[mode].find((o) => o.activityId === a.id && consumedOrderIds.has(o.id))
        if (!order) return a
        const position = order.positionId === null ? undefined : byId.get(order.positionId)
        const closing = order.side === 'STC' || order.side === 'BTC'
        return {
          ...a,
          status: 'filled' as const,
          price: order.limitPrice ?? order.stopPrice,
          pnl: closing && position ? position.pnl : null,
          pnlPct: closing && position ? position.pnlPct : null,
        }
      })

      return {
        openPositions: {
          ...s.openPositions,
          [mode]: positions.filter((p) => !closedIds.has(p.id)),
        },
        workingOrders: { ...s.workingOrders, [mode]: remainingOrders },
        activity: { ...s.activity, [mode]: [...filled, ...activity] },
        underlyings,
        lastTickAt: at,
        // Newest first, matching the feed's stored order. Left untouched when
        // the routing matrix suppressed everything this tick, so an
        // all-unchecked bell does not churn the array on every tick.
        notifications:
          notified.length > 0 ? [...notified, ...s.notifications] : s.notifications,
      }
    }),
  pollMarkets: (elapsedMs = DEFAULT_TICK_MS) =>
    set((s) => {
      const at = new Date().toISOString()
      // **Square root of elapsed time, not elapsed time.** A random walk
      // travels with sqrt(t), so scaling a draw linearly makes a 2s poll
      // five times more volatile per unit time than a 400ms tick rather
      // than the same. Unfixed, one snapshot swung a deep ITM call from
      // +15% to -0.7% on the day, which reads as a broken feed rather
      // than a moving market.
      const seconds = Math.sqrt(elapsedMs / 1_000)

      // Every quoted symbol, not just the ones behind positions. A
      // screener that only moves the six stocks you happen to hold is not
      // a screener.
      const underlyings: Record<string, UnderlyingQuote> = {}
      for (const [symbol, quote] of Object.entries(s.underlyings)) {
        const move = (marketStream() - 0.5) * 2 * POLL_UNDERLYING_VOLATILITY_PER_SECOND * seconds
        const price = round2(quote.price * (1 + move))
        const change = round2(price - quote.previousClose)
        underlyings[symbol] = {
          ...quote,
          price,
          change,
          changePct: round2((change / quote.previousClose) * 100),
          // Today's point *is* today's price so far, so it moves rather
          // than a new daily close being appended every two seconds.
          history: [
            ...quote.history.slice(0, -1),
            { date: quote.history[quote.history.length - 1].date, value: price },
          ],
        }
      }

      // The chain is re-derived from its underlying, never walked
      // independently. Giving each contract its own random step inverts the
      // ladder within seconds — a 225 call printing above the 220 beside
      // it — because nothing would hold the strikes in order. A chain moves
      // because the stock moved.
      const chain = s.chain.map((c) => {
        const spec = CHAIN_SPEC_BY_SYMBOL[c.symbol]
        const spot = underlyings[c.symbol]?.price
        if (!spec || spot === undefined) return c

        const dte = chainDte(c.expiration)
        const last = priceContract(spot, c.strike, dte, spec.baseIv, c.type)
        const m = moneyness(spot, c.strike, dte, spec.baseIv)
        const half = halfSpread(last, m, spec.liquidity)
        // Yesterday's settle does not move during the session, so the day's
        // change follows the price rather than being drawn again.
        const change = round2(last - c.previousClose)

        return {
          ...c,
          last,
          change,
          changePct: round2((change / c.previousClose) * 100),
          bid: Math.min(last, Math.max(0.01, round2(last - half))),
          ask: round2(last + half),
          iv: surfaceIv(spec.baseIv, m),
          // Volume only ever accumulates through a session. A screener
          // sorted on a figure that can fall would reorder backwards.
          volume: c.volume + Math.round(marketStream() * 25 * spec.liquidity * seconds),
        }
      })

      // A resting open order fills against the contract it names, from the
      // feed that is actually printing it. Position-keyed orders fill in
      // `tick` instead, against their own position's mark.
      const mode = s.accountMode
      const byKey = new Map(chain.map((c) => [contractKey(c), c]))
      const filledIds = new Set<string>()
      const opened: Position[] = []
      const fills: ActivityItem[] = []

      const remaining = s.workingOrders[mode].filter((order) => {
        if (order.contractKey === null) return true
        const contract = byKey.get(order.contractKey)
        if (!contract) return true
        if (!orderWouldFill(order, contract.last)) return true

        filledIds.add(order.id)
        const price = order.limitPrice ?? order.stopPrice ?? contract.last
        opened.push(
          openedPosition(contract, {
            side: order.side === 'BTO' ? 'BTO' : 'STO',
            quantity: order.quantity,
            price,
            at,
            strategyId: s.activeStrategyId,
            underlying: underlyings[contract.symbol]?.price ?? contract.last,
          }),
        )
        return false
      })

      if (filledIds.size === 0) {
        return { underlyings, chain, lastPollAt: at }
      }

      const activity = s.activity[mode].map((a) => {
        const order = s.workingOrders[mode].find((o) => o.activityId === a.id && filledIds.has(o.id))
        if (!order) return a
        // An opening fill has realized nothing, so P&L stays null.
        return { ...a, status: 'filled' as const, price: order.limitPrice ?? order.stopPrice }
      })

      return {
        underlyings,
        chain,
        lastPollAt: at,
        openPositions: { ...s.openPositions, [mode]: [...opened, ...s.openPositions[mode]] },
        workingOrders: { ...s.workingOrders, [mode]: remaining },
        activity: { ...s.activity, [mode]: [...fills, ...activity] },
      }
    }),
  submitOpenOrder: (contract, draft) =>
    set((s) => {
      const mode = s.accountMode
      const at = new Date().toISOString()
      const { pricePerContract } = estimateOpen(contract, draft)
      const name = `${contract.symbol} ${contractLabel(contract)}`

      // A market order fills. Anything else rests until the contract
      // reaches it, which is what the working orders list is for.
      if (isWorkingOrderType(draft.orderType)) {
        const activityId = `act-open-${contractKey(contract)}-${at}`
        return {
          workingOrders: {
            ...s.workingOrders,
            [mode]: [
              {
                id: `wo-open-${contractKey(contract)}-${at}`,
                positionId: null,
                contractKey: contractKey(contract),
                contract: name,
                side: draft.side,
                orderType: draft.orderType,
                quantity: draft.quantity,
                limitPrice: draft.limitPrice,
                stopPrice: draft.stopPrice,
                timeInForce: draft.timeInForce,
                placedAt: at,
                activityId,
              },
              ...s.workingOrders[mode],
            ],
          },
          activity: {
            ...s.activity,
            [mode]: [
              {
                id: activityId,
                time: at,
                contract: name,
                action: draft.side,
                price: draft.limitPrice ?? draft.stopPrice,
                quantity: draft.quantity,
                pnl: null,
                pnlPct: null,
                amount: null,
                status: 'pending' as const,
              },
              ...s.activity[mode],
            ],
          },
        }
      }

      return {
        openPositions: {
          ...s.openPositions,
          [mode]: [
            openedPosition(contract, {
              side: draft.side,
              quantity: draft.quantity,
              price: pricePerContract,
              at,
              strategyId: s.activeStrategyId,
              underlying: s.underlyings[contract.symbol]?.price ?? contract.last,
            }),
            ...s.openPositions[mode],
          ],
        },
        activity: {
          ...s.activity,
          [mode]: [
            {
              id: `act-open-${contractKey(contract)}-${at}`,
              time: at,
              contract: name,
              action: draft.side,
              price: pricePerContract,
              quantity: draft.quantity,
              // An opening fill has realized nothing; only a close reports
              // P&L.
              pnl: null,
              pnlPct: null,
              amount: null,
              status: 'filled' as const,
            },
            ...s.activity[mode],
          ],
        },
      }
    }),
  cancelWorkingOrder: (id) =>
    set((s) => {
      const mode = s.accountMode
      const order = s.workingOrders[mode].find((o) => o.id === id)
      if (!order) return s

      return {
        workingOrders: { ...s.workingOrders, [mode]: s.workingOrders[mode].filter((o) => o.id !== id) },
        activity: {
          ...s.activity,
          [mode]: s.activity[mode].map((a) =>
            a.id === order.activityId ? { ...a, status: 'canceled' } : a,
          ),
        },
      }
    }),
  upsertExit: (id, exit) =>
    set((s) => updatePosition(s, id, (p) => ({
      ...p,
      attachedExit: exit,
      // Placing a manual exit takes the position off its strategy's rules.
      strategyId: null,
      managedExit: null,
    }))),
  cancelExit: (id) => set((s) => updatePosition(s, id, (p) => ({ ...p, attachedExit: null }))),
  detachFromStrategy: (id) =>
    set((s) => updatePosition(s, id, (p) => ({ ...p, strategyId: null, managedExit: null }))),
  reattachToStrategy: (id) =>
    set((s) => updatePosition(s, id, (p) => ({
      ...p,
      // Back to the strategy that opened it, not to whichever one is
      // active now — those are different sets of exit rules.
      strategyId: p.openedByStrategyId,
      managedExit: DEFAULT_MANAGED_EXIT,
      // The strategy's rules and a manual exit cannot both run.
      attachedExit: null,
    }))),

  // -------------------------------------------------------------------- //
  // Settings (PRD.md §8.7)
  // -------------------------------------------------------------------- //

  riskLimits: RISK_LIMITS,
  auditLog: AUDIT_LOG,
  notificationRoutes: NOTIFICATION_ROUTES,
  dataFeeds: DATA_FEEDS,
  dataPlan: CURRENT_PLAN,
  apiKeys: API_KEYS,
  notifications: NOTIFICATIONS,
  setRiskLimit: (key, value) =>
    set((s) => {
      const limit = s.riskLimits.find((l) => l.key === key)
      if (!limit) return s
      // A no-op writes no audit row. The log exists to be read on the day
      // something went wrong, and padding it with unchanged values is how it
      // becomes unreadable.
      if (limit.value === value) return s
      // Checked here as well as in the field. Rule 4 gives enforcement to
      // the engine, but there is no reason for the client to hold a value it
      // already knows is nonsense.
      if (validateRiskLimit(limit, value) !== null) return s

      return {
        riskLimits: s.riskLimits.map((l) => (l.key === key ? { ...l, value } : l)),
        auditLog: [
          auditEntry('risk', key, String(limit.value), String(value)),
          ...s.auditLog,
        ],
      }
    }),
  setNotificationRoute: (event, channel, enabled) =>
    set((s) => {
      const route = s.notificationRoutes.find((r) => r.event === event)
      if (!route || route[channel] === enabled) return s

      return {
        notificationRoutes: s.notificationRoutes.map((r) =>
          r.event === event ? { ...r, [channel]: enabled } : r,
        ),
        auditLog: [
          auditEntry(
            'notification',
            notificationAuditField(event, channel),
            route[channel] ? 'on' : 'off',
            enabled ? 'on' : 'off',
          ),
          ...s.auditLog,
        ],
      }
    }),
  setDataFeed: (key, value) =>
    set((s) => {
      const feed = s.dataFeeds.find((f) => f.key === key)
      if (!feed || feed.value === value) return s

      // Refuses what the plan cannot serve. This is not politeness: CLAUDE.md
      // notes that requesting `opra` or real-time `sip` on Basic returns an
      // auth error rather than empty data, so storing it would break every
      // subsequent request for no gain.
      const option = feedOptionsFor(key, s.dataPlan).find((o) => o.value === value)
      if (!option || option.requiresUpgrade) return s

      return {
        dataFeeds: s.dataFeeds.map((f) => (f.key === key ? { ...f, value } : f)),
        auditLog: [auditEntry('feed', key, feed.value, value), ...s.auditLog],
      }
    }),
  markNotificationsRead: () =>
    set((s) => ({
      notifications: s.notifications.map((n) =>
        n.account === s.accountMode || n.account === null ? { ...n, read: true } : n,
      ),
    })),
  dismissNotification: (id) =>
    set((s) => ({ notifications: s.notifications.filter((n) => n.id !== id) })),

  // -------------------------------------------------------------------- //
  // Research (PRD.md §8.5)
  // -------------------------------------------------------------------- //

  strategies: STRATEGIES,
  chat: [],
  chatHistory: CHAT_HISTORY,
  newChat: () =>
    set((s) => {
      const filed = archiveChat(s.chat, new Date().toISOString())
      if (filed === null) return s
      return { chat: [], chatHistory: [filed, ...s.chatHistory] }
    }),
  openChat: (id) =>
    set((s) => {
      const target = s.chatHistory.find((c) => c.id === id)
      if (target === undefined) return s

      const rest = s.chatHistory.filter((c) => c.id !== id)
      const filed = archiveChat(s.chat, new Date().toISOString())
      return {
        chat: target.messages,
        chatHistory: filed === null ? rest : [filed, ...rest],
      }
    }),
  dispositions: {},
  executeRecommendation: (id) =>
    set((s) => ({ dispositions: { ...s.dispositions, [id]: 'executed' } })),
  dismissRecommendation: (id) =>
    set((s) => ({ dispositions: { ...s.dispositions, [id]: 'dismissed' } })),
  refreshRecommendations: () =>
    set((s) => {
      // Drops dismissals and keeps everything else. Rebuilding the whole map
      // would also erase what you executed, which is a decision rather than
      // something you waved off.
      const kept: Dispositions = {}
      for (const [id, disposition] of Object.entries(s.dispositions)) {
        if (disposition !== 'dismissed') kept[id] = disposition
      }
      return { dispositions: kept }
    }),
  sendChatMessage: (text) =>
    set((s) => {
      const trimmed = text.trim()
      if (trimmed === '') return s

      const at = new Date().toISOString()
      const reply = chatReply(trimmed)

      return {
        chat: [
          ...s.chat,
          { id: `msg-user-${at}`, role: 'user' as const, text: trimmed, at, proposal: null },
          {
            id: `msg-bot-${at}`,
            role: 'assistant' as const,
            text: reply.text,
            at,
            proposal: reply.proposal,
          },
        ],
      }
    }),
  acceptProposal: (proposal) =>
    set((s) => ({ strategies: [...s.strategies, proposalToStrategy(proposal, s.strategies)] })),
  renameStrategy: (id, name) =>
    set((s) => {
      const trimmed = name.trim()
      if (trimmed === '') return s
      return {
        strategies: s.strategies.map((st) => (st.id === id ? { ...st, name: trimmed } : st)),
      }
    }),
  setStrategyStatus: (id, status) =>
    set((s) => {
      const target = s.strategies.find((st) => st.id === id)
      if (!target || !canTransition(target.status, status)) return s

      return {
        strategies: s.strategies.map((st) => {
          if (st.id === id) return { ...st, status }
          // §5.2 permits one active strategy. The incumbent steps back to
          // paper rather than being retired — it keeps its record and can be
          // promoted again.
          if (status === 'active' && st.status === 'active') return { ...st, status: 'paper' }
          return st
        }),
      }
    }),
  deleteStrategy: (id) =>
    set((s) => {
      const target = s.strategies.find((st) => st.id === id)
      if (!target || target.status === 'active') return s

      const strategies = s.strategies.filter((st) => st.id !== id)
      return {
        strategies,
        // The dropdown cannot keep pointing at something that is gone.
        activeStrategyId:
          s.activeStrategyId === id ? (strategies[0]?.id ?? '') : s.activeStrategyId,
      }
    }),
}))
