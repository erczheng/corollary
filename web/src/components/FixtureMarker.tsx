import { Chip } from './Chip'

/** The one sentence that says a surface is still fixture-backed.
 *
 * Phase 2 decision 8. PRD §8.5's argument that *"a table of invented numbers
 * reads as invented"* held while every page was a fixture. It stops holding
 * the moment the page next door is the engine's own data: News' sentiment
 * composite in particular reads as *computed* when Account, Activity,
 * Markets and Settings' ceilings all are. So the three surfaces that are
 * still mock say so, in the same words as the scripted chat's chip.
 *
 * One component rather than three copies of the sentence, because three
 * copies drift and the wording is the whole point of the thing. The label is
 * fixed here; `detail` is the hover text, which has to differ — what is
 * invented on News is not what is invented on Research.
 *
 * Small enough to sit on a **panel** as well as a page title. Settings needed
 * that until Phase 3 step 6 made its sentiment table the server's: a marker
 * over a page title would label the real panels beside a fixture as invented,
 * which is worse than no marker. Settings now carries none.
 *
 * **`neutral`, not `caution`.** Being fixture-backed is a stated condition of
 * the build, not something to act on — and `caution` has to keep meaning
 * something — a demoted-source banner, say; two amber things in one panel
 * means neither gets read. Not `error`, which is a fault (a rejected
 * order, a dead connection), and not `bearish`, which is a loss — those two
 * stay apart and this is neither. Not `accent`: it is capped at two ranked
 * roles per screen and a build-stage label is not worth one of them. `Chip`
 * is `rounded-full` already, which is right — this reads as status, not as a
 * control. `FixtureMarker.test.tsx` pins the variant, because every word of
 * that argument lives in this comment and a comment is what a future diff
 * deletes.
 *
 * **The detail is said twice, to two audiences, from one string.** `title` is
 * mouse-only: the chip is a non-focusable `<span>`, so there is no keyboard
 * path to the tooltip, and `title` on a role-less generic is announced
 * inconsistently anyway. The visible label is the load-bearing claim — nobody
 * is misled about *whether* a surface is a fixture — but *what* is invented
 * and *when* it becomes real would otherwise be reachable by hover alone. The
 * `sr-only` span carries the same variable, so the two cannot drift.
 */
export function FixtureMarker({
  detail,
  /** Set where the surface already restates `detail` in visible prose, which
   * no surface does today (the Settings sentiment panel did, until it became
   * the server's). Without this a screen reader
   * reads the same claim twice in a row, and a description repeated verbatim
   * gets tuned out the same way a duplicated banner does. */
  detailRestated = false,
}: {
  detail: string
  detailRestated?: boolean
}) {
  return (
    <>
      <Chip variant="neutral" title={detail}>
        Sample data — Phase 1
      </Chip>
      {detailRestated ? null : <span className="sr-only">{detail}</span>}
    </>
  )
}
