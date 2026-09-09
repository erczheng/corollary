import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, within, fireEvent, act } from '@testing-library/react'
import App from '../App'
import { useUIStore } from '../lib/store'
import {
  CHAT_FALLBACK,
  LLM_ORIGINATION,
  MARKET_PULSE,
  RECOMMENDATIONS,
  SENTIMENT_COMPONENTS,
  recommendationTitle,
} from '../lib/mockData'
import { compositeScore } from '../lib/news'
import { GATE_THRESHOLDS } from '../lib/research'
import { formatStrategyName } from '../lib/format'

const initialState = useUIStore.getState()

beforeEach(() => {
  window.history.pushState({}, '', '/research')
  useUIStore.setState({ ...initialState }, true)
})

function section(name: string): HTMLElement {
  return screen.getByRole('region', { name })
}

/** The strategy rows are collapsed by default. */
function expandStrategy(name: string): void {
  fireEvent.click(within(section('Strategies')).getByRole('button', { name: new RegExp(name) }))
}

/** One strategy's row. Needed because "Backtest" is both a *status* chip on one
 * strategy and a *stat heading* on another, so a section-wide text query for it
 * matches two different meanings. */
function strategyRow(name: string): HTMLElement {
  return within(section('Strategies'))
    .getByRole('button', { name: new RegExp(name) })
    .closest('li')!
}

/** Transcript messages only — counted by their `You ·` / `Corollary ·` footer
 * rather than by list items, because a proposal renders its rules as a nested
 * list and those are list items too. */
function messageCount(): number {
  return within(section('Chat')).queryAllByText(/^(You|Corollary) ·/).length
}

describe('Market Pulse', () => {
  it('shows VIX, the sentiment composite and the top sector', () => {
    render(<App />)

    expect(within(screen.getByRole('group', { name: 'VIX' })).getByText('16.80')).toBeInTheDocument()
    expect(
      within(screen.getByRole('group', { name: 'Top sector' })).getByText(MARKET_PULSE.topSector),
    ).toBeInTheDocument()
  })

  /** Derived from the components rather than stored, so it can never disagree
   * with the breakdown the News page prints under the same number. */
  it('derives the composite rather than reading a stored copy', () => {
    render(<App />)
    const expected = String(compositeScore(SENTIMENT_COMPONENTS))

    expect(
      within(screen.getByRole('group', { name: 'Sentiment composite' })).getByText(expected),
    ).toBeInTheDocument()
  })
})

describe('the chat shell', () => {
  /** The one place in the app that could actually mislead: a fluent reply reads
   * as a considered answer even when it came from a lookup table. */
  it('says on screen that it is scripted', () => {
    render(<App />)
    expect(within(section('Chat')).getByText(/Scripted — Phase 1/)).toBeInTheDocument()
  })

  it('opens empty with suggested prompts', () => {
    render(<App />)
    const chat = section('Chat')

    expect(within(chat).queryByRole('listitem')).not.toBeInTheDocument()
    expect(within(chat).getByRole('button', { name: /Propose a strategy/ })).toBeInTheDocument()
  })

  it('appends the question and its reply', () => {
    render(<App />)
    const chat = section('Chat')

    fireEvent.change(within(chat).getByLabelText('Message'), {
      target: { value: 'how is my risk configured?' },
    })
    fireEvent.click(within(chat).getByRole('button', { name: 'Send' }))

    const messages = within(section('Chat')).getAllByRole('listitem')
    expect(messages).toHaveLength(2)
    expect(messages[0].textContent).toContain('how is my risk configured?')
    expect(messages[1].textContent).toMatch(/7% of equity/)
  })

  it('sends a suggested prompt on click', () => {
    render(<App />)
    fireEvent.click(within(section('Chat')).getByRole('button', { name: /Propose a strategy/ }))

    expect(messageCount()).toBe(2)
  })

  it('will not send an empty message', () => {
    render(<App />)
    expect(within(section('Chat')).getByRole('button', { name: 'Send' })).toBeDisabled()
  })

  /** Improvising past the end of the script is the failure mode that would make
   * this panel actively dishonest. */
  it('admits when it has no scripted answer', () => {
    render(<App />)
    const chat = section('Chat')

    fireEvent.change(within(chat).getByLabelText('Message'), {
      target: { value: 'what is the capital of France?' },
    })
    fireEvent.click(within(chat).getByRole('button', { name: 'Send' }))

    expect(within(section('Chat')).getByText(CHAT_FALLBACK)).toBeInTheDocument()
  })
})

