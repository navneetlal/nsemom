export interface Meta {
  latest_date: string
  first_date: string
  symbols: number
  bars: number
  presets: string[]
  default_preset: string
  slots: number
  quarantined: number
  exit_rules: {
    initial_stop_atr: number
    trailing_stop_atr: number
    max_hold_days: number
    min_hold_days: number
  }
}

export type Row = Record<string, number | string | null>

export interface Shortlist {
  date: string
  preset: string
  rank_by: string
  columns: string[]
  rows: Row[]
}

export interface OpenPosition {
  position_id: number
  symbol: string
  entry_date: string
  entry_price: number
  quantity: number
  last_close: number | null
  peak_close: number | null
  bars_held: number | null
  initial_stop: number | null
  trailing_stop: number | null
  unrealised_return: number | null
  action: string
}

export interface ClosedPosition {
  position_id: number
  symbol: string
  entry_date: string
  entry_price: number
  quantity: number
  exit_date: string
  exit_price: number
  exit_reason: string | null
  realised_return: number | null
}

export interface Positions {
  as_of: string
  open: OpenPosition[]
  closed: ClosedPosition[]
}

export interface Bar {
  trade_date: string
  open: number | null
  high: number | null
  low: number | null
  close: number | null
  volume: number | null
  ema_20: number | null
  ema_50: number | null
  ema_100: number | null
  ema_200: number | null
  rsi: number | null
  adx: number | null
  atr: number | null
  median_turnover: number | null
  mom_short: number | null
  mom_long_skip: number | null
  ext_zscore: number | null
  trend_age_days: number | null
  pct_from_high: number | null
}

export interface CorporateAction {
  ex_date: string
  action_type: string
  adj_factor: number | null
  verification: string
  subject: string
}

export interface SymbolDetail {
  symbol: string
  bars: Bar[]
  corporate_actions: CorporateAction[]
  quarantined: { from_date: string; to_date: string; reason: string }[]
}
