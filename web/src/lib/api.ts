/**
 * The HTTP client for the Corollary API. One function per endpoint, typed
 * against `types.ts`, with no React in it — the hooks that wrap these live
 * in `queries.ts`, and every function here is callable and testable without
 * rendering anything.
 *
 * Three things here are load-bearing rather than plumbing:
 *
 * **Account scoping is a required argument, never a default.** Every
 * account-scoped function takes `AccountMode` as its first parameter and
 * there is no fallback value. The server defaults a missing `?account` to
 * paper (rule 5), and that default is correct *for the server*; a client
 * that quietly relied on it would render one book's positions under the
 * other's name the moment the toggle moved, which is the same misreport
 * that keeps the store's state keyed by `AccountMode` instead of flattened.
 *
 * **A failure throws `ApiError`, carrying the status and the server's own
 * code and reason.** Nothing here returns `null` for an error or resolves
 * with an empty list. `?account=cash` without live credentials answers 409
 * with a stated reason, deliberately — an honest-looking empty ledger is
 * exactly the failure that 409 exists to prevent — so it surfaces as
 * `isAccountUnavailable(error)` and a page renders "Cash is not configured"
 * rather than "something went wrong".
 *
 * **Money crosses the wire as a JSON number and is display-only on this
 * side.** The server computes every total authoritatively; a figure derived
 * in the browser is the live estimate between refreshes. The one exception
 * runs the other way: a risk ceiling being *written* leaves as a string,
 * because it is stored exactly — see `wireMoney`.
 */
import {
  formatDateET,
  formatDateOnly,
  formatDateTimeET,
  formatSessionDateTimeET,
  formatTimeET,
  marketToday,
} from './format'
import type {
  AccountMode,
  AccountResponse,
  ActivityItem,
  ActivityStats,
  ActivityStatus,
  ApiErrorBody,
  ApiKeyPresence,
  AuditLogEntry,
  ChartRange,
  DataFeed,
  DataSourceStatus,
  EngineStateResponse,
  FeedKey,
  NotificationEvent,
  NotificationRoute,
  OptionContract,
  Page,
  PortfolioHistoryResponse,
  Position,
  RiskLimit,
  RiskLimitKey,
  StockQuote,
  UnderlyingQuote,
  WorkingOrder,
} from './types'

/* -------------------------------------------------------------------------
 * Where the API is
 * ---------------------------------------------------------------------- */

function resolveBase(raw: unknown): string {
  const value = typeof raw === 'string' && raw.trim() !== '' ? raw.trim() : '/api'
  return value.endsWith('/') ? value.slice(0, -1) : value
}

/** The prefix every request is built on.
 *
 * Relative by default, so the terminal never names a host: Vite proxies
 * `/api` to `127.0.0.1:8000` in development (see `vite.config.ts`) and the
 * built bundle is served from the same origin as the API. A hardcoded
 * absolute host would break the moment either side moved, and this terminal
 * binds to loopback and has to work with no internet at all.
 *
 * `VITE_API_BASE` overrides it for the case where the engine runs somewhere
 * the dev server does not proxy to. Config, not a literal. */
export const API_BASE: string = resolveBase(import.meta.env.VITE_API_BASE)

/** A query string value. `null` and `undefined` are dropped rather than sent
 * as the strings `"null"` and `"undefined"`, which is what a bare
 * `String(value)` would do and what would turn "no filter" into a filter
 * that matches nothing. */
export type QueryParams = Record<string, string | number | boolean | null | undefined>

/** Build a request URL. Exported because the tests assert on it directly:
 * the parameter spellings are a contract with FastAPI, and three of them are
 * not the spelling you would guess. */
export function apiUrl(path: string, params?: QueryParams): string {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value === undefined || value === null) continue
    query.set(key, String(value))
  }
  const search = query.toString()
  return search === '' ? `${API_BASE}${path}` : `${API_BASE}${path}?${search}`
}

/* -------------------------------------------------------------------------
 * Failures
 * ---------------------------------------------------------------------- */

/** The 409 a book with no credentials answers with. Paper is never
 * substituted: serving one book under the other's name misreports which
 * money moved. */
export const ACCOUNT_UNAVAILABLE = 'account_unavailable'

/** The engine could not be reached at all — not running, wrong port, or the
 * dev proxy has nothing behind it. Distinct from any HTTP status because
 * there was no response to carry one, and distinct in the remedy too. */
export const NETWORK_UNREACHABLE = 'network_unreachable'

/** A non-2xx whose body was not the API's error envelope: a proxy's own 502
 * page, an HTML error, a truncated response. `status` still carries the
 * number; only the machine-readable code is missing. */
export const HTTP_ERROR = 'http_error'

/** A failed request, with the server's stated condition attached.
 *
 * `code` is the branch point and `message` is for the person reading the
 * screen. Rule 8's standard applies on this side too: a rejection that
 * reaches the UI as a bare "something went wrong" has thrown away the only
 * part of it anybody can act on. */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly url: string

  constructor(params: { status: number; code: string; message: string; url: string }) {
    super(params.message)
    this.name = 'ApiError'
    this.status = params.status
    this.code = params.code
    this.url = params.url
  }
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError
}

/** The requested book has no credentials configured.
 *
 * A page branching on this renders "Cash is not configured" and names what
 * is missing (`AccountResponse.missingLiveCredentialEnvVars`, which the
 * *paper* read carries precisely so the question is answerable without the
 * book that cannot be read). It must never render as an empty ledger. */
export function isAccountUnavailable(error: unknown): boolean {
  return isApiError(error) && error.code === ACCOUNT_UNAVAILABLE
}

/** The engine is not answering. Worth its own branch: every other failure
 * means the server said something, and this one means it did not. */
export function isUnreachable(error: unknown): boolean {
  return isApiError(error) && error.code === NETWORK_UNREACHABLE
}

