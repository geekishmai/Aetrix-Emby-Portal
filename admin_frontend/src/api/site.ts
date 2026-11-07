/**
 * 站点品牌与人机验证挂件信息（v2.20.0）
 *
 * 这两个端点都在 `/api/user/*` 与 `/api/site/*` 下，而管理端的 axios 实例 baseURL 是
 * `/api/admin`，所以这里用裸 axios 走同源绝对路径：它们都是**公开**端点（站名要给未登录
 * 访客看、站点密钥本来就在前端挂件里），不需要带管理员 token，也没必要塞进带鉴权的实例。
 */
import axios from 'axios'

export interface Branding {
  site_name: string
  logo_url: string
  theme_color: string
  seo_title: string
  seo_description: string
  seo_keywords: string
  gamification_enabled?: boolean
}

export interface CaptchaInfo {
  enabled: boolean
  provider: 'none' | 'turnstile' | 'recaptcha' | 'hcaptcha'
  label: string
  site_key: string
  script_url: string
  /** 哪些动作受保护（含 admin_login：后台登录也能开） */
  actions: Record<string, boolean>
}

export const siteApi = {
  branding: async () => (await axios.get<Branding>('/api/site/branding')).data,
  captcha: async () => (await axios.get<CaptchaInfo>('/api/user/auth/captcha')).data,
}
