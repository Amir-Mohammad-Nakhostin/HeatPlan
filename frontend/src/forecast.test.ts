import { describe, expect, it } from 'vitest'

import { fmtTime, humanizeDate } from './types'

/** Display helpers used by the polished result/pre-flight UI. */
describe('humanizeDate', () => {
  it('renders a full readable date', () => {
    expect(humanizeDate('2026-08-28')).toBe('Friday, August 28, 2026')
  })

  it('passes through unparseable input unchanged', () => {
    expect(humanizeDate('not-a-date')).toBe('not-a-date')
  })
})

describe('fmtTime (24h -> 12h display)', () => {
  it('formats afternoon times', () => {
    expect(fmtTime('13:00')).toBe('1:00 PM')
    expect(fmtTime('17:30')).toBe('5:30 PM')
  })

  it('handles midnight and noon edges', () => {
    expect(fmtTime('00:00')).toBe('12:00 AM')
    expect(fmtTime('12:00')).toBe('12:00 PM')
  })
})
