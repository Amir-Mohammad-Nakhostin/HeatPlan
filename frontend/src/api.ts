import type {
  AppConfig,
  ForecastWindow,
  LocationSuggestion,
  PlanError,
  PlanErrorBody,
  PlanResponse,
  StructuredError,
  StructuredErrorBody,
  StructuredPlanRequest,
  StructuredPlanResponse,
} from './types'

export async function fetchConfig(): Promise<AppConfig> {
  const r = await fetch('/api/config')
  if (!r.ok) throw new Error('config failed')
  return r.json()
}

/** Parse the structured user-level error body the API returns for an
 *  unsupported thermal interval (422) into a PlanError. The frontend renders
 *  ONLY this: the concise message and the requested interval's own status -
 *  never internal alternative-candidate diagnostics. */
export function parsePlanError(body: Partial<PlanErrorBody> | null): PlanError | null {
  if (!body || typeof body.message !== 'string') return null
  const err = new Error(body.message) as PlanError
  err.code = body.code || 'PLAN_ERROR'
  if (body.requested_interval) err.requested = body.requested_interval
  return err
}

export async function requestPlan(text: string): Promise<PlanResponse> {
  const r = await fetch('/api/plan', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  })
  const body = await r.json().catch(() => ({}))
  if (!r.ok) {
    // Structured 422 error body: {"code","message","requested_interval",...}.
    const structured = parsePlanError(body?.detail)
    if (structured) throw structured
    throw new Error((body as { detail?: string })?.detail || `Request failed (HTTP ${r.status})`)
  }
  return body as PlanResponse
}

// ---------------------------------------------------------------------------
// Structured-input architecture (primary workflow)
// ---------------------------------------------------------------------------

/** Parse a structured-endpoint error body into a StructuredError. The UI
 *  renders the concise human-readable message only - never traces, keys,
 *  signed URLs, or raw FortyGuard payloads. */
export function parseStructuredError(
  body: Partial<StructuredErrorBody> | null,
  httpStatus?: number,
): StructuredError {
  const err = new Error(
    (body && typeof body.message === 'string' && body.message) ||
      `Request failed${httpStatus ? ` (HTTP ${httpStatus})` : ''}`,
  ) as StructuredError
  err.code = ((body?.code as StructuredError['code']) || 'UNKNOWN')
  if (body) err.body = body as StructuredErrorBody
  return err
}

export async function requestStructuredPlan(
  req: StructuredPlanRequest,
): Promise<StructuredPlanResponse> {
  const r = await fetch('/api/plan/structured', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  })
  const body = await r.json().catch(() => ({}))
  if (!r.ok) {
    // FastAPI wraps our structured detail under "detail"; pydantic 422s
    // arrive as {detail: [{loc, msg, ...}]} and are flattened to a message.
    const detail = body?.detail
    if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
      throw parseStructuredError(detail as StructuredErrorBody, r.status)
    }
    if (Array.isArray(detail)) {
      const first = detail[0] as { msg?: string } | undefined
      throw parseStructuredError(
        { code: 'INVALID_TIME_WINDOW', message: first?.msg || 'Invalid request.' },
        r.status,
      )
    }
    throw parseStructuredError(
      typeof detail === 'string' ? { code: 'UNKNOWN', message: detail } : null,
      r.status,
    )
  }
  return body as StructuredPlanResponse
}

export async function searchLocations(q: string): Promise<LocationSuggestion[]> {
  if (!q.trim()) return []
  const r = await fetch(`/api/locations/search?q=${encodeURIComponent(q)}`)
  if (!r.ok) return []
  return r.json()
}

/** Pre-flight forecast-horizon check. Resolves the location and runs the
 *  same deterministic window/horizon validation as the planning endpoint
 *  WITHOUT submitting anything to FortyGuard, so the UI can flag an
 *  out-of-horizon window (and show the available forecast span) early. */
export async function fetchForecastWindow(params: {
  q?: string
  lat?: number
  lon?: number
  date?: string
  start?: string
  end?: string
  duration_minutes?: number
}): Promise<ForecastWindow> {
  const qs = new URLSearchParams()
  if (params.q) qs.set('q', params.q)
  if (params.lat != null) qs.set('lat', String(params.lat))
  if (params.lon != null) qs.set('lon', String(params.lon))
  if (params.date) qs.set('date', params.date)
  if (params.start) qs.set('start', params.start)
  if (params.end) qs.set('end', params.end)
  if (params.duration_minutes) qs.set('duration_minutes', String(params.duration_minutes))
  const r = await fetch(`/api/forecast/window?${qs.toString()}`)
  const body = await r.json().catch(() => ({}))
  if (!r.ok) {
    const detail = body?.detail
    if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
      throw parseStructuredError(detail as StructuredErrorBody, r.status)
    }
    throw parseStructuredError(
      typeof detail === 'string' ? { code: 'UNKNOWN', message: detail } : null,
      r.status,
    )
  }
  return body as ForecastWindow
}
