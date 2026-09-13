/**
 * TanStack Query hooks over `api.ts` — one hook per endpoint, named for the
 * question it answers.
 *
 * **Every account-scoped key carries the account, and the hook reads it from
 * the store by default.** `['positions', 'paper']` and `['positions',
 * 'cash']` are two cache entries, so switching books refetches instead of
 * rendering the other one for a frame — the same invariant the store keeps
 * by holding `Record<AccountMode, …>` rather than a flattened book.
 * Defaulting from the store rather than taking the account as a required
 * argument is deliberate: a call site cannot forget a parameter it does not
 * pass, and the failure it would cause is misreporting real money.
 *
 * **Nothing here persists.** `theme.ts` is the only state that survives a
 * reload, and that boundary is rule 5: every cold start comes up in Paper,
 * so a cache that restored the selected account would restore Cash without a
 * confirmation.
 *
 * **Live quotes are not here.** A WebSocket push is not a query, and writing
 * `setQueryData` per tick fights the cache's staleness model; `store.tick()`
 * and `store.pollMarkets()` keep the `underlyings` map in Zustand. These
 * hooks cover the things that are *fetched*: balances, the ledger,
 * positions, chains, settings.
 */
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  DEFAULT_HISTORY_PERIOD,
  DEFAULT_HISTORY_TIMEFRAME,
  fetchAccount,
  fetchAccountHistory,
  fetchActivity,
  fetchActivityStats,
  fetchApiKeys,
  fetchAuditLog,
  fetchChain,
  fetchDataFeeds,
  fetchDataSources,
  fetchEngineState,
  fetchNotificationRoutes,
  fetchPositions,
  fetchRiskLimits,
  fetchStocks,
  fetchTransfers,
  fetchUnderlyings,
  fetchWorkingOrders,
  haltEngine,
  resumeEngine,
  updateDataFeeds,
  updateNotificationRoutes,
  updateRiskLimits,
} from './api'
import type {
  ActivityQuery,
  ChainQuery,
  DataFeedUpdate,
  HistoryWindow,
  NotificationRouteUpdate,
  PageParams,
  RiskLimitUpdate,
  SeriesWindow,
} from './api'
import { useUIStore } from './store'
import type { AccountMode } from './types'

/* -------------------------------------------------------------------------
 * Keys
 *
 * The account sits at index 1 on every scoped key, so a prefix invalidation
 * reaches one book and leaves the other alone. Optional parameters are
 * normalised to `null` rather than omitted: a key of a different *length*
 * for the same question is a second cache entry that never gets invalidated
 * with the first.
 * ---------------------------------------------------------------------- */

export const queryKeys = {
  account: (account: AccountMode) => ['account', account] as const,
  accountHistory: (account: AccountMode, window: HistoryWindow = {}) =>
    [
      'account',
      account,
      'history',
      window.period ?? DEFAULT_HISTORY_PERIOD,
      window.timeframe ?? DEFAULT_HISTORY_TIMEFRAME,
    ] as const,
  transfers: (account: AccountMode, params: PageParams = {}) =>
    ['account', account, 'transfers', params.page ?? null, params.pageSize ?? null] as const,

  activity: (account: AccountMode, query: ActivityQuery = {}) =>
    [
      'activity',
      account,
      query.page ?? null,
      query.pageSize ?? null,
      query.search ?? null,
      query.status ?? null,
    ] as const,
  activityStats: (account: AccountMode) => ['activity', account, 'stats'] as const,

  positions: (account: AccountMode) => ['positions', account] as const,
  workingOrders: (account: AccountMode) => ['positions', account, 'working'] as const,

  stocks: (symbols?: readonly string[]) => ['markets', 'stocks', symbols ?? null] as const,
  /** The window is **part of the key**. A range control drives the request
   * now, so 1D-at-5Min and 1Y-at-1D are two different answers to two
   * different questions; keyed on the symbols alone every range would
   * collide on one cache entry and the second one clicked would draw the
   * first one's data. */
  underlyings: (symbols?: readonly string[], window?: SeriesWindow) =>
    [
      'markets',
      'underlyings',
      symbols ?? null,
      window?.period ?? null,
      window?.timeframe ?? null,
    ] as const,
  chain: (underlying: string, query: ChainQuery = {}) =>
    [
      'markets',
      'chain',
      underlying,
      query.type ?? null,
      query.expiration ?? null,
      query.maxDte ?? null,
      query.moneynessPct ?? null,
    ] as const,

  engineState: () => ['engine', 'state'] as const,

  riskLimits: () => ['settings', 'limits'] as const,
  dataFeeds: () => ['settings', 'feeds'] as const,
  notificationRoutes: () => ['settings', 'routes'] as const,
  apiKeys: () => ['settings', 'keys'] as const,
  auditLog: (params: PageParams = {}) =>
    ['settings', 'audit', params.page ?? null, params.pageSize ?? null] as const,
  dataSources: () => ['settings', 'sources'] as const,
}