/** The API's error envelope, read out of an already-parsed body — or null
 * when the body is not that envelope.
 *
 * **The one error parser, and it is exported because a socket needs it
 * too.** `WsErrorFrame` carries `ApiErrorBody` under the same `error` key
 * and draws its codes from the same vocabulary (`invalid_request` means over
 * the socket exactly what the 422 means over HTTP) precisely so that there
 * is one shape to read. `schemas.py` says why in its own words: *"a client
 * holding two error parsers uses the wrong one on the day it matters."* So
 * this is a function rather than four lines inside `errorFor`, and
 * `liveSocket.ts` calls it rather than owning a second copy.
 *
 * Both fields are checked as strings: `request<T>` casts unvalidated JSON,
 * so a half-formed envelope is reachable, and a `code` that is not a string
 * is not something a caller may branch on. */
export function errorBodyOf(body: unknown): ApiErrorBody | null {
  const stated = (body as { error?: Partial<ApiErrorBody> } | null | undefined)?.error
  if (!stated || typeof stated.code !== 'string' || typeof stated.message !== 'string') {
    return null
  }
  return { code: stated.code, message: stated.message }
}

async function errorFor(response: Response, url: string): Promise<ApiError> {
  let code = HTTP_ERROR
  let message = `The engine answered ${response.status} for ${url}.`
  try {
    const stated = errorBodyOf(await response.json())
    if (stated) {
      code = stated.code
      message = stated.message
    }
  } catch {
    // Not the envelope — a proxy page, or nothing at all. The status is
    // still true and is the whole of what we know.
  }
  return new ApiError({ status: response.status, code, message, url })
}

/* -------------------------------------------------------------------------
 * The one fetch
 * ---------------------------------------------------------------------- */

/** What every endpoint function accepts. `signal` is TanStack Query's, so a
 * query that unmounts or is superseded aborts its request instead of
 * finishing into a cache nobody reads. */
export interface RequestOptions {
  signal?: AbortSignal
}

interface FetchInit extends RequestOptions {
  method?: 'GET' | 'POST' | 'PUT'
  params?: QueryParams
  body?: unknown
}

async function request<T>(path: string, init: FetchInit = {}): Promise<T> {
  const url = apiUrl(path, init.params)
  const hasBody = init.body !== undefined
  let response: Response
  try {
    response = await fetch(url, {
      method: init.method ?? 'GET',
      signal: init.signal,
      headers: hasBody
        ? { Accept: 'application/json', 'Content-Type': 'application/json' }
        : { Accept: 'application/json' },
      body: hasBody ? JSON.stringify(init.body) : undefined,
    })
  } catch (cause) {
    // An abort is the caller's own doing and has to stay an abort, or
    // TanStack Query records a cancellation as a failure and retries it.
    if (cause instanceof Error && cause.name === 'AbortError') throw cause
    throw new ApiError({
      status: 0,
      code: NETWORK_UNREACHABLE,
      message: `Could not reach the Corollary engine at ${url}. Is it running?`,
      url,
    })
  }
  if (!response.ok) throw await errorFor(response, url)
  return (await response.json()) as T
}

/* -------------------------------------------------------------------------
 * Shared parameter shapes
 * ---------------------------------------------------------------------- */

/** A page of a paginated endpoint. **`page` is zero-based** — the server's
 * convention, and not `usePagination`'s, which counts from 1 over an array
 * already in hand. Omitted values are omitted from the request, so the
 * server's own default page size stays the only one. */
export interface PageParams {
  page?: number
  pageSize?: number
}

function pageParams(params: PageParams): QueryParams {
  return { page: params.page, pageSize: params.pageSize }
}

/** Alpaca's portfolio-history window. `period` is a count and a unit, and
 * **the unit for a year is `A`, not `Y`** — the server rejects the rest with
 * a 422 rather than sending it on to be rejected three layers away. */
export interface HistoryWindow {
  period?: string
  timeframe?: string
}

export const DEFAULT_HISTORY_PERIOD = '1M'
export const DEFAULT_HISTORY_TIMEFRAME = '1D'

/* -------------------------------------------------------------------------
 * Series — what a range asks for, and how to read the answer
 *
 * A range control that slices one fixed daily series is not a range control:
 * `1D` over daily closes is a single point and `1W` is about five. The button
 * has to drive the *request*, which is what everything in this section is
 * for. `/api/markets/underlyings` and `/api/account/history` both take
 * `period` + `timeframe`, so one table serves both charts.
 * ---------------------------------------------------------------------- */

/** Every timeframe the series endpoints accept.
 *
 * **`1H` and `1D`, not the provider's `1Hour`/`1Day`.** The API owns that
 * normalisation; sending the vendor's own spelling is a 422. */
export type SeriesTimeframe = '1Min' | '5Min' | '15Min' | '1H' | '1D'

/** Which resolution a response came back at.
 *
 * `daily` points are keyed by a bare `YYYY-MM-DD` — a *date*, formatted in
 * UTC (format.ts#formatDateOnly), because rendering a date-only value in ET
 * shows the previous day. `intraday` points are keyed by a full ISO
 * **instant**, which carries its own offset and is rendered in
 * America/New_York like every other timestamp. Two rules in one chart; keep
 * them straight. */
export type SeriesResolution = 'daily' | 'intraday'

/** One window of a series: a depth and a resolution.
 *
 * `period` is `^[1-9][0-9]{0,2}[DWMA]$` and is **inclusive of today**. `A`
 * means years, not `Y`. */
export interface SeriesWindow {
  period: string
  timeframe: SeriesTimeframe
}

/** The deepest window the server will serve, and the reason the quoted
 * universe has carried 400 calendar days all along: at a quarter of history,
 * 3M, YTD, 1Y and All all draw the same chart. */
export const MAX_SERIES_PERIOD_DAYS = 400

/** The server's own ceiling: 2,000 points per symbol, 20,000 per request.
 * Quoted here so a new mapping can be checked without going to read the API.
 * A long period at a fine timeframe is refused with `invalid_series_window`,
 * and **that 422's message names the finest timeframe that would have fit** —
 * surface it, do not reword it. */
