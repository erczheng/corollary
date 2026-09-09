import { describe, it, expect } from 'vitest'
import {
  CHAT_FALLBACK,
  CHAT_SCRIPT,
  LLM_ORIGINATION,
  RECOMMENDATIONS,
  CHAT_HISTORY,
  STRATEGIES,
  type ChatMessage,
  type Strategy,
  type StrategyStatus,
} from './mockData'
import {
  GATE_THRESHOLDS,
  LIFECYCLE,
  activeStrategy,
  archiveChat,
  canTransition,
  chatReply,
  conversationTitle,
  dispositionOf,
  nextStatuses,
  promotionGate,
  proposalToStrategy,
  recommendationCsvRows,
  visibleRecommendations,
} from './research'

const strategy = (id: string): Strategy => {
  const found = STRATEGIES.find((s) => s.id === id)
  if (!found) throw new Error(`no fixture for ${id}`)
  return found
}

describe('recommendation dispositions', () => {
  it('treats an untouched recommendation as open', () => {
    expect(dispositionOf('rec-1', {})).toBe('open')
  })

  it('hides dismissed recommendations and keeps the rest', () => {
    const visible = visibleRecommendations(RECOMMENDATIONS, { 'rec-1': 'dismissed' })

    expect(visible.map((r) => r.id)).not.toContain('rec-1')
    expect(visible).toHaveLength(RECOMMENDATIONS.length - 1)
  })

  /** An executed row disappearing would make the list lie about what you
   * just did. */
  it('keeps executed rows visible', () => {
    const visible = visibleRecommendations(RECOMMENDATIONS, { 'rec-2': 'executed' })
    expect(visible.map((r) => r.id)).toContain('rec-2')
  })

  it('does not mutate the array it is given', () => {
    const before = RECOMMENDATIONS.map((r) => r.id)
    visibleRecommendations(RECOMMENDATIONS, { 'rec-1': 'dismissed' })
    expect(RECOMMENDATIONS.map((r) => r.id)).toEqual(before)
  })
})

describe('recommendationCsvRows', () => {
  it('exports every recommendation with its disposition', () => {
    const rows = recommendationCsvRows(RECOMMENDATIONS, { 'rec-1': 'executed' })

    expect(rows).toHaveLength(RECOMMENDATIONS.length)
    expect(rows[0].disposition).toBe('executed')
    expect(rows[1].disposition).toBe('open')
  })

  /** §6.3: where no base rate exists confidence is an em dash on screen. In a
   * CSV it has to be empty, not the character — you reconcile against this in
   * a spreadsheet, and "—" in a numeric column is a parse error. */
  it('leaves a null confidence empty rather than writing an em dash', () => {
    const rows = recommendationCsvRows(RECOMMENDATIONS, {})
    const noBaseRate = rows.find((r) => r.id === 'rec-7')!

    expect(noBaseRate.confidence).toBe('')
    expect(noBaseRate.confidence).not.toBe('—')
  })

  it('carries the setup class and the reason as separate columns', () => {
    const rows = recommendationCsvRows(RECOMMENDATIONS, {})
    expect(rows[0].setup).toBe('mean_reversion')
    expect(rows[0].reason).toBe('RSI 28, below 20d SMA')
  })

  it('records whether a row was an LLM origination and whether it was unvalidated', () => {
    const rows = recommendationCsvRows(RECOMMENDATIONS, {})
    const llm = rows.find((r) => r.id === 'rec-4')!

    expect(llm.origin).toBe('llm')
    expect(llm.unvalidated).toBe('true')
  })

  it('exports dismissed rows too, since the export is a record not a view', () => {
    const rows = recommendationCsvRows(RECOMMENDATIONS, { 'rec-1': 'dismissed' })
    expect(rows.map((r) => r.id)).toContain('rec-1')
  })
})

/** The chat is a scripted shell, not a model. These tests pin that it stays
 * one — deterministic, and honest when it has nothing. */
