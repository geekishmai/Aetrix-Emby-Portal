import { cleanPtTitle } from '../src/utils/mediaTitle.ts'
import { shareAccountCardFlight } from '../src/api/imageKey.ts'
import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import test from 'node:test'

const feRoot = join(import.meta.dirname, '..')

test('帆桥暗厅全局契约：首帧 index.html 锁定 #111111 暗厅底且不再写入 data-theme=light', () => {
  const indexHtml = readFileSync(join(feRoot, 'index.html'), 'utf-8')
  assert.match(indexHtml, /meta\s+name="theme-color"\s+content="#111111"/i)
  assert.match(indexHtml, /meta\s+name="color-scheme"\s+content="dark"/i)
  assert.equal(indexHtml.includes("setAttribute('data-theme', 'light')"), false)
  assert.equal(indexHtml.includes('#faf8f5'), false)
})

test('帆桥暗厅主题锁定：useTheme / router / AppHeader 全程保持暗厅且隐藏亮暗切换', () => {
  const themeSrc = readFileSync(join(feRoot, 'src/composables/useTheme.ts'), 'utf-8')
  assert.equal(themeSrc.includes("setAttribute('data-theme', 'light')"), false)
  assert.ok(themeSrc.includes("removeAttribute('data-theme')"))

  const routerSrc = readFileSync(join(feRoot, 'src/router/index.ts'), 'utf-8')
  assert.equal(routerSrc.includes("setAttribute('data-theme', 'light')"), false)

  const headerSrc = readFileSync(join(feRoot, 'src/components/AppHeader.vue'), 'utf-8')
  assert.match(headerSrc, /const\s+showThemeToggle\s*=\s*computed\(\(\)\s*=>\s*false\)/)
})

test('帆桥暗厅样式层：App.vue 唯一引入 dark-hall.css，旧暖纸增强层 portal-ui-p1.css 已退场', () => {
  assert.equal(existsSync(join(feRoot, 'src/styles/portal-ui-p1.css')), false)
  const appSrc = readFileSync(join(feRoot, 'src/App.vue'), 'utf-8')
  assert.ok(appSrc.includes("import './styles/dark-hall.css'"))

  const dhCss = readFileSync(join(feRoot, 'src/styles/dark-hall.css'), 'utf-8')
  for (const hex of ['#111111', '#171717', '#1e1e1e', '#f2f2f2', '#9a9a9a', '#2a2a2a', '#c3f53c']) {
    assert.ok(dhCss.toLowerCase().includes(hex), `missing Dark Hall token ${hex}`)
  }

  const heroPath = join(feRoot, 'src/components/panel/DarkHallHero.vue')
  assert.equal(existsSync(heroPath), true)
})


test('英文原名与标题剥除 MA5.1 / DDP5.1 声道尾巴，防止详情页副标题残留压制参数', () => {
  assert.equal(
    cleanPtTitle('Spider.Man.Across.The.Spider.Verse.2023.2160p.MA5.1'),
    'Spider Man Across The Spider Verse',
  )
})

test('shareAccountCardFlight：首次登录后同页并发读卡合并为单次请求，防止双路互踢图片 key', async () => {
  let calls = 0
  let release!: (v: { emby_api_key: string }) => void
  const gate = new Promise<{ emby_api_key: string }>((resolve) => {
    release = resolve
  })
  const loader = () => {
    calls += 1
    return gate
  }
  const p1 = shareAccountCardFlight(loader)
  const p2 = shareAccountCardFlight(loader)
  release({ emby_api_key: `key_${calls}` })
  const [a, b] = await Promise.all([p1, p2])
  assert.equal(calls, 1)
  assert.deepEqual(a, b)
})