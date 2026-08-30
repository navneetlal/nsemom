async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  const text = await response.text()
  let payload: unknown
  try {
    payload = text ? JSON.parse(text) : null
  } catch {
    throw new Error(`${response.status}: ${text.slice(0, 200)}`)
  }
  if (!response.ok) {
    const message =
      payload && typeof payload === 'object' && 'error' in payload
        ? String((payload as { error: unknown }).error)
        : `request failed (${response.status})`
    throw new Error(message)
  }
  return payload as T
}

export const api = {
  meta: () => request<import('./types').Meta>('/api/meta'),
  dates: () => request<{ trade_date: string }[]>('/api/dates?limit=250'),
  shortlist: (date: string, preset: string) =>
    request<import('./types').Shortlist>(
      `/api/shortlist?date=${encodeURIComponent(date)}&preset=${encodeURIComponent(preset)}`,
    ),
  positions: () => request<import('./types').Positions>('/api/positions'),
  symbol: (symbol: string, days = 320) =>
    request<import('./types').SymbolDetail>(
      `/api/symbol/${encodeURIComponent(symbol)}?days=${days}`,
    ),
  searchSymbols: (q: string) =>
    request<{ symbol: string; last_seen: string }[]>(
      `/api/symbols?q=${encodeURIComponent(q)}`,
    ),
  addPosition: (body: {
    symbol: string
    entry_price: number
    quantity: number
    entry_date: string
  }) => request<{ position_id: number }>('/api/positions', {
    method: 'POST',
    body: JSON.stringify(body),
  }),
  closePosition: (body: {
    position_id: number
    exit_price: number
    exit_date?: string
    reason?: string
  }) => request<{ closed: boolean }>('/api/positions/close', {
    method: 'POST',
    body: JSON.stringify(body),
  }),
  deletePosition: (position_id: number) =>
    request<{ deleted: boolean }>('/api/positions/delete', {
      method: 'POST',
      body: JSON.stringify({ position_id }),
    }),
}