export const MAX_SERIES_POINTS_PER_SYMBOL = 2_000

/** The 422 a window past the ceiling answers with. Its message is written to
 * be read by a person ("Ask for 1W at 5Min instead, or shorten the period"),
 * so the UI renders it verbatim. */
export const INVALID_SERIES_WINDOW = 'invalid_series_window'

export function isInvalidSeriesWindow(error: unknown): boolean {
  return isApiError(error) && error.code === INVALID_SERIES_WINDOW
}

/** Year-to-date as a day count, inclusive of January 1 and of today.
 *
 * Computed rather than mapped onto `1A`, so YTD and 1Y stay two different
 * questions. Mapping several ranges onto one window is exactly the defect
 * that made 3M, YTD, 1Y and All draw the same chart.
 *
 * The arithmetic runs on **today's calendar date in market time**, parsed as
 * UTC on both sides — a local `new Date()` on one side of a date comparison
 * is an off-by-one on any afternoon in New York. */
function ytdPeriod(now: Date): string {
  const [year, month, day] = marketToday(now).split('-').map(Number)
  const elapsedMs = Date.UTC(year, month - 1, day) - Date.UTC(year, 0, 1)
  const days = Math.round(elapsedMs / 86_400_000) + 1
  // A year is at most 366 days, so the clamp is defensive: it is what stops
  // a bad clock producing a period the server would refuse.
  return `${Math.min(Math.max(days, 1), MAX_SERIES_PERIOD_DAYS)}D`
}

/** What a range button asks the server for.
 *
 *     1D  -> period 1D    timeframe 5Min     ~78 points
 *     1W  -> period 1W    timeframe 15Min   ~130
 *     1M  -> period 1M    timeframe 1D       ~22
 *     3M  -> period 3M    timeframe 1D       ~64
 *     YTD -> period nD    timeframe 1D       n × 5/7, n = days since Jan 1
 *     1Y  -> period 1A    timeframe 1D      ~251
 *     All -> period 400D  timeframe 1D      ~275
 *
 * The two intraday rows are **regular hours only** — the server filters
 * extended hours out — so a session is 78 five-minute bars or 26
 * fifteen-minute ones, and 1W is five of the latter. Size a new button
 * against those figures and not against a 6.5-hour day stretched over 24:
 * an all-hours 1D at 5Min would be 288, and these counts read as though
 * they were once computed that way.
 *
 * Every row sits inside the 2,000-point-per-symbol ceiling with room to
 * spare. Finer is permitted where it fits — 1M at 15Min is ~572 points —
 * but a year at 5Min is ~19,600 bars for one symbol, ten times that
 * ceiling, against a budget the Markets page already polls into; and a
 * ~900px chart can only draw ~900 of them anyway.
 *
 * `now` is injectable so the YTD row is testable without a clock. */
export function windowForRange(range: ChartRange, now: Date = new Date()): SeriesWindow {
  switch (range) {
    case '1D':
      return { period: '1D', timeframe: '5Min' }
    case '1W':
      return { period: '1W', timeframe: '15Min' }
    case '1M':
      return { period: '1M', timeframe: '1D' }
    case '3M':
      return { period: '3M', timeframe: '1D' }
    case 'YTD':
      return { period: ytdPeriod(now), timeframe: '1D' }
    case '1Y':
      return { period: '1A', timeframe: '1D' }
    case 'All':
      return { period: `${MAX_SERIES_PERIOD_DAYS}D`, timeframe: '1D' }
  }
}

/** One plotted point, with its x key left exactly as the server stated it.
 *
 * Deliberately **not** `PricePoint`: that type's `date` is a *day*, and
 * seventy-eight five-minute points sharing one `date` would all draw at the
 * same x — working-looking code that is not. What `key` means is stated by
 * the series' `resolution`, never guessed from the string. */
export interface SeriesPoint {
  key: string
  value: number
}

/** A series and the resolution it is actually at. */
export interface ChartSeries {
  /** `null` means **no resolution was stated**: for an underlying, neither
   * `history` nor `intraday` was populated, which is what `1D` asked on a
   * Sunday looks like. Render "no session in this window", not an empty
   * chart and not a flat line. */
  resolution: SeriesResolution | null
  points: SeriesPoint[]
  /** True when the response was **missing a series field** — not the same
   * thing as carrying an empty one. Either field absent is enough: both
   * are non-optional in the schema.
   *
   * Two empty arrays is the server *stating* that the window held no
   * session. A field that is not there at all is a response this client
   * cannot read a series out of, which means the running API and this page
   * disagree about the shape. The two share `resolution: null` and must not share wording:
   * reporting a skew as a quiet Sunday is a confident answer to a question
   * nobody answered.
   *
   * **Set whether or not the other field was populated**, which is the
   * whole point of the flag and the part that was missing. It used to be
   * computed only on the both-empty return path, so the shape that
   * actually occurs — `history: [...]` with no `intraday` key, a server
   * predating the split — came back as an ordinary
   * `{ resolution: 'daily', points }`. The only remaining detector was
   * requested-against-served resolution, and that mismatches only when the
   * request was intraday: `1D` and `1W`. On `1M`/`3M`/`YTD`/`1Y`/`All` a
   * stale server's default 400 closes drew cleanly under an `over 3M`
   * label measuring a year, with nothing on screen saying so.
   *
   * **It states a fact about the response, not what to render.** Which of
   * the two states it is comes from combining it with `points`:
   *
   * - **no points** — nothing drawable arrived, and the chart says the
   *   response carried no series at all, in place of a chart.
   * - **points** — draw them, and withhold everything that describes the
   *   *window*: the change figure and the session lead. A server that
   *   omits a field this build reads cannot be taken to have honoured the
   *   `period` it was sent either, and the drawn series is still the best
   *   thing to show.
   *
   * Optional because a series assembled on this side
   * (`/api/account/history`, which has no two-field split to read) always
   * states one. Absent means both fields were there. */
  missingField?: boolean
}

