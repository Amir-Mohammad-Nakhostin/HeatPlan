import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

/**
 * P1 regression: the timeline chart plots `temperature_f`, so its label
 * must say "Temperature over time" - never "thermal exposure over time".
 * If a true cumulative exposure series is ever implemented, update both
 * the chart data mapping AND this test together.
 */
describe('temperature chart terminology', () => {
  const appSrc = readFileSync(resolve(__dirname, 'App.tsx'), 'utf8')
  const chartSrc = readFileSync(
    resolve(__dirname, 'components/TemperatureChart.tsx'),
    'utf8',
  )

  it('the panel title matches what the chart actually plots', () => {
    expect(chartSrc).toContain('pt.temperature_f')
    expect(appSrc).toContain('Temperature over time')
    expect(appSrc).not.toMatch(/exposure over time/i)
  })

  it('the safety disclaimer is preserved', () => {
    expect(appSrc).toMatch(/not medical advice/i)
  })
})
