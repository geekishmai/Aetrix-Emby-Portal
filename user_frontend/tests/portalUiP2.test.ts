import assert from 'node:assert/strict'
import test from 'node:test'

import { getVisiblePrimaryNav, primaryNav, resolveActiveNavPath } from '../src/config/navigation.ts'
import {
  buildInfuseLaunchUrl,
  findPlayerImportScheme,
  isPlayerDeepLink,
  listImportSchemes,
} from '../src/utils/playerSchemes.ts'

test('P2 导航：发现改为首页；默认展示全部 6 项，游戏化关闭时隐藏世界与补给站', () => {
  assert.equal(primaryNav[0]?.name, '首页')
  assert.equal(primaryNav[0]?.path, '/')

  const allNames = getVisiblePrimaryNav(true).map((n) => n.name)
  assert.deepEqual(allNames, ['首页', '片库', '世界', '服务器', '补给站', '我的'])

  const cleanNames = getVisiblePrimaryNav(false).map((n) => n.name)
  assert.deepEqual(cleanNames, ['首页', '片库', '服务器', '我的'])

  // 无论开关状态，/game/account 均精确命中「我的」
  assert.equal(resolveActiveNavPath('/game/account', getVisiblePrimaryNav(true)), '/game/account')
  assert.equal(resolveActiveNavPath('/game/account', getVisiblePrimaryNav(false)), '/game/account')
  assert.equal(resolveActiveNavPath('/', getVisiblePrimaryNav(false)), '/')
})

test('P2/P3 详情页播放器：复用 VidHub/SenPlayer 一键导入 scheme 并构造安全 Infuse 直连播放协议', () => {
  const schemes = listImportSchemes({
    Rex: 'rex://import?type=emby&scheme=https&host=emby.fluxquata.com&port=443&username=u1',
    senplayer: 'senplayer://importserver?type=emby&name=Aetrix&address=https%3A%2F%2Femby.fluxquata.com&username=u1',
    VidHub: 'vidhub://import?type=emby&scheme=https&host=emby.fluxquata.com&port=443&username=u1',
  })

  const vidhub = findPlayerImportScheme(schemes, 'VidHub')
  const senplayer = findPlayerImportScheme(schemes, 'SenPlayer')
  assert.ok(vidhub?.url.startsWith('vidhub://import?type=emby&'))
  assert.ok(senplayer?.url.startsWith('senplayer://importserver?type=emby&'))
  assert.equal(isPlayerDeepLink(vidhub!.url), true)
  assert.equal(isPlayerDeepLink(senplayer!.url), true)

  const infuseStream = buildInfuseLaunchUrl(
    '/emby/videos/item123/stream.mkv?s=sig1',
    'https://emby.fluxquata.com',
  )
  assert.equal(
    infuseStream,
    'infuse://x-callback-url/play?url=https%3A%2F%2Femby.fluxquata.com%2Femby%2Fvideos%2Fitem123%2Fstream.mkv%3Fs%3Dsig1',
  )
  assert.equal(isPlayerDeepLink(infuseStream), true)

  assert.equal(buildInfuseLaunchUrl('', 'https://emby.fluxquata.com'), 'infuse://')
  assert.equal(buildInfuseLaunchUrl('javascript:alert(1)', 'https://emby.fluxquata.com'), 'infuse://')
  assert.equal(isPlayerDeepLink('javascript:alert(1)'), false)
  assert.equal(isPlayerDeepLink('https://evil.example.com'), false)
})