/** Read an underlying's series **off the response**, not off what was asked
 * for.
 *
 * `history` and `intraday` are never both populated: whichever is non-empty
 * states the resolution. Inferring it from the requested timeframe would be
 * right until the server answered something else, and then wrong silently. */
export function quoteSeries(quote: UnderlyingQuote): ChartSeries {
  // Both fields are read through `Array.isArray` rather than indexed
  // straight into. An API that predates the two-field split answers with
  // `history` and **no `intraday` key**, and `undefined.length` threw here
  // during render — which unmounted the whole Markets tree rather than
  // costing the one chart. The type says `IntradayPoint[]`, so nothing on
  // this side can catch that: only the server the browser is talking to can
  // be wrong about it, and it is not the client's to assume.
  const intraday = Array.isArray(quote.intraday) ? quote.intraday : null
  const history = Array.isArray(quote.history) ? quote.history : null

  // `||`, not `&&`, and computed **before** the returns rather than on the
  // empty one. Both fields are non-optional lists in the schema, so a
  // *missing* one is skew whichever it is and whether or not the other one
  // came back full — there is no shape in which the server means "no
  // session" by omitting a key. It was `&&` on the both-empty path only,
  // which missed the shape that actually occurs: `history` populated, no
  // `intraday` key. That returned a clean daily series and left the whole
  // detection to requested-against-served resolution, which is blind on
  // the five daily ranges. See `ChartSeries#missingField`.
  const missingField = intraday === null || history === null

  if (intraday !== null && intraday.length > 0) {
    return {
      resolution: 'intraday',
      points: intraday.map((point) => ({ key: point.at, value: point.value })),
      missingField,
    }
  }
  if (history !== null && history.length > 0) {
    return {
      resolution: 'daily',
      points: history.map((point) => ({ key: point.date, value: point.value })),
      missingField,
    }
  }
  // Empty either way, but for two different reasons — see `missingField`.
  // A stale server answering `history: []` with no `intraday` key for a
  // symbol it has no daily bars for (a recent listing, a halted name) read
  // as a quiet Sunday: the chart told the user to try a longer range, at a
  // server that ignores `period` and answers every range the same.
  return { resolution: null, points: [], missingField }
}

/** The resolution an echoed `timeframe` states.
 *
 * `/api/account/history` has no two-field split to read — it echoes the
 * `timeframe` it served, which answers the same question. Both spellings of
 * a day are accepted so a provider literal leaking through cannot quietly
 * turn a daily series into an intraday one. */
export function resolutionForTimeframe(timeframe: string): SeriesResolution {
  return timeframe === '1D' || timeframe === '1Day' ? 'daily' : 'intraday'
}

/** How a point's x key reads on an axis, a tooltip or a readout.
 *
 * The two resolutions take **two different rules and always will**: a daily
 * key is a bare `YYYY-MM-DD`, which is UTC midnight and prints the previous
 * day if it goes through an ET formatter; an intraday key is an instant
 * carrying its own offset, and every timestamp in this terminal is shown in
 * America/New_York. Nothing here formats anything itself — it picks which of
 * `format.ts`'s formatters the key has earned.
 *
 * `compact` drops the date, for an axis whose whole window is one ET day.
 *
 * It stays in `api.ts` rather than moving to `format.ts`, and that is the
 * settled arrangement, not a deferral. What this adds over the formatters
 * it calls is **dispatch on a `SeriesResolution`** — an `api.ts` type, read
 * off a response shape `api.ts` parses — plus the fail-closed guard below,
 * which exists because of where Recharts calls it from. Neither is
 * formatting, and `format.ts` would have to import the resolution to hold
 * it. It sits beside `seriesPointLabel`, which makes the same decision for
 * the tooltip. */
export function formatSeriesKey(
  resolution: SeriesResolution,
  key: string,
  opts: { compact?: boolean } = {},
): string {
  // Fails closed, exactly like `latestSession`'s caption guard and for a
  // worse version of the same reason: this runs inside Recharts'
  // `tickFormatter` and `labelFormatter`, so a key `Intl` cannot parse
  // raises RangeError *during render* and takes the chart and the Markets
  // tree down for an axis label. The caption was guarded and this was not,
  // which only moved the crash from the caption to the axis.
  //
  // The key itself is the fallback: it is what the server said, it is
  // never empty, and an axis reading `2026-13-45` is a legible fault where
  // a blank page is not. Only the server can be wrong about this shape —
  // a conforming one serialises `date`/`datetime` through pydantic and
  // cannot emit these.
  if (!isFormattableSeriesKey(resolution, key)) return key
  if (resolution === 'daily') return formatDateOnly(key)
  return opts.compact === true ? formatTimeET(key) : formatDateTimeET(key)
}

/** Would `Intl` accept this key, under the rule its resolution takes?
 *
 * The two constructions are the ones the formatters themselves use —
 * `format.ts#formatDateOnly` appends `T00:00:00Z` to a bare date, and every
 * ET formatter hands the key straight to `new Date`. Testing any other
 * construction would pass keys the real call then throws on. */
function isFormattableSeriesKey(resolution: SeriesResolution, key: string): boolean {
  const at = new Date(resolution === 'daily' ? `${key}T00:00:00Z` : key)
  return !Number.isNaN(at.getTime())
}

/** Does an intraday window cross an ET date boundary?
 *
 * A 1D window does not, so its ticks read `9:30 AM`. A 1W window does, and a
 * tick reading `3:45 PM` four times over would not say which day it meant.
 *
 * Asked only of an intraday series — a daily key is a day already — so the
 * endpoints are parsed the way an instant is.
 *
 * **Fails closed as `true`, and that is the safe direction.** `formatDateET`
 * raises RangeError on a key `Intl` cannot parse, in the component body
 * rather than in a formatter, which is a blank page instead of a wrong tick.
 * An unparseable endpoint means the span is unknown, and the answer that
 * survives not knowing is the one that keeps the date on every tick: a
 * labelled day is never ambiguous, a bare `3:45 PM` repeated is. */
