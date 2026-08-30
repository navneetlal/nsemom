import { useState } from 'react'
import { copyText } from './clipboard'
import { toCsv } from './format'

type State = 'idle' | 'copied' | 'failed'

// Generic over the row type so typed interfaces (OpenPosition, ClosedPosition)
// can be passed directly - a TS interface has no implicit index signature, so
// Record<string, unknown> would reject them at every call site.
export default function CopyCsvButton<T extends object>({
  columns, rows, label = 'copy CSV', title,
}: {
  columns: string[]
  rows: readonly T[]
  label?: string
  title?: string
}) {
  const [state, setState] = useState<State>('idle')

  const copy = async () => {
    const ok = await copyText(toCsv(columns, rows))
    setState(ok ? 'copied' : 'failed')
    setTimeout(() => setState('idle'), 1800)
  }

  return (
    <button
      className="act"
      onClick={copy}
      disabled={rows.length === 0}
      title={title ?? `${rows.length} rows, in the order shown`}
      style={state === 'copied' ? { color: 'var(--up)', borderColor: 'var(--up)' }
           : state === 'failed' ? { color: 'var(--down)', borderColor: 'var(--down)' }
           : undefined}
    >
      {state === 'copied' ? `copied ${rows.length} rows` : state === 'failed' ? 'copy failed' : label}
    </button>
  )
}
