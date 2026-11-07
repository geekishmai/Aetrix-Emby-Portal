export interface PlayerScheme {
  name: string
  url: string
}

/** 账号卡的键。界面上 senplayer 显示成 SenPlayer。 */
const SCHEME_ORDER = ['Rex', 'senplayer', 'VidHub']
const SCHEME_LABEL: Record<string, string> = { senplayer: 'SenPlayer' }

const ALLOWED_SCHEME = /^(?:rex|senplayer|vidhub):\/\//i
const ALLOWED_PLAYER_DEEPLINK = /^(?:infuse|rex|senplayer|vidhub):\/\//i

export function isImportScheme(url: string): boolean {
  if (!url || /\s/.test(url)) return false
  return ALLOWED_SCHEME.test(url)
}

export function isPlayerDeepLink(url: string): boolean {
  if (!url || /\s/.test(url)) return false
  return ALLOWED_PLAYER_DEEPLINK.test(url)
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

/** 从账号卡已解析的 import_schemes 列表中匹配指定播放器（VidHub / SenPlayer / Rex） */
export function findPlayerImportScheme(
  schemes: readonly PlayerScheme[],
  target: 'VidHub' | 'SenPlayer' | 'Rex',
): PlayerScheme | undefined {
  const needle = target.toLowerCase()
  return schemes.find((s) => s.name.toLowerCase() === needle)
}

export function withImportPassword(url: string, password: string): string {
  if (!url || !password) return url
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
  const out: PlayerScheme[] = []
  for (const key of ordered) {
    const url = schemes[key]
    if (typeof url === 'string' && isImportScheme(url)) {
      out.push({ name: SCHEME_LABEL[key] ?? key, url })
    }
  }
  return out
}
