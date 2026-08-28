import { useEffect, useRef, useState } from 'react'
import { fetchConfig, fetchForecastWindow, requestStructuredPlan } from './api'
import AlternativesList from './components/AlternativesList'
import BestCard from './components/BestCard'
import LocationPicker from './components/LocationPicker'
import MapView from './components/MapView'
import PlanningStages from './components/PlanningStages'
import TemperatureChart from './components/TemperatureChart'
import type {
  AppConfig,
  ForecastWindow,
  LocationSuggestion,
  StructuredError,
  StructuredPlanResponse,
} from './types'
import { humanizeDate, validateStructuredForm } from './types'

const DURATIONS: { value: number; label: string }[] = [
  { value: 15, label: '15 minutes' },
  { value: 30, label: '30 minutes' },
  { value: 45, label: '45 minutes' },
  { value: 60, label: '1 hour' },
  { value: 90, label: '1 hour 30 minutes' },
  { value: 120, label: '2 hours' },
  { value: 180, label: '3 hours' },
  { value: 240, label: '4 hours' },
]

/** YYYY-MM-DD for "today"/"tomorrow" in the DESTINATION's timezone when one
 *  is known (the backend interprets the date as destination-local); falls
 *  back to the browser's local date before a location is picked. */
function dateInZone(timezone: string | undefined, offsetDays = 0): string {
  const d = new Date()
  d.setDate(d.getDate() + offsetDays)
  try {
    return d.toLocaleDateString('en-CA', timezone ? { timeZone: timezone } : undefined)
  } catch {
    const mm = String(d.getMonth() + 1).padStart(2, '0')
    const dd = String(d.getDate()).padStart(2, '0')
    return `${d.getFullYear()}-${mm}-${dd}`
  }
}

