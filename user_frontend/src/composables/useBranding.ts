/**
 * 站点品牌（能力：站点与品牌）
 *
 * 管理后台「系统设置 → 站点与品牌」里填的站名 / Logo / 主题色 / SEO，在这里落地：
 *
 * - **主题色**：覆盖 Aurora 令牌 `--au-primary` 及其派生值（悬停色、浅底、描边、上面那层文字）。
 *   只改一个 `--au-primary` 是不够的——按钮上的文字色、悬停色、浅底都各有各的令牌，
 *   只换主色会让页面变成「新主色 + 旧配色」，所以这里一起算出来。
 * - **标题与 meta**：浏览器标题、描述、关键词。
 *
 * 拉取失败（后端没起、老版本后端没有这个端点）时**保持默认值**：品牌信息不该把页面拖住。
 */
import { reactive } from 'vue'
import { brandingApi, type Branding } from '@/api'

export const DEFAULT_SITE_NAME = '帆桥影院'
export const DEFAULT_THEME_COLOR = '#c3f53c'

/**
 * 旧主题（Aurora 电光青）的默认主题色。暗房影院改版起用户端换成「暗房影院」琥珀主题，
 * 后端 / 管理后台的默认值仍是这支青色——没改过主题色的站点会原样下发它。
 * 这种情况视作「没有自定义」，交给主题自己的琥珀令牌，不再注入覆盖；
 * 管理员真的填了别的颜色才按品牌色覆盖。
 */
const LEGACY_DEFAULT_THEME_COLORS = new Set(['#22d3ee', '#e8a84a', DEFAULT_THEME_COLOR])

const BRANDING_CACHE_KEY = 'aetrix:branding:v1'

export function normalizeBrandingInfo(info?: Partial<Branding> | null): Branding {
  const rawName = (info?.site_name || '').trim()
  const siteName =
    !rawName || rawName === 'Aetrix' || rawName === '帆桥' ? DEFAULT_SITE_NAME : rawName
  return {
    site_name: siteName,
    logo_url: (info?.logo_url || '').trim(),
    theme_color: (info?.theme_color || '').trim() || DEFAULT_THEME_COLOR,
    seo_title: info?.seo_title || '',
    seo_description: info?.seo_description || '',
    seo_keywords: info?.seo_keywords || '',
    gamification_enabled: info?.gamification_enabled !== false,
    collections_enabled: Boolean(info?.collections_enabled),
    home_sections_enabled: Boolean(info?.home_sections_enabled),
    series_follow_enabled: Boolean(info?.series_follow_enabled),
  }
}

function readInitialBranding(): Branding {
  if (typeof window !== 'undefined') {
    const injected = (window as unknown as { __AETRIX_BOOT__?: Partial<Branding> }).__AETRIX_BOOT__
    if (injected && typeof injected === 'object') {
      return normalizeBrandingInfo(injected)
    }
    try {
      const raw = localStorage.getItem(BRANDING_CACHE_KEY)
      if (raw) {
        return normalizeBrandingInfo(JSON.parse(raw) as Partial<Branding>)
      }
    } catch {
      /* ignore */
    }
  }
  return normalizeBrandingInfo(null)
}

export const branding = reactive<Branding>(readInitialBranding())

let initPromise: Promise<void> | null = null
/** 页面标题：`页面名 - 站名`（没有页面名时用 SEO 标题或站名） */
export function pageTitle(page?: string): string {
  if (page) return `${page} - ${branding.site_name}`
  return branding.seo_title || branding.site_name
}

function hexToRgb(hex: string): [number, number, number] {
  let value = hex.replace('#', '').trim()
  if (value.length === 3) value = value.split('').map((c) => c + c).join('')
  if (value.length === 8) value = value.slice(0, 6)   // 忽略 alpha，透明度由各令牌自己定
  const num = Number.parseInt(value, 16)
  if (!Number.isFinite(num) || value.length !== 6) return [232, 168, 74]
  return [(num >> 16) & 255, (num >> 8) & 255, num & 255]
}

function rgba(rgb: [number, number, number], alpha: number): string {
  return `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${alpha})`
}

