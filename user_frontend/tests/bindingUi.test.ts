import assert from 'node:assert/strict'
import test from 'node:test'

import {
  resolveBindingUiState,
  shouldShowBindingGuide,
} from '../src/utils/bindingUi.ts'

test('开关关闭：providers 即使非空也按关闭处理', () => {
  const state = resolveBindingUiState({ providers: ['pan115'], enabled: false })
  assert.equal(state.enabled, false)
  assert.deepEqual(state.providers, ['pan115'])
})

test('开关开启：保留 provider 名单', () => {
  const state = resolveBindingUiState({ providers: ['drive', 'pan115'], enabled: true })
  assert.equal(state.enabled, true)
  assert.deepEqual(state.providers, ['drive', 'pan115'])
})

test('旧后端缺 enabled 字段：按开处理（保持旧行为）', () => {
  assert.equal(resolveBindingUiState({ providers: ['pan115'] }).enabled, true)
  assert.equal(resolveBindingUiState({}).enabled, true)
  assert.equal(resolveBindingUiState(null).enabled, true)
  assert.equal(resolveBindingUiState(undefined).enabled, true)
})

test('缺 providers：返回空名单而不是 undefined', () => {
  assert.deepEqual(resolveBindingUiState({ enabled: false }).providers, [])
})

test('绑定引导：开关关时永不展示', () => {
  assert.equal(shouldShowBindingGuide(false, false), false)
  assert.equal(shouldShowBindingGuide(false, true), false)
})

test('绑定引导：开关开且无可用绑定时展示', () => {
  assert.equal(shouldShowBindingGuide(true, false), true)
  assert.equal(shouldShowBindingGuide(true, true), false)
})
