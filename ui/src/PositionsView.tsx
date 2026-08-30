import { useCallback, useEffect, useState } from 'react'
import { api } from './api'
import { num, pct, signClass } from './format'
import type { Meta, Positions } from './types'

const badgeClass = (action: string) =>
  action.startsWith('EXIT') ? 'exit' : action.startsWith('WATCH') ? 'watch' : 'hold'

export default function PositionsView({
  latestDate, exitRules, slots, onPickSymbol, onCount,
}: {
  latestDate: string
  exitRules: Meta['exit_rules']
  slots: number
  onPickSymbol: (s: string) => void
  onCount: (n: number) => void
}) {
  const [data, setData] = useState<Positions | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const [symbol, setSymbol] = useState('')
  const [price, setPrice] = useState('')
  const [quantity, setQuantity] = useState('')
  const [entryDate, setEntryDate] = useState(latestDate)

  const load = useCallback(() => {
    api.positions()
      .then((d) => { setData(d); onCount(d.open.length) })
      .catch((e: Error) => setError(e.message))
  }, [onCount])

  useEffect(load, [load])

  const guard = async (action: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await action()
      load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const add = (e: React.FormEvent) => {
    e.preventDefault()
    void guard(async () => {
      await api.addPosition({
        symbol: symbol.trim().toUpperCase(),
        entry_price: Number(price),
        quantity: Number(quantity),
        entry_date: entryDate,
      })
      setSymbol(''); setPrice(''); setQuantity('')
    })
  }

  const close = (id: number, symbolName: string, lastClose: number | null) => {
    const entered = window.prompt(`Exit price for ${symbolName}`, lastClose ? String(lastClose) : '')
    if (entered === null) return
    const exitPrice = Number(entered)
    if (!Number.isFinite(exitPrice) || exitPrice <= 0) {
      setError('exit price must be a positive number')
      return
    }
    void guard(() => api.closePosition({ position_id: id, exit_price: exitPrice }))
  }

  const remove = (id: number, symbolName: string) => {
    if (!window.confirm(`Delete position ${id} (${symbolName})? This removes the row entirely — use Close if you actually sold.`))
      return
    void guard(() => api.deletePosition(id))
  }

  if (!data && !error) return <div className="panel"><div className="msg">loading…</div></div>

  const open = data?.open ?? []
  const closed = data?.closed ?? []

  return (
    <div className="stack">
      {error && <div className="panel"><div className="msg error">{error}</div></div>}

      <div className="panel">
        <div className="panel-head">
          <strong>{open.length} of {slots} slots</strong>
          <span>
            · as of {data?.as_of ?? '–'} · stop {exitRules.initial_stop_atr}×ATR,
            trail {exitRules.trailing_stop_atr}×ATR, time stop {exitRules.max_hold_days} sessions
          </span>
        </div>

        {open.length === 0 ? (
          <div className="msg">No open positions. Add one below after you buy.</div>
        ) : (
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th className="left">symbol</th>
                  <th>entry</th><th>qty</th><th>held</th>
                  <th>last</th><th>peak</th>
                  <th>stop</th><th>trail</th>
                  <th>P&amp;L</th>
                  <th className="left">action</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {open.map((p) => (
                  <tr key={p.position_id}>
                    <td className="left">
                      <button className="sym" onClick={() => onPickSymbol(p.symbol)}>{p.symbol}</button>
                    </td>
                    <td>{num(p.entry_price)}<br /><span className="rank">{p.entry_date}</span></td>
                    <td>{p.quantity}</td>
                    <td>{p.bars_held ?? '–'}</td>
                    <td>{num(p.last_close)}</td>
                    <td>{num(p.peak_close)}</td>
                    <td>{num(p.initial_stop)}</td>
                    <td>{p.trailing_stop === null ? <span className="rank">unarmed</span> : num(p.trailing_stop)}</td>
                    <td className={signClass(p.unrealised_return)}>{pct(p.unrealised_return, 2)}</td>
                    <td className="left">
                      <span className={`badge ${badgeClass(p.action)}`}>{p.action}</span>
                    </td>
                    <td>
                      <div className="row-actions">
                        <button className="act" disabled={busy}
                          onClick={() => close(p.position_id, p.symbol, p.last_close)}>close</button>
                        <button className="act danger" disabled={busy}
                          onClick={() => remove(p.position_id, p.symbol)}>delete</button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <form className="add" onSubmit={add}>
          <label>symbol
            <input className="wide" type="text" value={symbol} required
              onChange={(e) => setSymbol(e.target.value)} placeholder="HFCL" />
          </label>
          <label>entry price
            <input type="number" step="0.01" min="0.01" value={price} required
              onChange={(e) => setPrice(e.target.value)} />
          </label>
          <label>quantity
            <input type="number" step="1" min="1" value={quantity} required
              onChange={(e) => setQuantity(e.target.value)} />
          </label>
          <label>entry date
            <input type="date" value={entryDate} required
              onChange={(e) => setEntryDate(e.target.value)} />
          </label>
          <button className="primary" type="submit" disabled={busy}>Add position</button>
        </form>
        <div className="hint">
          The entry date must be a session this symbol actually traded — exits are
          evaluated from that bar’s ATR, so there is nothing to compute without it.
        </div>
      </div>

      {closed.length > 0 && (
        <div className="panel">
          <div className="panel-head"><strong>{closed.length} closed</strong></div>
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th className="left">symbol</th><th>entry</th><th>exit</th>
                  <th>qty</th><th>return</th><th className="left">reason</th>
                </tr>
              </thead>
              <tbody>
                {closed.map((p) => (
                  <tr key={p.position_id}>
                    <td className="left">
                      <button className="sym" onClick={() => onPickSymbol(p.symbol)}>{p.symbol}</button>
                    </td>
                    <td>{num(p.entry_price)}<br /><span className="rank">{p.entry_date}</span></td>
                    <td>{num(p.exit_price)}<br /><span className="rank">{p.exit_date}</span></td>
                    <td>{p.quantity}</td>
                    <td className={signClass(p.realised_return)}>{pct(p.realised_return, 2)}</td>
                    <td className="left rank">{p.exit_reason ?? '–'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