describe('chatReply', () => {
  it('routes a strategy question to the proposal reply', () => {
    const reply = chatReply('can you propose a strategy from my history?')
    expect(reply.proposal).not.toBeNull()
    expect(reply.proposal!.name).toBe('index_mean_reversion_v2')
  })

  it('routes a recommendations question to a reply with no proposal attached', () => {
    const reply = chatReply('what are today’s recommendations?')
    expect(reply.proposal).toBeNull()
    expect(reply.text).toMatch(/candidates/)
  })

  it('is case-insensitive', () => {
    expect(chatReply('STRATEGY').proposal).not.toBeNull()
  })

  it('is deterministic — the same question gives the same answer', () => {
    expect(chatReply('how is my risk configured?')).toEqual(chatReply('how is my risk configured?'))
  })

  /** Fluently improvising past the end of the script is the one failure mode
   * that would make this panel actively misleading. */
  it('admits it is a shell rather than improvising', () => {
    const reply = chatReply('what is the capital of France?')
    expect(reply.text).toBe(CHAT_FALLBACK)
    expect(reply.proposal).toBeNull()
  })

  it('has a reachable route for every scripted entry', () => {
    for (const entry of CHAT_SCRIPT) {
      const probe = entry.match.source.split('|')[0].replace(/[\\^$.*+?()[\]{}]/g, '')
      expect(chatReply(probe).text).not.toBe(CHAT_FALLBACK)
    }
  })
})

/** PRD.md §5.2: draft → backtest → paper → active → retired, one active at a
 * time. */
describe('strategy lifecycle', () => {
  it('advances one step at a time', () => {
    expect(canTransition('draft', 'backtest')).toBe(true)
    expect(canTransition('backtest', 'paper')).toBe(true)
    expect(canTransition('paper', 'active')).toBe(true)
  })

  it('refuses to skip a stage', () => {
    expect(canTransition('draft', 'paper')).toBe(false)
    expect(canTransition('draft', 'active')).toBe(false)
    expect(canTransition('backtest', 'active')).toBe(false)
  })

  it('refuses to move backwards', () => {
    expect(canTransition('paper', 'backtest')).toBe(false)
    expect(canTransition('active', 'paper')).toBe(false)
  })

  /** Retiring is always available once a strategy has been somewhere, and it
   * is terminal — a retired strategy comes back as a new version, not by
   * being un-retired. */
  it('allows retiring from any traded stage but not from draft', () => {
    expect(canTransition('backtest', 'retired')).toBe(true)
    expect(canTransition('paper', 'retired')).toBe(true)
    expect(canTransition('active', 'retired')).toBe(true)
    expect(canTransition('draft', 'retired')).toBe(false)
  })

  it('makes retired terminal', () => {
    for (const status of LIFECYCLE) {
      expect(canTransition('retired', status)).toBe(false)
    }
  })

  it('never reports a transition to the status a strategy already has', () => {
    for (const status of LIFECYCLE) {
      expect(nextStatuses(status)).not.toContain(status)
    }
  })

  it('offers exactly the transitions canTransition permits', () => {
    for (const from of LIFECYCLE) {
      for (const to of LIFECYCLE) {
        expect(nextStatuses(from).includes(to)).toBe(canTransition(from, to))
      }
    }
  })
})

describe('activeStrategy', () => {
  it('finds the one active strategy', () => {
    expect(activeStrategy(STRATEGIES)?.id).toBe('strat-1')
  })

  it('returns null when nothing is active', () => {
    const none = STRATEGIES.map((s) => ({ ...s, status: 'paper' as StrategyStatus }))
    expect(activeStrategy(none)).toBeNull()
  })

  /** §5.2 permits exactly one. If the fixture ever carries two, the Dashboard
   * dropdown and this function disagree about which one is running. */
  it('the fixture carries exactly one active strategy', () => {
    expect(STRATEGIES.filter((s) => s.status === 'active')).toHaveLength(1)
  })
})

