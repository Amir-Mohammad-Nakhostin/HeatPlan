export interface TimelinePoint {
  offset_minutes: number
  temperature_f: number
  heat_burden: number
}

export interface Plan {
  id: 'A' | 'B' | 'C'
  name: string
  strategy: string
  start_time: string
  duration_minutes: number
  distance_m: number
  exposure: number
  exposure_reduction_pct: number
  time_penalty_minutes: number
  avg_temperature_f: number
  geometry: [number, number][]
  timeline: TimelinePoint[]
  tradeoff: string
}

export interface ParsedIntent {
  task: string
  task_type: 'stationary' | 'route'
  duration_minutes: number
  start_time: string
  date: string
  location_name: string
  lat: number
  lon: number
  end_location_name?: string | null
  heat_priority: 'low' | 'medium' | 'high'
  parser: 'llm' | 'fallback'
  warnings: string[]
}

export interface PlanResponse {
  intent: ParsedIntent
  plans: Plan[]
  recommended_id: 'A' | 'B' | 'C'
  scores: Record<string, number>
  explanation: string
  data_source: 'fortyguard_live' | 'mock'
  location_name: string
  generated_at: string
}

export interface ThermalCheck {
  timestamp: string
  status: 'usable' | 'unavailable' | 'outside_horizon'
  detail: string
}

export interface RequestedIntervalError {
  required: string[]
  checks: ThermalCheck[]
  horizon_start?: string | null
  horizon_end?: string | null
}

export interface PlanErrorBody {
  code: string
  message: string
  requested_interval: RequestedIntervalError
  diagnostics: {
    alternative_candidates: number
    search_summary: { candidate_shifts: number[]; feasible_shifts: number[] }
  }
}

/** Structured user-level plan error: concise message + the REQUESTED
 *  interval's own per-hour status. Internal search-space diagnostics are
 *  never surfaced to the UI. */
export interface PlanError extends Error {
  code: string
  requested?: RequestedIntervalError
}

export interface AppConfig {
  default_city: string
  default_lat: number
  default_lon: number
  data_source: 'fortyguard_live' | 'mock'
  coverage: string
  forecast_horizon_hours: number
}

/** Pure UI helper, unit-tested: build summary chips for a plan card. */
export function planChips(p: Plan): string[] {
  const chips = [`${Math.round(p.duration_minutes)} min`]
  if (p.distance_m > 0) chips.push(`${(p.distance_m / 1609.34).toFixed(2)} mi`)
  chips.push(`avg ${Math.round(p.avg_temperature_f)}°F`)
  if (p.exposure_reduction_pct > 0) chips.push(`-${p.exposure_reduction_pct}% exposure`)
  if (p.time_penalty_minutes > 0) chips.push(`+${p.time_penalty_minutes} min`)
  return chips
}

// ---------------------------------------------------------------------------
// Structured-input architecture (primary workflow)
// ---------------------------------------------------------------------------

export interface LocationSuggestion {
  display_name: string
  latitude: number
  longitude: number
  timezone: string
}

export interface StructuredPlanRequest {
  date: string // YYYY-MM-DD
  duration_minutes: number
  available_start: string // HH:MM (24h)
  available_end: string // HH:MM (24h)
  location?: { name: string; latitude: number; longitude: number }
  location_text?: string
}

export interface StructuredCandidate {
  start_time: string
  end_time: string
  duration_minutes: number
  thermal_status: string
  avg_temperature_f: number | null
  exposure: number | null
  timeline: { offset_minutes: number; temperature_f: number }[]
  /** Deterministic, backend-derived explanation of this candidate's rank. */
  reason: string
}

export interface StructuredPlanResponse {
  best: StructuredCandidate
  alternatives: StructuredCandidate[]
  candidates: StructuredCandidate[]
  location_name: string
  latitude: number
  longitude: number
  timezone: string
  date: string
  duration_minutes: number
  available_start: string
  available_end: string
  data_source: 'fortyguard_live' | 'mock'
  generated_at: string
  disclaimer: string
}

/** Structured error codes surfaced by /api/plan/structured (spec §17). */
export type StructuredErrorCode =
  | 'INVALID_LOCATION'
  | 'LOCATION_OUTSIDE_SUPPORTED_AREA'
  | 'INVALID_DATE'
  | 'INVALID_DURATION'
  | 'INVALID_TIME_WINDOW'
  | 'FORECAST_HORIZON_EXCEEDED'
  | 'THERMAL_INTERVAL_UNSUPPORTED'
  | 'NO_FEASIBLE_ALTERNATIVE'
  | 'FORTYGUARD_ERROR'
  | 'INTERNAL'
  | 'UNKNOWN'

export interface StructuredErrorBody {
  code: string
  message: string
  requested_window?: { start: string; end: string }
  horizon_start?: string
  horizon_end?: string
  first_unavailable_hour?: string
  unavailable_hours?: string[]
}

export interface StructuredError extends Error {
  code: StructuredErrorCode
  body?: StructuredErrorBody
}

/** GET /api/forecast/window response: the rolling +12h horizon expressed in
 *  destination-local wall-clock time, plus an optional deterministic
 *  pre-flight check of a candidate window (no FortyGuard request involved). */
export interface ForecastWindow {
  timezone: string
  horizon_start: string
  horizon_end: string
  horizon_display: string
  location_name: string
  latitude: number | null
  longitude: number | null
  ok: boolean
  code: string | null
  message: string | null
  first_unavailable_hour: string | null
}

/** "2026-08-28" -> "Friday, August 28, 2026" for display. */
export function humanizeDate(iso: string): string {
  const d = new Date(`${iso}T12:00:00`)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString('en-US', {
    weekday: 'long',
    month: 'long',
    day: 'numeric',
    year: 'numeric',
  })
}

/** Format "13:00" (24h) as "1:00 PM" for display. */
export function fmtTime(hhmm: string): string {
  const [hStr, mStr] = hhmm.split(':')
  let h = parseInt(hStr, 10)
  const m = mStr ?? '00'
  if (Number.isNaN(h)) return hhmm
  const ampm = h >= 12 ? 'PM' : 'AM'
  h = h % 12
  if (h === 0) h = 12
  return `${h}:${m} ${ampm}`
}

/** Client-side mirror of the backend's deterministic form validation.
 *  Returns a user-facing message, or null when the input is valid. */
export function validateStructuredForm(f: {
  date: string
  duration_minutes: number
  available_start: string
  available_end: string
  hasLocation: boolean
}): string | null {
  if (!f.date || Number.isNaN(Date.parse(`${f.date}T00:00:00`))) {
    return 'Pick a valid date.'
  }
  if (!f.duration_minutes || f.duration_minutes <= 0) {
    return 'Duration must be greater than 0 minutes.'
  }
  const toMin = (t: string) => {
    const [h, m] = t.split(':').map((x) => parseInt(x, 10))
    return (h || 0) * 60 + (m || 0)
  }
  const start = toMin(f.available_start)
  const end = toMin(f.available_end)
  if (end <= start) return 'The end of your time window must be after its start.'
  if (f.duration_minutes > end - start) {
    return 'The duration is longer than the available time window.'
  }
  if (!f.hasLocation) return 'Choose where you are going.'
  return null
}
