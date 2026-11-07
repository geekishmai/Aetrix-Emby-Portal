export type ClientPlatformKey = 'apple' | 'android' | 'android_tv' | 'windows' | 'harmony' | 'all'

export type ImportSchemeTarget =
  | 'VidHub'
  | 'SenPlayer'
  | 'Forward'
  | 'CapyPlayer'
  | 'Rex'
  | '小幻影视'

export interface PlatformTabSpec {
  key: ClientPlatformKey
  label: string
  deviceHint: string
  count: number
}

export interface PlayerCardSpec {
  id: string
  /** 16 款去重主标识 */
  canonicalId:
    | 'vidhub'
    | 'infuse'
    | 'senplayer'
    | 'forward'
    | 'capyplayer'
    | 'rex'
    | 'baomi'
    | 'filebar'
    | 'hamhub'
    | 'lenna'
    | 'eplayerx'
    | 'yamby'
    | 'afusekt'
    | 'hills'
    | 'xiaohuan'
    | 'hosplayer'
  platform: Exclude<ClientPlatformKey, 'all'>
  name: string
  subtitle: string
  monogram: string
  tierBadge?: string
  priceLabel: string
  isFreeTier: boolean
  downloadLabel: string
  downloadUrl: string
  /** 支持服务器地址+用户名免密 URL Scheme 一键导入的软件标识 */
  importTarget: ImportSchemeTarget | null
  /** 对应 docs/player-tutorials/<slug>.md */
  tutorialSlug: string
  summary: string
  caution?: string
}