/** 变暗（悬停/按下态用）：按比例压暗，比「调透明度」在深色底上更自然 */
function darken(rgb: [number, number, number], factor: number): string {
  const part = (v: number) => Math.max(0, Math.min(255, Math.round(v * factor)))
  return `rgb(${part(rgb[0])}, ${part(rgb[1])}, ${part(rgb[2])})`
}

/** 主色上的文字色：按亮度选深/浅，保证对比度 */
function onColor(rgb: [number, number, number]): string {
  const luminance = (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255
  return luminance > 0.6 ? '#1a1205' : '#ffffff'
}

function luminance(rgb: [number, number, number]): number {
  return (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255
}

function toHex(rgb: [number, number, number]): string {
  const part = (v: number) => v.toString(16).padStart(2, '0')
  return `#${part(rgb[0])}${part(rgb[1])}${part(rgb[2])}`
}

/** 品牌色注入用的 <style>（随主题切换分叉深浅两套，见 applyBrandingTokens） */
let brandStyle: HTMLStyleElement | null = null

function setMeta(name: string, content: string) {
  if (!content) return
  let tag = document.querySelector<HTMLMetaElement>(`meta[name="${name}"]`)
  if (!tag) {
    tag = document.createElement('meta')
    tag.setAttribute('name', name)
    document.head.appendChild(tag)
  }
  tag.setAttribute('content', content)
}

function setMetaProperty(prop: string, content: string) {
  if (!content) return
  let tag = document.querySelector<HTMLMetaElement>(`meta[property="${prop}"]`)
  if (!tag) {
    tag = document.createElement('meta')
    tag.setAttribute('property', prop)
    document.head.appendChild(tag)
  }
  tag.setAttribute('content', content)
}

/** 支持后台「站点与品牌 → Logo 图片地址」动态覆盖浏览器标签页图标与分享卡片 */
function syncFaviconAndSocial() {
  if (typeof document === 'undefined') return
  const customLogo = (branding.logo_url || '').trim()
  const iconHref = customLogo || '/favicon.svg'
  const touchHref = customLogo || '/apple-touch-icon.png'
  const isSvg = iconHref.toLowerCase().split('?')[0].endsWith('.svg')

  const iconLinks = document.querySelectorAll<HTMLLinkElement>('link[rel="icon"]')
  iconLinks.forEach((link) => {
    link.href = iconHref
    if (isSvg) link.type = 'image/svg+xml'
    else link.removeAttribute('type')
  })
  if (customLogo) {
    document
      .querySelectorAll<HTMLLinkElement>('link[rel="alternate icon"]')
      .forEach((link) => {
        link.href = customLogo
      })
  }
  document
    .querySelectorAll<HTMLLinkElement>('link[rel="apple-touch-icon"]')
    .forEach((link) => {
      link.href = touchHref
    })

  const desc =
    branding.seo_description || '帆桥影院：高清片库在线浏览、多端播放器一键直连与个人观影管理。'
  setMeta('description', desc)
  setMeta('keywords', branding.seo_keywords)
  setMetaProperty('og:site_name', branding.site_name)
  setMetaProperty('og:title', document.title || branding.site_name)
  setMetaProperty('og:description', desc)
  setMetaProperty('og:image', customLogo || '/og-image.png')
}

/** 把品牌信息写进文档（主题色令牌 / 标题 / 动态 favicon / OG meta） */
export function applyBranding() {
  applyBrandingTokens()

  const sepIdx = document.title.lastIndexOf(' - ')
  if (sepIdx > 0) {
    document.title = `${document.title.slice(0, sepIdx)} - ${branding.site_name}`
  } else {
    document.title = pageTitle()
  }
  syncFaviconAndSocial()
}

/**
 * 品牌主题色注入（v2.42.3 改造）：不再写 documentElement.style（inline 自定义属性
 * 优先级最高，双主题的浅色覆盖永远赢不了它——外观切到白日时主色仍是亮青，
 * 白底上发白看不清）。改为注入一段 <style>，深浅各一条规则：
 *
 * - 深色（:root）用管理员填的原色；
 * - 浅色（html[data-theme='light']）把品牌色连乘压暗到白底可读（亮度阈值 0.42），
 *   派生色（hover/浅底/描边/文字色）跟着有效色重算，对比度始终成立。
 *
 * 选择器用 :root:root 提高特异性，防后续注入的同特异性样式表抢赢；
 * 主题切换只改 html 的 data-theme 属性，两套品牌色即时切换，无需重新计算。
 */
function applyBrandingTokens() {
  const color = (branding.theme_color || '').trim().toLowerCase()
  if (!color || LEGACY_DEFAULT_THEME_COLORS.has(color)) {
    // 默认主题色：不注入，主题令牌（深色琥珀 / 浅色深琥珀）自己说了算
    if (brandStyle) brandStyle.textContent = ''
    return
  }
  const rgb = hexToRgb(branding.theme_color)

  let light = [...rgb] as [number, number, number]
  for (let i = 0; i < 6 && luminance(light) > 0.42; i++) {
    light = [
      Math.max(0, Math.min(255, Math.round(light[0] * 0.82))),
      Math.max(0, Math.min(255, Math.round(light[1] * 0.82))),
      Math.max(0, Math.min(255, Math.round(light[2] * 0.82))),
    ]
  }

  const darkRules = [
    `--au-primary:${branding.theme_color}`,
    `--au-primary-strong:${darken(rgb, 0.85)}`,
    `--au-primary-deep:${darken(rgb, 0.55)}`,
    `--au-primary-soft:${rgba(rgb, 0.12)}`,
    `--au-primary-mid:${rgba(rgb, 0.2)}`,
    `--au-primary-border:${rgba(rgb, 0.28)}`,
    '--au-primary-glow:transparent',
    `--au-on-primary:${onColor(rgb)}`,
    `--au-border-focus:${rgba(rgb, 0.55)}`,
  ].join(';')

  const lightRules = [
    `--au-primary:${toHex(light)}`,
    `--au-primary-strong:${darken(light, 0.85)}`,
    `--au-primary-deep:${darken(light, 0.55)}`,
    `--au-primary-soft:${rgba(light, 0.1)}`,
    `--au-primary-mid:${rgba(light, 0.16)}`,
    `--au-primary-border:${rgba(light, 0.26)}`,
    '--au-primary-glow:transparent',
    `--au-on-primary:${onColor(light)}`,
    `--au-border-focus:${rgba(light, 0.55)}`,
  ].join(';')

  if (!brandStyle) {
    brandStyle = document.createElement('style')
    brandStyle.id = 'brand-tokens'
    document.head.appendChild(brandStyle)
  }
  brandStyle.textContent = `:root:root{${darkRules}}:root:root[data-theme='light']{${lightRules}}`
}

function persistBrandingCache(next: Branding): void {
  try {
    localStorage.setItem(BRANDING_CACHE_KEY, JSON.stringify(next))
  } catch {
    /* ignore */
  }
}

async function fetchBrandingSafe(): Promise<Partial<Branding> | null> {
  try {
    return await brandingApi.get()
  } catch {
    return null
  }
}

/** 启动前拉取并落位（幂等；优先复用 index.html 并行预拉结果，自带 2.5s 超时保护不卡死首屏） */
export function initBranding(): Promise<void> {
  if (initPromise) return initPromise
  initPromise = (async () => {
    try {
      const w =
        typeof window !== 'undefined'
          ? (window as unknown as {
              __AETRIX_BOOT__?: Partial<Branding> | null
              __AETRIX_BOOT_PROMISE__?: Promise<Partial<Branding> | null>
            })
          : null

      let info: Partial<Branding> | null = null
      if (w?.__AETRIX_BOOT__ && typeof w.__AETRIX_BOOT__ === 'object') {
        info = w.__AETRIX_BOOT__
      } else if (w?.__AETRIX_BOOT_PROMISE__) {
        info = (await w.__AETRIX_BOOT_PROMISE__) || (await fetchBrandingSafe())
      } else {
        info = await fetchBrandingSafe()
      }
      if (info && typeof info === 'object') {
        const normalized = normalizeBrandingInfo(info)
        Object.assign(branding, normalized)
        persistBrandingCache(normalized)
      }
    } catch {
      /* 拉取失败保持预热/默认品牌配置 */
    }
    applyBranding()
  })()
  return initPromise
}
