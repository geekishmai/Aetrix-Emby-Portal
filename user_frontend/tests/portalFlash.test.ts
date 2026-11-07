import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import test from 'node:test'

const feRoot = join(import.meta.dirname, '..')

function decideBootSurface(pathname: string, hasToken: boolean, cookie = ''): 'portal' | 'auth' {
  const rawPath = (pathname || '/').replace(/\/+$/, '') || '/'
  const hasSso = cookie.includes('aetrix_sso_pending=')
  const isAuthPath = rawPath === '/login' || rawPath === '/tg-login' || rawPath === '/privacy'
  const isPublicPortalPath = rawPath === '/want'
  return !isAuthPath && (hasToken || hasSso || isPublicPortalPath) ? 'portal' : 'auth'
}

test('首屏零闪烁预引导：未登录受保护路由首帧走 auth 骨架，已登录门户路由首帧直锁 portal 暖纸面', () => {
  assert.equal(decideBootSurface('/', false), 'auth')
  assert.equal(decideBootSurface('/library', false), 'auth')
  assert.equal(decideBootSurface('/media/abc', false), 'auth')
  assert.equal(decideBootSurface('/login', true), 'auth')
  assert.equal(decideBootSurface('/', true), 'portal')
  assert.equal(decideBootSurface('/library', true), 'portal')
  assert.equal(decideBootSurface('/media/abc', true), 'portal')
  assert.equal(decideBootSurface('/', false, 'aetrix_sso_pending=1'), 'portal')
  assert.equal(decideBootSurface('/want', false), 'portal')
})

test('首屏零闪烁契约：首页挂载前校验 requiresAuth，且 main.ts 等 initBranding + router.isReady 后再 mount', () => {
  const routerSrc = readFileSync(join(feRoot, 'src/router/index.ts'), 'utf-8')
  assert.match(routerSrc, /path:\s*'\/'[\s\S]{0,180}requiresAuth:\s*true/)

  const mainSrc = readFileSync(join(feRoot, 'src/main.ts'), 'utf-8')
  const readyPos = mainSrc.indexOf('router.isReady()')
  const brandPos = mainSrc.indexOf('initBranding()')
  const mountPos = mainSrc.indexOf("app.mount('#app')")
  assert.ok(readyPos > 0 && brandPos > 0 && mountPos > readyPos && mountPos > brandPos)

  const indexHtml = readFileSync(join(feRoot, 'index.html'), 'utf-8')
  assert.ok(indexHtml.includes('__AETRIX_BOOT_PROMISE__'))
  assert.ok(indexHtml.includes('data-boot-mode'))
})
