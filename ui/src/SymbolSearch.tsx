import { useEffect, useRef, useState } from 'react'
import { api } from './api'

export default function SymbolSearch({ onPick }: { onPick: (s: string) => void }) {
  const [term, setTerm] = useState('')
  const [hits, setHits] = useState<string[]>([])
  const box = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (term.trim().length < 2) {
      setHits([])
      return
    }
    // debounce: the endpoint runs a LIKE across 3,765 symbols per keystroke
    const timer = setTimeout(() => {
      api.searchSymbols(term.trim())
        .then((r) => setHits(r.map((x) => x.symbol)))
        .catch(() => setHits([]))
    }, 180)
    return () => clearTimeout(timer)
  }, [term])

  useEffect(() => {
    const away = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setHits([])
    }
    document.addEventListener('mousedown', away)
    return () => document.removeEventListener('mousedown', away)
  }, [])

  const pick = (s: string) => {
    onPick(s)
    setTerm('')
    setHits([])
  }

  return (
    <div className="search" ref={box}>
      <input
        type="text"
        placeholder="find symbol…"
        value={term}
        onChange={(e) => setTerm(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && hits.length) pick(hits[0])
          if (e.key === 'Escape') setHits([])
        }}
      />
      {hits.length > 0 && (
        <div className="results">
          {hits.map((s) => (
            <button key={s} onClick={() => pick(s)}>{s}</button>
          ))}
        </div>
      )}
    </div>
  )
}
