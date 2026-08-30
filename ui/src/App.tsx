import { useCallback, useEffect, useState } from 'react'
import { api } from './api'
import type { Meta } from './types'
import ShortlistView from './ShortlistView'
import PositionsView from './PositionsView'
import SymbolView from './SymbolView'
import SymbolSearch from './SymbolSearch'

type Tab = 'shortlist' | 'positions' | 'symbol'

export default function App() {
  const [meta, setMeta] = useState<Meta | null>(null)
  const [dates, setDates] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)

  const [tab, setTab] = useState<Tab>('shortlist')
  const [date, setDate] = useState('')
  const [preset, setPreset] = useState('')
  const [symbol, setSymbol] = useState<string | null>(null)
  const [openCount, setOpenCount] = useState<number | null>(null)

  useEffect(() => {
    Promise.all([api.meta(), api.dates()])
      .then(([m, d]) => {
        setMeta(m)
        setDates(d.map((x) => x.trade_date))
        setDate(m.latest_date)
        setPreset(m.default_preset)
      })
      .catch((e: Error) => setError(e.message))
  }, [])

  const openSymbol = useCallback((next: string) => {
    setSymbol(next)
    setTab('symbol')
  }, [])

  if (error) {
    return (
      <div className="app">
        <header className="top"><div className="brand">nsemom</div></header>
        <div className="msg error">{error}</div>
        <div className="hint">
          Is the database there? Try <code>nsemom status</code>. If the daily job
          is running it holds the write lock and this page cannot read until it
          finishes.
        </div>
      </div>
    )
  }
  if (!meta) return <div className="app"><div className="msg">loading…</div></div>

  return (
    <div className="app">
      <header className="top">
        <div className="brand">nsemom <span>· momentum screener</span></div>
        <div className="meta">
          {meta.symbols.toLocaleString()} symbols · {(meta.bars / 1e6).toFixed(2)}M bars ·{' '}
          {meta.first_date} → {meta.latest_date}
          {meta.quarantined > 0 && ` · ${meta.quarantined} quarantined`}
        </div>
        <div className="spacer" />
        <div className="controls">
          <SymbolSearch onPick={openSymbol} />
          <select value={preset} onChange={(e) => setPreset(e.target.value)} title="screen preset">
            {meta.presets.map((p) => (
              <option key={p} value={p}>
                {p}{p === meta.default_preset ? ' (default)' : ''}
              </option>
            ))}
          </select>
          <select value={date} onChange={(e) => setDate(e.target.value)} title="session">
            {dates.map((d) => (
              <option key={d} value={d}>{d}</option>
            ))}
          </select>
        </div>
      </header>

      <nav className="tabs">
        <button data-active={tab === 'shortlist'} onClick={() => setTab('shortlist')}>
          Shortlist
        </button>
        <button data-active={tab === 'positions'} onClick={() => setTab('positions')}>
          Positions
          {openCount !== null && <span className="count">{openCount}</span>}
        </button>
        <button data-active={tab === 'symbol'} onClick={() => setTab('symbol')}>
          Symbol
          {symbol && <span className="count">{symbol}</span>}
        </button>
      </nav>

      {tab === 'shortlist' && date && preset && (
        <ShortlistView date={date} preset={preset} onPickSymbol={openSymbol} />
      )}
      {tab === 'positions' && (
        <PositionsView
          latestDate={meta.latest_date}
          exitRules={meta.exit_rules}
          slots={meta.slots}
          onPickSymbol={openSymbol}
          onCount={setOpenCount}
        />
      )}
      {tab === 'symbol' && (
        <SymbolView symbol={symbol} onPickSymbol={openSymbol} />
      )}
    </div>
  )
}
