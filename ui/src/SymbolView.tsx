import { useEffect, useState } from 'react'
import { api } from './api'
import Chart from './Chart'
import { crore, num, pct, signClass } from './format'
import type { SymbolDetail } from './types'

const RANGES = [
  { label: '6m', days: 130 },
  { label: '1y', days: 260 },
  { label: '2y', days: 520 },
  { label: '5y', days: 1250 },
]

export default function SymbolView({
  symbol, onPickSymbol,
}: { symbol: string | null; onPickSymbol: (s: string) => void }) {
  const [data, setData] = useState<SymbolDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [days, setDays] = useState(260)

  useEffect(() => {
    if (!symbol) return
    setData(null)
    setError(null)
    api.symbol(symbol, days).then(setData).catch((e: Error) => setError(e.message))
  }, [symbol, days])

  if (!symbol)
    return (
      <div className="panel">
        <div className="msg">
          Pick a symbol from the shortlist, or search for one in the header.
        </div>
      </div>
    )
  if (error) return <div className="panel"><div className="msg error">{error}</div></div>
  if (!data) return <div className="panel"><div className="msg">loading {symbol}…</div></div>

  const last = data.bars[data.bars.length - 1]
  const stacked =
    last.ema_20 !== null && last.ema_50 !== null && last.ema_100 !== null && last.ema_200 !== null &&
    last.ema_20 > last.ema_50 && last.ema_50 > last.ema_100 && last.ema_100 > last.ema_200

  return (
    <div className="stack">
      {data.quarantined.length > 0 && (
        <div className="panel">
          <div className="panel-head">
            <span className="badge exit">quarantined</span>
            <span>
              excluded from the universe {data.quarantined[0].from_date} →{' '}
              {data.quarantined[0].to_date} · {data.quarantined[0].reason}
            </span>
          </div>
        </div>
      )}

      <div className="panel">
        <div className="panel-head">
          <strong>{data.symbol}</strong>
          <span>· {last.trade_date} · close {num(last.close)}</span>
          <span className={stacked ? 'up' : 'rank'}>
            · EMA stack {stacked ? 'intact' : 'broken'}
          </span>
          <div className="spacer" />
          <div className="controls">
            {RANGES.map((r) => (
              <button key={r.days} className="act"
                      style={days === r.days ? { color: 'var(--text)', borderColor: 'var(--accent)' } : undefined}
                      onClick={() => setDays(r.days)}>{r.label}</button>
            ))}
          </div>
        </div>

        <div className="grid">
          <div className="stat"><div className="k">RSI(14)</div><div className="v">{num(last.rsi, 1)}</div></div>
          <div className="stat"><div className="k">ADX(14)</div><div className="v">{num(last.adx, 1)}</div></div>
          <div className="stat">
            <div className="k">Stochastic %K</div>
            <div className={`v ${last.stoch_k !== null && last.stoch_k < 50 ? 'up' : ''}`}>
              {num(last.stoch_k, 0)}
            </div>
          </div>
          <div className="stat"><div className="k">ATR(14)</div><div className="v">{num(last.atr, 2)}</div></div>
          <div className="stat"><div className="k">30d momentum</div>
            <div className={`v ${signClass(last.mom_short)}`}>{pct(last.mom_short)}</div></div>
          <div className="stat"><div className="k">6m mom (skip 1m)</div>
            <div className={`v ${signClass(last.mom_long_skip)}`}>{pct(last.mom_long_skip)}</div></div>
          <div className="stat"><div className="k">extension z</div>
            <div className={`v ${last.ext_zscore !== null && last.ext_zscore >= 2 ? 'warn' : ''}`}>
              {num(last.ext_zscore, 2)}</div></div>
          <div className="stat"><div className="k">vs 52w high</div>
            <div className={`v ${signClass(last.pct_from_high)}`}>{pct(last.pct_from_high)}</div></div>
          <div className="stat"><div className="k">trend age</div>
            <div className="v">{last.trend_age_days ?? '–'}<span className="k"> sessions</span></div></div>
          <div className="stat"><div className="k">20d median turnover</div>
            <div className="v">{crore(last.median_turnover)}</div></div>
        </div>

        <Chart bars={data.bars} />
      </div>

      {data.corporate_actions.length > 0 && (
        <div className="panel">
          <div className="panel-head">
            <strong>corporate actions</strong>
            <span>· prices before an ex-date are divided by the factor</span>
          </div>
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th className="left">ex-date</th><th className="left">type</th>
                  <th>factor</th><th className="left">verification</th>
                  <th className="left">subject</th>
                </tr>
              </thead>
              <tbody>
                {data.corporate_actions.map((a) => (
                  <tr key={`${a.ex_date}-${a.subject}`}>
                    <td className="left">{a.ex_date}</td>
                    <td className="left">{a.action_type}</td>
                    <td>{num(a.adj_factor, 3)}</td>
                    <td className="left">
                      <span className={`badge ${a.verification === 'verified' ? 'hold' : 'exit'}`}>
                        {a.verification}
                      </span>
                    </td>
                    <td className="left rank" title={a.subject}>{a.subject.slice(0, 70)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="hint">
        Charted from <code>prices_adjusted</code>, so splits and bonuses are already
        applied. Raw NSE prices are never rewritten —{' '}
        <button className="sym" onClick={() => onPickSymbol(data.symbol)}>reload</button>
        {' '}if the daily job has run since you opened this.
      </div>
    </div>
  )
}