export function seriesSpansDays(points: readonly SeriesPoint[]): boolean {
  if (points.length < 2) return false
  const first = points[0].key
  const last = points[points.length - 1].key
  if (!isFormattableSeriesKey('intraday', first) || !isFormattableSeriesKey('intraday', last)) {
    return true
  }
  return formatDateET(first) !== formatDateET(last)
}

/** How a point reads in the tooltip — **always the full date and the time**.
 *
 * This is not the axis label and must not be reduced to one. An intraday
 * range is drawn on an ordinal axis (`ordinalSeries`), so Friday 16:00 sits
 * directly beside Monday 09:30 and the x position no longer encodes elapsed
 * time. The crosshair is then the only surface that can say *when* a point
 * was, which is why it carries the weekday, the date, the year and the clock
 * time however compact the ticks below it are.
 *
 * The daily rule is untouched and stays the opposite one: a daily key is a
 * calendar date and formats in UTC, or it renders the previous day.
 *
 * Fails closed on an unparseable key exactly like `formatSeriesKey`, and for
 * the same reason — this runs inside Recharts' `labelFormatter`, where a
 * RangeError is a blank page rather than a bad label. */
export function seriesPointLabel(resolution: SeriesResolution, key: string): string {
  if (!isFormattableSeriesKey(resolution, key)) return key
  if (resolution === 'daily') return formatDateOnly(key)
  return `${formatSessionDateTimeET(key)} ET`
}

/** One point of a series, placed on an **ordinal** axis. */
export interface OrdinalPoint extends SeriesPoint {
  /** Position on the axis: the point's index in the series, and nothing
   * else. Consecutive points are one apart whether five minutes or a
   * weekend separates them. */
  index: number
}

/** Place an intraday series by **index rather than by instant**.
 *
 * **This is not what makes the sessions concatenate.** They already did.
 * The axis this replaced was a bare `dataKey="key"` with no `type` and no
 * `scale`, which Recharts reads as `type: 'category'` on a point scale:
 * one evenly spaced slot per bar, the overnight hole already closed. No
 * build of this chart ever drew a week as five thin bands with the nights
 * to scale, and a comment claiming one did sends the next reader hunting a
 * defect that was never there.
 *
 * What the explicit index buys is **the seams and the ticks**, neither of
 * which a category axis can do. A `ReferenceLine` on a category axis lands
 * *on top of* a bar, and a session seam has to sit in the half-step
 * *between* a close and the next open (`index - 0.5`) or it strikes
 * through one of the two bars it is separating. And a category axis takes
 * no explicit `ticks` array, so the session opens could not be made the
 * tick positions. A numeric axis over the index gives the same uniform
 * spacing, addressably.
 *
 * The cost is unchanged and was already being paid either way: x is a
 * position and not elapsed time, because the server serves regular hours
 * only and Friday 16:00 sits directly beside Monday 09:30. Two things pay
 * it back and both are load-bearing — `seriesPointLabel` puts the full
 * date and time in the tooltip, and `sessionBoundaries` puts a hairline at
 * each seam so the concatenation is visible rather than implied.
 *
 * The daily path keeps its category axis, and that is a decision about the
 * **axis**, not a property of the data: one point per session wants no
 * seams (there would be a boundary at every point) and no tick control (a
 * date per bar reads fine). It is *not* that a daily series is "already
 * evenly spaced" — even spacing is what a category axis does to whatever
 * it is handed. Feed that path weekly bars, or a gapped equity curve, and
 * it will space those evenly too, honest or not.
 *
 * Nothing is sorted or filtered here: the response's order is the axis
 * order, and a new array is returned rather than the points being mutated,
 * since the same array feeds the window readout. */
export function ordinalSeries(points: readonly SeriesPoint[]): OrdinalPoint[] {
  return points.map((point, index) => ({ ...point, index }))
}

/** Where one session ends and the next begins, as the **index of the first
 * point of each session after the first**.
 *
 * `[78]` over two concatenated 78-bar sessions: one entry per seam, so a
 * caller gets one separator per boundary rather than one per point. A
 * single-session window returns `[]` and draws no separator at all — a rule
 * with nothing to separate.
 *
 * **Intraday only, and now stated in the signature rather than implied.**
 * The resolution is a parameter and a daily series returns `[]`: its keys
 * are calendar dates, one per session, so every point after the first is a
 * boundary and the honest answer for that shape is a rule on every bar —
 * which is not a thing anybody wants drawn. Its sibling `seriesSpansDays`
 * documents the same constraint in prose; this one takes it as an
 * argument, because the wrong answer here is 250 hairlines rather than one
 * over-dated tick.
 *
 * The comparison is the **ET market date** of each instant, via
 * `marketToday`, never a bare `new Date()` and never the UTC date: a 16:00
 * ET bar is stamped 20:00Z, and a UTC-date comparison would find a seam in
 * the middle of every afternoon.
 *
 * **Fails closed by finding no seam** on a key `Intl` cannot parse, the
 * opposite direction from `seriesSpansDays` because the consequence is the
 * opposite. There the unknown answer costs a tick's date, which is only ever
 * more information; here it would draw a line asserting a session break that
 * may not exist, and a fabricated seam is a worse lie than a missing one. */
export function sessionBoundaries(
  resolution: SeriesResolution | null,
  points: readonly SeriesPoint[],
): number[] {
  // `null` — the server stated no resolution — lands here too, and lands
  // on the same side as daily: with nothing drawn there is nothing to
  // separate.
  if (resolution !== 'intraday') return []

  const seams: number[] = []
  let previous: string | null = null
  for (let i = 0; i < points.length; i += 1) {
    const key = points[i].key
    if (!isFormattableSeriesKey('intraday', key)) {
      previous = null
      continue
    }
    const day = marketToday(new Date(key))
    if (previous !== null && day !== previous) seams.push(i)
    previous = day
  }
  return seams
}

