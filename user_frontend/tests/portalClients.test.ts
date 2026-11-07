import assert from 'node:assert/strict'
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import test from 'node:test'

import { ALL_16_PLAYERS, detectDefaultPlatform, getCardsForPlatform, PLATFORM_TABS } from '../src/utils/playerCatalog.ts'
import {
  buildPasswordlessImportSchemes,
  findPlayerImportScheme,
  isImportScheme,
  isPlayerDeepLink,
  listImportSchemes,
  parseEmbyServerAddress,
} from '../src/utils/playerSchemes.ts'
import { renderTutorialMarkdown } from '../src/utils/tutorialMarkdown.ts'

const feRoot = join(import.meta.dirname, '..')

function decodeBase64UrlJson(b64url: string): Record<string, unknown> {
  const norm = b64url.replace(/-/g, '+').replace(/_/g, '/')
  const pad = '='.repeat((4 - (norm.length % 4)) % 4)
  return JSON.parse(Buffer.from(norm + pad, 'base64').toString('utf-8')) as Record<string, unknown>
}

test('免密一键导入协议：完整生成 6 款客户端链接（含 Forward、CapyPlayer、小幻影视），且绝不含密码字段', () => {
  const ep = parseEmbyServerAddress('https://emby.fanqiao.net/emby/')
  assert.deepEqual(ep, {
    scheme: 'https',
    host: 'emby.fanqiao.net',
    port: 443,
    rootUrl: 'https://emby.fanqiao.net',
  })

  const map = buildPasswordlessImportSchemes(
    'https://emby.fanqiao.net',
    'linxi_01',
    '帆桥影院',
    {
      // 模拟旧后端或脏链接误带 password，验证防御性剥离
      VidHub: 'vidhub://import?type=emby&scheme=https&host=emby.fanqiao.net&port=443&username=linxi_01&password=SHOULD_BE_STRIPPED',
    },
  )
  const schemes = listImportSchemes(map)
  assert.equal(schemes.length, 6)

  for (const s of schemes) {
    assert.equal(isImportScheme(s.url), true, `invalid scheme: ${s.url}`)
    assert.equal(isPlayerDeepLink(s.url), true, `invalid deeplink: ${s.url}`)
    assert.equal(/password|pwd|SHOULD_BE_STRIPPED/i.test(s.url), false, `password leaked in ${s.name}: ${s.url}`)
  }

  const forward = findPlayerImportScheme(schemes, 'Forward')
  assert.equal(
    forward?.url,
    'forward://import?type=emby&scheme=https&host=emby.fanqiao.net&port=443&username=linxi_01&title=%E5%B8%86%E6%A1%A5%E5%BD%B1%E9%99%A2',
  )

  const rodel = findPlayerImportScheme(schemes, '小幻影视')
  assert.equal(
    rodel?.url,
    'rodelplayer://import?type=emby&title=%E5%B8%86%E6%A1%A5%E5%BD%B1%E9%99%A2&scheme=https&host=emby.fanqiao.net&port=443&username=linxi_01',
  )

  const capy = findPlayerImportScheme(schemes, 'CapyPlayer')
  assert.ok(capy?.url.startsWith('com.feifeiduck.capyplayer://add-resource?data='))
  const b64Payload = capy!.url.slice('com.feifeiduck.capyplayer://add-resource?data='.length)
  assert.equal(b64Payload.includes('='), false)
  assert.equal(b64Payload.includes('+'), false)
  assert.equal(b64Payload.includes('/'), false)
  const decoded = decodeBase64UrlJson(b64Payload)
  assert.deepEqual(decoded, {
    serverType: 'emby',
    host: 'emby.fanqiao.net',
    port: 443,
    https: true,
    username: 'linxi_01',
  })
  assert.equal('password' in decoded, false)
})

test('协议安全白名单：拒绝 javascript: / http(s): / 空白字符注入', () => {
  assert.equal(isImportScheme('javascript:alert(1)'), false)
  assert.equal(isImportScheme('https://emby.fanqiao.net'), false)
  assert.equal(isImportScheme('forward://import?host=a b'), false)
  assert.equal(buildPasswordlessImportSchemes('https://emby.fanqiao.net', '').Forward, undefined)
})

test('客户端目录与五端分页签：16 款去重完整且平台嗅探准确', () => {
  assert.equal(ALL_16_PLAYERS.length, 16)
  assert.deepEqual(
    PLATFORM_TABS.map((t) => t.key),
    ['apple', 'android', 'android_tv', 'windows', 'harmony', 'all'],
  )
  assert.equal(getCardsForPlatform('apple').length, 11)
  assert.equal(getCardsForPlatform('windows').some((c) => c.importTarget === '小幻影视'), true)
  assert.equal(getCardsForPlatform('android_tv').find((c) => c.id === 'tv-xiaohuan-note')?.importTarget, null)

  assert.equal(detectDefaultPlatform('Mozilla/5.0 (Windows NT 10.0; Win64; x64)'), 'windows')
  assert.equal(detectDefaultPlatform('Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)'), 'apple')
  assert.equal(detectDefaultPlatform('Mozilla/5.0 (Linux; Android 14; Pixel 8)'), 'android')
  assert.equal(detectDefaultPlatform('Mozilla/5.0 (Linux; Android 12; BRAVIA 4K VH2)'), 'android_tv')
  assert.equal(detectDefaultPlatform('Mozilla/5.0 (Phone; OpenHarmony 5.0)'), 'harmony')
})

test('17 篇教程 Markdown 安全渲染：剥除内部草稿/截图占位标记、重写站内教程跳转并免疫 XSS', () => {
  const tutDir = join(feRoot, 'src/content/playerTutorials')
  const mdFiles = readdirSync(tutDir).filter((f) => f.endsWith('.md'))
  assert.equal(mdFiles.length, 17)

  for (const file of mdFiles) {
    const raw = readFileSync(join(tutDir, file), 'utf-8')
    const html = renderTutorialMarkdown(raw)
    assert.ok(html.length > 200, `${file} rendered empty`)
    assert.equal(html.includes('内部草稿'), false, `${file} leaked draft status`)
    assert.equal(html.includes('截图占位'), false, `${file} leaked screenshot placeholder`)
  }

  const readmeHtml = renderTutorialMarkdown(readFileSync(join(tutDir, 'README.md'), 'utf-8'))
  assert.ok(readmeHtml.includes('data-tutorial-slug="infuse"'))
  assert.ok(readmeHtml.includes('<table class="dh-md-table">'))

  const xssHtml = renderTutorialMarkdown(
    '# 测试\n<script>alert(1)</script>\n[恶意](javascript:alert(2))\n[正常](https://apps.apple.com/cn/app/id1)',
  )
  assert.equal(xssHtml.includes('<script>'), false)
  assert.ok(xssHtml.includes('&lt;script&gt;'))
  assert.equal(xssHtml.includes('javascript:'), false)
  assert.ok(xssHtml.includes('href="https://apps.apple.com/cn/app/id1"'))
})
