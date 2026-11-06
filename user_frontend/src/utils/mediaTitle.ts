import type { EmbyItem } from '@/api/emby'

const CJK_RE = /[\u4e00-\u9fff]/
const BOOK_TITLE_RE = /《([^》]{1,200})》/g
const BRACKET_NOISE_RE =
  /\[[^\]]*(?:字幕|音轨|配音|双语|国粤|国英|简繁|中英|特效|合集|tmdbid?|tmdb)[^\]]*\]|\{tmdb(?:id)?[-=]\d+\}/gi
const CASE_SENSITIVE_GROUP_RE = /(?:\b[A-Za-z0-9_]+@[A-Za-z0-9_]+\b|\bbeAst\b)/g
const PT_JUNK_RE =
  /\b(?:2160p|1440p|1080[ip]|720p|480p|4k|8k|uhd|hdr10\+?|hdr|sdr|dv|dolby|hevc|avc|[hx][.\s]?26[45]|blu[\s-]?ray|bluray|remux|web[\s-]?dl|webrip|bdrip|brrip|hdtv|dvdrip|bdmv|truehd(?:\s*\d[.\s]\d)?|dts(?:[-\s]?hd(?:[.\s]?ma|[.\s]?hra?)?)?(?:[.\s]?\d[.\s]?\d)?|dtshd(?:ma)?(?:[.\s]?\d[.\s]?\d)?|hdma(?:[.\s]?\d[.\s]?\d)?|hd\s+ma(?:\s+\d[\s.]\d)?|ddp?[.\s]?\d[.\s]?\d|(?:eac3|ac3|aac|flac)(?:[.\s]?\d[.\s]?\d)?|atmos|(?:8|10|12)\s*bit|(?:[2-9]|multi|dual|2ual)audios?|pgs|repack|proper|complete|minihd|mnhd|beitai|chdweb|chdbits|tagweb|taghd|momoweb|momohd|ctrlweb|ctrlhd|enthd|bathd|homeweb|homehd|hdbweb|frds|adweb|ourtv|harestv|hhweb|mweb|ttg|2d3d)\b/gi
const TAIL_SHORT_TAG_RE =
  /\s+[\[(（【]?\s*(?:hktv|hk|tw|us|gb|kr|jp|cn|gl|cee|dc|alt|chd|xiaomi|ma|hd)\s*[\])）】]?\s*$/i