/** Which indices of an ordinal axis get a tick.
 *
 * Stated here rather than left to Recharts, which picks its own round
 * numbers for a numeric axis — and on an axis whose numbers are *positions*,
 * a tick at 19.25 formats as whatever bar happens to sit near it.
 *
 * Two shapes, because the question a tick answers differs by window. With
 * session seams on screen the useful ticks are the **session opens**: one
 * label per day, each sitting where its day starts. Inside a single session
 * there are no opens to mark, so the candidates are the bar positions
 * themselves.
 *
 * Both shapes thin the same way, and that rule is the one property here
 * worth holding: **at most `max` picks, spread evenly across the
 * candidates, with the first and the last always among them.** Striding by
 * `ceil(n / max)` from the front looks equivalent and is not — at eight
 * session opens the stride is 2, ticks land on opens 0, 2, 4 and 6, and
 * open 7 goes unlabelled. That drops the *newest* day on screen, which is
 * the part of the window anybody is looking at, while the oldest keeps its
 * label. The spread is mildly irregular when `max` does not divide the
 * candidates (eight opens label days 1, 2, 4, 5, 7 and 8); an unevenly
 * spaced label is cosmetic and a missing last day is wrong.
 *
 * No live window reaches that case — `1D` has no seams at all and `1W` has
 * at most four — so this is a property kept against the next range button
 * rather than a fix to something on screen. It is here because
 * `api.test.ts` claimed it and the code did not hold it, and of the two the
 * claim was the better behaviour. */
export function ordinalTicks(
  count: number,
  boundaries: readonly number[] = [],
  max = 6,
): number[] {
  if (count <= 0) return []
  if (count === 1) return [0]

  if (boundaries.length > 0) {
    const opens = [0, ...boundaries]
    return spreadIndices(opens.length, max).map((i) => opens[i])
  }

  return spreadIndices(count, max)
}

/** At most `max` indices into a list of `length`, evenly spread, **both
 * ends included**. Deduplicated, because a list shorter than `max` would
 * otherwise pick the same index twice and put two ticks on one bar. */
function spreadIndices(length: number, max: number): number[] {
  if (length <= 1) return length === 1 ? [0] : []
  const n = Math.min(max, length)
  const picked = Array.from({ length: n }, (_, k) => Math.round((k * (length - 1)) / (n - 1)))
  return [...new Set(picked)]
}

/** Which session the newest point on screen belongs to, and whether that
 * session is today's.
 *
 * `1D` on a Saturday draws Friday, and that is the right data — there is no
 * session today and an empty chart would be worse — but a control reading
 * `1D` over it reads as *today*. This is what lets a chart name the day it
 * is actually showing.
 *
 * **The series answers "is this today", not a market calendar.** If the
 * newest point is not today's ET date then an earlier session is on screen,
 * whatever the reason: weekend, holiday, half-day, or a symbol that stopped
 * printing. The Markets volume column makes the same test with
 * `volumeSession` / `volumeDate`, and asking a calendar instead would give a
 * second answer that could disagree with the data.
 *
 * The two resolutions take the two **opposite** timestamp rules. A `daily`
 * key *is* a calendar date and is already in the shape this compares. An
 * `intraday` key is an **instant**, and the day it belongs to is the day it
 * fell on in New York — which is exactly what `marketToday` resolves for any
 * instant. Its UTC date would roll over to tomorrow for anything stamped
 * after 20:00 ET.
 *
 * `today` is injectable so this is testable without a clock; it defaults to
 * the real market date and never to a bare `new Date()`, whose day is the
 * browser's rather than New York's. */
export interface SeriesSession {
  /** The newest point's market date, `YYYY-MM-DD` — the shape every
   * date-only value in this app compares in, and the shape
   * `format.ts#formatSessionDay` renders. */
  date: string
  /** True when that is today in New York: a session in progress, or one
   * that closed a few hours ago. Either way it is not a *previous* day. */
  isToday: boolean
  /** How to introduce `date` in words, or **null on today's session**,
   * which needs no introduction — the chart reads as today because it is
   * today. Deciding it here rather than in each chart is what stops one of
   * them captioning a live session "last market day". */
  lead: string | null
}

export function latestSession(
  series: ChartSeries,
  today: string = marketToday(),
): SeriesSession | null {
  const { resolution, points } = series
  const newest = points[points.length - 1]
  // Both series fields empty: no session in the window at all. That state
  // has its own wording, which says more than a day label would.
  if (resolution === null || newest === undefined) return null

  let date: string
  if (resolution === 'daily') {
    date = newest.key.slice(0, 10)
    // The same fail-closed rule as the intraday branch below, for the same
    // reason: this date is handed to `formatSessionDay`, which runs it
    // through `Intl` and raises RangeError on an unparseable one *during
    // render* — taking the chart, and the page around it, down for a
    // caption. Only the server can be wrong about this shape.
    if (Number.isNaN(new Date(`${date}T00:00:00Z`).getTime())) return null
  } else {
    const at = new Date(newest.key)
    // Fails closed rather than throwing: Intl raises RangeError on an
    // invalid Date, and an unparseable key should cost the label, not the
    // chart it sits above.
    if (Number.isNaN(at.getTime())) return null
    date = marketToday(at)
  }

  const isToday = date === today
  return { date, isToday, lead: isToday ? null : 'Last market day' }
}

export interface SeriesChange {
  from: SeriesPoint
  to: SeriesPoint
  change: number
  changePct: number
}

/** The change between two points of a series.
 *
 * The same arithmetic as `chart.ts#rangeChange`, over a series whose x may
 * be an instant rather than a day: indices arrive in whichever order the
 * drag happened, a drag that never left its start is not a window, and a
 * zero starting value makes the percentage meaningless rather than merely
 * large. Fold the two together when `chart.ts` is next in scope — this
 * change does not own that file.
 *
 * Display-only, like every other figure computed in the browser. */
export function seriesChange(
  points: readonly SeriesPoint[],
  a: number,
  b: number,
): SeriesChange | null {
  const lo = Math.min(a, b)
  const hi = Math.max(a, b)
  if (lo === hi) return null
  const from = points[lo]
  const to = points[hi]
  if (!from || !to || from.value === 0) return null
  const change = to.value - from.value
  return { from, to, change, changePct: (change / from.value) * 100 }
}

