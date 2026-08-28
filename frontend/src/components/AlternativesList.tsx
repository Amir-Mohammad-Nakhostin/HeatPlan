import type { StructuredCandidate } from '../types'
import { fmtTime } from '../types'

/** The other available times in the user's window. Each row shows the time
 *  slot, its estimated temperature, and a compact exposure-vs-best bar so the
 *  trade-off is understandable at a glance. Reason text comes from the
 *  deterministic backend explanation (never invented client-side). */
export default function AlternativesList({
  alternatives,
  bestExposure,
}: {
  alternatives: StructuredCandidate[]
  bestExposure: number | null
}) {
  if (alternatives.length === 0) return null

  const maxExposure = Math.max(
    bestExposure ?? 0,
    ...alternatives.map((a) => a.exposure ?? 0),
    1e-9,
  )

  return (
    <section className="alt-box" aria-label="Other available times">
      <h3 className="alt-heading">Other available times</h3>
      <ul className="alt-list">
        {alternatives.map((a) => {
          const pct =
            a.exposure != null ? Math.max(2, (a.exposure / maxExposure) * 100) : 0
          return (
            <li key={a.start_time} className="alt-row">
              <div className="alt-main">
                <span className="alt-time">
                  {fmtTime(a.start_time)} – {fmtTime(a.end_time)}
                </span>
                {a.reason ? <span className="alt-reason">{a.reason}</span> : null}
              </div>
              <div className="alt-metrics">
                <span className="alt-temp">
                  {a.avg_temperature_f != null
                    ? `${Math.round(a.avg_temperature_f)}°F`
                    : 'n/a'}
                </span>
                <span className="alt-expo">
                  {a.exposure != null ? `est. exposure ${a.exposure.toLocaleString()}` : ''}
                </span>
                <span
                  className="alt-bar"
                  role="img"
                  aria-label={
                    a.exposure != null
                      ? `Estimated thermal exposure ${a.exposure.toLocaleString()}`
                      : 'No exposure data'
                  }
                >
                  <span className="alt-bar-fill" style={{ width: `${pct}%` }} />
                </span>
              </div>
            </li>
          )
        })}
      </ul>
    </section>
  )
}