export default function App() {
  const [cfg, setCfg] = useState<AppConfig | null>(null)

  // Structured inputs (spec §5): WHEN / HOW LONG / WHAT TIME / WHERE.
  const [date, setDate] = useState(dateInZone(undefined, 0))
  const [duration, setDuration] = useState(60)
  const [fromTime, setFromTime] = useState('13:00')
  const [toTime, setToTime] = useState('17:00')
  const [locText, setLocText] = useState('')
  const [picked, setPicked] = useState<LocationSuggestion | null>(null)

  const [loading, setLoading] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [error, setError] = useState<StructuredError | null>(null)
  const [result, setResult] = useState<StructuredPlanResponse | null>(null)

  // Pre-flight forecast-horizon check (no FortyGuard request involved).
  const [forecast, setForecast] = useState<ForecastWindow | null>(null)
  const preflightTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    fetchConfig().then(setCfg).catch(() => setCfg(null))
  }, [])

  // Debounced, honest pre-flight validation: resolves the location and runs
  // the SAME deterministic window/horizon validation as the planner, so an
  // out-of-horizon window is flagged BEFORE any thermal request is made.
  useEffect(() => {
    if (preflightTimer.current) clearTimeout(preflightTimer.current)
    if (!picked) {
      setForecast(null)
      return
    }
    preflightTimer.current = setTimeout(() => {
      fetchForecastWindow({
        lat: picked.latitude,
        lon: picked.longitude,
        date,
        start: fromTime,
        end: toTime,
        duration_minutes: duration,
      })
        .then(setForecast)
        .catch(() => setForecast(null))
    }, 300)
    return () => {
      if (preflightTimer.current) clearTimeout(preflightTimer.current)
    }
  }, [picked, date, fromTime, toTime, duration])

  const horizonExceeded =
    forecast != null && !forecast.ok && forecast.code === 'FORECAST_HORIZON_EXCEEDED'

  function onPickLocation(s: LocationSuggestion) {
    setPicked(s)
    setLocText(s.display_name)
  }

  function onLocationText(text: string) {
    setLocText(text)
    setPicked(null)
  }

  async function submit(e?: React.FormEvent) {
    e?.preventDefault()
    if (loading) return
    setFormError(null)
    setError(null)
    setResult(null)

    // Client-side mirror of the backend's deterministic validation, so
    // obvious mistakes never consume a request (or FortyGuard credits).
    const invalid = validateStructuredForm({
      date,
      duration_minutes: duration,
      available_start: fromTime,
      available_end: toTime,
      hasLocation: Boolean(picked) || locText.trim().length > 0,
    })
    if (invalid) {
      setFormError(invalid)
      return
    }
    if (horizonExceeded) {
      setFormError(
        `That time range extends beyond the currently available forecast. Available forecast: ${forecast!.horizon_display}.`,
      )
      return
    }

    setLoading(true)
    try {
      setResult(
        await requestStructuredPlan({
          date,
          duration_minutes: duration,
          available_start: fromTime,
          available_end: toTime,
          ...(picked
            ? {
                location: {
                  name: picked.display_name,
                  latitude: picked.latitude,
                  longitude: picked.longitude,
                },
              }
            : { location_text: locText.trim() }),
        }),
      )
    } catch (err) {
      setError(
        err instanceof Error
          ? (err as StructuredError)
          : Object.assign(new Error('Something went wrong.'), { code: 'UNKNOWN' as const }),
      )
    } finally {
      setLoading(false)
    }
  }

  const source = result?.data_source ?? cfg?.data_source

  return (
    <div className="page">
      <header className="topbar">
        <div className="brand">
          <span className="logo" aria-hidden="true">
            🌡️
          </span>
          <div>
            <h1>HeatPlan</h1>
            <p className="tagline">Find a better time to be outside.</p>
          </div>
        </div>
        {source && (
          <span
            className={`badge ${source === 'mock' ? 'badge-mock' : 'badge-live'}`}
            title={
              source === 'mock'
                ? 'Demo data — no live FortyGuard key is configured'
                : 'Live environmental data from FortyGuard'
            }
          >
            {source === 'mock' ? 'DEMO DATA · mock' : 'LIVE · FortyGuard'}
          </span>
        )}
      </header>

      <main>
        <section className="input-panel" aria-label="Plan your time outside">
          <form onSubmit={submit} noValidate={false}>
            <div className="field-grid">
              <div className="field">
                <label htmlFor="hp-date">When?</label>
                <input
                  id="hp-date"
                  type="date"
                  value={date}
                  onChange={(e) => setDate(e.target.value)}
                  required
                />
                <div className="chip-row" role="group" aria-label="Date shortcuts">
                  <button
                    type="button"
                    className="chip"
                    onClick={() => setDate(dateInZone(picked?.timezone, 0))}
                  >
                    Today
                  </button>
                  <button
                    type="button"
                    className="chip"
                    onClick={() => setDate(dateInZone(picked?.timezone, 1))}
                  >
                    Tomorrow
                  </button>
                </div>
              </div>

              <div className="field">
                <label htmlFor="hp-duration">How long do you need to be outside?</label>
                <select
                  id="hp-duration"
                  value={duration}
                  onChange={(e) => setDuration(parseInt(e.target.value, 10))}
                >
                  {DURATIONS.map((d) => (
                    <option key={d.value} value={d.value}>
                      {d.label}
                    </option>
                  ))}
                </select>
              </div>

              <div className="field field-wide">
                <span className="field-label" id="hp-window-label">
                  What time can you do it?
                </span>
                <div className="time-row" role="group" aria-labelledby="hp-window-label">
                  <label className="time-part">
                    <span className="time-label">From</span>
                    <input
                      aria-label="Available from"
                      type="time"
                      value={fromTime}
                      onChange={(e) => setFromTime(e.target.value)}
                      required
                    />
                  </label>
                  <span className="time-sep" aria-hidden="true">
                    →
                  </span>
                  <label className="time-part">
                    <span className="time-label">To</span>
                    <input
                      aria-label="Available to"
                      type="time"
                      value={toTime}
                      onChange={(e) => setToTime(e.target.value)}
                      required
                    />
                  </label>
                </div>
                {forecast && forecast.ok && (
                  <p className="hint hint-ok">
                    <span aria-hidden="true">✓ </span>
                    Live forecast currently covers {forecast.horizon_display}
                    {forecast.timezone
                      ? ` (${forecast.timezone.replace(/_/g, ' ')} time)`
                      : ''}
                    .
                  </p>
                )}
                {horizonExceeded && (
                  <p className="hint hint-warn" role="alert">
                    That time range extends beyond the currently available forecast.
                    Available forecast: {forecast!.horizon_display}.
                  </p>
                )}
              </div>

              <div className="field field-wide">
                <label htmlFor="hp-location">Where are you going?</label>
                <LocationPicker
                  id="hp-location"
                  picked={picked}
                  onPick={onPickLocation}
                  onTextChange={onLocationText}
                />
                {!picked && (
                  <p className="hint">
                    A U.S. park, city, or coordinates like 38.6126, -121.5188.
                  </p>
                )}
              </div>
            </div>

            {formError && (
              <p className="form-error" role="alert">
                <span aria-hidden="true">⚠️ </span>
                {formError}
              </p>
            )}

            <div className="input-row">
              <span className="hint">
                {cfg
                  ? `${cfg.coverage} · forecast up to +${cfg.forecast_horizon_hours}h`
                  : '…'}
              </span>
              <button type="submit" className="btn-primary" disabled={loading}>
                {loading ? 'Finding your best time…' : 'Find my best time'}
              </button>
            </div>
          </form>
        </section>

        {loading && <PlanningStages done={false} />}

        {error && (
          <section className="error-box" role="alert">
            <strong>
              <span aria-hidden="true">⚠️ </span>
              {error.message}
            </strong>
            <div className="error-meta">
              {error.code === 'FORECAST_HORIZON_EXCEEDED' && (
                <>
                  {error.body?.first_unavailable_hour && (
                    <p>
                      Outside current forecast horizon: {error.body.first_unavailable_hour}
                    </p>
                  )}
                  {error.body?.horizon_end && (
                    <p>Forecast currently available until: {error.body.horizon_end}</p>
                  )}
                </>
              )}
              {error.code === 'NO_FEASIBLE_ALTERNATIVE' &&
                (error.body?.unavailable_hours?.length ?? 0) > 0 && (
                  <p>
                    Unavailable live thermal data: {error.body!.unavailable_hours!.join(', ')}
                  </p>
                )}
              {error.code === 'FORTYGUARD_ERROR' && (
                <p>The live environmental data service reported a problem. Try again shortly.</p>
              )}
            </div>
          </section>
        )}

        {result && (
          <div
          className="results"
          aria-label="Estimated temperature and estimated thermal exposure results"
        >
            <BestCard best={result.best} locationName={result.location_name} />

            <section aria-label="Other available times">
              <AlternativesList
                alternatives={result.alternatives}
                bestExposure={result.best.exposure}
              />
            </section>

            <section className="viz-grid">
              <div className="panel">
                <h3>Map</h3>
                <MapView
                  center={[result.latitude, result.longitude]}
                  label={result.location_name}
                />
              </div>
              <div className="panel">
                <h3>Temperature over time</h3>
                <TemperatureChart
                  candidates={result.candidates}
                  bestStart={result.best.start_time}
                />
              </div>
            </section>

            <p className="result-meta">
              {result.date ? humanizeDate(result.date) : ''} · {result.location_name} ·{' '}
              {result.timezone.replace(/_/g, ' ')} time · window{' '}
              {result.available_start}–{result.available_end} ·{' '}
              {result.data_source === 'mock'
                ? 'demo data (mock — no live key configured)'
                : 'live FortyGuard environmental data'}
            </p>
          </div>
        )}
      </main>

      <footer>
        Estimated thermal exposure is decision support computed from FortyGuard environmental
        data — not medical advice, not a safety guarantee.
      </footer>
    </div>
  )
}