const ROMAN_RE = /^(?:I{1,3}|IV|VI{0,3}|IX|XI{0,2}|[\u2160-\u216b])$/
const BILINGUAL_SPLIT_RE =
  /^([\u4e00-\u9fff0-9][\u4e00-\u9fff0-9A-Za-z\u2160-\u216b：:·・，,！!？?—\-（）().\s]{0,60}?[\u4e00-\u9fff0-9）)\u2160-\u216b](?:\s+(?:I{2,3}|IV|VI{0,3}|IX|XI{0,2}))?)\s+([A-Za-z][A-Za-z0-9'’:,.\-\s]*)$/

export function hasCjk(text?: string | null): boolean {
  return !!text && CJK_RE.test(text)
}

function isEnglishTranslationTail(tail: string): boolean {
  const t = tail.replace(/^[.\-_:：()[\]\s]+|[.\-_:：()[\]\s]+$/g, '')
  if (!t || hasCjk(t) || ROMAN_RE.test(t)) return false
  const noYear = t.replace(/\b(?:19|20)\d{2}\b/g, ' ').trim()
  const words = (noYear.match(/[A-Za-z0-9]+/g) || []).filter((w) => !ROMAN_RE.test(w))
  const alphaWords = words.filter((w) => /[A-Za-z]/.test(w))
  if (alphaWords.length >= 2) return true
  if (alphaWords.length === 1 && alphaWords[0]!.length >= 4) return true
  return false
}

export function cleanPtTitle(raw?: string | null): string {
  const src = (raw || '').trim()
  if (!src) return ''
  let s = src.replace(BRACKET_NOISE_RE, ' ')
  s = s.replace(CASE_SENSITIVE_GROUP_RE, ' ')
  s = s.replace(PT_JUNK_RE, ' ')
  // 仅保护片名版本号小数点（如 梅根2.0 / 哥斯拉-1.0 / 0.0兆赫），把 .2025 / .M3GAN 等文件名分隔符转空格
  s = s.replace(/(?<!\d)\.|(?<=\d)\.(?!\d(?!\d))|[_+]+/g, ' ')
  s = s.replace(/(?<![\d.])\b(?:1\s0|2\s0|5\s1|7\s1)\b(?![\d.])/g, ' ')
  s = s.replace(/\s+/g, ' ').replace(/^[.\-_\s]+|[.\-_\s]+$/g, '')
  let prev = ''
  while (s && s !== prev) {
    prev = s
    s = s.replace(TAIL_SHORT_TAG_RE, ' ').replace(/\s+/g, ' ').replace(/^[.\-_\s]+|[.\-_\s]+$/g, '')
  }
  const cand = s || src
  const m = BILINGUAL_SPLIT_RE.exec(cand)
  if (m && m[1] && m[2] && hasCjk(m[1]) && isEnglishTranslationTail(m[2])) {
    const cjkHead = m[1].replace(/\s+/g, ' ').replace(/^[.\-_:：\s]+|[.\-_:：\s]+$/g, '')
    if (hasCjk(cjkHead) && cjkHead.length >= 2) return cjkHead
  }
  if (!hasCjk(cand) && (src.includes('.') || cand !== src)) {
    const noYr = cand.replace(/\s+(?:19|20)\d{2}$/, '').replace(/^[.\-_\s]+|[.\-_\s]+$/g, '')
    if (noYr && /[A-Za-z]/.test(noYr)) return noYr
  }
  return cand
}

export function cleanOriginalSubtitle(orig?: string | null, mainTitle?: string | null): string {
  const raw = (orig || '').trim()
  if (!raw) return ''
  // 先剥除压制参数尾巴，再看是否为「中文扫描串 + 英文原名」（如 `007 23 天幕杀机 Skyfall 2Audio HD MA beAst` → `Skyfall`）
  let stripped = raw
    .replace(BRACKET_NOISE_RE, ' ')
    .replace(CASE_SENSITIVE_GROUP_RE, ' ')
    .replace(PT_JUNK_RE, ' ')
    .replace(/(?<!\d)\.|(?<=\d)\.(?!\d(?!\d))|[_+]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
  let prev = ''
  while (stripped && stripped !== prev) {
    prev = stripped
    stripped = stripped.replace(TAIL_SHORT_TAG_RE, ' ').replace(/\s+/g, ' ').trim()
  }
  const m = BILINGUAL_SPLIT_RE.exec(stripped)
  if (m && m[2] && isEnglishTranslationTail(m[2])) {
    const eng = m[2].replace(/\s+(?:19|20)\d{2}$/, '').trim()
    if (eng && eng !== mainTitle) return eng
  }
  const cleaned = cleanPtTitle(raw)
  if (!cleaned || cleaned === (mainTitle || '').trim()) return ''
  if (hasCjk(cleaned) && hasCjk(mainTitle)) return ''
  return cleaned
}

export function displayMediaTitle(
  item?: Partial<EmbyItem> & {
    DisplayTitle?: string | null
    OriginalTitle?: string | null
    Aliases?: string[] | null
    name?: string | null
    title?: string | null
  } | null,
): string {
  if (!item) return '未命名影片'
  if (item.DisplayTitle && item.DisplayTitle.trim()) {
    return item.DisplayTitle.trim()
  }
  const aliases = Array.isArray(item.Aliases) ? item.Aliases : []
  for (const alias of aliases) {
    if (hasCjk(alias)) return cleanPtTitle(alias) || alias.trim()
  }
  const rawName = (item.Name || item.name || item.title || '').trim()
  if (hasCjk(rawName)) return cleanPtTitle(rawName) || rawName
  if (hasCjk(item.OriginalTitle)) return cleanPtTitle(item.OriginalTitle) || item.OriginalTitle!.trim()
  if (aliases.length > 0 && aliases[0]?.trim()) {
    return cleanPtTitle(aliases[0]) || aliases[0].trim()
  }
  return cleanPtTitle(rawName) || rawName || '未命名影片'
}

export function formatFeedText(raw?: string | null): string {
  const text = (raw || '').trim()
  if (!text || !text.includes('《')) return text
  return text.replace(BOOK_TITLE_RE, (_full, inner: string) => {
    const cleaned = cleanPtTitle(inner)
    return `《${cleaned || inner.trim()}》`
  })
}

export function resolutionBadge(item?: Partial<EmbyItem> | null): string {
  const w = item?.Width || 0
  const h = item?.Height || 0
  if (w >= 3800 || h >= 2100) return '4K'
  if (w >= 1900 || h >= 1000) return '1080P'
  if (w >= 1200 || h >= 700) return '720P'
  return ''
}
