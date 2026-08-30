/**
 * Copy text, working on a plain-HTTP LAN address too.
 *
 * navigator.clipboard requires a secure context. http://127.0.0.1 counts as one,
 * but http://192.168.x.x does not - which is exactly how you reach this page
 * from another machine when serving with --host 0.0.0.0. So there is a fallback.
 */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (window.isSecureContext && navigator.clipboard) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    /* fall through to the legacy path */
  }

  try {
    const area = document.createElement('textarea')
    area.value = text
    area.setAttribute('readonly', '')
    area.style.position = 'fixed'
    area.style.opacity = '0'
    document.body.appendChild(area)
    area.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(area)
    return ok
  } catch {
    return false
  }
}