describe('a proposed strategy', () => {
  function proposeStrategy(): void {
    render(<App />)
    fireEvent.click(within(section('Chat')).getByRole('button', { name: /Propose a strategy/ }))
  }

  it('offers the proposal with its rules', () => {
    proposeStrategy()
    const chat = section('Chat')

    expect(within(chat).getByText('index_mean_reversion_v2')).toBeInTheDocument()
    expect(within(chat).getByText(/RSI\(14\) < 30/)).toBeInTheDocument()
    expect(within(chat).getByRole('button', { name: 'Add as draft' })).toBeInTheDocument()
  })

  it('says the rules are intent, not a validated document', () => {
    proposeStrategy()
    expect(within(section('Chat')).getByText(/whitelist validation land with/)).toBeInTheDocument()
  })

  /** §5.3's gate is the only way past draft, and the LLM proposing something
   * buys no exemption — origin never buys one anywhere else either (§6.2). */
  it('lands as a draft in the strategy list, never further along', () => {
    proposeStrategy()
    fireEvent.click(within(section('Chat')).getByRole('button', { name: 'Add as draft' }))

    const created = useUIStore.getState().strategies.find((s) => s.name === 'index_mean_reversion_v2')!
    expect(created.status).toBe('draft')
    expect(created.live).toBeNull()

    expect(
      within(section('Strategies')).getByRole('button', { name: /Index Mean Reversion V2/ }),
    ).toBeInTheDocument()
  })

  it('stops offering to add it once it has been added', () => {
    proposeStrategy()
    fireEvent.click(within(section('Chat')).getByRole('button', { name: 'Add as draft' }))

    expect(within(section('Chat')).queryByRole('button', { name: 'Add as draft' })).not.toBeInTheDocument()
    expect(within(section('Chat')).getByText(/Added as a draft/)).toBeInTheDocument()
  })
})

describe('LLM origination', () => {
  it('reports the bucket’s own numbers', () => {
    render(<App />)
    const panel = section('LLM origination')

    expect(within(panel).getByText(String(LLM_ORIGINATION.count))).toBeInTheDocument()
    expect(within(panel).getByText(`${LLM_ORIGINATION.winRate}%`)).toBeInTheDocument()
    expect(within(panel).getByText(String(LLM_ORIGINATION.profitFactor))).toBeInTheDocument()
  })

  it('shows the validated/unvalidated split and what it means', () => {
    render(<App />)
    const panel = section('LLM origination')

    expect(
      within(panel).getByText(`${LLM_ORIGINATION.validated} / ${LLM_ORIGINATION.unvalidated}`),
    ).toBeInTheDocument()
    expect(within(panel).getByText(/third of normal size/)).toBeInTheDocument()
  })

  /** §8.5 keeps this off the Dashboard on purpose, so the headline win rate
   * stays a statement about the strategies. */
  it('says why it is not on the Dashboard', () => {
    render(<App />)
    expect(within(section('LLM origination')).getByText(/kept off the Dashboard/)).toBeInTheDocument()
  })
})

