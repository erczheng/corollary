import {
  CHAT_FALLBACK,
  CHAT_SCRIPT,
  type ArchivedChat,
  type ChatMessage,
  type Recommendation,
  type Strategy,
  type StrategyProposal,
  type StrategyStatus,
} from './mockData'

/** The Research page's rules, as pure functions with no React in them — the
 * arrangement `orders.ts`, `markets.ts` and `settings.ts` already use.
 *
 * Two of these are about not overstating what the app knows. `chatReply`
 * refuses to improvise past the end of its script, and `promotionGate`
 * reports *why* a strategy misses rather than vetoing it, because PRD.md §5.3
 * is explicit that the gate "automates the obvious cases; it does not remove
 * the decision".
 */

// ---------------------------------------------------------------------- //
// Recommendations
// ---------------------------------------------------------------------- //

/** What has been done with a recommendation.
 *
 * Two actions, not three. A `queued` disposition used to sit between these —
 * staged for the engine to place on its next run, distinct from executed
 * because nothing had reached a broker yet. It was removed deliberately:
 * every acceptance is now an immediate submission, so there is no state in
 * which the app has said yes to a candidate but nothing has been sent.
 *
 * The consequence to know before adding it back: there is no longer a way to
 * stage a candidate for the engine. Accepting one in Phase 6 goes straight
 * through `RiskManager.approve()`. */
export type Disposition = 'open' | 'executed' | 'dismissed'

export type Dispositions = Record<string, Disposition>

export function dispositionOf(id: string, dispositions: Dispositions): Disposition {
  return dispositions[id] ?? 'open'
}

/** Rows to show. Only `dismissed` disappears.
 *
 * Executed rows stay: one vanishing the moment you acted on it would make the
 * list quietly disagree with what you just did. */
export function visibleRecommendations(
  all: Recommendation[],
  dispositions: Dispositions,
): Recommendation[] {
  return all.filter((r) => dispositionOf(r.id, dispositions) !== 'dismissed')
}

/** CSV rows — **every** recommendation, including dismissed ones.
 *
 * The export is a record of the session's candidate set rather than a copy of
 * what happens to be on screen, the same reasoning behind the Dashboard
 * exporting everything its filter matched rather than the ten visible rows.
 *
 * A null confidence exports as empty, not as the em dash it renders as. This
 * lands in a spreadsheet column of numbers, where "—" is a parse error. */
export function recommendationCsvRows(
  all: Recommendation[],
  dispositions: Dispositions,
): Record<string, string | number>[] {
  return all.map((r) => ({
    id: r.id,
    symbol: r.symbol,
    strike: r.strike,
    structure: r.structure,
    expiry: r.expiry,
    setup: r.setup,
    reason: r.reason,
    confidence: r.confidence ?? '',
    unvalidated: String(r.unvalidated),
    origin: r.origin,
    disposition: dispositionOf(r.id, dispositions),
  }))
}

// ---------------------------------------------------------------------- //
// Chat
// ---------------------------------------------------------------------- //

/** One scripted reply, chosen by keyword.
 *
 * **Deliberately incapable of improvising.** This is a shell until the LLM
 * layer lands in Phase 4, and an unmatched question gets a reply that says so.
 * Everything else in Phase 1 is visibly a fixture — a table of made-up numbers
 * reads as made-up — but a fluent sentence reads as a considered answer, which
 * makes a plausible non-answer here the most misleading thing the app could
 * produce.
 *
 * Deterministic: same question, same reply, no PRNG, so a session replays
 * identically and a test does not flake. */
export function chatReply(text: string): { text: string; proposal: StrategyProposal | null } {
  const normalized = text.toLowerCase()
  const match = CHAT_SCRIPT.find((entry) => entry.match.test(normalized))

  if (!match) return { text: CHAT_FALLBACK, proposal: null }
  return { text: match.reply, proposal: match.proposal }
}

// ---------------------------------------------------------------------- //
// Strategy lifecycle (PRD.md §5.2)
// ---------------------------------------------------------------------- //

export const LIFECYCLE: StrategyStatus[] = ['draft', 'backtest', 'paper', 'active', 'retired']

/** Where each status may go next.
 *
 * Forward one stage at a time, because the stages *are* the evidence: a draft
 * promoted straight to active has no backtest and no paper record, and §5.3's
 * gate would have nothing to measure.
 *
 * Retiring is available from any stage that has actually run, and it is
 * terminal — a retired strategy returns as a new version rather than by being
 * un-retired, which keeps the version number meaningful. A draft cannot be
 * retired because it has nothing to retire *from*; deleting it is the action. */
const TRANSITIONS: Record<StrategyStatus, StrategyStatus[]> = {
  draft: ['backtest'],
  backtest: ['paper', 'retired'],
  paper: ['active', 'retired'],
  active: ['retired'],
  retired: [],
}

export function nextStatuses(status: StrategyStatus): StrategyStatus[] {
  return TRANSITIONS[status]
}

export function canTransition(from: StrategyStatus, to: StrategyStatus): boolean {
  return TRANSITIONS[from].includes(to)
}

/** The one running strategy, or null.
 *
 * §5.2 allows exactly one `active` at a time in v1. Returning the first match
 * rather than a list is what keeps that assumption visible at the call site —
 * a function returning `Strategy[]` invites code that quietly handles two. */