const APPLE_CARDS: PlayerCardSpec[] = [
  {
    id: 'apple-vidhub',
    canonicalId: 'vidhub',
    platform: 'apple',
    name: 'VidHub',
    subtitle: 'iPhone · iPad · Apple TV · Mac',
    monogram: 'VH',
    tierBadge: '首选推荐',
    priceLabel: '免费下载 · Pro ¥12/月 / ¥98买断',
    isFreeTier: false,
    downloadLabel: 'App Store 下载',
    downloadUrl: 'https://apps.apple.com/cn/app/id1659622164',
    importTarget: 'VidHub',
    tutorialSlug: 'vidhub',
    summary: '支持门户免密一键导入服务器，海报墙加载迅速，iPhone、iPad、Mac 与 Apple TV 进度同步。',
  },
  {
    id: 'apple-infuse',
    canonicalId: 'infuse',
    platform: 'apple',
    name: 'Infuse',
    subtitle: 'iPhone · iPad · Apple TV · Mac',
    monogram: 'IN',
    tierBadge: '画质标杆',
    priceLabel: '免费下载 · Pro ¥13/月 / ¥88/年',
    isFreeTier: false,
    downloadLabel: 'App Store 下载',
    downloadUrl: 'https://apps.apple.com/cn/app/infuse/id1136220934',
    importTarget: null,
    tutorialSlug: 'infuse',
    summary: '苹果生态杜比视界与多声道原盘解码标杆；支持在门户影片详情页直接点「用 Infuse 打开」拉起播放。',
    caution: '添加服务器时务必在「高级」中保持关闭「媒体库模式」（即直连模式），开启媒体库模式会被 403 拒绝。',
  },
  {
    id: 'apple-senplayer',
    canonicalId: 'senplayer',
    platform: 'apple',
    name: 'SenPlayer',
    subtitle: 'iPhone · iPad · Apple TV · Mac',
    monogram: 'SP',
    tierBadge: '深度适配',
    priceLabel: '免费下载 · Pro ¥58单端 / ¥88全端',
    isFreeTier: false,
    downloadLabel: 'App Store 下载',
    downloadUrl: 'https://apps.apple.com/cn/app/senplayer-%E5%85%A8%E8%83%BD%E8%A7%86%E9%A2%91%E6%92%AD%E6%94%BE%E5%99%A8-%E7%BD%91%E7%9B%98%E7%9B%B4%E8%BF%9E/id6443975850',
    importTarget: 'SenPlayer',
    tutorialSlug: 'senplayer',
    summary: '支持门户免密一键导入服务器，针对本站直连协议做过深度兼容适配，4K HDR 硬解稳定。',
  },
  {
    id: 'apple-forward',
    canonicalId: 'forward',
    platform: 'apple',
    name: 'Forward',
    subtitle: '新视界 · iPhone · iPad · Apple TV · Mac',
    monogram: 'FW',
    priceLabel: '免费下载 · ¥10/月 / ¥98+买断',
    isFreeTier: false,
    downloadLabel: 'App Store 下载',
    downloadUrl: 'https://apps.apple.com/cn/app/forward-%E6%96%B0%E8%A7%86%E7%95%8C/id6503940939',
    importTarget: 'Forward',
    tutorialSlug: 'forward',
    summary: '现代流畅界面，现已支持门户免密一键导入服务器与多线路切换。',
    caution: '首次连接后请进入「设置 → 播放设置」关闭「详情页预加载」开关，避免选片时提前占流。',
  },
  {
    id: 'apple-capyplayer',
    canonicalId: 'capyplayer',
    platform: 'apple',
    name: 'CapyPlayer',
    subtitle: '卡皮巴拉 · iPhone · iPad',
    monogram: 'CP',
    priceLabel: '基础免费 · Pro ¥22/年 / ¥38买断',
    isFreeTier: true,
    downloadLabel: 'App Store 下载',
    downloadUrl: 'https://apps.apple.com/cn/app/id6755650660',
    importTarget: 'CapyPlayer',
    tutorialSlug: 'capyplayer',
    summary: '连接 Emby 基础播放免费，配置简洁，现已支持门户免密一键导入服务器。',
  },
  {
    id: 'apple-rex',
    canonicalId: 'rex',
    platform: 'apple',
    name: 'Rex',
    subtitle: 'iPhone · iPad · Apple TV',
    monogram: 'RX',
    priceLabel: '免费下载 · 含应用内购',
    isFreeTier: false,
    downloadLabel: 'App Store 下载',
    downloadUrl: 'https://apps.apple.com/cn/app/rex-%E8%81%9A%E5%90%88%E5%AA%92%E4%BD%93%E6%92%AD%E6%94%BE%E5%99%A8/id6801583759',
    importTarget: 'Rex',
    tutorialSlug: 'rex',
    summary: '聚合媒体播放器，支持门户免密一键导入服务器，适配 iPhone、iPad 与 Apple TV。',
  },
  {
    id: 'apple-baomi',
    canonicalId: 'baomi',
    platform: 'apple',
    name: '网易爆米花',
    subtitle: 'iPhone · iPad · Apple TV · Mac',
    monogram: 'BM',
    tierBadge: '完全免费',
    priceLabel: '完全免费（需手机号登录）',
    isFreeTier: true,
    downloadLabel: 'App Store 下载',
    downloadUrl: 'https://apps.apple.com/cn/app/%E7%BD%91%E6%98%93%E7%88%86%E7%B1%B3%E8%8A%B1-%E6%99%BA%E8%83%BD%E5%AA%92%E4%BD%93%E5%BA%93-%E5%85%A8%E8%83%BD%E4%BA%91%E7%9B%98%E7%BD%91%E7%9B%98%E6%92%AD%E6%94%BE%E5%99%A8/id6480252695',
    importTarget: null,
    tutorialSlug: 'baomi',
    summary: '零付费门槛，国区商店直接下载；添加资源时请选择「Emby」而非网盘。',
  },
  {
    id: 'apple-filebar',
    canonicalId: 'filebar',
    platform: 'apple',
    name: 'Filebar',
    subtitle: '原 Fileball · iPhone · iPad · Apple TV',
    monogram: 'FB',
    priceLabel: '基础免费 · Pro ¥68买断',
    isFreeTier: true,
    downloadLabel: 'App Store 下载',
    downloadUrl: 'https://apps.apple.com/cn/app/filebar/id1558391784',
    importTarget: null,
    tutorialSlug: 'filebar',
    summary: '老牌苹果端播放器，仅观看 Emby 基础版免费够用；建议在设置中切换为 MPV 播放内核。',
  },
  {
    id: 'apple-hamhub',
    canonicalId: 'hamhub',
    platform: 'apple',
    name: 'HamHub',
    subtitle: 'iPhone · iPad · Apple TV · Mac',
    monogram: 'HH',
    priceLabel: '单端免费 · 多端 ¥6/月 / ¥38+买断',
    isFreeTier: true,
    downloadLabel: 'App Store 下载',
    downloadUrl: 'https://apps.apple.com/cn/app/hamhub-%E4%BD%A0%E7%9A%84%E7%A7%81%E4%BA%BA%E4%BA%91%E5%BD%B1%E4%B8%93%E5%AE%B6/id6458691598',
    importTarget: null,
    tutorialSlug: 'hamhub',
    summary: 'iOS 单端连接 Emby 基础免费；注意在商店搜索准确名称 HamHub（非 Humhub）。',
  },
  {
    id: 'apple-lenna',
    canonicalId: 'lenna',
    platform: 'apple',
    name: 'Lenna',
    subtitle: 'iPhone · iPad · Apple TV · Mac',
    monogram: 'LN',
    priceLabel: '核心播放免费（含可选内购）',
    isFreeTier: true,
    downloadLabel: 'App Store (外区)',
    downloadUrl: 'https://apps.apple.com/us/app/lenna-video-library-player/id6502967807',
    importTarget: null,
    tutorialSlug: 'lenna',
    summary: '界面干净轻量，核心 Emby 播放免费；若国区商店未搜到可切美区/港区 Apple ID 获取。',
  },
  {
    id: 'apple-eplayerx',
    canonicalId: 'eplayerx',
    platform: 'apple',
    name: 'EplayerX',
    subtitle: 'iPhone · iPad · Apple TV · Mac',
    monogram: 'EP',
    priceLabel: '免费下载 · 含应用内购',
    isFreeTier: false,
    downloadLabel: 'App Store 下载',
    downloadUrl: 'https://apps.apple.com/cn/app/eplayerx-%E8%A7%86%E9%A2%91%E6%92%AD%E6%94%BE%E5%99%A8/id6747369377',
    importTarget: null,
    tutorialSlug: 'eplayerx',
    summary: '国区商店可直接下载，支持标准 Emby 协议连接与 4K HDR 硬件解码直放。',
  },
]