describe('the strategy list', () => {
  it('badges the running strategy as active', () => {
    render(<App />)
    expect(within(section('Strategies')).getByText('Active')).toBeInTheDocument()
  })

  /** A 68% backtest and a 71% live record are different claims about different
   * evidence. A blended figure would be the most misleading number here. */
  it('labels backtest and live separately when expanded', () => {
    render(<App />)
    expandStrategy('Spx Mean Reversion')

    const row = strategyRow('Spx Mean Reversion')
    expect(within(row).getByText('Backtest')).toBeInTheDocument()
    expect(within(row).getByText('Live')).toBeInTheDocument()
    expect(within(row).getByText(/Spread modelled, not measured/)).toBeInTheDocument()
    expect(within(row).getByText(/Actual fills from this account/)).toBeInTheDocument()
  })

  it('says a never-traded strategy has no live record rather than showing its backtest there', () => {
    render(<App />)
    expandStrategy('Sector Momentum')

    expect(within(section('Strategies')).getByText(/Never traded live/)).toBeInTheDocument()
  })

  it('reports a passing promotion gate', () => {
    render(<App />)
    expandStrategy('Spx Mean Reversion')

    expect(within(section('Strategies')).getByText(/Clears every auto-approval threshold/)).toBeInTheDocument()
  })

  /** §5.3 reports rather than blocks — anything missing the gate can still be
   * promoted manually, so the failures have to be named. */
  it('names each threshold a strategy misses, and still allows promotion', () => {
    render(<App />)
    expandStrategy('Earnings Iv Crush')

    const strategies = section('Strategies')
    expect(within(strategies).getByText(new RegExp(`needs ${GATE_THRESHOLDS.winRate}%`))).toBeInTheDocument()
    expect(within(strategies).getByText(/it does not make\s+the decision/)).toBeInTheDocument()
    expect(within(strategies).getByRole('button', { name: 'Promote to Active' })).toBeEnabled()
  })

  it('offers only the next stage, never a skip', () => {
    render(<App />)
    expandStrategy('Vix Spike Fade') // draft

    const strategies = section('Strategies')
    expect(within(strategies).getByRole('button', { name: 'Promote to Backtest' })).toBeInTheDocument()
    expect(within(strategies).queryByRole('button', { name: 'Promote to Active' })).not.toBeInTheDocument()
    // A draft has nothing to retire from.
    expect(within(strategies).queryByRole('button', { name: 'Retire' })).not.toBeInTheDocument()
  })

  /** §5.2 permits exactly one active strategy. */
  it('demotes the incumbent to paper when another is promoted', () => {
    render(<App />)
    expandStrategy('Earnings Iv Crush')
    fireEvent.click(within(section('Strategies')).getByRole('button', { name: 'Promote to Active' }))

    const strategies = useUIStore.getState().strategies
    expect(strategies.find((s) => s.id === 'strat-2')!.status).toBe('active')
    expect(strategies.find((s) => s.id === 'strat-1')!.status).toBe('paper')
    expect(strategies.filter((s) => s.status === 'active')).toHaveLength(1)
  })

  it('renames a strategy, and the rename reaches the Dashboard dropdown', () => {
    render(<App />)
    expandStrategy('Vix Spike Fade')

    const strategies = section('Strategies')
    fireEvent.change(within(strategies).getByLabelText('Name'), { target: { value: 'renamed_fade' } })
    fireEvent.click(within(strategies).getByRole('button', { name: 'Rename' }))

    expect(useUIStore.getState().strategies.find((s) => s.id === 'strat-4')!.name).toBe('renamed_fade')

    fireEvent.click(screen.getByRole('link', { name: 'Dashboard' }))
    // The dropdown prefixes each option with "Strategy: ".
    expect(
      screen.getByRole('option', { name: `Strategy: ${formatStrategyName('renamed_fade')}` }),
    ).toBeInTheDocument()
  })

  it('deletes a strategy behind a confirm that states what goes', () => {
    render(<App />)
    expandStrategy('Vix Spike Fade')
    fireEvent.click(within(section('Strategies')).getByRole('button', { name: 'Delete' }))

    const dialog = screen.getByRole('alertdialog')
    expect(dialog.textContent).toMatch(/cannot be undone/)
    expect(useUIStore.getState().strategies).toHaveLength(initialState.strategies.length)

    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete strategy' }))
    expect(useUIStore.getState().strategies).toHaveLength(initialState.strategies.length - 1)
  })

  /** Deleting the running strategy would leave the positions it opened with
   * nothing managing them. */
  it('refuses to delete the active strategy', () => {
    render(<App />)
    expandStrategy('Spx Mean Reversion')

    expect(within(section('Strategies')).getByRole('button', { name: 'Delete' })).toBeDisabled()
  })
})

