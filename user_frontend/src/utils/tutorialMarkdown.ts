export interface TutorialRenderOptions {
  /** 当前用户的真实服务器地址（可选，仅用于替换示例展示或保持原文） */
  serverUrl?: string
}

function escapeHtml(raw: string): string {
  return raw
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function sanitizeHref(rawHref: string): { kind: 'tutorial'; slug: string } | { kind: 'external'; url: string } | null {
  const trimmed = rawHref.trim()
  const tutMatch = /^\.?\/?([a-z0-9_-]+)\.md(?:#.*)?$/i.exec(trimmed)
  if (tutMatch && tutMatch[1]) {
    const slug = tutMatch[1].toLowerCase() === 'readme' ? 'readme' : tutMatch[1].toLowerCase()
    return { kind: 'tutorial', slug }
  }
  if (/^https?:\/\/[^\s<>"']+$/i.test(trimmed)) {
    return { kind: 'external', url: trimmed }
  }
  return null
}

function renderInline(rawText: string): string {
  // 1. Extract inline code spans first so inner asterisks/brackets are not parsed as markdown
  const codeTokens: string[] = []
  const withPlaceholders = rawText.replace(/`([^`]+)`/g, (_m, code: string) => {
    const idx = codeTokens.length
    codeTokens.push(`<code class="dh-md-code">${escapeHtml(code)}</code>`)
    return `\u0000CODE${idx}\u0000`
  })

  // 2. Parse markdown links [label](href) on unescaped text, escaping parts safely
  let out = ''
  const linkRe = /\[([^\]]+)\]\(([^)]+)\)/g
  let lastIdx = 0
  let m: RegExpExecArray | null
  while ((m = linkRe.exec(withPlaceholders)) !== null) {
    out += formatBold(escapeHtml(withPlaceholders.slice(lastIdx, m.index)))
    const labelHtml = formatBold(escapeHtml(m[1] || ''))
    const target = sanitizeHref(m[2] || '')
    if (!target) {
      out += labelHtml
    } else if (target.kind === 'tutorial') {
      const safeSlug = escapeHtml(target.slug)
      out += `<a href="#tutorial:${safeSlug}" data-tutorial-slug="${safeSlug}" class="dh-md-link is-internal">${labelHtml}</a>`
    } else {
      const safeUrl = escapeHtml(target.url)
      out += `<a href="${safeUrl}" target="_blank" rel="noopener noreferrer" class="dh-md-link">${labelHtml} ↗</a>`
    }
    lastIdx = m.index + m[0].length
  }
  out += formatBold(escapeHtml(withPlaceholders.slice(lastIdx)))

  // 3. Restore inline code tokens
  return out.replace(/\u0000CODE(\d+)\u0000/g, (_m, i: string) => codeTokens[Number(i)] || '')
}

function formatBold(escaped: string): string {
  return escaped.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
}

function isInternalDraftMetaLine(trimmed: string): boolean {
  if (/^>\s*状态[:：]/.test(trimmed)) return true
  if (/^>\s*核验日期[:：]/.test(trimmed)) return true
  if (/^>\s*适用站点[:：]/.test(trimmed)) return true
  if (/^\[截图占位[:：].*\]$/.test(trimmed)) return true
  return false
}

function splitTableCells(rowLine: string): string[] {
  const clean = rowLine.trim().replace(/^\|/, '').replace(/\|$/, '')
  return clean.split('|').map((c) => c.trim())
}

function isTableDivider(line: string): boolean {
  const trimmed = line.trim()
  if (!trimmed.includes('|')) return false
  const cells = splitTableCells(trimmed)
  return cells.length >= 2 && cells.every((c) => /^:?-{2,}:?$/.test(c))
}

/**
 * 将 docs/player-tutorials/*.md 安全编译为暗厅排版 HTML：
 * - 全量转义原始 HTML 标签，拒绝 javascript:/data: 链接（零 XSS）；
 * - 自动剥除内部编辑草稿头（状态：内部草稿 / 核验日期）与 [截图占位：...] 标记；
 * - 将站内相对教程链接 `./infuse.md` 转为 `data-tutorial-slug="infuse"` 供抽屉无缝切换。
 */
export function renderTutorialMarkdown(markdown: string, _opts?: TutorialRenderOptions): string {
  const rawLines = (markdown || '').replace(/\r\n/g, '\n').split('\n')
  const lines = rawLines.filter((line) => !isInternalDraftMetaLine(line.trim()))

  const html: string[] = []
  let i = 0

  while (i < lines.length) {
    const raw = lines[i] ?? ''
    const trimmed = raw.trim()

    if (!trimmed) {
      i += 1
      continue
    }

    // Horizontal rule
    if (/^-{3,}$/.test(trimmed)) {
      html.push('<hr class="dh-md-hr" />')
      i += 1
      continue
    }

    // Headings
    const hMatch = /^(#{1,4})\s+(.+)$/.exec(trimmed)
    if (hMatch) {
      const level = Math.min(4, hMatch[1]!.length)
      html.push(`<h${level} class="dh-md-h${level}">${renderInline(hMatch[2]!.trim())}</h${level}>`)
      i += 1
      continue
    }

    // Blockquote
    if (trimmed.startsWith('>')) {
      const quoteLines: string[] = []
      while (i < lines.length && (lines[i] ?? '').trim().startsWith('>')) {
        const qText = (lines[i] ?? '').trim().replace(/^>\s?/, '').trim()
        if (qText) quoteLines.push(renderInline(qText))
        i += 1
      }
      if (quoteLines.length > 0) {
        html.push(`<blockquote class="dh-md-quote"><p>${quoteLines.join('<br />')}</p></blockquote>`)
      }
      continue
    }

    // Pipe table
    if (trimmed.startsWith('|') && i + 1 < lines.length && isTableDivider(lines[i + 1] ?? '')) {
      const headers = splitTableCells(trimmed)
      i += 2
      const rows: string[][] = []
      while (i < lines.length && (lines[i] ?? '').trim().startsWith('|')) {
        rows.push(splitTableCells(lines[i] ?? ''))
        i += 1
      }
      const thead = `<thead><tr>${headers.map((h) => `<th>${renderInline(h)}</th>`).join('')}</tr></thead>`
      const tbody = `<tbody>${rows
        .map(
          (r) =>
            `<tr>${headers.map((_h, ci) => `<td>${renderInline(r[ci] ?? '')}</td>`).join('')}</tr>`,
        )
        .join('')}</tbody>`
      html.push(`<div class="dh-md-table-wrap"><table class="dh-md-table">${thead}${tbody}</table></div>`)
      continue
    }

    // Ordered list (with optional indented sub-bullets / continuation lines)
    if (/^\d+\.\s+/.test(trimmed)) {
      const items: string[] = []
      while (i < lines.length) {
        const curRaw = lines[i] ?? ''
        const curTrim = curRaw.trim()
        if (!curTrim) {
          // Look ahead to see if next non-empty line continues the ordered list or indented sub-item
          let nextIdx = i + 1
          while (nextIdx < lines.length && !(lines[nextIdx] ?? '').trim()) nextIdx += 1
          const nextRaw = lines[nextIdx] ?? ''
          const nextTrim = nextRaw.trim()
          if (/^\d+\.\s+/.test(nextTrim) || (/^\s{2,}[-•*]\s+/.test(nextRaw) && items.length > 0)) {
            i = nextIdx
            continue
          }
          break
        }
        const olMatch = /^\d+\.\s+(.+)$/.exec(curTrim)
        if (olMatch) {
          const bodyParts: string[] = [`<div class="dh-md-li-main">${renderInline(olMatch[1]!)}</div>`]
          const subBullets: string[] = []
          i += 1
          while (i < lines.length) {
            const subRaw = lines[i] ?? ''
            const subTrim = subRaw.trim()
            if (!subTrim) break
            const subMatch = /^\s{2,}[-•*]\s+(.+)$/.exec(subRaw)
            if (subMatch) {
              subBullets.push(`<li>${renderInline(subMatch[1]!.trim())}</li>`)
              i += 1
            } else if (/^\s{2,}(?!\d+\.\s)/.test(subRaw)) {
              bodyParts.push(`<div class="dh-md-li-note">${renderInline(subTrim)}</div>`)
              i += 1
            } else {
              break
            }
          }
          if (subBullets.length > 0) {
            bodyParts.push(`<ul class="dh-md-sublist">${subBullets.join('')}</ul>`)
          }
          items.push(`<li>${bodyParts.join('')}</li>`)
        } else {
          break
        }
      }
      html.push(`<ol class="dh-md-ol">${items.join('')}</ol>`)
      continue
    }

    // Unordered list
    if (/^[-•*]\s+/.test(trimmed)) {
      const items: string[] = []
      while (i < lines.length) {
        const curTrim = (lines[i] ?? '').trim()
        const ulMatch = /^[-•*]\s+(.+)$/.exec(curTrim)
        if (!ulMatch) break
        items.push(`<li>${renderInline(ulMatch[1]!)}</li>`)
        i += 1
      }
      html.push(`<ul class="dh-md-ul">${items.join('')}</ul>`)
      continue
    }

    // Paragraph
    const paraLines: string[] = []
    while (i < lines.length) {
      const pTrim = (lines[i] ?? '').trim()
      if (
        !pTrim ||
        /^#{1,4}\s+/.test(pTrim) ||
        /^-{3,}$/.test(pTrim) ||
        pTrim.startsWith('>') ||
        pTrim.startsWith('|') ||
        /^\d+\.\s+/.test(pTrim) ||
        /^[-•*]\s+/.test(pTrim)
      ) {
        break
      }
      paraLines.push(renderInline(pTrim))
      i += 1
    }
    if (paraLines.length > 0) {
      html.push(`<p class="dh-md-p">${paraLines.join(' ')}</p>`)
    }
  }

  return html.join('\n')
}
