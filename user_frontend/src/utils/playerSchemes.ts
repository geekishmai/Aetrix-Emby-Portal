export interface PlayerScheme {
  name: string
  url: string
}

export interface ParsedEmbyEndpoint {
  scheme: 'http' | 'https'
  host: string
  port: number
  rootUrl: string
}

/** 账号卡与客户端页的一键导入展示顺序。界面上 senplayer 统一显示成 SenPlayer。 */
const SCHEME_ORDER = ['VidHub', 'SenPlayer', 'senplayer', 'Forward', 'CapyPlayer', 'Rex', '小幻影视']
const SCHEME_LABEL: Record<string, string> = {
  senplayer: 'SenPlayer',
  rodelplayer: '小幻影视',
  capyplayer: 'CapyPlayer',
  forward: 'Forward',
  vidhub: 'VidHub',
  rex: 'Rex',
}

const ALLOWED_SCHEME = /^(?:rex|senplayer|vidhub|forward|rodelplayer|com\.feifeiduck\.capyplayer):\/\//i
const ALLOWED_PLAYER_DEEPLINK =
  /^(?:infuse|rex|senplayer|vidhub|forward|rodelplayer|com\.feifeiduck\.capyplayer):\/\//i

export function isImportScheme(url: string): boolean {
  if (!url || /\s/.test(url)) return false
  return ALLOWED_SCHEME.test(url)
}

export function isPlayerDeepLink(url: string): boolean {
  if (!url || /\s/.test(url)) return false
  return ALLOWED_PLAYER_DEEPLINK.test(url)
}

export function toBase64UrlNoPad(text: string): string {
  const bytes = new TextEncoder().encode(text)
  let binary = ''
  for (const b of bytes) binary += String.fromCharCode(b)
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

function fromBase64Url(b64url: string): string {
  const norm = b64url.replace(/-/g, '+').replace(/_/g, '/')
  const padLen = (4 - (norm.length % 4)) % 4
  const binary = atob(norm + '='.repeat(padLen))
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)
  return new TextDecoder().decode(bytes)
}

/**
 * 解析 Emby 服务器根地址（剥除末尾 `/` 与误带的 `/emby`），供六款一键导入协议统一取参。
 */
export function parseEmbyServerAddress(rawUrl?: string | null): ParsedEmbyEndpoint | null {
  const trimmed = (rawUrl || '').trim().replace(/\/+$/, '').replace(/\/emby$/i, '')
  if (!trimmed || /\s/.test(trimmed)) return null
  const candidate = /^[a-z][a-z0-9+.-]*:\/\//i.test(trimmed) ? trimmed : `https://${trimmed}`
  try {
    const parsed = new URL(candidate)
    if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') return null
    const host = parsed.hostname
    if (!host) return null
    const scheme: 'http' | 'https' = parsed.protocol === 'http:' ? 'http' : 'https'
    const port = parsed.port ? Number(parsed.port) : scheme === 'https' ? 443 : 80
    if (!Number.isInteger(port) || port <= 0 || port > 65535) return null
    const isDefaultPort = (scheme === 'https' && port === 443) || (scheme === 'http' && port === 80)
    const rootUrl = isDefaultPort ? `${scheme}://${host}` : `${scheme}://${host}:${port}`
    return { scheme, host, port, rootUrl }
  } catch {
    return null
  }
}

/**
 * 生成带「服务器地址 + 接线用户名」的免密一键导入 URL Scheme 字典。
 *
 * 安全红线：密码绝不进入本函数生成的任何链接（无论是 query 参数还是 CapyPlayer 的 JSON 载荷）。
 */