/** §5.3. Auto-approval needs all three thresholds on the paper period, plus
 * the trade count. Missing it does not block a manual promotion — the gate
 * automates the obvious cases, it does not remove the decision — so this
 * returns *why* rather than a veto. */
describe('promotionGate', () => {
  it('passes a strategy that clears every threshold', () => {
    const result = promotionGate(strategy('strat-1'))
    expect(result.passes).toBe(true)
    expect(result.failures).toEqual([])
  })

  it('names every threshold a strategy misses', () => {
    const result = promotionGate(strategy('strat-2')) // 59%, PF 1.2, DD 12, 31 trades
    expect(result.passes).toBe(false)
    expect(result.failures.join(' ')).toMatch(/win rate/i)
    expect(result.failures.join(' ')).toMatch(/profit factor/i)
    expect(result.failures.join(' ')).toMatch(/closed trades/i)
    // Drawdown is fine at 12%, so it must not be listed.
    expect(result.failures.join(' ')).not.toMatch(/drawdown/i)
  })

  it('fails a strategy with no live record at all', () => {
    const result = promotionGate(strategy('strat-3'))
    expect(result.passes).toBe(false)
    expect(result.failures.join(' ')).toMatch(/no paper record/i)
  })

  it('permits at each boundary exactly', () => {
    const boundary: Strategy = {
      ...strategy('strat-1'),
      live: {
        winRate: GATE_THRESHOLDS.winRate,
        profitFactor: GATE_THRESHOLDS.profitFactor,
        maxDrawdown: GATE_THRESHOLDS.maxDrawdown,
        trades: GATE_THRESHOLDS.trades,
      },
    }
    expect(promotionGate(boundary).passes).toBe(true)
  })

  it('rejects just outside each boundary', () => {
    const at = {
      winRate: GATE_THRESHOLDS.winRate,
      profitFactor: GATE_THRESHOLDS.profitFactor,
      maxDrawdown: GATE_THRESHOLDS.maxDrawdown,
      trades: GATE_THRESHOLDS.trades,
    }
    const base = strategy('strat-1')

    expect(promotionGate({ ...base, live: { ...at, winRate: at.winRate - 1 } }).passes).toBe(false)
    expect(promotionGate({ ...base, live: { ...at, profitFactor: at.profitFactor - 0.1 } }).passes).toBe(false)
    expect(promotionGate({ ...base, live: { ...at, maxDrawdown: at.maxDrawdown + 1 } }).passes).toBe(false)
    expect(promotionGate({ ...base, live: { ...at, trades: at.trades - 1 } }).passes).toBe(false)
  })
})

describe('proposalToStrategy', () => {
  const proposal = CHAT_SCRIPT.find((s) => s.proposal !== null)!.proposal!

  /** §5.3 exists so nothing skips it. An accepted proposal is a draft and
   * cannot arrive any further along. */
  it('lands as a draft with no record of its own', () => {
    const created = proposalToStrategy(proposal, STRATEGIES)

    expect(created.status).toBe('draft')
    expect(created.live).toBeNull()
    expect(created.backtest.trades).toBe(0)
    expect(created.version).toBe(1)
  })

  it('takes its name from the proposal', () => {
    expect(proposalToStrategy(proposal, STRATEGIES).name).toBe(proposal.name)
  })

  it('does not collide with an existing id', () => {
    const created = proposalToStrategy(proposal, STRATEGIES)
    expect(STRATEGIES.some((s) => s.id === created.id)).toBe(false)
  })

  it('does not collide with a draft it just created', () => {
    const first = proposalToStrategy(proposal, STRATEGIES)
    const second = proposalToStrategy(proposal, [...STRATEGIES, first])
    expect(second.id).not.toBe(first.id)
  })
})

/** The number the Dashboard was getting wrong. §8.1 scopes its win rate to
 * validated trades and the excluded bucket is account-wide, so the count has
 * to come off LLM_ORIGINATION rather than off a strategy. */