/** Which book a hook is asking about: the caller's, or the selected one.
 *
 * Exported because a page occasionally needs the same answer for a label. */
export function useAccountScope(override?: AccountMode): AccountMode {
  const selected = useUIStore((s) => s.accountMode)
  return override ?? selected
}

/** Market data refetches faster than balances do — the snapshot poll runs at
 * 2s against its own rate-limit bucket, while the account trio runs at 15s
 * against the trading host's. */
const MARKET_STALE_TIME = 2_000

/* -------------------------------------------------------------------------
 * Account
 * ---------------------------------------------------------------------- */

/** Balances, margin class and options entitlement for the selected book.
 *
 * The **paper** read is the one that answers whether Cash is configured:
 * `cashAccountAvailable` and `missingLiveCredentialEnvVars` come back on
 * every account response, so the toggle can disable with a stated reason
 * without first trying the book it cannot read. */
export function useAccount(account?: AccountMode) {
  const mode = useAccountScope(account)
  return useQuery({
    queryKey: queryKeys.account(mode),
    queryFn: ({ signal }) => fetchAccount(mode, { signal }),
  })
}

/** The broker's own equity curve, with t₀ marked. Nothing is reconstructed
 * and nothing is stored; `pointsBeforeT0` is how much of the curve predates
 * Corollary, so the chart cannot claim credit for manual trading. */
export function useAccountHistory(window: HistoryWindow = {}, account?: AccountMode) {
  const mode = useAccountScope(account)
  return useQuery({
    queryKey: queryKeys.accountHistory(mode, window),
    queryFn: ({ signal }) => fetchAccountHistory(mode, window, { signal }),
    // The window is a control the reader operates, so switching it must not
    // blank the chart: the previous range stays drawn until the new one
    // lands, with `isPlaceholderData` saying that is what is on screen. An
    // empty plot between two ranges reads as "there is no data", which is a
    // different and much worse claim than "still reading".
    placeholderData: keepPreviousData,
  })
}

/** Deposits and withdrawals, newest first. Not orders. */
export function useTransfers(params: PageParams = {}, account?: AccountMode) {
  const mode = useAccountScope(account)
  return useQuery({
    queryKey: queryKeys.transfers(mode, params),
    queryFn: ({ signal }) => fetchTransfers(mode, params, { signal }),
  })
}

/* -------------------------------------------------------------------------
 * Activity
 * ---------------------------------------------------------------------- */

/** One page of the ledger. `total` on the envelope is every matching row,
 * not the page size — the pager reads it, and no header card does. */
export function useActivity(query: ActivityQuery = {}, account?: AccountMode) {
  const mode = useAccountScope(account)
  return useQuery({
    queryKey: queryKeys.activity(mode, query),
    queryFn: ({ signal }) => fetchActivity(mode, query, { signal }),
  })
}

/** The header cards: lifetime realized P&L, average win, average loss, and
 * what could not be booked. Folded server-side over every trade, which is
 * why they are a separate request from the page of rows beneath them. */
export function useActivityStats(account?: AccountMode) {
  const mode = useAccountScope(account)
  return useQuery({
    queryKey: queryKeys.activityStats(mode),
    queryFn: ({ signal }) => fetchActivityStats(mode, { signal }),
  })
}

/* -------------------------------------------------------------------------
 * Positions
 * ---------------------------------------------------------------------- */

export function usePositions(account?: AccountMode) {
  const mode = useAccountScope(account)
  return useQuery({
    queryKey: queryKeys.positions(mode),
    queryFn: ({ signal }) => fetchPositions(mode, { signal }),
  })
}

export function useWorkingOrders(account?: AccountMode) {
  const mode = useAccountScope(account)
  return useQuery({
    queryKey: queryKeys.workingOrders(mode),
    queryFn: ({ signal }) => fetchWorkingOrders(mode, { signal }),
  })
}

/* -------------------------------------------------------------------------
 * Markets — one price per symbol, and no account in the key
 * ---------------------------------------------------------------------- */

export function useStocks(symbols?: readonly string[]) {
  return useQuery({
    queryKey: queryKeys.stocks(symbols),
    queryFn: ({ signal }) => fetchStocks(symbols, { signal }),
    staleTime: MARKET_STALE_TIME,
  })
}

/** Quoted underlyings and their series.
 *
 * `window` is what a range control resolved to (`windowForRange`), and it is
 * in the key — see `queryKeys.underlyings`. Whether the answer is daily or
 * intraday is read off the response (`quoteSeries`), never inferred from
 * what was asked for. */
