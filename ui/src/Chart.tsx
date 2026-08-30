import { useMemo } from 'react'
import type { Bar } from './types'

const W = 1000
const PRICE_H = 300
const VOL_H = 56
const RSI_H = 70
const GAP = 16
const PAD_R = 54
const H = PRICE_H + GAP + VOL_H + GAP + RSI_H

const EMAS = [
  { key: 'ema_20' as const, colour: '#58a6ff' },
  { key: 'ema_50' as const, colour: '#d29922' },
  { key: 'ema_100' as const, colour: '#a371f7' },
  { key: 'ema_200' as const, colour: '#8b949e' },
]

/** Hand-rolled rather than pulling in a charting library: the whole bundle
 *  ships to a Raspberry Pi, and this is a few dozen lines of SVG. */
export default function Chart({ bars }: { bars: Bar[] }) {
  const view = useMemo(() => {
    const usable = bars.filter((b) => b.close !== null)
    if (usable.length < 2) return null

    const plotW = W - PAD_R
    const step = plotW / usable.length
    const bodyW = Math.max(1, Math.min(step * 0.68, 9))

    const highs = usable.map((b) => b.high ?? b.close!)
    const lows = usable.map((b) => b.low ?? b.close!)
    for (const { key } of EMAS) {
      for (const b of usable) {
        const v = b[key]
        if (v !== null) { highs.push(v); lows.push(v) }
      }
    }
    const hi = Math.max(...highs)
    const lo = Math.min(...lows)
    const span = hi - lo || 1
    const y = (v: number) => PRICE_H - ((v - lo) / span) * (PRICE_H - 8) - 4
    const x = (i: number) => i * step + step / 2

    const maxVol = Math.max(...usable.map((b) => b.volume ?? 0), 1)
    const volTop = PRICE_H + GAP
    const rsiTop = volTop + VOL_H + GAP
    const rsiY = (v: number) => rsiTop + RSI_H - (v / 100) * RSI_H

    const line = (key: (typeof EMAS)[number]['key']) => {
      const points: string[] = []
      usable.forEach((b, i) => {
        const v = b[key]
        if (v !== null) points.push(`${points.length ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`)
      })
      return points.join(' ')
    }

    const rsiPath = usable
      .map((b, i) => (b.rsi === null ? null : `${x(i).toFixed(1)},${rsiY(b.rsi).toFixed(1)}`))
      .filter(Boolean)
      .map((p, i) => `${i ? 'L' : 'M'}${p}`)
      .join(' ')

    const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => lo + span * f)
    return { usable, x, y, step, bodyW, maxVol, volTop, rsiTop, rsiY, line, rsiPath, ticks, hi, lo }
  }, [bars])

  if (!view) return <div className="msg">not enough bars to plot</div>

  const { usable, x, y, bodyW, maxVol, volTop, rsiTop, rsiY, line, rsiPath, ticks } = view

  return (
    <div className="chart-wrap">
      <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="price chart">
        {ticks.map((t) => (
          <g key={t}>
            <line x1={0} x2={W - PAD_R} y1={y(t)} y2={y(t)} stroke="#1e242d" strokeWidth={1} />
            <text x={W - PAD_R + 6} y={y(t) + 3.5} fill="#6e7681" fontSize={10} fontFamily="monospace">
              {t.toFixed(t > 1000 ? 0 : 1)}
            </text>
          </g>
        ))}

        {usable.map((b, i) => {
          const up = (b.close ?? 0) >= (b.open ?? b.close ?? 0)
          const colour = up ? '#3fb950' : '#f85149'
          const top = y(Math.max(b.open ?? b.close!, b.close!))
          const bottom = y(Math.min(b.open ?? b.close!, b.close!))
          return (
            <g key={b.trade_date}>
              <line x1={x(i)} x2={x(i)} y1={y(b.high ?? b.close!)} y2={y(b.low ?? b.close!)}
                    stroke={colour} strokeWidth={1} opacity={0.75} />
              <rect x={x(i) - bodyW / 2} y={top} width={bodyW}
                    height={Math.max(1, bottom - top)} fill={colour} opacity={0.9} />
              <rect x={x(i) - bodyW / 2} width={bodyW}
                    y={volTop + VOL_H - ((b.volume ?? 0) / maxVol) * VOL_H}
                    height={((b.volume ?? 0) / maxVol) * VOL_H} fill={colour} opacity={0.35} />
            </g>
          )
        })}

        {EMAS.map((e) => (
          <path key={e.key} d={line(e.key)} fill="none" stroke={e.colour}
                strokeWidth={1.3} opacity={0.9} />
        ))}

        {[30, 50, 70].map((level) => (
          <line key={level} x1={0} x2={W - PAD_R} y1={rsiY(level)} y2={rsiY(level)}
                stroke={level === 50 ? '#262d38' : '#1e242d'} strokeWidth={1}
                strokeDasharray={level === 50 ? undefined : '3 3'} />
        ))}
        <path d={rsiPath} fill="none" stroke="#58a6ff" strokeWidth={1.2} />
        <text x={W - PAD_R + 6} y={rsiY(70) + 3.5} fill="#6e7681" fontSize={10} fontFamily="monospace">70</text>
        <text x={W - PAD_R + 6} y={rsiY(30) + 3.5} fill="#6e7681" fontSize={10} fontFamily="monospace">30</text>
        <text x={2} y={rsiTop - 4} fill="#6e7681" fontSize={10} fontFamily="monospace">RSI(14)</text>
        <text x={2} y={volTop - 4} fill="#6e7681" fontSize={10} fontFamily="monospace">volume</text>
      </svg>
      <div className="legend">
        {EMAS.map((e) => (
          <span key={e.key}>
            <i style={{ background: e.colour }} />
            {e.key.replace('ema_', 'EMA ')}
          </span>
        ))}
        <span>{usable.length} sessions · {usable[0].trade_date} → {usable[usable.length - 1].trade_date}</span>
      </div>
    </div>
  )
}