export function activeStrategy(strategies: Strategy[]): Strategy | null {
  return strategies.find((s) => s.status === 'active') ?? null
}

// ---------------------------------------------------------------------- //
// Promotion gate (PRD.md §5.3)
// ---------------------------------------------------------------------- //

/** Auto-approval thresholds, measured on the paper period. */
export const GATE_THRESHOLDS = {
  trades: 50,
  winRate: 65,
  profitFactor: 1.4,
  maxDrawdown: 25,
} as const

export interface GateResult {
  passes: boolean
  /** Each threshold missed, in words. Empty when it passes. */
  failures: string[]
}

/** Whether a strategy clears §5.3's auto-approval bar.
 *
 * Reports rather than vetoes. The PRD is explicit that anything missing the
 * gate "can still be promoted manually" and anything passing "can still be
 * rejected manually", so a UI that greyed out the promote button would be
 * enforcing a rule the spec deliberately left to a person. The failures are
 * what the button needs to warn about.
 *
 * All four comparisons are inclusive of the threshold — 65% exactly passes.
 * `maxDrawdown` runs the other way, since a *smaller* drawdown is better. */
export function promotionGate(strategy: Strategy): GateResult {
  const live = strategy.live
  if (live === null) {
    return { passes: false, failures: ['No paper record yet — nothing to measure.'] }
  }

  const failures: string[] = []
  if (live.trades < GATE_THRESHOLDS.trades) {
    failures.push(`${live.trades} closed trades, needs ${GATE_THRESHOLDS.trades}.`)
  }
  if (live.winRate < GATE_THRESHOLDS.winRate) {
    failures.push(`Win rate ${live.winRate}%, needs ${GATE_THRESHOLDS.winRate}%.`)
  }
  if (live.profitFactor < GATE_THRESHOLDS.profitFactor) {
    failures.push(`Profit factor ${live.profitFactor}, needs ${GATE_THRESHOLDS.profitFactor}.`)
  }
  if (live.maxDrawdown > GATE_THRESHOLDS.maxDrawdown) {
    failures.push(`Max drawdown ${live.maxDrawdown}%, ceiling ${GATE_THRESHOLDS.maxDrawdown}%.`)
  }

  return { passes: failures.length === 0, failures }
}

// ---------------------------------------------------------------------- //
// Accepting a proposal
// ---------------------------------------------------------------------- //

/** Turns a chat proposal into a strategy.
 *
 * Always a `draft`, always version 1, always with an empty record. The LLM may
 * propose; it may not promote. §5.3's gate is the only way past draft and the
 * entire point of it is that nothing skips it — so this function has no
 * parameter that could ask for anything else.
 *
 * Takes the existing strategies to derive an id that does not collide,
 * including with a draft created moments earlier. */
export function proposalToStrategy(
  proposal: StrategyProposal,
  existing: Strategy[],
): Strategy {
  const used = new Set(existing.map((s) => s.id))
  let n = existing.length + 1
  while (used.has(`strat-${n}`)) n += 1

  return {
    id: `strat-${n}`,
    name: proposal.name,
    version: 1,
    status: 'draft',
    backtest: { winRate: 0, profitFactor: 0, maxDrawdown: 0, trades: 0 },
    live: null,
  }
}


// ---------------------------------------------------------------------- //
// Chat history
// ---------------------------------------------------------------------- //

/** How long a derived conversation title may run before it is clipped. */
const TITLE_MAX = 48

/** What a conversation gets called in the history menu.
 *
 * The **first user message**, never the first message overall and never the
 * assistant's reply. The question is what you were asking about; the answer
 * is what the app said back, and titling a conversation with the reply would
 * label your own history in the shell's words rather than yours.
 *
 * Clipped on a word boundary where there is one, so a long question does not
 * cut mid-word. A conversation with no user message has no subject yet and
 * says so rather than inventing one — that state is reachable, because the
 * transcript can hold a greeting before you have typed anything.
 */
export function conversationTitle(messages: ChatMessage[]): string {
  const first = messages.find((m) => m.role === 'user')
  const text = first?.text.trim() ?? ''
  if (text === '') return 'Untitled conversation'
  if (text.length <= TITLE_MAX) return text

  const clipped = text.slice(0, TITLE_MAX)
  const lastSpace = clipped.lastIndexOf(' ')
  // Only break on a space if one falls late enough to leave a useful title;
  // otherwise a single long token would clip down to almost nothing.
  const stem = lastSpace > TITLE_MAX / 2 ? clipped.slice(0, lastSpace) : clipped
  return `${stem}…`
}

/** Files a finished transcript into the history.
 *
 * Returns `null` for an empty transcript rather than an empty entry: opening
 * the app and never typing is not a conversation, and a history full of blank
 * rows is how a useful list becomes one nobody reads. The caller treats null
 * as "nothing to file" — which is why `newChat` on an untouched chat is a
 * no-op rather than an archive.
 */
export function archiveChat(messages: ChatMessage[], at: string): ArchivedChat | null {
  if (messages.length === 0) return null
  return {
    id: `chat-${at}`,
    title: conversationTitle(messages),
    at,
    messages,
  }
}
