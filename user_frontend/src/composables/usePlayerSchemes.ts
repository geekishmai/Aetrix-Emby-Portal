// 播放器一键导入：合并账号卡 import_schemes 与前端基于 base_url + emby_username 生成的 6 款免密协议。
// 只接受 rex:// / senplayer:// / vidhub:// / forward:// / rodelplayer:// / com.feifeiduck.capyplayer://，用 location.href 拉起。
// 其余一律拒绝，包括 javascript:、data:、http(s):。
import { embyApi, type AccountCard } from '@/api'
import { useUserStore } from '@/stores/user'
import { branding } from '@/composables/useBranding'

import {
  buildInfuseLaunchUrl,
  buildPasswordlessImportSchemes,
  embyImportTip,
  findPlayerImportScheme,
  isImportScheme,
  isPlayerDeepLink,
  listImportSchemes,
  parseEmbyServerAddress,
  toBase64UrlNoPad,
  withImportPassword,
  type ParsedEmbyEndpoint,
  type PlayerScheme,
} from '@/utils/playerSchemes'

export {
  buildInfuseLaunchUrl,
  buildPasswordlessImportSchemes,
  embyImportTip,
  findPlayerImportScheme,
  isImportScheme,
  isPlayerDeepLink,
  listImportSchemes,
  parseEmbyServerAddress,
  toBase64UrlNoPad,
  withImportPassword,
  type ParsedEmbyEndpoint,
  type PlayerScheme,
}

/** 接线员 openScheme 的跳转本体。不在白名单里的地址直接拒绝。 */
export function assignImportScheme(url: string): boolean {
  if (!isImportScheme(url)) return false
  window.location.href = url
  return true
}

/** 详情页播放器直达/导入协议拉起（仅放行 infuse / rex / senplayer / vidhub）。 */
export function assignPlayerDeepLink(url: string): boolean {
  if (!isPlayerDeepLink(url)) return false
  window.location.href = url
  return true
}

let cached: { userId: number | null; schemes: PlayerScheme[] } | null = null
let inflight: { userId: number | null; promise: Promise<PlayerScheme[]> } | null = null

function cacheUserId(): number | null {
  return useUserStore().user?.id ?? null
}

/** 账号卡 import_schemes。失败或空列表不写入缓存，下次再拉；缓存按用户 id 分开，换号不串。 */
export function loadImportSchemes(force = false): Promise<PlayerScheme[]> {
  const userId = cacheUserId()
  if (cached && cached.userId !== userId) cached = null
  if (!force && cached && cached.userId === userId) return Promise.resolve(cached.schemes)
  if (!force && inflight && inflight.userId === userId) return inflight.promise

  const promise = embyApi
    .getAccountCard()
    .then((card: AccountCard) => {
      const allowed = !card?.external && card?.view_permission?.granted !== false
      const merged = allowed
        ? buildPasswordlessImportSchemes(
            card?.base_url,
            card?.emby_username,
            branding.site_name || '帆桥影院',
            card?.import_schemes,
          )
        : {}
      const schemes = listImportSchemes(merged)
      if (schemes.length > 0 && cacheUserId() === userId) cached = { userId, schemes }
      return schemes
    })
    .catch(() => [] as PlayerScheme[])
    .finally(() => {
      if (inflight?.promise === promise) inflight = null
    })
  inflight = { userId, promise }
  return promise
}