const ANDROID_PHONE_CARDS: PlayerCardSpec[] = [
  {
    id: 'android-vidhub',
    canonicalId: 'vidhub',
    platform: 'android',
    name: 'VidHub',
    subtitle: 'Android 手机 · 平板',
    monogram: 'VH',
    tierBadge: '首选推荐',
    priceLabel: '安卓端免费为主',
    isFreeTier: true,
    downloadLabel: '官网 APK 下载',
    downloadUrl: 'https://zh.vidhub.okaapps.com/',
    importTarget: 'VidHub',
    tutorialSlug: 'vidhub',
    summary: '官网直供安卓安装包，支持门户免密一键导入服务器，海报浏览与选集直观顺手。',
  },
  {
    id: 'android-yamby',
    canonicalId: 'yamby',
    platform: 'android',
    name: 'Yamby',
    subtitle: 'Android 手机 · 平板',
    monogram: 'YB',
    tierBadge: '免费首选',
    priceLabel: '核心功能免费（限 3 个服）',
    isFreeTier: true,
    downloadLabel: '官方频道下载',
    downloadUrl: 'https://t.me/yamby_release',
    importTarget: null,
    tutorialSlug: 'yamby',
    summary: '专为安卓打造的轻量原生 Emby 客户端，手机端请下载 arm64-v8a 安装包。',
  },
  {
    id: 'android-capyplayer',
    canonicalId: 'capyplayer',
    platform: 'android',
    name: 'CapyPlayer',
    subtitle: '卡皮巴拉 · Android 手机 · 平板',
    monogram: 'CP',
    priceLabel: '基础免费 · Pro ¥22/年 / ¥38买断',
    isFreeTier: true,
    downloadLabel: '中文官网下载',
    downloadUrl: 'https://capyplayer.feifeiduck.com/zh',
    importTarget: 'CapyPlayer',
    tutorialSlug: 'capyplayer',
    summary: '支持安卓端免密一键导入服务器，连接 Emby 基础播放免费。',
  },
  {
    id: 'android-afusekt',
    canonicalId: 'afusekt',
    platform: 'android',
    name: 'AfuseKt',
    subtitle: 'Android 手机 · 平板',
    monogram: 'AF',
    priceLabel: '基础免费 · Pro ¥10/月 / ¥69+买断',
    isFreeTier: true,
    downloadLabel: '官网 APK 下载',
    downloadUrl: 'https://www.afusekt.cn/AfuseKt/',
    importTarget: null,
    tutorialSlug: 'afusekt',
    summary: '内置 Exo / MPV / VLC 三播放内核，遇特殊杜比或 ASS 特效字幕可随时一键切核。',
  },
  {
    id: 'android-hills',
    canonicalId: 'hills',
    platform: 'android',
    name: 'Hills',
    subtitle: 'Android 手机 · 平板',
    monogram: 'HL',
    priceLabel: '单服免费 · Pro 内购',
    isFreeTier: true,
    downloadLabel: '官方频道 / 商店',
    downloadUrl: 'https://t.me/Hills_app',
    importTarget: null,
    tutorialSlug: 'hills',
    summary: '只连接本站单台服务器时免费版即可满足日常观影，支持跨库极速搜索。',
  },
  {
    id: 'android-baomi',
    canonicalId: 'baomi',
    platform: 'android',
    name: '网易爆米花',
    subtitle: 'Android 手机 · 平板',
    monogram: 'BM',
    tierBadge: '完全免费',
    priceLabel: '完全免费（需手机号登录）',
    isFreeTier: true,
    downloadLabel: '官方网站下载',
    downloadUrl: 'https://bmh.163.com/',
    importTarget: null,
    tutorialSlug: 'baomi',
    summary: '国内网络直接高速下载安装，零付费门槛；添加时请选「Emby」协议。',
  },
]

