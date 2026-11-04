/**
 * 绑定入口总开关的纯判定（可单测，不依赖 Vue 运行时）。
 *
 * 后端 GET /storage-bindings/providers 返回 { providers, enabled }：
 * - enabled === false → 模式 B：用户端「我的网盘」整段不渲染、不做绑定引导、
 *   不出现“未绑定不能点播/转存到自己的盘”文案；
 * - enabled 缺省（旧后端/请求失败）→ 按开处理，保持旧行为（fail-open 是
 *   入口展示控制，不是安全门禁）。
 */
export interface BindingProvidersPayload {
  providers?: string[]
  enabled?: boolean
}

export interface BindingUiState {
  enabled: boolean
  providers: string[]
}

export function resolveBindingUiState(
  payload: BindingProvidersPayload | null | undefined,
): BindingUiState {
  return {
    enabled: payload?.enabled !== false,
    providers: payload?.providers ?? [],
  }
}

/** 是否展示“去绑定自己的盘才能点播”引导：开关开且名下无可用绑定。 */
export function shouldShowBindingGuide(
  enabled: boolean,
  hasActiveBinding: boolean,
): boolean {
  return enabled && !hasActiveBinding
}
