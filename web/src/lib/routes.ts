/** Every destination in the app, in header order.
 *
 * One list rather than one per surface. The command palette and the
 * not-found page are both answering "everywhere you could have meant to
 * go", and two copies of that answer would drift the first time a page is
 * added — the palette would offer a route the 404 page had never heard of.
 *
 * The header's own nav is deliberately *not* built from this: it shows five
 * labelled links and puts Account and Settings in the icon cluster instead
 * (see `Header.tsx`), so it is a different set for a stated reason rather
 * than a stale copy of this one.
 *
 * `/design` is absent on purpose. It exists to prove the token system, not
 * as a place anyone means to navigate during a session, and offering it as
 * a suggestion after a mistyped URL would be noise.
 */
export interface Destination {
  to: string
  label: string
  /** What you would go there to do.
   *
   * The not-found page prints these: someone who guessed a URL wrong knows
   * what they wanted but not what this app calls it, and a bare list of
   * seven nouns does not help with that. */
  blurb: string
}

export const DESTINATIONS: Destination[] = [
  { to: '/', label: 'Dashboard', blurb: 'Balance, performance, and today’s recommended trades' },
  { to: '/activity', label: 'Activity', blurb: 'Open positions, working orders, and the ledger' },
  { to: '/news', label: 'News', blurb: 'Headlines, sentiment, and the market calendar' },
  { to: '/markets', label: 'Markets', blurb: 'Option chains and the stock universe' },
  { to: '/research', label: 'Research', blurb: 'Chat, strategies, and the full candidate list' },
  { to: '/account', label: 'Account', blurb: 'Cash, buying power, and transfers' },
  { to: '/settings', label: 'Settings', blurb: 'Risk limits, data feeds, and notifications' },
]