const ANDROID_TV_CARDS: PlayerCardSpec[] = [
  {
    id: 'tv-afusekt',
    canonicalId: 'afusekt',
    platform: 'android_tv',
    name: 'AfuseKt TV 版',
    subtitle: 'AfuseKtV · 智能电视 · 电视盒子',
    monogram: 'AF',
    tierBadge: '电视首选',
    priceLabel: '基础免费 · Pro ¥10/月 / ¥69+买断',
    isFreeTier: true,
    downloadLabel: 'TV 专用 APK',
    downloadUrl: 'https://www.afusekt.cn/AfuseKt/',
    importTarget: null,
    tutorialSlug: 'afusekt',
    summary: '独立电视大屏客户端，专为遥控器方向键设计，内置多内核硬解直放。',
    caution: '请下载电视专用版 AfuseKtV（勿装手机版）：老盒子选 TV 32位，新款设备选 TV 64位。',
  },
  {
    id: 'tv-vidhub',
    canonicalId: 'vidhub',
    platform: 'android_tv',
    name: 'VidHub TV 版',
    subtitle: 'Android TV · 电视盒子',
    monogram: 'VH',
    tierBadge: '首选推荐',
    priceLabel: '安卓电视端免费为主',
    isFreeTier: true,
    downloadLabel: '官网 TV APK',
    downloadUrl: 'https://zh.vidhub.okaapps.com/',
    importTarget: null,
    tutorialSlug: 'vidhub',
    summary: '大屏海报墙清晰直观，官网提供 Android TV 安装包直接下载安装。',
  },
  {
    id: 'tv-baomi',
    canonicalId: 'baomi',
    platform: 'android_tv',
    name: '网易爆米花 TV',
    subtitle: 'Android TV · 电视盒子',
    monogram: 'BM',
    tierBadge: '完全免费',
    priceLabel: '完全免费（手机扫码登录）',
    isFreeTier: true,
    downloadLabel: '官网 TV 版下载',
    downloadUrl: 'https://bmh.163.com/',
    importTarget: null,
    tutorialSlug: 'baomi',
    summary: '支持手机扫码快速登录电视端，完全免费，国内网络直下无需折腾。',
  },
  {
    id: 'tv-yamby',
    canonicalId: 'yamby',
    platform: 'android_tv',
    name: 'Yamby',
    subtitle: 'Android TV · 电视盒子',
    monogram: 'YB',
    priceLabel: '核心功能免费',
    isFreeTier: true,
    downloadLabel: '官方频道下载',
    downloadUrl: 'https://t.me/yamby_release',
    importTarget: null,
    tutorialSlug: 'yamby',
    summary: '轻量流畅省内存，对老款低配电视盒子非常友好。',
    caution: '多数电视盒子为 32 位系统，请务必安装 armeabi-v7a（32位）APK，装 v8a 会报解析包错误。',
  },
  {
    id: 'tv-hills',
    canonicalId: 'hills',
    platform: 'android_tv',
    name: 'Hills',
    subtitle: 'Android TV · 电视盒子',
    monogram: 'HL',
    priceLabel: '单服免费 · Pro 内购',
    isFreeTier: true,
    downloadLabel: '官方频道 APK',
    downloadUrl: 'https://t.me/Hills_app',
    importTarget: null,
    tutorialSlug: 'hills',
    summary: '单服务器免费可用，电视上部分按钮可能需要配合遥控器鼠标模式点击。',
  },
  {
    id: 'tv-xiaohuan-note',
    canonicalId: 'xiaohuan',
    platform: 'android_tv',
    name: '小幻影视（电视说明）',
    subtitle: '同名包辨析 · 电视推荐改用 AfuseKtV',
    monogram: 'XH',
    priceLabel: '无官方电视版（Win版 ¥45）',
    isFreeTier: false,
    downloadLabel: '官方说明页',
    downloadUrl: 'https://player.richasy.net/',
    importTarget: null,
    tutorialSlug: 'xiaohuan',
    summary: '正版《小幻影视 Rodel Player》为 Windows 电脑独占，无官方 Android TV 版；电视请优先选 AfuseKtV 或 VidHub。',
  },
]

