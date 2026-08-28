import type { StructuredCandidate } from '../types'
import { fmtTime } from '../types'

/** Primary result card: the best time, prominently, with its estimated
 *  temperature, estimated thermal exposure, and the deterministic
 *  backend-derived reason it was selected (never invented). */
export default function BestCard({
  best,
  locationName,
}: {
  best: StructuredCandidate
  locationName: string
}) {
  return (
    <section className="best-box" aria-label="Best time">
      <div className="best-top">
        <h2 className="best-title">Best time</h2>
        <p className="best-time">
          {fmtTime(best.start_time)} <span className="best-time-sep">–</span>{' '}
          {fmtTime(best.end_time)}
        </p>
        <p className="best-place">{locationName}</p>
      </div>

      <dl className="best-stats">
        <div className="stat">
          <dt>Estimated temperature</dt>
          <dd>
            {best.avg_temperature_f != null ? (
              <>
                <span className="stat-value">{Math.round(best.avg_temperature_f)}</span>
                <span className="stat-unit">°F</span>
              </>
            ) : (
              <span className="stat-na">n/a</span>
            )}
          </dd>
        </div>
        <div className="stat">
          <dt>Estimated thermal exposure</dt>
          <dd>
            {best.exposure != null ? (
              <span className="stat-value">{best.exposure.toLocaleString()}</span>
            ) : (
              <span className="stat-na">n/a</span>
            )}
          </dd>
        </div>
      </dl>

      {best.reason ? <p className="best-reason">{best.reason}</p> : null}
    </section>
  )
}