/* -------------------------------------------------------------------------
 * Account
 * ---------------------------------------------------------------------- */

export function fetchAccount(
  account: AccountMode,
  options: RequestOptions = {},
): Promise<AccountResponse> {
  return request<AccountResponse>('/account', { params: { account }, ...options })
}

export function fetchAccountHistory(
  account: AccountMode,
  window: HistoryWindow = {},
  options: RequestOptions = {},
): Promise<PortfolioHistoryResponse> {
  return request<PortfolioHistoryResponse>('/account/history', {
    params: {
      account,
      period: window.period ?? DEFAULT_HISTORY_PERIOD,
      timeframe: window.timeframe ?? DEFAULT_HISTORY_TIMEFRAME,
    },
    ...options,
  })
}

/** Deposits and withdrawals — **never orders**. The server reads the
 * non-trade branch of the activity feed; `account.ts` filters fixtures the
 * same way through `isOrderAction`, and the two agree on what a transfer
 * is. */
export function fetchTransfers(
  account: AccountMode,
  params: PageParams = {},
  options: RequestOptions = {},
): Promise<Page<ActivityItem>> {
  return request<Page<ActivityItem>>('/account/transfers', {
    params: { account, ...pageParams(params) },
    ...options,
  })
}

/* -------------------------------------------------------------------------
 * Activity
 * ---------------------------------------------------------------------- */

/** `search` and `status` **combine** rather than replacing one another —
 * "everything I did in AAPL that was rejected" is one question. */
export interface ActivityQuery extends PageParams {
  search?: string
  status?: ActivityStatus
}

export function fetchActivity(
  account: AccountMode,
  query: ActivityQuery = {},
  options: RequestOptions = {},
): Promise<Page<ActivityItem>> {
  return request<Page<ActivityItem>>('/activity', {
    params: {
      account,
      ...pageParams(query),
      search: query.search,
      status: query.status,
    },
    ...options,
  })
}

/** The header cards, folded over **every** realized trade rather than over
 * the page on screen. Do not recompute these from a page of `ActivityItem`:
 * a "lifetime" figure summed over a window means something narrower than the
 * word, and `notBooked` counts trades that have no P&L at all — a gap the
 * cards state rather than absorb. */
export function fetchActivityStats(
  account: AccountMode,
  options: RequestOptions = {},
): Promise<ActivityStats> {
  return request<ActivityStats>('/activity/stats', { params: { account }, ...options })
}

/* -------------------------------------------------------------------------
 * Positions
 * ---------------------------------------------------------------------- */

export function fetchPositions(
  account: AccountMode,
  options: RequestOptions = {},
): Promise<Position[]> {
  return request<Position[]>('/positions', { params: { account }, ...options })
}

/** Orders resting at the broker. **There is no cancel and no modify** in
 * this phase — a cancel would be the first broker write in the codebase and
 * would land before the risk manager exists, so the control renders disabled
 * with the reason stated rather than calling an endpoint that is
 * deliberately absent. */
export function fetchWorkingOrders(
  account: AccountMode,
  options: RequestOptions = {},
): Promise<WorkingOrder[]> {
  return request<WorkingOrder[]>('/positions/working', { params: { account }, ...options })
}

/* -------------------------------------------------------------------------
 * Markets
 *
 * Not account-scoped: a quote is a fact about the market, not about a book,
 * and the same price is right in both.
 * ---------------------------------------------------------------------- */

function symbolList(symbols?: readonly string[]): string | undefined {
  return symbols === undefined || symbols.length === 0 ? undefined : symbols.join(',')
}

/** The stock table. An unknown symbol is refused for the whole request
 * rather than skipped — a silently dropped row reads as "that stock has no
 * data" when the truth is "this server has never heard of it". */
export function fetchStocks(
  symbols?: readonly string[],
  options: RequestOptions = {},
): Promise<StockQuote[]> {
  return request<StockQuote[]>('/markets/stocks', {
    params: { symbols: symbolList(symbols) },
    ...options,
  })
}

/** Quoted underlyings with their series, at the resolution asked for.
 *
 * `window` is whatever a range control resolved to — see `windowForRange`.
 * Omit it and the server's own defaults apply (`400D` at `1D`).
 *
 * **`history_days` is not sent and is not accepted here any more.** It is a
 * deprecated alias for `period`, and sending both is a 422; one spelling on
 * this side is what makes that unreachable. */
export function fetchUnderlyings(
  symbols?: readonly string[],
  window?: SeriesWindow,
  options: RequestOptions = {},
): Promise<UnderlyingQuote[]> {
  return request<UnderlyingQuote[]>('/markets/underlyings', {
    // FastAPI names a query parameter by its Python spelling unless it
    // carries an alias, and only `pageSize` and `type` do — so a
    // multi-word parameter is snake case here. `period` and `timeframe`
    // are one word each and are the same on both sides.
    params: {
      symbols: symbolList(symbols),
      period: window?.period,
      timeframe: window?.timeframe,
    },
    ...options,
  })
}

/** One underlying's chain, bounded by a window rather than by a limit: both
 * vendor endpoints return contracts ordered by strike, so a limit returns
 * the deep-ITM tail and nothing near the money. */
export interface ChainQuery {
  /** Narrow to calls or puts. Sent as `type`, the one aliased parameter
   * here. */
  type?: 'call' | 'put'
  /** One expiry, `YYYY-MM-DD`. Replaces the DTE window rather than narrowing
   * it. A date, not an instant: it parses as UTC on both sides. */
  expiration?: string
  maxDte?: number
  /** Half-width of the strike band around spot, in percent. */
  moneynessPct?: number
}

export function fetchChain(
  underlying: string,
  query: ChainQuery = {},
  options: RequestOptions = {},
): Promise<OptionContract[]> {
  return request<OptionContract[]>(`/markets/chain/${encodeURIComponent(underlying)}`, {
    params: {
      type: query.type,
      expiration: query.expiration,
      max_dte: query.maxDte,
      moneyness_pct: query.moneynessPct,
    },
    ...options,
  })
}