const WINDOWS_CARDS: PlayerCardSpec[] = [
  {
    id: 'win-xiaohuan',
    canonicalId: 'xiaohuan',
    platform: 'windows',
    name: '小幻影视',
    subtitle: 'Rodel Player · Windows 10 / 11',
    monogram: 'XH',
    tierBadge: '画质首选',
    priceLabel: '本体免费 · Emby解锁 ¥45买断（早鸟¥27）',
    isFreeTier: false,
    downloadLabel: '微软商店下载',
    downloadUrl: 'https://apps.microsoft.com/detail/9nb0h051m4v4?hl=zh-CN',
    importTarget: '小幻影视',
    tutorialSlug: 'xiaohuan',
    summary: 'Windows 原生 WinUI 3 客户端，内置 libmpv 强力解码，支持门户免密一键导入服务器。',
    caution: '需在「在线媒体」页一次性买断解锁 Emby 连接器后方可导入保存；请保持官方默认 mpv 预设。',
  },
  {
    id: 'win-baomi',
    canonicalId: 'baomi',
    platform: 'windows',
    name: '网易爆米花',
    subtitle: 'Windows 10 / 11 桌面端',
    monogram: 'BM',
    tierBadge: '完全免费',
    priceLabel: '完全免费（需手机号登录）',
    isFreeTier: true,
    downloadLabel: '官方网站下载',
    downloadUrl: 'https://bmh.163.com/',
    importTarget: null,
    tutorialSlug: 'baomi',
    summary: '完全免费的 Windows 桌面客户端，官网直装即用；添加资源时请选「Emby」。',
  },
  {
    id: 'win-hills',
    canonicalId: 'hills',
    platform: 'windows',
    name: 'Hills Lite',
    subtitle: 'Windows 10 / 11 微软商店版',
    monogram: 'HL',
    priceLabel: '一次性买断 ¥32–¥46',
    isFreeTier: false,
    downloadLabel: '微软商店下载',
    downloadUrl: 'https://apps.microsoft.com/detail/9nxnzfrllwzx?hl=zh-cn',
    importTarget: null,
    tutorialSlug: 'hills',
    summary: '轻量桌面客户端，微软应用商店直接搜索安装，支持多服聚合浏览。',
  },
]

