import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { parseStructuredError, requestStructuredPlan } from './api'
import { fmtTime, validateStructuredForm } from './types'

/** Structured-input frontend regression tests (spec §21-H). */

const valid = {
  date: '2026-08-28',
  duration_minutes: 60,
  available_start: '13:00',
  available_end: '17:00',
  hasLocation: true,
}

describe('structured form validation', () => {
  it('accepts a valid form', () => {
    expect(validateStructuredForm(valid)).toBeNull()
  })

  it('rejects zero / negative duration', () => {
    expect(validateStructuredForm({ ...valid, duration_minutes: 0 })).toMatch(
      /greater than 0/i,
    )
    expect(validateStructuredForm({ ...valid, duration_minutes: -30 })).toMatch(
      /greater than 0/i,
    )
  })

  it('rejects a duration longer than the window', () => {
    expect(
      validateStructuredForm({ ...valid, duration_minutes: 300 }),
    ).toMatch(/longer than the available time window/i)
  })

  it('rejects end before start and equal start/end', () => {
    expect(
      validateStructuredForm({ ...valid, available_start: '17:00', available_end: '13:00' }),
    ).toMatch(/after its start/i)
    expect(
      validateStructuredForm({ ...valid, available_start: '13:00', available_end: '13:00' }),
    ).toMatch(/after its start/i)
  })

  it('rejects an invalid date and a missing location', () => {
    expect(validateStructuredForm({ ...valid, date: 'not-a-date' })).toMatch(/date/i)
    expect(validateStructuredForm({ ...valid, date: '' })).toMatch(/date/i)
    expect(validateStructuredForm({ ...valid, hasLocation: false })).toMatch(/where/i)
  })
})

describe('structured error rendering', () => {
  it('parses FORECAST_HORIZON_EXCEEDED with horizon details', () => {
    const err = parseStructuredError({
      code: 'FORECAST_HORIZON_EXCEEDED',
      message: 'The requested time window extends beyond the current +12h forecast horizon.',
      first_unavailable_hour: '2026-08-27 23:00',
      horizon_end: '2026-08-27T22:00:00-07:00',
    }, 422)
    expect(err.code).toBe('FORECAST_HORIZON_EXCEEDED')
    expect(err.message).toContain('forecast horizon')
    expect(err.body!.first_unavailable_hour).toBe('2026-08-27 23:00')
  })

  it('parses NO_FEASIBLE_ALTERNATIVE with unavailable hours', () => {
    const err = parseStructuredError({
      code: 'NO_FEASIBLE_ALTERNATIVE',
      message: 'No time inside your window has usable live thermal data.',
      unavailable_hours: ['14:00', '15:00'],
    }, 422)
    expect(err.code).toBe('NO_FEASIBLE_ALTERNATIVE')
    expect(err.body!.unavailable_hours).toEqual(['14:00', '15:00'])
  })

  it('falls back cleanly for non-structured errors', () => {
    const err = parseStructuredError(null, 500)
    expect(err.code).toBe('UNKNOWN')
    expect(err.message).toContain('500')
  })
})

describe('structured request submission', () => {
  it('posts the structured contract to /api/plan/structured', async () => {
    const payload = {
      date: '2026-08-28',
      duration_minutes: 60,
      available_start: '13:00',
      available_end: '17:00',
      location: { name: 'Discovery Park', latitude: 38.6126, longitude: -121.5188 },
    }
    const mockResponse = {
      best: {
        start_time: '14:00', end_time: '15:00', duration_minutes: 60,
        thermal_status: 'ok', avg_temperature_f: 99.1, exposure: 1234.5, timeline: [],
      },
      alternatives: [], candidates: [], location_name: 'Discovery Park',
      latitude: 38.6126, longitude: -121.5188, timezone: 'America/Los_Angeles',
      date: '2026-08-28', duration_minutes: 60, available_start: '13:00',
      available_end: '17:00', data_source: 'mock', generated_at: '2026-08-27T00:00:00Z',
      disclaimer: 'not medical advice',
    }
    const calls: { url: string; body: string }[] = []
    const origFetch = globalThis.fetch
    globalThis.fetch = (async (url: string, init?: RequestInit) => {
      calls.push({ url: String(url), body: String(init?.body) })
      return new Response(JSON.stringify(mockResponse), { status: 200 })
    }) as typeof fetch
    try {
      const res = await requestStructuredPlan(payload)
      expect(calls[0].url).toBe('/api/plan/structured')
      const sent = JSON.parse(calls[0].body)
      // The structured contract carries explicit fields - no free text.
      expect(sent).toEqual(payload)
      expect(sent.text).toBeUndefined()
      expect(res.best.start_time).toBe('14:00')
      expect(res.disclaimer).toMatch(/not medical advice/i)
    } finally {
      globalThis.fetch = origFetch
    }
  })

  it('throws a structured error on a 422 detail object', async () => {
    const origFetch = globalThis.fetch
    globalThis.fetch = (async () =>
      new Response(
        JSON.stringify({
          detail: { code: 'INVALID_DATE', message: 'The date must be valid.' },
        }),
        { status: 422 },
      )) as typeof fetch
    try {
      await expect(
        requestStructuredPlan({
          date: 'x', duration_minutes: 60,
          available_start: '13:00', available_end: '17:00',
          location_text: 'Phoenix',
        }),
      ).rejects.toMatchObject({ code: 'INVALID_DATE' })
    } finally {
      globalThis.fetch = origFetch
    }
  })
})

describe('time formatting', () => {
  it('renders 24h HH:MM as 12-hour display', () => {
    expect(fmtTime('13:00')).toBe('1:00 PM')
    expect(fmtTime('00:00')).toBe('12:00 AM')
    expect(fmtTime('12:30')).toBe('12:30 PM')
    expect(fmtTime('09:05')).toBe('9:05 AM')
  })
})

describe('structured UI architecture (spec §14/§17)', () => {
  const appSrc = readFileSync(resolve(__dirname, 'App.tsx'), 'utf8')

  it('the primary UI collects the four structured fields', () => {
    expect(appSrc).toContain('When?')
    expect(appSrc).toContain('How long do you need to be outside?')
    expect(appSrc).toContain('What time can you do it?')
    expect(appSrc).toContain('Where are you going?')
    expect(appSrc).toContain('Find my best time')
  })

  it('the primary UI never requires implementation knowledge', () => {
    for (const leak of ['AOI', 'GeoJSON', 'FortyGuard', 'filter_type', 'polling', 'cache']) {
      // Allowed only in the LIVE badge context ("LIVE · FortyGuard").
      if (leak === 'FortyGuard') continue
      expect(appSrc).not.toContain(leak)
    }
  })

  it('shows BEST TIME and OTHER AVAILABLE TIMES sections', () => {
    expect(appSrc).toMatch(/Best time/i)
    expect(appSrc).toMatch(/Other available times/i)
    expect(appSrc).toContain('Estimated temperature')
    expect(appSrc).toContain('Estimated thermal exposure')
  })

  it('renders structured errors without exposing internals', () => {
    expect(appSrc).toContain('error.message')
    expect(appSrc).not.toMatch(/diagnostics/)
    expect(appSrc).not.toMatch(/search_summary/)
    expect(appSrc).not.toMatch(/activity_id/)
    expect(appSrc).toContain('Unavailable live thermal data')
    expect(appSrc).toContain('Outside current forecast horizon')
  })

  it('keeps the medical disclaimer', () => {
    expect(appSrc).toMatch(/not medical advice/i)
    expect(appSrc).toMatch(/not a safety guarantee/i)
  })
})
