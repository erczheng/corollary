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
import type {
  AccountMode,
  AccountResponse,
  ActivityItem,
  ActivityStats,
  ActivityStatus,
  ApiErrorBody,
  ApiKeyPresence,
  AuditLogEntry,
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

async function errorFor(response: Response, url: string): Promise<ApiError> {
  let code = HTTP_ERROR
  let message = `The engine answered ${response.status} for ${url}.`
  try {
    const body: unknown = await response.json()
    const stated = (body as { error?: Partial<ApiErrorBody> } | null)?.error
    if (stated && typeof stated.code === 'string' && typeof stated.message === 'string') {
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

/** Quoted underlyings with their daily series. `historyDays` is capped at
 * 400 server-side and the cap is the point: with a quarter of data, 3M, YTD,
 * 1Y and All all draw the same chart. */
export function fetchUnderlyings(
  symbols?: readonly string[],
  historyDays?: number,
  options: RequestOptions = {},
): Promise<UnderlyingQuote[]> {
  return request<UnderlyingQuote[]>('/markets/underlyings', {
    // Snake case, and deliberately so: FastAPI names a query parameter by
    // its Python spelling unless it carries an alias, and only `pageSize`
    // and `type` do. Guessing camelCase here sends an unknown parameter and
    // silently gets the default back.
    params: { symbols: symbolList(symbols), history_days: historyDays },
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