const HARMONY_CARDS: PlayerCardSpec[] = [
  {
    id: 'harmony-hosplayer',
    canonicalId: 'hosplayer',
    platform: 'harmony',
    name: 'hosplayer',
    subtitle: 'HarmonyOS NEXT · 华为手机 / 平板 / 智慧屏',
    monogram: 'HO',
    tierBadge: '鸿蒙首选',
    priceLabel: '免费',
    isFreeTier: true,
    downloadLabel: '华为应用市场',
    downloadUrl: 'https://appgallery.huawei.com/app/detail?id=com.colorlifee.hosplayer',
    importTarget: null,
    tutorialSlug: 'hosplayer',
    summary: '纯血鸿蒙原生第三方 Emby 播放器，在华为「应用市场」直接搜索 hosplayer 安装即可直连播放。',
    caution: '若设备为兼容安卓 APK 的 HarmonyOS 4.x 机型，也可直接选用「安卓手机」页签下的 VidHub 或 AfuseKt。',
  },
]

export const PLATFORM_CARDS_MAP: Record<Exclude<ClientPlatformKey, 'all'>, readonly PlayerCardSpec[]> = {
  apple: APPLE_CARDS,
  android: ANDROID_PHONE_CARDS,
  android_tv: ANDROID_TV_CARDS,
  windows: WINDOWS_CARDS,
  harmony: HARMONY_CARDS,
}

/** 16 款去重全集（按代表性主平台优先抽取，保证 win-xiaohuan 等带一键导入的主卡入选） */
export const ALL_16_PLAYERS: readonly PlayerCardSpec[] = (() => {
  const orderedPool: PlayerCardSpec[] = [
    ...APPLE_CARDS,
    ...WINDOWS_CARDS,
    ...ANDROID_PHONE_CARDS,
    ...ANDROID_TV_CARDS,
    ...HARMONY_CARDS,
  ]
  const seen = new Set<string>()
  const out: PlayerCardSpec[] = []
  for (const card of orderedPool) {
    // 小幻影视在全量视图取 Windows 官方主卡（win-xiaohuan），跳过 TV 说明卡
    if (card.id === 'tv-xiaohuan-note') continue
    if (!seen.has(card.canonicalId)) {
      seen.add(card.canonicalId)
      out.push(card)
    }
  }
  return out
})()

export const PLATFORM_TABS: readonly PlatformTabSpec[] = [
  { key: 'apple', label: '苹果', deviceHint: 'iPhone / iPad / Apple TV / Mac', count: APPLE_CARDS.length },
  { key: 'android', label: '安卓手机', deviceHint: 'Android 手机 / 平板', count: ANDROID_PHONE_CARDS.length },
  { key: 'android_tv', label: '安卓电视', deviceHint: 'Android TV / 电视盒子', count: ANDROID_TV_CARDS.length },
  { key: 'windows', label: 'Windows', deviceHint: 'Windows 10 / 11 电脑', count: WINDOWS_CARDS.length },
  { key: 'harmony', label: '鸿蒙', deviceHint: 'HarmonyOS NEXT', count: HARMONY_CARDS.length },
  { key: 'all', label: '全部 16 款', deviceHint: '全平台去重总览', count: ALL_16_PLAYERS.length },
]

export function getCardsForPlatform(platform: ClientPlatformKey): readonly PlayerCardSpec[] {
  if (platform === 'all') return ALL_16_PLAYERS
  return PLATFORM_CARDS_MAP[platform] ?? APPLE_CARDS
}

export function detectDefaultPlatform(ua?: string | null): ClientPlatformKey {
  const s = (ua || '').toLowerCase()
  if (!s) return 'apple'
  if (s.includes('openharmony') || (s.includes('harmonyos') && !s.includes('android'))) return 'harmony'
  if (s.includes('aft') || s.includes('googletv') || s.includes('android tv') || s.includes('bravia') || s.includes('mibox')) {
    return 'android_tv'
  }
  if (s.includes('android')) return 'android'
  if (s.includes('iphone') || s.includes('ipad') || s.includes('macintosh') || s.includes('appletv')) return 'apple'
  if (s.includes('windows')) return 'windows'
  return 'apple'
}
