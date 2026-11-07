// 【UI 统一 U0】主题 CSS 引入已冻结：新增全量引入必须经设计审查，并同步更新
// scripts/check_frontend_tokens.py 的 MAIN_CSS_ALLOWLIST，否则 CI 直接失败。
import './styles/aurora.css'
import './styles/neo-noir-tokens.css'
import './styles/index.css'
import './styles/mobile.css'
// v4「探险家日志」面板纸色设计系统（PR-2）
import './styles/v4-paper.css'

import { createApp } from 'vue'
import { createPinia } from 'pinia'

import App from './App.vue'
import router from './router'
// 站点品牌（能力：站点与品牌）：站名 / Logo / 主题色 / SEO 由后台配置，启动时读一次。
// 先挂载再拉取，不让一次额外的请求把首屏拖住；拉到之后主题色与标题会自己更新。
import { initBranding } from './composables/useBranding'

// iOS Safari 要求页面有 touchstart 监听器才会触发 :active 伪类
// 空函数即可，passive 避免滚动性能问题
document.addEventListener('touchstart', () => {}, { passive: true })

const app = createApp(App)
const pinia = createPinia()

app.use(pinia)
app.use(router)

// 首屏零闪烁门控：等公开品牌配置（站名 / Logo / 游戏化开关）与首个目标路由（含登录态核验和异步页面 chunk）
// 全部就绪后再替换 #app-boot 统一骨架屏，杜绝先闪空白导航栏、旧主题色或未登录首页。
async function bootstrap() {
  await Promise.all([
    initBranding(),
    router.isReady(),
  ])
  ;(window as unknown as { __AETRIX_MOUNTED__?: boolean }).__AETRIX_MOUNTED__ = true
  app.mount('#app')
}

void bootstrap()