describe('Recommended Trades (full)', () => {
  function table(): HTMLElement {
    return within(section('Recommended Trades')).getByRole('table')
  }

  it('shows every candidate with its setup, reason and origin', () => {
    render(<App />)

    expect(within(table()).getAllByRole('row')).toHaveLength(RECOMMENDATIONS.length + 1)
    // Two candidates share this setup class, which is the point of a setup
    // class — it is not a per-row identifier.
    expect(within(table()).getAllByText('mean_reversion').length).toBeGreaterThan(1)
    expect(within(table()).getByText('RSI 28, below 20d SMA')).toBeInTheDocument()
    expect(within(table()).getByText('LLM')).toBeInTheDocument()
  })

  it('executes behind a confirm naming the risk manager', () => {
    render(<App />)
    const target = RECOMMENDATIONS[0]

    const row = within(table())
      .getAllByRole('row')
      .find((r) => r.textContent?.includes(recommendationTitle(target)))!
    fireEvent.click(within(row).getByRole('button', { name: 'Execute' }))

    const dialog = screen.getByRole('alertdialog')
    expect(dialog.textContent).toMatch(/risk manager/)
    expect(useUIStore.getState().dispositions[target.id]).toBeUndefined()

    fireEvent.click(within(dialog).getByRole('button', { name: 'Submit to risk manager' }))
    expect(useUIStore.getState().dispositions[target.id]).toBe('executed')
    expect(within(table()).getAllByText('Executed').length).toBeGreaterThan(0)
  })

  it('warns in the confirm when the candidate is an unvalidated origination', () => {
    render(<App />)
    const llm = RECOMMENDATIONS.find((r) => r.unvalidated)!

    const row = within(table())
      .getAllByRole('row')
      .find((r) => r.textContent?.includes(recommendationTitle(llm)))!
    fireEvent.click(within(row).getByRole('button', { name: 'Execute' }))

    expect(screen.getByRole('alertdialog').textContent).toMatch(/third of normal size/)
  })

  /** Queued is not executed. Nothing has been sent to a broker. */
  it('queues without a confirm and says the engine will place it', () => {
    render(<App />)
    const target = RECOMMENDATIONS[0]

    const row = within(table())
      .getAllByRole('row')
      .find((r) => r.textContent?.includes(recommendationTitle(target)))!
    fireEvent.click(within(row).getByRole('button', { name: 'Queue' }))

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(useUIStore.getState().dispositions[target.id]).toBe('queued')
    expect(within(table()).getByText('Queued')).toBeInTheDocument()
  })

  it('stops offering Execute and Queue once a row has been acted on', () => {
    render(<App />)
    const target = RECOMMENDATIONS[0]
    act(() => useUIStore.getState().queueRecommendation(target.id))

    const row = within(table())
      .getAllByRole('row')
      .find((r) => r.textContent?.includes(recommendationTitle(target)))!
    expect(within(row).getByRole('button', { name: 'Execute' })).toBeDisabled()
    expect(within(row).getByRole('button', { name: 'Queue' })).toBeDisabled()
  })

  it('removes a dismissed row and restores it on refresh', () => {
    render(<App />)
    const target = RECOMMENDATIONS[0]

    const row = within(table())
      .getAllByRole('row')
      .find((r) => r.textContent?.includes(recommendationTitle(target)))!
    fireEvent.click(within(row).getByRole('button', { name: new RegExp(`Dismiss ${target.symbol}`) }))

    expect(within(table()).queryByText(recommendationTitle(target))).not.toBeInTheDocument()

    fireEvent.click(within(section('Recommended Trades')).getByRole('button', { name: /Refresh/ }))
    expect(within(table()).getByText(recommendationTitle(target))).toBeInTheDocument()
  })

  /** A refresh restores dismissals but must not discard what you decided. */
  it('leaves queued rows alone on refresh', () => {
    render(<App />)
    const target = RECOMMENDATIONS[0]
    act(() => useUIStore.getState().queueRecommendation(target.id))

    fireEvent.click(within(section('Recommended Trades')).getByRole('button', { name: /Refresh/ }))
    expect(useUIStore.getState().dispositions[target.id]).toBe('queued')
  })

  it('disables Execute and Queue while the engine is halted, but not Dismiss', () => {
    render(<App />)
    act(() => {
      useUIStore.getState().setExecutionMode('auto')
      useUIStore.getState().halt()
    })

    const row = within(table()).getAllByRole('row')[1]
    expect(within(row).getByRole('button', { name: 'Execute' })).toBeDisabled()
    expect(within(row).getByRole('button', { name: 'Queue' })).toBeDisabled()
    expect(within(row).getByRole('button', { name: /^Dismiss/ })).toBeEnabled()
  })

  it('explains an emptied list rather than showing a bare table', () => {
    render(<App />)
    act(() => {
      for (const r of RECOMMENDATIONS) useUIStore.getState().dismissRecommendation(r.id)
    })

    const panel = section('Recommended Trades')
    expect(within(panel).queryByRole('table')).not.toBeInTheDocument()
    expect(within(panel).getByText(/does not remember what you waved off/)).toBeInTheDocument()
  })
})

/** The reason dismissal moved into the store. Two pages act on one candidate
 * set, and each holding its own copy would have them disagree. */
describe('recommendation state is shared with the Dashboard', () => {
  it('carries a dismissal from Research to the Dashboard panel', () => {
    render(<App />)
    const target = RECOMMENDATIONS[0]
    act(() => useUIStore.getState().dismissRecommendation(target.id))

    fireEvent.click(screen.getByRole('link', { name: 'Dashboard' }))
    const panel = screen.getByRole('heading', { name: 'Recommended Trades' }).closest('section')!

    expect(within(panel).queryByText(recommendationTitle(target))).not.toBeInTheDocument()
  })

  it('carries a Dashboard execution back to Research', () => {
    render(<App />)
    const target = RECOMMENDATIONS[0]
    act(() => useUIStore.getState().executeRecommendation(target.id))

    expect(
      within(within(section('Recommended Trades')).getByRole('table')).getAllByText('Executed').length,
    ).toBeGreaterThan(0)
  })
})
