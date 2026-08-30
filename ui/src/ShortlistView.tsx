import { useEffect, useMemo, useState } from 'react'
import { api } from './api'
import CopyCsvButton from './CopyCsvButton'
import { crore, num, pct, signClass } from './format'
import type { Row, Shortlist } from './types'

type Render = (v: number | string | null, row: Row) => React.ReactNode

interface Column {
  key: string
  label: string
  title?: string
  render?: Render
  cls?: (v: number | string | null) => string
}

const asNum = (v: number | string | null) => (v === null ? null : Number(v))

const COLUMNS: Column[] = [
  { key: 'rank', label: '#', render: (v) => v, cls: () => 'rank' },
  { key: 'symbol', label: 'symbol' },
  { key: 'close', label: 'close', render: (v) => num(v, 2) },
  { key: 'mom_long_skip', label: '6m mom', title: '6-month return skipping the last month — the ranking key',
    render: (v) => pct(asNum(v)), cls: (v) => signClass(asNum(v)) },
  { key: 'mom_short', label: '30d mom', render: (v) => pct(asNum(v)), cls: (v) => signClass(asNum(v)) },
  { key: 'rsi', label: 'rsi', render: (v) => num(v, 1) },
  { key: 'adx', label: 'adx', title: 'trend strength', render: (v) => num(v, 1) },
  { key: 'ext_zscore', label: 'ext z', title: 'how stretched vs this stock’s own trailing year — high means late',
    render: (v) => num(v, 2),
    cls: (v) => { const n = asNum(v); return n !== null && n >= 2 ? 'warn' : '' } },
  { key: 'ext_atr', label: 'ext atr', title: 'ATRs above the 50 EMA', render: (v) => num(v, 1) },
  { key: 'pct_from_high', label: 'vs 52w', title: 'distance below the 52-week high',
    render: (v) => pct(asNum(v)), cls: (v) => signClass(asNum(v)) },
  { key: 'trend_age_days', label: 'age', title: 'sessions the EMA stack has held', render: (v) => v },
  { key: 'up_day_ratio', label: 'up days', title: 'share of up days in the last 20', render: (v) => pct(asNum(v), 0) },
  { key: 'rsi_divergence', label: 'div', title: 'new price high without a new RSI high',
    render: (v) => (Number(v) ? '●' : '·'),
    cls: (v) => (Number(v) ? 'warn' : 'rank') },
  { key: 'vol_sustained', label: 'vol 5/50', title: 'sustained participation', render: (v) => num(v, 2) },
  { key: 'vol_ratio', label: 'vol 1/20', render: (v) => num(v, 2) },
  { key: 'median_turnover', label: 'liq 20d', title: '20-day median turnover', render: (v) => crore(asNum(v)) },
  { key: 'atr', label: 'atr', render: (v) => num(v, 2) },
  { key: 'suggested_stop', label: 'stop', title: 'where the deterministic initial stop would sit', render: (v) => num(v, 2) },
]

export default function ShortlistView({
  date, preset, onPickSymbol,
}: { date: string; preset: string; onPickSymbol: (s: string) => void }) {
  const [data, setData] = useState<Shortlist | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 }>({ key: 'rank', dir: 1 })

  useEffect(() => {
    setData(null)
    setError(null)
    api.shortlist(date, preset).then(setData).catch((e: Error) => setError(e.message))
  }, [date, preset])

  const rows = useMemo(() => {
    if (!data) return []
    const { key, dir } = sort
    return [...data.rows].sort((a, b) => {
      const x = a[key], y = b[key]
      if (x === null) return 1
      if (y === null) return -1
      if (typeof x === 'string' || typeof y === 'string')
        return String(x).localeCompare(String(y)) * dir
      return (Number(x) - Number(y)) * dir
    })
  }, [data, sort])

  if (error) return <div className="panel"><div className="msg error">{error}</div></div>
  if (!data) return <div className="panel"><div className="msg">loading…</div></div>

  const columns = COLUMNS.filter((c) => data.columns.includes(c.key))

  return (
    <div className="panel">
      <div className="panel-head">
        <strong>{data.rows.length} candidates</strong>
        <span>· {data.date} · preset <strong>{data.preset}</strong> · ranked by {data.rank_by}</span>
        <div className="spacer" />
        <CopyCsvButton
          columns={columns.map((c) => c.key)}
          rows={rows}
          title="Paste straight into the research prompt in the README"
        />
      </div>
      {data.rows.length === 0 ? (
        <div className="msg">
          Nothing passed the screen on this session. That is a normal outcome, not an error.
        </div>
      ) : (
        <div className="scroll">
          <table>
            <thead>
              <tr>
                {columns.map((c) => (
                  <th
                    key={c.key}
                    className={c.key === 'symbol' ? 'left' : undefined}
                    title={c.title}
                    data-sorted={sort.key === c.key}
                    onClick={() =>
                      setSort((s) =>
                        s.key === c.key ? { key: c.key, dir: (s.dir * -1) as 1 | -1 }
                                        : { key: c.key, dir: c.key === 'rank' ? 1 : -1 })
                    }
                  >
                    {c.label}{sort.key === c.key ? (sort.dir === 1 ? ' ↑' : ' ↓') : ''}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={String(row.symbol)}>
                  {columns.map((c) => (
                    <td key={c.key} className={[c.key === 'symbol' ? 'left' : '', c.cls?.(row[c.key]) ?? ''].join(' ').trim()}>
                      {c.key === 'symbol' ? (
                        <button className="sym" onClick={() => onPickSymbol(String(row.symbol))}>
                          {String(row.symbol)}
                        </button>
                      ) : (
                        c.render ? c.render(row[c.key], row) : String(row[c.key] ?? '–')
                      )}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="hint">
        This is an evidence pack, not a buy list. Paste it into the research prompt
        in the README — the screen has already done the arithmetic.
      </div>
    </div>
  )
}