/* -------------------------------------------------------------------------
 * Engine
 * ---------------------------------------------------------------------- */

export function fetchEngineState(options: RequestOptions = {}): Promise<EngineStateResponse> {
  return request<EngineStateResponse>('/engine/state', options)
}

/** Halt: stop new entries. Existing positions keep their managed exits and
 * **nothing is closed** — halt and flatten are distinct controls and distinct
 * calls, and merging them is rule 7's whole subject.
 *
 * The reason is required and is recorded. An unexplained halt presents
 * identically to a bot that has decided not to trade. */
export function haltEngine(
  reason: string,
  options: RequestOptions = {},
): Promise<EngineStateResponse> {
  return request<EngineStateResponse>('/engine/halt', {
    method: 'POST',
    body: { reason },
    ...options,
  })
}

/** Resume: end a halt. **Only ever from a human action.** Rule 9 forbids an
 * automatic resume — never on reconnect, never inside a retry, never in an
 * effect that notices the socket came back. Reconnecting into an unverified
 * position state is how a bot doubles a position it already holds. */
export function resumeEngine(options: RequestOptions = {}): Promise<EngineStateResponse> {
  return request<EngineStateResponse>('/engine/resume', { method: 'POST', ...options })
}

/* -------------------------------------------------------------------------
 * Settings
 * ---------------------------------------------------------------------- */

/** A ceiling on its way to being stored.
 *
 * `value` is a `number` here because that is what the form produced and what
 * `validateRiskLimit` checked; `wireMoney` is what puts it on the wire. */
export interface RiskLimitUpdate {
  key: RiskLimitKey
  value: number
}

/** A risk ceiling, spelled for the request boundary.
 *
 * **A JSON float is refused by the server, on purpose.** `7.5` decodes to an
 * IEEE double before anything can round it, and this value is *stored* — the
 * exact path CLAUDE.md keeps floats off. A quoted string parses exactly, so
 * every ceiling leaves as a string whether or not it has a fraction.
 *
 * This is the one direction in which money is not a JSON number, and the
 * asymmetry is deliberate: a response is a display value, a stored ceiling
 * is not. */
export function wireMoney(value: number): string {
  return String(value)
}

/** The five ceilings. `value` may be `null`, which means **no ceiling is
 * configured** — say so, never substitute a number. Two order tickets once
 * did this lookup themselves with different fallbacks, `?? 7` and `?? 0`, so
 * one invented a ceiling nobody had set and the other reported every trade
 * as over-limit. */
export function fetchRiskLimits(options: RequestOptions = {}): Promise<RiskLimit[]> {
  return request<RiskLimit[]>('/settings/limits', options)
}

/** Edit ceilings. Applied all or none — half a set leaves the engine
 * enforcing a combination nobody chose.
 *
 * Rule 4 is unchanged by this: it edits what is *stored*.
 * `RiskManager.approve()` decides what is allowed and reads the table, never
 * anything that arrived from the client. */
export function updateRiskLimits(
  updates: readonly RiskLimitUpdate[],
  options: RequestOptions = {},
): Promise<RiskLimit[]> {
  return request<RiskLimit[]>('/settings/limits', {
    method: 'PUT',
    body: {
      limits: updates.map((update) => ({ key: update.key, value: wireMoney(update.value) })),
    },
    ...options,
  })
}

export interface DataFeedUpdate {
  key: FeedKey
  value: string
}

export function fetchDataFeeds(options: RequestOptions = {}): Promise<DataFeed[]> {
  return request<DataFeed[]>('/settings/feeds', options)
}

/** Select a feed. The client marks upgrade-only values in the control; the
 * server refuses them, because requesting `opra` on Basic returns an auth
 * error from Alpaca rather than empty data, and storing it moves the failure
 * into the middle of a poll several layers from its cause. */
export function updateDataFeeds(
  updates: readonly DataFeedUpdate[],
  options: RequestOptions = {},
): Promise<DataFeed[]> {
  return request<DataFeed[]>('/settings/feeds', {
    method: 'PUT',
    body: { feeds: updates.map((update) => ({ key: update.key, value: update.value })) },
    ...options,
  })
}

export interface NotificationRouteUpdate {
  event: NotificationEvent
  bell: boolean
  discord: boolean
}

export function fetchNotificationRoutes(
  options: RequestOptions = {},
): Promise<NotificationRoute[]> {
  return request<NotificationRoute[]>('/settings/routes', options)
}

/** Route an event to a channel, or stop routing it.
 *
 * The gate this edits is applied **when an event is emitted**, never when
 * the panel renders: filtering at read time would let unchecking a route
 * retroactively erase notifications already received, and those happened. */
export function updateNotificationRoutes(
  updates: readonly NotificationRouteUpdate[],
  options: RequestOptions = {},
): Promise<NotificationRoute[]> {
  return request<NotificationRoute[]>('/settings/routes', {
    method: 'PUT',
    body: {
      routes: updates.map((update) => ({
        event: update.event,
        bell: update.bell,
        discord: update.discord,
      })),
    },
    ...options,
  })
}

/** Which credentials are configured. **Presence only** — no endpoint serves
 * a key value and none ever will. */
export function fetchApiKeys(options: RequestOptions = {}): Promise<ApiKeyPresence[]> {
  return request<ApiKeyPresence[]>('/settings/keys', options)
}

/** One log across risk, feed and routing changes, newest first. */
export function fetchAuditLog(
  params: PageParams = {},
  options: RequestOptions = {},
): Promise<Page<AuditLogEntry>> {
  return request<Page<AuditLogEntry>>('/settings/audit', {
    params: pageParams(params),
    ...options,
  })
}

/** What is live, and what is still a fixture. */
export function fetchDataSources(options: RequestOptions = {}): Promise<DataSourceStatus[]> {
  return request<DataSourceStatus[]>('/settings/sources', options)
}
