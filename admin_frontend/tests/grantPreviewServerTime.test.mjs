// PA-059 回归测试：node 直接运行（无需前端测试框架，仓库 admin_frontend 无 vitest/jest）。
// 运行：cd admin_frontend && node tests/grantPreviewServerTime.test.mjs
// 背景：后端 subscription_end 是 naive UTC 串；Users.vue grantPreview 曾用 new Date(串)
// 裸解析，在 Asia/Shanghai 下 UTC 00:00–07:59 时段的起止日期各早一天（#1712 漏改）。
// 本测试断言与本地时区无关的修正后行为（toISOString 恒为 UTC 口径）。
import assert from 'node:assert'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { parseServerTime } from '../src/utils/serverTime.ts'

const here = dirname(fileURLToPath(import.meta.url))
const usersVue = readFileSync(join(here, '../src/views/Users.vue'), 'utf8')

let passed = 0
function check(name, actual, expected) {
  assert.deepStrictEqual(actual, expected, name)
  passed += 1
  console.log(`PASS ${name}`)
}

// 与 Users.vue grantPreview（:326-330）同形的计算，parse 函数由调用方注入。
function grantPreview(subscription_end, days, parse) {
  const base = subscription_end ? parse(subscription_end) : new Date()
  const from = base.getTime() > Date.now() ? base : new Date()
  const end = new Date(from.getTime() + days * 86400000)
  return `${from.toISOString().slice(0, 10)} → ${end.toISOString().slice(0, 10)}`
}

// 修正后行为：三档 naive UTC 到期日，预览起止均为 UTC 日期口径。
check('00:00 UTC 到期预览', grantPreview('2027-12-01T00:00:00', 30, parseServerTime), '2027-12-01 → 2027-12-31')
check('07:59 UTC 到期预览', grantPreview('2027-12-01T07:59:00', 30, parseServerTime), '2027-12-01 → 2027-12-31')
check('10:00 UTC 到期预览', grantPreview('2027-12-01T10:00:00', 30, parseServerTime), '2027-12-01 → 2027-12-31')
check('带 Z 后缀串原样解析', grantPreview('2027-12-01T00:00:00Z', 30, parseServerTime), '2027-12-01 → 2027-12-31')

// 源码门：grantPreview 的 base 行必须用 parseServerTime，不许再裸 new Date(服务端串)。
const baseLine = usersVue.split('\n').find((l) => l.includes('const base = u.has_subscription'))
assert.ok(baseLine, 'grantPreview base 行存在')
assert.ok(baseLine.includes('parseServerTime(u.subscription_end)'), `base 行须用 parseServerTime，实为：${baseLine.trim()}`)
assert.ok(!baseLine.includes('new Date(u.subscription_end)'), 'base 行不许残留 new Date(u.subscription_end)')
passed += 1
console.log('PASS grantPreview base 行用 parseServerTime')

console.log(`\n${passed} passed`)
