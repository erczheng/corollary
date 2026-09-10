import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, within, fireEvent, act } from '@testing-library/react'
import App from '../App'
import { useUIStore } from '../lib/store'
import { NOTIFICATIONS } from '../lib/mockData'
import { NOTIFICATION_EVENT_LABEL } from '../lib/types'
import { unreadCount, visibleNotifications } from '../lib/notifications'

const initialState = useUIStore.getState()

beforeEach(() => {
  window.history.pushState({}, '', '/')
  useUIStore.setState({ ...initialState }, true)
})

function bell(): HTMLElement {
  return screen.getByRole('button', { name: /^Notifications/ })
}

function openPanel(): HTMLElement {
  render(<App />)
  fireEvent.click(bell())
  return screen.getByRole('dialog', { name: 'Notifications' })
}

describe('the bell', () => {
  it('opens and closes the panel', () => {
    render(<App />)
    expect(screen.queryByRole('dialog', { name: 'Notifications' })).not.toBeInTheDocument()

    fireEvent.click(bell())
    expect(screen.getByRole('dialog', { name: 'Notifications' })).toBeInTheDocument()

    fireEvent.click(bell())
    expect(screen.queryByRole('dialog', { name: 'Notifications' })).not.toBeInTheDocument()
  })

  it('closes on Escape', () => {
    openPanel()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: 'Notifications' })).not.toBeInTheDocument()
  })

  it('closes on a click outside it', () => {
    openPanel()
    fireEvent.mouseDown(document.body)
    expect(screen.queryByRole('dialog', { name: 'Notifications' })).not.toBeInTheDocument()
  })

  it('reports expansion state for assistive tech', () => {
    render(<App />)
    expect(bell()).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(bell())
    expect(bell()).toHaveAttribute('aria-expanded', 'true')
  })
})

describe('the panel contents', () => {
  it('lists the notifications visible in the current book, newest first', () => {
    const panel = openPanel()
    const visible = visibleNotifications(NOTIFICATIONS, 'paper')

    expect(within(panel).getAllByRole('listitem')).toHaveLength(visible.length)
    expect(within(panel).getAllByRole('listitem')[0].textContent).toContain(
      NOTIFICATION_EVENT_LABEL[visible[0].event],
    )
  })

  it('shows each notification’s detail, not just its type', () => {
    const panel = openPanel()
    const rejection = NOTIFICATIONS.find((n) => n.event === 'order_rejected')!
    expect(within(panel).getByText(rejection.detail)).toBeInTheDocument()
  })

  /** An engine fault belongs to no book, so it appears in both. Scoping it
   * away would hide the class of event you most need to see. */
  it('shows account-less events regardless of which account is selected', () => {
    const engineError = NOTIFICATIONS.find((n) => n.event === 'engine_error')!
    const panel = openPanel()
    expect(within(panel).getByText(engineError.detail)).toBeInTheDocument()

    act(() => useUIStore.setState({ accountMode: 'cash' }))
    expect(within(screen.getByRole('dialog', { name: 'Notifications' })).getByText(engineError.detail)).toBeInTheDocument()
  })

  /** A paper fill announcing itself while Cash is live misreports which money
   * moved — the same failure as rendering paper's balance. */
  it('hides the other account’s notifications', () => {
    const halt = NOTIFICATIONS.find((n) => n.event === 'daily_loss_halt')!
    expect(halt.account).toBe('cash')

    const panel = openPanel()
    expect(within(panel).queryByText(halt.detail)).not.toBeInTheDocument()

    act(() => useUIStore.setState({ accountMode: 'cash' }))
    expect(within(screen.getByRole('dialog', { name: 'Notifications' })).getByText(halt.detail)).toBeInTheDocument()
  })

  /** Glancing at a badge and closing the panel is not the same act as reading
   * eight notifications. Auto-clearing on open would destroy the only state
   * that says what you have not yet seen. */
  it('does not mark anything read merely by opening', () => {
    const before = unreadCount(NOTIFICATIONS, 'paper')
    openPanel()
    expect(unreadCount(useUIStore.getState().notifications, 'paper')).toBe(before)
  })

  it('clears the unread count on an explicit Mark all read', () => {
    const panel = openPanel()
    fireEvent.click(within(panel).getByRole('button', { name: 'Mark all read' }))

    expect(unreadCount(useUIStore.getState().notifications, 'paper')).toBe(0)
    // And the affordance goes away, since there is nothing left to clear.
    expect(
      within(screen.getByRole('dialog', { name: 'Notifications' })).queryByRole('button', {
        name: 'Mark all read',
      }),
    ).not.toBeInTheDocument()
  })

  it('leaves the other book’s unread count alone when marking read', () => {
    const panel = openPanel()
    fireEvent.click(within(panel).getByRole('button', { name: 'Mark all read' }))

    expect(unreadCount(useUIStore.getState().notifications, 'cash')).toBeGreaterThan(0)
  })

  it('dismisses one notification without touching the others', () => {
    const panel = openPanel()
    const target = visibleNotifications(NOTIFICATIONS, 'paper')[0]
    const before = within(panel).getAllByRole('listitem').length

    fireEvent.click(
      within(panel).getByRole('button', {
        name: `Dismiss ${NOTIFICATION_EVENT_LABEL[target.event]}`,
      }),
    )

    const after = screen.getByRole('dialog', { name: 'Notifications' })
    expect(within(after).getAllByRole('listitem')).toHaveLength(before - 1)
    expect(within(after).queryByText(target.detail)).not.toBeInTheDocument()
  })

  /** An empty bell during the session means the engine had nothing to report,
   * which is a different claim from the bell being broken. */
  it('explains an empty feed instead of rendering an empty list', () => {
    useUIStore.setState({ notifications: [] })
    const panel = openPanel()

    expect(within(panel).queryByRole('listitem')).not.toBeInTheDocument()
    expect(within(panel).getByText(/nothing to report/)).toBeInTheDocument()
  })
})