export function buildPasswordlessImportSchemes(
  baseUrl?: string | null,
  username?: string | null,
  siteTitle = '帆桥影院',
  serverSchemes?: Record<string, string> | null,
): Record<string, string> {
  const cleanUser = (username || '').trim()
  const ep = parseEmbyServerAddress(baseUrl)
  const out: Record<string, string> = {}

  if (serverSchemes && typeof serverSchemes === 'object') {
    for (const [k, raw] of Object.entries(serverSchemes)) {
      if (typeof raw !== 'string' || !isImportScheme(raw)) continue
      // 防御性剥除可能误带的 password/pwd 查询项
      const safeUrl = raw.replace(/([?&])(?:password|pwd)=[^&#]*(&?)/gi, (_m, p1: string, p2: string) =>
        p1 === '?' && p2 === '&' ? '?' : p2 ? p1 : '',
      )
      const label = SCHEME_LABEL[k] ?? SCHEME_LABEL[k.toLowerCase()] ?? k
      out[label] = safeUrl
    }
  }

  if (!ep || !cleanUser) return out

  const cleanTitle = (siteTitle || '帆桥影院').trim() || '帆桥影院'
  const encUser = encodeURIComponent(cleanUser)
  const encTitle = encodeURIComponent(cleanTitle)
  const encHost = encodeURIComponent(ep.host)

  if (!out.SenPlayer) {
    out.SenPlayer = `senplayer://importserver?type=emby&name=${encTitle}&address=${encodeURIComponent(ep.rootUrl)}&username=${encUser}`
  }
  if (!out.VidHub) {
    out.VidHub = `vidhub://import?type=emby&scheme=${ep.scheme}&host=${encHost}&port=${ep.port}&username=${encUser}`
  }
  if (!out.Rex) {
    out.Rex = `rex://import?type=emby&scheme=${ep.scheme}&host=${encHost}&port=${ep.port}&username=${encUser}`
  }
  if (!out.Forward) {
    out.Forward = `forward://import?type=emby&scheme=${ep.scheme}&host=${encHost}&port=${ep.port}&username=${encUser}&title=${encTitle}`
  }
  if (!out.CapyPlayer) {
    const payload = JSON.stringify({
      serverType: 'emby',
      host: ep.host,
      port: ep.port,
      https: ep.scheme === 'https',
      username: cleanUser,
    })
    out.CapyPlayer = `com.feifeiduck.capyplayer://add-resource?data=${toBase64UrlNoPad(payload)}`
  }
  if (!out['小幻影视']) {
    out['小幻影视'] =
      `rodelplayer://import?type=emby&title=${encTitle}&scheme=${ep.scheme}&host=${encHost}&port=${ep.port}&username=${encUser}`
  }

  return out
}

/**
 * 构造 Infuse 直连播放协议地址（Infuse-Direct，不触发媒体库扫库模式）：
 * - 有可播流地址（http/https 或站内相对路径）时：infuse://x-callback-url/play?url=<encoded>
 * - 容器条目尚无单集流地址时：退回 infuse:// 直接唤起客户端
 */
export function buildInfuseLaunchUrl(streamUrl?: string | null, baseOrigin?: string): string {
  const raw = (streamUrl || '').trim()
  if (!raw) return 'infuse://'
  let resolved = raw
  if (raw.startsWith('/')) {
    const origin = (
      baseOrigin ||
      (typeof window !== 'undefined' && window.location ? window.location.origin : '')
    ).replace(/\/+$/, '')
    if (!origin) return 'infuse://'
    resolved = `${origin}${raw}`
  }
  if (!/^https?:\/\//i.test(resolved) || /\s/.test(resolved)) {
    return 'infuse://'
  }
  return `infuse://x-callback-url/play?url=${encodeURIComponent(resolved)}`
}

/** 从已解析的一键导入列表中按播放器名匹配（大小写不敏感） */
export function findPlayerImportScheme(
  schemes: readonly PlayerScheme[],
  target: string,
): PlayerScheme | undefined {
  const needle = target.trim().toLowerCase()
  return schemes.find((s) => s.name.trim().toLowerCase() === needle)
}

export function withImportPassword(url: string, password: string): string {
  if (!url || !password) return url
  // CapyPlayer 采用 base64url JSON 载荷而非 query 参数（仅在接线员页用户显式输入时内存拼接）
  const capyPrefix = 'com.feifeiduck.capyplayer://add-resource?data='
  if (url.toLowerCase().startsWith(capyPrefix)) {
    try {
      const b64 = url.slice(capyPrefix.length)
      const parsed = JSON.parse(fromBase64Url(b64)) as Record<string, unknown>
      if (parsed && typeof parsed === 'object' && !('password' in parsed)) {
        return `${url.slice(0, capyPrefix.length)}${toBase64UrlNoPad(JSON.stringify({ ...parsed, password }))}`
      }
      return url
    } catch {
      return url
    }
  }
  const hashAt = url.indexOf('#')
  const before = hashAt >= 0 ? url.slice(0, hashAt) : url
  const hash = hashAt >= 0 ? url.slice(hashAt) : ''
  if (/[?&](?:password|pwd)=/i.test(before)) return url
  const joiner = before.includes('?') ? '&' : '?'
  return `${before}${joiner}password=${encodeURIComponent(password)}${hash}`
}

export function embyImportTip(hasUsername: boolean, playPwdLoaded: boolean): string {
  if (!hasUsername) {
    return '还没有接线账号。先在上面设好用户名和播放密码。设好后这里会出现导入按钮，点的时候会带上播放密码。'
  }
  if (!playPwdLoaded) {
    return '点导入时会先问一次播放密码，填上就一起带过去。本页开着不用再填，离开或点清除就清掉。'
  }
  return '点一下直接导入，密码一起带过去，只用在这次跳转里，不会存'
}

export function listImportSchemes(schemes: Record<string, string> | null | undefined): PlayerScheme[] {
  if (!schemes || typeof schemes !== 'object') return []
  const keys = Object.keys(schemes)
  const ordered = [
    ...SCHEME_ORDER.filter((k) => keys.includes(k)),
    ...keys.filter((k) => !SCHEME_ORDER.includes(k)),
  ]
  const seenNames = new Set<string>()
  const out: PlayerScheme[] = []
  for (const key of ordered) {
    const url = schemes[key]
    if (typeof url === 'string' && isImportScheme(url)) {
      const name = SCHEME_LABEL[key] ?? SCHEME_LABEL[key.toLowerCase()] ?? key
      const lower = name.toLowerCase()
      if (!seenNames.has(lower)) {
        seenNames.add(lower)
        out.push({ name, url })
      }
    }
  }
  return out
}