export function useUnderlyings(symbols?: readonly string[], window?: SeriesWindow) {
  return useQuery({
    queryKey: queryKeys.underlyings(symbols, window),
    queryFn: ({ signal }) => fetchUnderlyings(symbols, window, { signal }),
    staleTime: MARKET_STALE_TIME,
    // Same reason as the equity curve: a range click is a new key, and
    // without this the chart empties for as long as the request takes.
    placeholderData: keepPreviousData,
  })
}

/** One underlying's chain, fetched only once a row is expanded: `null` means
 * nothing is open, and the query stays disabled rather than fetching a chain
 * for a symbol nobody asked about. Three vendor operations hang off this
 * call, so an eager one is not free. */
export function useChain(underlying: string | null, query: ChainQuery = {}) {
  return useQuery({
    queryKey: queryKeys.chain(underlying ?? '', query),
    queryFn: ({ signal }) => fetchChain(underlying as string, query, { signal }),
    enabled: underlying !== null && underlying !== '',
  })
}

/* -------------------------------------------------------------------------
 * Engine
 * ---------------------------------------------------------------------- */

/** Halt state and the t₀ marker. Not account-scoped: one engine, one halt,
 * and a halt found while Cash is selected is the same halt. */
export function useEngineState() {
  return useQuery({
    queryKey: queryKeys.engineState(),
    queryFn: ({ signal }) => fetchEngineState({ signal }),
  })
}

/** Halt the engine, with a reason. Stops new entries; closes nothing. */
export function useHaltEngine() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (reason: string) => haltEngine(reason),
    onSuccess: (state) => {
      client.setQueryData(queryKeys.engineState(), state)
    },
  })
}

/** End a halt.
 *
 * **Call this from a human action and nowhere else.** Rule 9: the engine
 * never auto-resumes, so this must not be reachable from an effect that
 * notices a reconnect, from a retry, or from anything that runs on its own.
 * Reconnecting into an unverified position state is how a bot doubles a
 * position it already holds. */
export function useResumeEngine() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: () => resumeEngine(),
    onSuccess: (state) => {
      client.setQueryData(queryKeys.engineState(), state)
    },
  })
}

/* -------------------------------------------------------------------------
 * Settings
 *
 * Config is the one thing this phase writes, so these are the only
 * mutations. Each edit writes an audit row server-side, which is why every
 * one of them invalidates the log as well as its own list.
 * ---------------------------------------------------------------------- */

export function useRiskLimits() {
  return useQuery({
    queryKey: queryKeys.riskLimits(),
    queryFn: ({ signal }) => fetchRiskLimits({ signal }),
  })
}

export function useUpdateRiskLimits() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (updates: readonly RiskLimitUpdate[]) => updateRiskLimits(updates),
    onSuccess: (limits) => {
      client.setQueryData(queryKeys.riskLimits(), limits)
      void client.invalidateQueries({ queryKey: ['settings', 'audit'] })
    },
  })
}

export function useDataFeeds() {
  return useQuery({
    queryKey: queryKeys.dataFeeds(),
    queryFn: ({ signal }) => fetchDataFeeds({ signal }),
  })
}

export function useUpdateDataFeeds() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (updates: readonly DataFeedUpdate[]) => updateDataFeeds(updates),
    onSuccess: (feeds) => {
      client.setQueryData(queryKeys.dataFeeds(), feeds)
      void client.invalidateQueries({ queryKey: ['settings', 'audit'] })
    },
  })
}

export function useNotificationRoutes() {
  return useQuery({
    queryKey: queryKeys.notificationRoutes(),
    queryFn: ({ signal }) => fetchNotificationRoutes({ signal }),
  })
}

export function useUpdateNotificationRoutes() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (updates: readonly NotificationRouteUpdate[]) =>
      updateNotificationRoutes(updates),
    onSuccess: (routes) => {
      client.setQueryData(queryKeys.notificationRoutes(), routes)
      void client.invalidateQueries({ queryKey: ['settings', 'audit'] })
    },
  })
}

/** Which credentials are configured. Presence only — never a value. */
export function useApiKeys() {
  return useQuery({
    queryKey: queryKeys.apiKeys(),
    queryFn: ({ signal }) => fetchApiKeys({ signal }),
  })
}

export function useAuditLog(params: PageParams = {}) {
  return useQuery({
    queryKey: queryKeys.auditLog(params),
    queryFn: ({ signal }) => fetchAuditLog(params, { signal }),
  })
}

/** What is live and what is still a fixture. */
export function useDataSources() {
  return useQuery({
    queryKey: queryKeys.dataSources(),
    queryFn: ({ signal }) => fetchDataSources({ signal }),
  })
}
