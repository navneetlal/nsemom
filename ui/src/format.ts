export const num = (v: number | string | null | undefined, places = 2): string =>
  v === null || v === undefined || v === '' || Number.isNaN(Number(v))
    ? '–'
    : Number(v).toLocaleString('en-IN', {
        minimumFractionDigits: places,
        maximumFractionDigits: places,
      })

export const pct = (v: number | null | undefined, places = 1): string =>
  v === null || v === undefined || Number.isNaN(v)
    ? '–'
    : `${(v * 100).toFixed(places)}%`

/** Indian market convention: lakh and crore read faster than 4.0e8 here. */
export const crore = (v: number | null | undefined): string => {
  if (v === null || v === undefined || Number.isNaN(v)) return '–'
  if (v >= 1e7) return `${(v / 1e7).toFixed(1)}cr`
  if (v >= 1e5) return `${(v / 1e5).toFixed(1)}L`
  return v.toFixed(0)
}

export const signClass = (v: number | null | undefined): string =>
  v === null || v === undefined || Number.isNaN(v) ? '' : v > 0 ? 'up' : v < 0 ? 'down' : ''

/** Trim float noise without losing meaning: 70.04838562011719 -> 70.0484. */
const tidy = (v: number): string => String(Number(v.toPrecision(6)))

const escapeCell = (raw: string): string =>
  /[",\n\r]/.test(raw) ? `"${raw.replace(/"/g, '""')}"` : raw

/**
 * CSV of exactly what is on screen — current sort, current columns.
 * Values are raw rather than display-formatted, so percentages stay as ratios
 * and nothing arrives at an LLM pre-rounded into a different number.
 */
export const toCsv = <T extends object>(
  columns: string[],
  rows: readonly T[],
): string => {
  const header = columns.map(escapeCell).join(',')
  const body = rows.map((row) =>
    columns
      .map((c) => {
        const v = (row as Record<string, unknown>)[c]
        if (v === null || v === undefined) return ''
        return escapeCell(typeof v === 'number' ? tidy(v) : String(v))
      })
      .join(','),
  )
  return [header, ...body].join('\n')
}
