import assert from 'node:assert/strict'
import test from 'node:test'

import { cleanPtTitle, displayMediaTitle } from '../src/utils/mediaTitle.ts'

test('CO2-P2：双语标题切分保留罗马数字续集号（教父 II、天之杯 II）与中文副标题', () => {
  assert.equal(cleanPtTitle('教父 II'), '教父 II')
  assert.equal(cleanPtTitle('教父 III'), '教父 III')
  assert.equal(cleanPtTitle('教父 II The Godfather Part II 1974 1080p BluRay'), '教父 II')
  assert.equal(cleanPtTitle('天之杯 II'), '天之杯 II')
  assert.equal(cleanPtTitle('命运之夜——天之杯 II ：迷失之蝶'), '命运之夜——天之杯 II ：迷失之蝶')
  assert.equal(
    cleanPtTitle('命运之夜——天之杯 II：迷失之蝶 Heaven Feel II 2019 1080p BluRay'),
    '命运之夜——天之杯 II：迷失之蝶',
  )
})

test('CO2-P2：保留片名内小数点版本号（梅根2.0、哥斯拉-1.0、0.0兆赫）且剥除压制参数尾巴', () => {
  assert.equal(cleanPtTitle('梅根2.0'), '梅根2.0')
  assert.equal(cleanPtTitle('梅根2.0.M3GAN.2.0.2025.2160p.WEB-DL.DDP5.1.H265'), '梅根2.0')
  assert.equal(cleanPtTitle('哥斯拉-1.0'), '哥斯拉-1.0')
  assert.equal(cleanPtTitle('0.0兆赫'), '0.0兆赫')
  assert.equal(cleanPtTitle('赤壁（下） Red Cliff II H 265 10 bit HD'), '赤壁（下）')
  assert.equal(cleanPtTitle('那夜凌晨，我坐上了旺角开往大埔的红VAN'), '那夜凌晨，我坐上了旺角开往大埔的红VAN')
})

test('displayMediaTitle：优先 DisplayTitle 与中文 Aliases，兜底清洗 PT 尾巴', () => {
  assert.equal(
    displayMediaTitle({
      Name: 'Eight.and.a.Half.1963.1080p.10bit.MiniHD-FRDS',
      Aliases: ['八部半', '8½'],
    }),
    '八部半',
  )
  assert.equal(
    displayMediaTitle({
      Name: 'Sherlock.Holmes.A.Game.of.Shadows.2011.BluRay.1080p.DTS-HD.MA.5.1.x264-beAst',
      Aliases: [],
    }),
    'Sherlock Holmes A Game of Shadows',
  )
})

test('CO2-P0/P1：/emby/* 协议路径判定绝不误伤 /api/user/emby/server 等门户路由', () => {
  function checkEmbyUrl(url?: string): boolean {
    if (!url || url.startsWith('/api/')) return false
    if (url.startsWith('/emby/')) return true
    try {
      return new URL(url, 'http://localhost').pathname.startsWith('/emby/')
    } catch {
      return false
    }
  }
  assert.equal(checkEmbyUrl('/api/user/emby/server'), false)
  assert.equal(checkEmbyUrl('/api/user/emby/devices'), false)
  assert.equal(checkEmbyUrl('https://portal.example.com/api/user/emby/server'), false)
  assert.equal(checkEmbyUrl('/emby/Users/me/Views'), true)
  assert.equal(checkEmbyUrl('/emby/Users/me/Items?Recursive=true'), true)
  assert.equal(checkEmbyUrl('https://ea.example.com/emby/Items/abc'), true)
})
