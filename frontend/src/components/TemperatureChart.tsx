import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import type { StructuredCandidate } from '../types'
import { fmtTime } from '../types'

const PALETTE = ['#1d5fa8', '#e08a3e', '#1c7a4d', '#b3351f', '#7a5fc0', '#a1801c']

/** Plots raw temperature (°F) over elapsed minutes for each candidate time
 *  slot. This is TEMPERATURE data - it is never labeled as exposure. The
 *  best option's series is emphasized; the others stay quiet so the
 *  recommendation reads at a glance. */
export default function TemperatureChart({
  candidates,
  bestStart,
}: {
  candidates: StructuredCandidate[]
  bestStart?: string
}) {
  const usable = candidates.filter(
    (c) => c.thermal_status === 'ok' && c.timeline.length > 0,
  )
  if (usable.length === 0) {
    return <p className="muted">No temperature timeline available.</p>
  }

  const maxOffset = Math.max(...usable.flatMap((c) => c.timeline.map((t) => t.offset_minutes)))
  const data = Array.from({ length: maxOffset + 1 }, (_, minute) => {
    const row: Record<string, number> = { minute }
    for (const c of usable) {
      const pt = c.timeline.find((t) => t.offset_minutes === minute)
      if (pt) row[c.start_time] = pt.temperature_f
    }
    return row
  })

  const allTemps = usable.flatMap((c) => c.timeline.map((t) => t.temperature_f))
  const yMin = Math.floor(Math.min(...allTemps) - 2)
  const yMax = Math.ceil(Math.max(...allTemps) + 2)

  return (
    <ResponsiveContainer width="100%" height={280}>
      <LineChart data={data} margin={{ top: 8, right: 14, bottom: 4, left: -8 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#e7ded2" />
        <XAxis
          dataKey="minute"
          tick={{ fontSize: 12, fill: '#6f6355' }}
          tickLine={false}
          label={{
            value: 'minutes into the activity',
            position: 'insideBottom',
            offset: -2,
            style: { fontSize: 12, fill: '#6f6355' },
          }}
        />
        <YAxis
          domain={[yMin, yMax]}
          unit="°F"
          tick={{ fontSize: 12, fill: '#6f6355' }}
          tickLine={false}
          axisLine={false}
        />
        <Tooltip
          formatter={(value, name) => [
            `${Number(value).toFixed(1)} °F`,
            `Start ${fmtTime(String(name))}`,
          ]}
          labelFormatter={(minute) => `${minute} min in`}
          contentStyle={{
            borderRadius: 10,
            border: '1px solid #e7ded2',
            fontSize: 13,
          }}
        />
        <Legend
          formatter={(name) =>
            `Start ${fmtTime(String(name))}${String(name) === bestStart ? ' · best' : ''}`
          }
          wrapperStyle={{ fontSize: 12.5 }}
        />
        {usable.map((c, i) => {
          const isBest = c.start_time === bestStart
          return (
            <Line
              key={c.start_time}
              type="monotone"
              dataKey={c.start_time}
              name={c.start_time}
              stroke={isBest ? '#e05a2b' : PALETTE[i % PALETTE.length]}
              strokeWidth={isBest ? 3 : 1.75}
              strokeOpacity={bestStart && !isBest ? 0.55 : 1}
              dot={false}
            />
          )
        })}
      </LineChart>
    </ResponsiveContainer>
  )
}
