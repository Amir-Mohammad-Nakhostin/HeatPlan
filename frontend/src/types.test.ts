import { describe, expect, it } from 'vitest'

import { planChips, type Plan } from './types'

function plan(partial: Partial<Plan> = {}): Plan {
  return {
    id: 'A',
    name: 'Fastest',
    strategy: '',
    start_time: '14:00',
    duration_minutes: 60,
    distance_m: 0,
    exposure: 100,
    exposure_reduction_pct: 0,
    time_penalty_minutes: 0,
    avg_temperature_f: 95,
    geometry: [],
    timeline: [],
    tradeoff: '',
    ...partial,
  }
}

describe('planChips', () => {
  it('always shows the duration', () => {
    expect(planChips(plan())).toContain('60 min')
  })

  it('adds distance in miles when present', () => {
    expect(planChips(plan({ distance_m: 3218.68 }))).toContain('2.00 mi')
  })

  it('adds exposure reduction and time penalty', () => {
    const chips = planChips(plan({ exposure_reduction_pct: 25, time_penalty_minutes: 90 }))
    expect(chips).toContain('-25% exposure')
    expect(chips).toContain('+90 min')
  })
})