/** A stop firing is a loss doing what it was told to do. Rendering it in
 * `error` would say the system failed, which CLAUDE.md keeps distinct from a
 * position losing money. */
describe('severity styling', () => {
  it('renders a rejection in error and a stop loss in caution', () => {
    const panel = openPanel()

    const rejection = within(panel).getByText(NOTIFICATION_EVENT_LABEL.order_rejected)
    const stop = within(panel).getByText(NOTIFICATION_EVENT_LABEL.stop_loss_hit)

    expect(rejection.className).toContain('text-error')
    expect(stop.className).toContain('text-caution')
    expect(stop.className).not.toContain('text-error')
  })

  it('never renders a notification in bearish, which is for losses', () => {
    const panel = openPanel()
    expect(panel.innerHTML).not.toContain('text-bearish')
  })
})

/** The bell is the mock broker reporting what it did while you were not
 * looking, and the routing matrix decides what reaches it. */
describe('the bell and the routing matrix together', () => {
  it('gains a notification when the tick fills a resting order', () => {
    render(<App />)
    const target = initialState.openPositions.paper.find((p) => p.id === 'pos-2')!

    act(() => {
      useUIStore.setState({
        workingOrders: {
          paper: [
            {
              id: 'wo-bell',
              positionId: target.id,
              contractKey: null,
              contract: `${target.symbol} ${target.contract}`,
              side: 'STC',
              orderType: 'limit',
              quantity: target.quantity,
              limitPrice: 0.01,
              stopPrice: null,
              timeInForce: 'gtc',
              placedAt: '2026-08-07T15:00:00Z',
              activityId: 'act-1',
            },
          ],
          cash: [],
        },
      })
      useUIStore.getState().tick()
    })

    fireEvent.click(bell())
    const panel = screen.getByRole('dialog', { name: 'Notifications' })
    expect(within(panel).getAllByText(NOTIFICATION_EVENT_LABEL.order_filled).length).toBeGreaterThan(1)
  })

  it('delivers nothing to the bell once that event’s route is off', () => {
    render(<App />)
    const before = useUIStore.getState().notifications.length

    act(() => {
      useUIStore.getState().setNotificationRoute('order_filled', 'bell', false)
      const target = useUIStore.getState().openPositions.paper.find((p) => p.direction === 'long')!
      useUIStore.getState().upsertExit(target.id, {
        takeProfit: 0.01,
        stopPrice: 0.001,
        stopLimitPrice: null,
        timeInForce: 'gtc',
        heldBy: 'broker',
      })
      useUIStore.getState().tick()
    })

    expect(useUIStore.getState().notifications).toHaveLength(before)
  })
})
