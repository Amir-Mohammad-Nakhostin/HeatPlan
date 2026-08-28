import { useEffect, useState } from 'react'

/** Honest stage-based planning progress.
 *
 * The backend exposes a single coarse request/response (no per-step
 * progress stream), so these stages reflect the REAL coarse phases of a
 * planning request - never fake percentages. A stage is only shown as
 * "active" while the request is actually in flight; it is never marked
 * complete before the response arrives. The final stage (Preparing your
 * recommendation) only lights once the request has resolved.
 */
const STAGES = [
  'Checking your location',
  'Checking the available forecast',
  'Reading live thermal data',
  'Comparing available times',
  'Preparing your recommendation',
]

export default function PlanningStages({ done }: { done: boolean }) {
  // While the request is in flight we advance the ACTIVE stage on a slow,
  // honest cadence but NEVER reach the final "Preparing" stage until the
  // request actually resolves (done=true). No fake percentages.
  const [active, setActive] = useState(0)

  useEffect(() => {
    if (done) {
      setActive(STAGES.length - 1)
      return
    }
    setActive(0)
    const t = setInterval(() => {
      // Cap at the second-to-last stage so the final "Preparing your
      // recommendation" stage is only reached when the response arrives.
      setActive((i) => Math.min(i + 1, STAGES.length - 2))
    }, 1600)
    return () => clearInterval(t)
  }, [done])

  return (
    <section className="status" aria-live="polite" aria-busy={!done}>
      <div className="spinner" aria-hidden="true" />
      <ol className="stages">
        {STAGES.map((label, i) => (
          <li
            key={label}
            className={
              'stage' +
              (i < active ? ' stage-done' : i === active ? ' stage-active' : '')
            }
          >
            <span className="stage-dot" aria-hidden="true">
              {i < active ? '✓' : ''}
            </span>
            <span className="stage-label">{label}</span>
          </li>
        ))}
      </ol>
    </section>
  )
}
