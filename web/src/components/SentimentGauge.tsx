import { SENTIMENT_COMPONENTS, SENTIMENT_PREVIOUS, SENTIMENT_AS_OF } from '../lib/mockData'
import {
  SENTIMENT_BAND_LABEL,
  compositeScore,
  sentimentBand,
  sentimentTone,
  type SentimentTone,
} from '../lib/news'
import { formatDateTimeET } from '../lib/format'

/** Text colour per tone.
 *
 * `bullish`/`bearish` are correct here and this is the one place on the
 * News page where they are — the composite is a directional read on the
 * tape, so greed really is the bullish end. Never `error`: extreme fear is
 * a market condition, not a system failure. */
const TONE_TEXT: Record<SentimentTone, string> = {
  bullish: 'text-bullish',
  bearish: 'text-bearish',
  neutral: 'text-on-surface-variant',
}

const TONE_FILL: Record<SentimentTone, string> = {
  bullish: 'bg-bullish',
  bearish: 'bg-bearish',
  neutral: 'bg-neutral',
}

/** A component's score as a labelled hairline bar.
 *
 * One line per component rather than the three it used to take. Each still
 * carries its own tone rather than the composite's, because that is the
 * whole reason to show the breakdown: a 58 built from four greedy readings
 * and three fearful ones is a different market from a 58 built from seven
 * readings at 58, and a column of uniformly-coloured bars would hide
 * exactly that. The description moves to the row's title — it explains the
 * input, which is a thing you look up once, not a thing you re-read on
 * every glance. */
function ComponentRow({
  name,
  description,
  score,
}: {
  name: string
  description: string
  score: number
}) {
  const tone = sentimentTone(score)
  return (
    <div
      title={`${name} — ${description}`}
      className="flex items-center gap-2 py-1"
    >
      {/* w-32, not w-28: "Safe-haven demand" is the longest label and it
          truncated to "Safe-haven dema…" at the narrower width, which is
          the one component name a reader cannot reconstruct. */}
      <span className="w-32 shrink-0 truncate text-caption text-on-surface">{name}</span>
      <span
        className="h-1 flex-1 overflow-hidden rounded-full bg-surface-container-high"
        role="img"
        aria-label={`${name}: ${score} out of 100, ${SENTIMENT_BAND_LABEL[
          sentimentBand(score)
        ].toLowerCase()}`}
      >
        <span className={`block h-full rounded-full ${TONE_FILL[tone]}`} style={{ width: `${score}%` }} />
      </span>
      {/* w-8 leaves room for a three-digit 100 — at w-6 the top of the
          scale would clip the one reading that means the most. */}
      <span className={`w-8 shrink-0 text-right text-data-md ${TONE_TEXT[tone]}`}>{score}</span>
    </div>
  )
}

/** The 0-100 market sentiment composite and its seven components.
 *
 * The headline number is **derived** from the components rather than stored
 * beside them, so the figure and the breakdown printed underneath it cannot
 * disagree — see `compositeScore`.
 *
 * PRD.md §8.3 asks for the breakdown "on hover". It is rendered inline
 * instead: a hover-only disclosure does not exist on a screenshot, on a
 * touch device, or to a keyboard, which is the same argument CLAUDE.md
 * already makes for rejection reasons on Activity. At one line per
 * component it costs seven rows in a sidebar, which is cheaper than the
 * interaction it replaces. */
export function SentimentGauge() {
  const score = compositeScore(SENTIMENT_COMPONENTS)
  const band = sentimentBand(score)
  const tone = sentimentTone(score)
  const change = score - SENTIMENT_PREVIOUS

  return (
    <section
      aria-labelledby="market-sentiment-heading"
      className="rounded-lg border border-outline-warm bg-surface-container-lowest p-4"
    >
      <h2 id="market-sentiment-heading" className="text-title-lg text-on-surface">
        Market Sentiment
      </h2>

      <div className="mt-2 flex items-baseline justify-between gap-2">
        <p className={`text-data-xl ${TONE_TEXT[tone]}`}>{score}</p>
        <div className="text-right">
          <p className={`text-label-md ${TONE_TEXT[tone]}`}>{SENTIMENT_BAND_LABEL[band]}</p>
          {/* Points, not percent: this is a 0-100 index, so a move from 51
              to 58 is +7 pts. Calling it +13.7% would be arithmetic on a
              number with no units to be a percentage of. */}
          <p className="text-caption text-on-surface-variant">
            <span className={change === 0 ? '' : change > 0 ? 'text-bullish' : 'text-bearish'}>
              {change > 0 ? '+' : change < 0 ? '−' : ''}
              {Math.abs(change)} pts
            </span>{' '}
            since yesterday
          </p>
        </div>
      </div>

      {/* The scale, so the number has somewhere to sit. Fear left, greed
          right, on the same 0-100 axis every component bar below uses. */}
      <div className="mt-3">
        <div
          // `bg-linear-to-r`, not `bg-gradient-to-r`: the latter is v3
          // syntax that v4 still honours as a deprecated alias. This is the
          // only gradient in the app, so it may as well not be the one
          // thing pinning a v3 name.
          className="relative h-1.5 rounded-full bg-linear-to-r from-bearish-container via-neutral-container to-bullish-container"
          role="img"
          aria-label={`Composite ${score} out of 100 — ${SENTIMENT_BAND_LABEL[band].toLowerCase()}`}
        >
          <span
            className="absolute top-1/2 h-3 w-1 -translate-x-1/2 -translate-y-1/2 rounded-full bg-on-surface"
            style={{ left: `${score}%` }}
          />
        </div>
        <div className="mt-1 flex justify-between text-caption text-on-surface-variant">
          <span>Extreme fear</span>
          <span>Extreme greed</span>
        </div>
      </div>

      <div className="mt-3 border-t border-outline-warm pt-2">
        {SENTIMENT_COMPONENTS.map((c) => (
          <ComponentRow key={c.name} {...c} />
        ))}
      </div>

      {/* Daily, after the close. The live pill on this page covers the feed
          and says nothing about this number, whose inputs are daily series —
          a composite restamped every fifteen seconds would be claiming a
          freshness it does not have. */}
      <p className="mt-2 border-t border-outline-warm pt-2 text-caption text-on-surface-variant">
        Seven components, equally weighted · computed {formatDateTimeET(SENTIMENT_AS_OF)}
      </p>
    </section>
  )
}
