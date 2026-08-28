import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { parsePlanError } from './api'
import type { PlanErrorBody } from './types'

/**
 * Frontend error semantics (Phase 7): the UI must render a concise,
 * structured user-level error (message + the REQUESTED interval's own
 * status), never raw internal optimizer diagnostics. OUTSIDE_HORIZON stays
 * distinct from UNAVAILABLE and the medical disclaimer remains intact.
 */
function body(partial: Partial<PlanErrorBody> = {}): PlanErrorBody {
  return {
    code: 'THERMAL_INTERVAL_UNSUPPORTED',
    message: 'Requested thermal interval: 14:00, 15:00. Unavailable live thermal data: 15:00.',
    requested_interval: {
      required: ['14:00', '15:00'],
      checks: [
        { timestamp: '14:00', status: 'usable', detail: '99 tile samples' },
        { timestamp: '15:00', status: 'unavailable', detail: 'n_cells == 0' },
      ],
      horizon_start: '2026-08-26T10:00:00-07:00',
      horizon_end: '2026-08-26T22:00:00-07:00',
    },
    diagnostics: {
      alternative_candidates: 0,
      search_summary: { candidate_shifts: [], feasible_shifts: [] },
    },
    ...partial,
  }
}

describe('structured plan errors', () => {
  it('parses the structured API error into message + requested interval', () => {
    const err = parsePlanError(body())
    expect(err).not.toBeNull()
    expect(err!.message).toContain('Requested thermal interval: 14:00, 15:00')
    expect(err!.code).toBe('THERMAL_INTERVAL_UNSUPPORTED')
    expect(err!.requested!.required).toEqual(['14:00', '15:00'])
    expect(err!.requested!.checks[1].status).toBe('unavailable')
  })

  it('distinguishes outside_horizon from unavailable', () => {
    const err = parsePlanError(body({
      message: 'Requested thermal interval: 22:00, 23:00. Outside current forecast horizon: 23:00.',
      requested_interval: {
        required: ['22:00', '23:00'],
        checks: [
          { timestamp: '22:00', status: 'usable', detail: '' },
          { timestamp: '23:00', status: 'outside_horizon', detail: '' },
        ],
      },
    }))
    expect(err!.requested!.checks[1].status).toBe('outside_horizon')
    expect(err!.requested!.checks[1].status).not.toBe('unavailable')
  })

  it('falls back cleanly for non-structured errors', () => {
    expect(parsePlanError(null)).toBeNull()
    expect(parsePlanError({ detail: 'boom' } as unknown as PlanErrorBody)).toBeNull()
  })

  it('App renders the user-level message, never raw diagnostics', () => {
    const appSrc = readFileSync(resolve(__dirname, 'App.tsx'), 'utf8')
    expect(appSrc).toContain('error.message')
    expect(appSrc).not.toMatch(/diagnostics/)
    expect(appSrc).not.toMatch(/search_summary/)
    expect(appSrc).toContain('Unavailable live thermal data')
    expect(appSrc).toContain('Outside current forecast horizon')
    expect(appSrc).toMatch(/not medical advice/i)
  })
})
