import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClientProvider } from '@tanstack/react-query'
import { ChainOrderTicket } from './ChainOrderTicket'
import { OPTION_CHAIN } from '../lib/mockData'
import { queryClient } from '../lib/queryClient'
import { ENGINE_STATE_POLL_MS, queryKeys } from '../lib/queries'
import { useUIStore } from '../lib/store'
import type { EngineStateResponse } from '../lib/types'

const RUNNING: EngineStateResponse = {
  halted: false,
  haltedReason: null,
  haltedAt: null,
  t0: '2026-09-12T19:14:37.242679Z',
}
const HALTED: EngineStateResponse = {
  ...RUNNING,
  halted: true,
  haltedReason: 'Halted by hand from the command palette',
  haltedAt: '2026-11-17T14:00:00Z',
}

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } as unknown as Response
}

/** `GET /api/engine/state` answers with a state, a failure, or never. Every
 * other read is refused: the risk limits then read as "no ceiling
 * configured", which this file makes no claim about. */
function stubEngine(answer: EngineStateResponse | 'fail' | 'never') {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: unknown) => {
      const url = String(input)
      if (url.includes('/engine/state')) {
        if (answer === 'never') return new Promise<Response>(() => {})
        if (answer === 'fail') {
          return Promise.resolve(
            jsonResponse(503, {
              error: { code: 'engine_unavailable', message: 'The engine is not reachable.' },
            }),
          )
        }
        return Promise.resolve(jsonResponse(200, answer))
      }
      return Promise.resolve(
        jsonResponse(503, { error: { code: 'not_stubbed', message: 'Not stubbed.' } }),
      )
    }),
  )
}

/** A contract with a two-sided quote, so the default draft (one contract, a
 * limit at the mid) is valid and only the engine's state can disable Review. */
const contract = OPTION_CHAIN.find((c) => c.bid !== null && c.ask !== null && c.bid > 0)!

function renderTicket() {
  render(
    <QueryClientProvider client={queryClient}>
      <ChainOrderTicket
        contract={contract}
        quote={{ bid: contract.bid!, ask: contract.ask! }}
        spot={contract.strike}
        equity={100_000}
        equityUnavailable={false}
        onDone={() => {}}
      />
    </QueryClientProvider>,
  )
}

function review(): HTMLElement {
  return screen.getByRole('button', { name: /^Review / })
}

const initialState = useUIStore.getState()

beforeEach(() => {
  useUIStore.setState({ ...initialState }, true)
  queryClient.clear()
})

afterEach(() => {
  vi.unstubAllGlobals()
  queryClient.clear()
})

/** Spec decision C12 and parent decision 16. Review reads the **engine's**
 * halt. It used to read the store's fixture halt, which nothing in production
 * sets, so a halted engine never disabled it. */
describe('ChainOrderTicket and the engine halt', () => {
  it('offers Review once the engine reports running', async () => {
    stubEngine(RUNNING)
    renderTicket()

    await waitFor(() => expect(review()).toBeEnabled())
    expect(screen.queryByText(/^Trading is halted/)).not.toBeInTheDocument()
  })

  /** The gate is only as fresh as the engine-state cache, and the halt the
   * dead-man's switch takes reaches an open page by this poll alone: the
   * halt stays off the websocket because rule 9 halts *because* the socket
   * died. Until 2026-11-17 the query was not polled at all. Its cadence is
   * the contract, pinned the way `NotificationBell.test.tsx` pins the bell's. */
  it('reads an engine state that is re-read every 15 seconds, focused or not', async () => {
    stubEngine(RUNNING)
    renderTicket()
    await waitFor(() => expect(review()).toBeEnabled())

    const query = queryClient.getQueryCache().find({ queryKey: queryKeys.engineState() })!
    const options = query.observers[0].options
    expect(ENGINE_STATE_POLL_MS).toBe(15_000)
    expect(options.refetchInterval).toBe(15_000)
    expect(options.refetchIntervalInBackground).toBe(true)
  })

  it('disables Review while the engine is halted, and says so', async () => {
    stubEngine(HALTED)
    renderTicket()

    expect(await screen.findByText(/^Trading is halted/)).toBeInTheDocument()
    expect(review()).toBeDisabled()
  })

  it('ignores the fixture halt in the store', async () => {
    act(() => useUIStore.getState().halt())
    stubEngine(RUNNING)
    renderTicket()

    await waitFor(() => expect(review()).toBeEnabled())
  })

  it('disables Review, and says so, while the engine state is being read', () => {
    stubEngine('never')
    renderTicket()

    expect(screen.getByText(/^Reading engine state/)).toBeInTheDocument()
    expect(review()).toBeDisabled()
  })

  it('disables Review, and says why, when the engine state cannot be read', async () => {
    stubEngine('fail')
    renderTicket()

    expect(
      await screen.findByText(
        /^Engine state unavailable: The engine is not reachable\./,
        {},
        // One retry of a 5xx, a second apart, before the read gives up.
        { timeout: 4000 },
      ),
    ).toBeInTheDocument()
    expect(review()).toBeDisabled()
  })

  /** The halt can arrive while the confirm is open: the dead-man's switch
   * fires on its own (rule 9). The confirm is the last step, so it honours
   * the halt too, and nothing is submitted. */
  it('refuses the confirm if the engine halts while it is open', async () => {
    stubEngine(RUNNING)
    const submitOpenOrder = vi.fn()
    act(() => useUIStore.setState({ submitOpenOrder }))
    renderTicket()
    await waitFor(() => expect(review()).toBeEnabled())
    fireEvent.click(review())

    // TanStack delivers a cache write on its next tick, not inside `act`.
    act(() => queryClient.setQueryData(queryKeys.engineState(), HALTED))

    const dialog = screen.getByRole('alertdialog')
    const confirm = within(dialog).getByRole('button', { name: /^Buy to open/ })
    await waitFor(() => expect(confirm).toBeDisabled())
    expect(dialog.textContent).toMatch(/Trading is halted/)
    fireEvent.click(confirm)
    expect(submitOpenOrder).not.toHaveBeenCalled()
  })
})