describe('the origination bucket', () => {
  it('splits into validated and unvalidated that sum to the total', () => {
    expect(LLM_ORIGINATION.validated + LLM_ORIGINATION.unvalidated).toBe(LLM_ORIGINATION.count)
  })

  it('has unvalidated trades to exclude, so the Dashboard note is reachable', () => {
    expect(LLM_ORIGINATION.unvalidated).toBeGreaterThan(0)
  })
})


describe('conversationTitle', () => {
  const msg = (role: ChatMessage['role'], text: string): ChatMessage => ({
    id: `${role}-${text}`,
    role,
    text,
    at: '2026-08-07T14:00:00Z',
    proposal: null,
  })

  it('titles a conversation with the first user message, not the reply', () => {
    const title = conversationTitle([
      msg('user', 'How is my risk configured?'),
      msg('assistant', 'Max risk per trade is 7% of equity.'),
    ])
    expect(title).toBe('How is my risk configured?')
  })

  /** The assistant speaking first is reachable — a greeting can land before
   * you have typed. The subject is still whatever *you* asked. */
  it('skips leading assistant messages', () => {
    const title = conversationTitle([
      msg('assistant', 'Ask about this account and its data.'),
      msg('user', 'What are today’s recommendations?'),
    ])
    expect(title).toBe('What are today’s recommendations?')
  })

  it('says a conversation with no user message is untitled', () => {
    expect(conversationTitle([])).toBe('Untitled conversation')
    expect(conversationTitle([msg('assistant', 'Hello.')])).toBe('Untitled conversation')
    expect(conversationTitle([msg('user', '   ')])).toBe('Untitled conversation')
  })

  it('clips a long question on a word boundary', () => {
    const long =
      'Propose a mean reversion strategy on the index ETFs using RSI and a twenty day moving average'
    const title = conversationTitle([msg('user', long)])

    const stem = title.slice(0, -1)

    expect(title.endsWith('…')).toBe(true)
    expect(title.length).toBeLessThanOrEqual(49)
    expect(long.startsWith(stem)).toBe(true)
    // Broke *at* a space, so the last word kept is whole rather than sliced
    // through the middle.
    expect(long[stem.length]).toBe(' ')
  })

  /** A single unbroken token has no late space to break on, and clipping to
   * the first one would leave a title of almost nothing. */
  it('clips mid-token when there is no usable word boundary', () => {
    const title = conversationTitle([msg('user', `a ${'x'.repeat(80)}`)])
    expect(title).toHaveLength(49)
  })
})

describe('archiveChat', () => {
  const at = '2026-08-07T15:00:00Z'
  const messages: ChatMessage[] = [
    { id: 'm1', role: 'user', text: 'What is my exposure?', at, proposal: null },
    { id: 'm2', role: 'assistant', text: '25% per underlying.', at, proposal: null },
  ]

  it('files a transcript with a derived title and the messages intact', () => {
    const filed = archiveChat(messages, at)

    expect(filed).not.toBeNull()
    expect(filed?.title).toBe('What is my exposure?')
    expect(filed?.at).toBe(at)
    expect(filed?.messages).toEqual(messages)
  })

  /** Opening the app and never typing is not a conversation. A history full
   * of blank rows is how a useful list stops being read. */
  it('refuses to file an empty transcript', () => {
    expect(archiveChat([], at)).toBeNull()
  })
})

describe('CHAT_HISTORY fixture', () => {
  it('seeds the menu so a populated history is reachable on a cold start', () => {
    expect(CHAT_HISTORY.length).toBeGreaterThan(0)
  })

  it('carries titles matching what conversationTitle would derive', () => {
    for (const c of CHAT_HISTORY) {
      expect(c.title).toBe(conversationTitle(c.messages))
    }
  })

  it('has unique ids, so opening one is unambiguous', () => {
    expect(new Set(CHAT_HISTORY.map((c) => c.id)).size).toBe(CHAT_HISTORY.length)
  })
})
