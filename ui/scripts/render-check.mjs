/**
 * Renders the components in node to catch what HTTP tests cannot: an import
 * that does not resolve, JSX that throws, or a chart that divides by zero on
 * real data. Not a substitute for looking at it, but it fails loudly when the
 * bundle is broken.
 *
 *   node scripts/render-check.mjs [baseUrl]
 */
import { build } from 'esbuild'
import { createRequire } from 'node:module'

const base = process.argv[2] ?? 'http://127.0.0.1:8787'
const require = createRequire(import.meta.url)

const bundle = await build({
  entryPoints: ['src/App.tsx', 'src/Chart.tsx', 'src/format.ts'],
  bundle: true, write: false, format: 'cjs', platform: 'node',
  // outdir is required for multiple entry points even though write:false
  // means nothing reaches disk; it only names the in-memory outputs.
  outdir: 'out',
  jsx: 'automatic', external: ['react', 'react-dom', 'react/jsx-runtime'],
  loader: { '.css': 'empty' }, logLevel: 'silent',
})

const load = (name) => {
  const file = bundle.outputFiles.find((f) => f.path.endsWith(`${name}.js`))
  const module = { exports: {} }
  new Function('require', 'module', 'exports', file.text)(require, module, module.exports)
  return module.exports.default ?? module.exports
}

const { renderToString } = require('react-dom/server')
const { createElement } = require('react')

const checks = []
const check = (name, fn) => {
  try { fn(); checks.push([true, name, '']) }
  catch (e) { checks.push([false, name, e.message]) }
}

// 1. the whole app mounts without throwing
const App = load('App')
let appHtml = ''
check('App renders', () => {
  appHtml = renderToString(createElement(App))
  if (!appHtml.includes('loading')) throw new Error('expected an initial loading state')
})

// 2. the chart against real bars from the running server
const Chart = load('Chart')
let bars = []
try {
  const res = await fetch(`${base}/api/symbol/HFCL?days=260`)
  bars = (await res.json()).bars ?? []
} catch { /* server not running - covered by the guard below */ }

if (bars.length === 0) {
  checks.push([false, 'fetched real bars', `no data from ${base} - is \`nsemom serve\` running?`])
} else {
  let svg = ''
  check('Chart renders real bars', () => {
    svg = renderToString(createElement(Chart, { bars }))
    if (!svg.includes('<svg')) throw new Error('no svg emitted')
  })
  check('candles drawn for every bar', () => {
    const plotted = bars.filter((b) => b.close !== null).length
    const rects = (svg.match(/<rect/g) ?? []).length
    if (rects < plotted) throw new Error(`${rects} rects for ${plotted} bars`)
  })
  check('four EMA lines drawn', () => {
    const paths = (svg.match(/<path/g) ?? []).length
    if (paths < 5) throw new Error(`only ${paths} paths (4 EMAs + RSI expected)`)
  })
  check('no NaN leaked into coordinates', () => {
    if (/NaN|Infinity/.test(svg)) throw new Error('NaN or Infinity in SVG output')
  })
  check('chart survives a single bar', () => {
    renderToString(createElement(Chart, { bars: bars.slice(0, 1) }))
  })
  check('chart survives all-null closes', () => {
    renderToString(createElement(Chart, {
      bars: bars.slice(0, 5).map((b) => ({ ...b, close: null })),
    }))
  })
  check('chart survives a flat series (zero price range)', () => {
    const flat = bars.slice(0, 30).map((b) => ({
      ...b, open: 100, high: 100, low: 100, close: 100,
      ema_20: 100, ema_50: 100, ema_100: 100, ema_200: 100,
    }))
    const out = renderToString(createElement(Chart, { bars: flat }))
    if (/NaN/.test(out)) throw new Error('flat series produced NaN')
  })
}

// 3. the CSV builder, which is pure and therefore properly testable
const { toCsv } = load('format')

check('csv emits a header and one line per row', () => {
  const out = toCsv(['symbol', 'close'], [{ symbol: 'A', close: 1 }, { symbol: 'B', close: 2 }])
  if (out !== 'symbol,close\nA,1\nB,2') throw new Error(JSON.stringify(out))
})
check('csv respects the column order given, not object key order', () => {
  const out = toCsv(['close', 'symbol'], [{ symbol: 'A', close: 1 }])
  if (out !== 'close,symbol\n1,A') throw new Error(JSON.stringify(out))
})
check('csv writes null and undefined as empty, not "null"', () => {
  const out = toCsv(['a', 'b'], [{ a: null, b: undefined }])
  if (out !== 'a,b\n,') throw new Error(JSON.stringify(out))
})
check('csv quotes commas, quotes and newlines', () => {
  const out = toCsv(['x'], [{ x: 'a,b' }, { x: 'say "hi"' }, { x: 'one\ntwo' }])
  const want = 'x\n"a,b"\n"say ""hi"""\n"one\ntwo"'
  if (out !== want) throw new Error(JSON.stringify(out))
})
check('csv trims float noise but keeps meaning', () => {
  const out = toCsv(['rsi', 'mom'], [{ rsi: 70.04838562011719, mom: 0.17966901 }])
  if (out !== 'rsi,mom\n70.0484,0.179669') throw new Error(JSON.stringify(out))
})
check('csv leaves an empty row set as just the header', () => {
  if (toCsv(['a', 'b'], []) !== 'a,b') throw new Error('unexpected body')
})

for (const [ok, name, detail] of checks) {
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ` -> ${detail}` : ''}`)
}
const failed = checks.filter(([ok]) => !ok).length
console.log(failed ? `\n${failed} check(s) failed` : `\nall ${checks.length} checks passed`)
process.exit(failed ? 1 : 0)
