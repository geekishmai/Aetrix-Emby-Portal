<script setup lang="ts">
/**
 * 登录 / 注册 — 全页认证视图
 *
 * 统一后端 JWT 认证：
 * - POST /api/user/auth/login     登录（access + refresh token）
 * - POST /api/user/auth/register  注册（自动生成自建 Emby 凭据）
 *
 * 登录成功后自动跳转 ?redirect= 指定的受保护页面。
 */
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import { watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { useToast } from '@/composables/useToast'
import { toUserMessage } from '@/utils/toUserMessage'
import { toastFallback } from '@/constants/toastMessages'
import { User, Lock, Mail, Eye, EyeOff, Clapperboard, Gift, Send } from 'lucide-vue-next'
// 人机验证挂件（能力：人机验证）：管理员未开启时该组件自己什么都不渲染
import CaptchaChallenge from '@/components/ui/CaptchaChallenge.vue'
// 站名来自「站点与品牌」能力（未配置时用默认值）
import { branding } from '@/composables/useBranding'
import { getRegisterConfig } from '@/api/user'
import { tgApi } from '@/api/tg'
import { fetchLoginPosters, fetchTgBotUsername, type LoginPoster } from '@/api/site'
import PosterCollage from '@/components/ui/PosterCollage.vue'

const router = useRouter()
const route = useRoute()
const userStore = useUserStore()
const toast = useToast()

// 注册页开关（管理端可配）：注册模式 + 邀请码开关
// registration_mode: open=开放注册 / closed=关闭注册（code 模式已下线）
const registrationMode = ref<'open' | 'closed'>('open')
const invitationEnabled = ref(true)
const registrationClosed = computed(() => registrationMode.value === 'closed')
 // 注册配置拉取失败标记：失败时注册区显示错误态+重试，不让用户填完表才被拒
 const configError = ref(false)

const mode = ref<'login' | 'register'>('login')

const loginForm = reactive({ username: '', password: '' })
// 邀请码：此前只有从带参链接进来（?invite=）才拿得到，表单里根本没有输入框，
// 现在能手填，链接进来自动预填。
const registerForm = reactive({
  username: '', password: '', confirmPassword: '', email: '',
  inviteCode: '',
})

const showLoginPassword = ref(false)
const showRegisterPassword = ref(false)
const loginLoading = ref(false)
const registerLoading = ref(false)
const error = ref('')

// 人机验证令牌：由挂件回调写入；未开启保护时一直是空串（后端也不校验）
const captchaToken = ref('')
const captchaRef = ref<InstanceType<typeof CaptchaChallenge> | null>(null)
const brandLogoBroken = ref(false)
watch(
  () => branding.logo_url,
  () => {
    brandLogoBroken.value = false
  },
)

// 强度文案映射 ['弱','弱','中','强'][score]：score=0（1–5 位密码）时旧写法 ['弱','中','强'][score-1] 会取到 undefined 显示空文案
const passwordStrength = computed(() => {
  const pwd = registerForm.password
  if (!pwd) return 0
  let score = 0
  if (pwd.length >= 6) score++
  if (pwd.length >= 10) score++
  if (/[a-z]/.test(pwd) && /[A-Z]/.test(pwd)) score++
  if (/\d/.test(pwd)) score++
  if (/[^a-zA-Z0-9]/.test(pwd)) score++
  return Math.min(score, 3)
})

function toggleMode(next: 'login' | 'register') {
  mode.value = next
  error.value = ''
  // 人机验证令牌显式清空：即使挂件会随表单重建，也防止时序上复用旧令牌
  captchaToken.value = ''
}

function onTabKey(e: KeyboardEvent, current: 'login' | 'register') {
  if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
  e.preventDefault()
  const next = current === 'login' ? 'register' : 'login'
  toggleMode(next)
  document.getElementById(next === 'login' ? 'tab-login' : 'tab-register')?.focus()
}

function friendlyError(err: any, fallback: string): string {
  // 认证表单（登录/注册）中 401/403 表示的是凭据错误（如密码错误），
  // 而不是已登录后接口调用时的“会话过期”。因此这里必须先于 toUserMessage 的
  // 全局“状态码优先”规则单独处理，避免把“密码错了”误报成“登录已过期”。
  const status = err?.response?.status
  const detail = err?.response?.data?.detail
  if ((status === 401 || status === 403) && typeof detail === 'string' && detail.trim() !== '') {
    const trimmed = detail.trim()
    const isMachineCode = /^[A-Z][A-Z0-9_]{2,}$/.test(trimmed)
    const hasLeakKeyword = /数据库|sql|连接失败|超时|异常|堆栈|traceback/i.test(trimmed)
    const isServerErrorLeak = /internal server error|服务器内部错误/i.test(trimmed)
    if (!isMachineCode && !hasLeakKeyword && !isServerErrorLeak) {
      return trimmed
    }
  }

  // 统一走 toUserMessage 解析；只有落到通用兜底时才用调用方的场景文案
  const msg = toUserMessage(err, 'saveFailed')
  return msg === toastFallback.saveFailed ? fallback : msg
}

/** query 参数取首个字符串值（多值 query 取第一个，防止数组被直接提交） */
function firstQueryValue(v: unknown): string {
  if (Array.isArray(v)) return String(v[0] ?? '')
  return String(v ?? '')
}

/** redirect 白名单：只允许站内路径，防止开放重定向 */
function safeRedirect(v: unknown): string {
  const s = firstQueryValue(v)
  if (s.startsWith('/') && !s.startsWith('//') && !s.includes('://')) return s
  return '/'
}

async function handleLogin() {
  // 回车双提交防护：表单 @submit 与输入框回车各触发一次，此处用 loading 状态做守卫兜底
  if (loginLoading.value) return
  if (!loginForm.username || !loginForm.password) {
    error.value = '请输入用户名和密码'
    return
  }
  error.value = ''
  loginLoading.value = true
  try {
    await userStore.login(loginForm.username.trim(), loginForm.password, captchaToken.value)
    toast.success('登录成功')
    router.push(safeRedirect(route.query.redirect))
  } catch (err) {
    error.value = friendlyError(err, '登录失败，请稍后重试')
    // 令牌是一次性的：失败后换一个新的，否则第二次点击会因「已使用」而失败
    captchaRef.value?.reset()
  } finally {
    loginLoading.value = false
  }
}

async function handleRegister() {
  // 回车双提交防护（同 handleLogin）
  if (registerLoading.value) return
  if (registrationClosed.value) {
    error.value = '当前未开放注册'
    return
  }
  const f = registerForm
  if (!f.username || !f.password || !f.confirmPassword) {
    error.value = '请完整填写注册信息'
    return
  }
  if (f.username.length < 3 || f.username.length > 32 || !/^[a-zA-Z0-9_]+$/.test(f.username)) {
    error.value = '用户名需 3-32 位，仅限字母、数字、下划线'
    return
  }
  if (f.password.length < 6) {
    error.value = '密码至少 6 位'
    return
  }
  if (f.password !== f.confirmPassword) {
    error.value = '两次输入的密码不一致'
    return
  }
  error.value = ''
  registerLoading.value = true
  try {
    // 表单里填的优先；没填时沿用链接带来的（多值 query 取第一个）
    const inviteCode = f.inviteCode.trim() || firstQueryValue(route.query.invite)
    await userStore.register(
      f.username.trim(), f.password, f.email || undefined,
      inviteCode || undefined, captchaToken.value,
    )
    toast.success('注册成功，已自动开通观影账号')
    // 注册后 TG 绑定引导：仅注册流程触发，普通登录不动。
    // 状态接口失败时按默认放行进首页，绝不锁死用户。
    const redirect = safeRedirect(route.query.redirect)
    try {
      const st = await tgApi.status()
      if (st.required && !st.bound && st.guide_enabled) {
        router.push({ path: '/tg-bind', query: route.query.redirect ? { redirect: safeRedirect(route.query.redirect) } : {} })
        return
      }
    } catch {
      /* 拿不到绑定状态就按默认放行 */
    }
    router.push(redirect)
  } catch (err) {
    error.value = friendlyError(err, '注册失败，请稍后重试')
    captchaRef.value?.reset()
  } finally {
    registerLoading.value = false
  }
}

// 登录页跟随用户主题（用户决策 2026-10-14 21:14，取代 2026-10-11 的强制深色口径）：
// 不再自己动 data-theme，路由守卫 afterEach 已按 localStorage 'aetrix-theme' 恢复用户偏好。

// 海报拼贴背景：延迟到浏览器空闲时拉取，不阻塞表单首屏渲染
const loginPosters = ref<LoginPoster[]>([])
// EA 海报直链基地址（后端下发；为空则不渲染拼贴，避免相对路径被反代到错误后端）
const loginPostersBaseUrl = ref('')

// TG 登录入口：站点机器人用户名（公开接口下发）；为空=未配置机器人，不渲染入口
const tgBotUsername = ref('')

let postersIdleHandle: number | null = null
let postersTimeout: ReturnType<typeof setTimeout> | null = null

function loadRegisterConfig() {
  configError.value = false
  // 拉取注册页开关（公开接口）：按管理端配置显示/隐藏邀请码输入框、是否关闭注册
  getRegisterConfig().then((cfg) => {
    // code 模式已下线：后端/此处统一归一为 open
    registrationMode.value = cfg.registration_mode === 'closed' ? 'closed' : 'open'
    invitationEnabled.value = cfg.invitation_enabled
    // 注册已关闭时忽略 ?mode=register / ?invite= 参数，强制留在登录页
    if (registrationMode.value === 'closed') mode.value = 'login'
  }).catch(() => { configError.value = true /* 拉取失败时注册区显示错误态+重试，不锁死 */ })
}

onMounted(() => {
  // 登录页不再强制深色：跟随用户偏好（见文件顶部注释）。

  loadRegisterConfig()
  // TG 登录入口与注册配置并行拉取，失败保持空串（不渲染入口、不阻塞页面）
  fetchTgBotUsername().then(name => { tgBotUsername.value = name }).catch(() => { tgBotUsername.value = '' })
  if (route.query.mode === 'register') mode.value = 'register'
  // 邀请链接 ?invite=CODE（多值 query 取第一个）：自动切到注册页、预填邀请码并提示
  if (route.query.invite) {
    mode.value = 'register'
    registerForm.inviteCode = firstQueryValue(route.query.invite)
    toast.info(`已收到好友邀请码，注册成功后双方都可获得能量奖励`, 5000)
  }

  // 海报拼贴背景：requestIdleCallback 延迟拉取（降级 setTimeout 800ms），
  // 失败时静默降级（loginPosters 为空或基地址为空 → 不渲染拼贴，回退现有背景）
  const loadPosters = () => {
    fetchLoginPosters().then(({ items, embyBaseUrl }) => {
      if (items.length > 0 && embyBaseUrl) {
        loginPosters.value = items
        loginPostersBaseUrl.value = embyBaseUrl
      }
    }).catch(() => { /* 静默降级 */ })
  }
  const ric = (window as unknown as { requestIdleCallback?: (cb: () => void) => number }).requestIdleCallback
  if (typeof ric === 'function') postersIdleHandle = ric(loadPosters)
  else postersTimeout = setTimeout(loadPosters, 800)
})

onUnmounted(() => {
  // 页面卸载时取消海报延迟拉取，避免回调里写已卸载组件的状态
  if (postersIdleHandle !== null) (window as unknown as { cancelIdleCallback?: (h: number) => void }).cancelIdleCallback?.(postersIdleHandle)
  if (postersTimeout !== null) clearTimeout(postersTimeout)
})
</script>

<template>
  <div class="auth-page">
    <PosterCollage v-if="loginPosters.length > 0 && loginPostersBaseUrl" :posters="loginPosters" :emby-base-url="loginPostersBaseUrl" />
    <div class="auth-grid" aria-hidden="true"></div>

    <div class="auth-column">
      <div class="auth-brand">
        <div class="brand-mark">
          <img
            v-if="!brandLogoBroken"
            decoding="async"
            loading="lazy"
            :src="branding.logo_url || '/logo.png'"
            :alt="branding.site_name"
            @error="brandLogoBroken = true"
          />
          <Clapperboard v-else :size="22" aria-hidden="true" />
        </div>
        <h1 class="brand-title">{{ branding.site_name }}</h1>
        <p class="brand-subtitle">高清片库在线浏览 · 多端播放器一键直连</p>
      </div>

      <!-- 模式切换 -->
      <div class="mode-switch" role="tablist" aria-label="登录或注册">
        <button
          id="tab-login"
          class="mode-btn"
          :class="{ active: mode === 'login' }"
          role="tab"
          :aria-selected="mode === 'login'"
          aria-controls="panel-login"
          @click="toggleMode('login')"
          @keydown="onTabKey($event, 'login')"
        >
          <span class="mode-btn-label">登录</span>
        </button>
        <button
          id="tab-register"
          class="mode-btn"
          :class="{ active: mode === 'register' }"
          role="tab"
          :aria-selected="mode === 'register'"
          aria-controls="panel-register"
          @click="toggleMode('register')"
          @keydown="onTabKey($event, 'register')"
        >
          <span class="mode-btn-label">注册</span>
        </button>
        <div class="mode-indicator" :class="{ right: mode === 'register' }" aria-hidden="true"></div>
      </div>

      <!-- 登录表单 -->
      <form v-if="mode === 'login'" id="panel-login" class="auth-form" role="tabpanel" aria-labelledby="tab-login" tabindex="0" @submit.prevent="handleLogin">
        <label class="field">
          <span class="field-label">用户名</span>
          <div class="field-box">
            <User :size="16" class="field-icon" aria-hidden="true" />
            <input
              v-model="loginForm.username"
              type="text"
              name="username"
              autocomplete="username"
              placeholder="输入用户名"
              :aria-invalid="!!error"
              aria-describedby="login-form-error"
            />
          </div>
        </label>

        <label class="field">
          <span class="field-label">密码</span>
          <div class="field-box">
            <Lock :size="16" class="field-icon" aria-hidden="true" />
            <input
              v-model="loginForm.password"
              :type="showLoginPassword ? 'text' : 'password'"
              name="password"
              autocomplete="current-password"
              placeholder="输入密码"
              :aria-invalid="!!error"
              aria-describedby="login-form-error"
            />
            <button type="button" class="eye-btn" :aria-label="showLoginPassword ? '隐藏密码' : '显示密码'" :aria-pressed="showLoginPassword" @click="showLoginPassword = !showLoginPassword">
              <EyeOff v-if="showLoginPassword" :size="15" aria-hidden="true" />
              <Eye v-else :size="15" aria-hidden="true" />
            </button>
          </div>
        </label>

        <p v-if="error" id="login-form-error" class="form-error" role="alert">{{ error }}</p>

        <CaptchaChallenge ref="captchaRef" action="login" @update:token="captchaToken = $event" />

        <button type="submit" class="submit-btn" :disabled="loginLoading">
          {{ loginLoading ? '登录中…' : '登 录' }}
        </button>

        <!-- TG 一键登录入口：跳到站点机器人私聊，菜单首项即免密登录（后端未配置机器人时不渲染） -->
        <div v-if="tgBotUsername" class="tg-login">
          <div class="tg-login-divider" aria-hidden="true"><span>或</span></div>
          <a class="tg-login-btn" :href="`https://t.me/${tgBotUsername}`" target="_blank" rel="noopener noreferrer">
            <Send :size="16" aria-hidden="true" /> 用 Telegram 登录
          </a>
          <p class="tg-login-hint">在机器人私聊里点「一键免密进入控制面板」即可免密登录（需先绑定过 Telegram）</p>
        </div>
      </form>

      <!-- 注册表单 -->
      <form v-else id="panel-register" class="auth-form" role="tabpanel" aria-labelledby="tab-register" tabindex="0" @submit.prevent="handleRegister">
        <label class="field">
          <span class="field-label">用户名</span>
          <div class="field-box">
            <User :size="16" class="field-icon" aria-hidden="true" />
            <input
              v-model="registerForm.username"
              type="text"
              name="username"
              autocomplete="username"
              placeholder="3-32 位字母、数字、下划线"
              :aria-invalid="!!error"
              aria-describedby="register-form-error"
            />
          </div>
        </label>

        <label class="field">
          <span class="field-label">密码</span>
          <div class="field-box">
            <Lock :size="16" class="field-icon" aria-hidden="true" />
            <input
              v-model="registerForm.password"
              :type="showRegisterPassword ? 'text' : 'password'"
              name="new-password"
              autocomplete="new-password"
              placeholder="至少 6 位"
              :aria-invalid="!!error"
              aria-describedby="register-form-error"
            />
            <button type="button" class="eye-btn" :aria-label="showRegisterPassword ? '隐藏密码' : '显示密码'" :aria-pressed="showRegisterPassword" @click="showRegisterPassword = !showRegisterPassword">
              <EyeOff v-if="showRegisterPassword" :size="15" aria-hidden="true" />
              <Eye v-else :size="15" aria-hidden="true" />
            </button>
          </div>
          <div v-if="registerForm.password" class="strength">
            <div class="strength-track">
              <div class="strength-fill" :class="`lv-${passwordStrength}`"></div>
            </div>
            <span class="strength-text" aria-live="polite">{{
              ['弱', '弱', '中', '强'][passwordStrength]
            }}</span>
          </div>
        </label>

        <label class="field">
          <span class="field-label">确认密码</span>
          <div class="field-box">
            <Lock :size="16" class="field-icon" aria-hidden="true" />
            <input
              v-model="registerForm.confirmPassword"
              type="password"
              name="confirm-password"
              autocomplete="new-password"
              placeholder="再次输入密码"
              :aria-invalid="!!error"
              aria-describedby="register-form-error"
            />
          </div>
        </label>

        <label class="field">
          <span class="field-label">邮箱 <em class="optional">选填</em></span>
          <div class="field-box">
            <Mail :size="16" class="field-icon" aria-hidden="true" />
            <input
              v-model="registerForm.email"
              type="email"
              name="email"
              autocomplete="email"
              placeholder="用于找回密码"
              :aria-invalid="!!error"
              aria-describedby="register-form-error"
            />
          </div>
        </label>

        <label v-if="invitationEnabled" class="field">
          <span class="field-label">邀请码 <em class="optional">选填，双方得能量</em></span>
          <div class="field-box">
            <Gift :size="16" class="field-icon" aria-hidden="true" />
            <input
              v-model="registerForm.inviteCode"
              type="text"
              name="invite-code"
              placeholder="好友的邀请码"
              :aria-invalid="!!error"
              aria-describedby="register-form-error"
            />
          </div>
        </label>

        <p v-if="registrationClosed" class="closed-notice">当前未开放注册</p>
        <p v-if="configError" class="closed-notice">注册配置拉取失败，<button type="button" class="retry-link" @click="loadRegisterConfig">点重试</button></p>
        <p v-if="error" id="register-form-error" class="form-error" role="alert">{{ error }}</p>

        <CaptchaChallenge ref="captchaRef" action="register" @update:token="captchaToken = $event" />

        <button type="submit" class="submit-btn" :disabled="registerLoading || registrationClosed">
          {{ registerLoading ? '注册中…' : '注 册' }}
        </button>
      </form>

      <p class="auth-footnote">
        {{ mode === 'login' ? '密码加密存储' : '密码加密存储 · 注册成功自动开通观影账号' }}
      </p>
    </div>
  </div>
</template>

<style scoped>
.auth-page {
  box-sizing: border-box;
  min-height: 100vh;
  min-height: 100svh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 48px 20px;
  position: relative;
  isolation: isolate;
  overflow: hidden;
  background: #111111;
  color: #f2f2f2;
}

/* 建筑感极细暗网辅助线：无发光、无球形渐变遮罩 */
.auth-grid {
  position: absolute;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  background-image:
    linear-gradient(to right, rgba(255, 255, 255, 0.03) 1px, transparent 1px),
    linear-gradient(to bottom, rgba(255, 255, 255, 0.03) 1px, transparent 1px);
  background-size: 56px 56px;
}

/* 居中暗厅硬朗鉴权面板 */
.auth-column {
  position: relative;
  z-index: 1;
  box-sizing: border-box;
  width: 100%;
  max-width: 416px;
  min-width: 0;
  padding: 36px 32px 32px;
  background: #171717;
  border: 1px solid #2a2a2a;
  border-radius: 2px;
  animation: authColumnIn 0.2s ease-out both;
}

@keyframes authColumnIn {
  from { opacity: 0; }
  to { opacity: 1; }
}

.auth-brand {
  text-align: left;
  margin-bottom: 24px;
  padding-bottom: 18px;
  border-bottom: 1px solid #242424;
}

.brand-mark {
  width: 40px;
  height: 40px;
  margin: 0 0 14px;
  border-radius: 2px;
  border: 1px solid #2a2a2a;
  background: #111111;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #c3f53c;
}

.brand-mark img {
  width: 100%;
  height: 100%;
  object-fit: contain;
  border-radius: inherit;
}

.brand-title {
  font-size: 26px;
  font-weight: 800;
  letter-spacing: -0.02em;
  color: #f2f2f2;
  margin: 0 0 6px;
  line-height: 1.2;
  overflow-wrap: anywhere;
}

.brand-subtitle {
  font-size: 13px;
  color: #9a9a9a;
  margin: 0;
  line-height: 1.5;
}

/* 建筑感分栏页签：底边 2px #c3f53c 激活指示，把实心荧光绿留给唯一主提交按钮 */
.mode-switch {
  box-sizing: border-box;
  position: relative;
  display: grid;
  grid-template-columns: 1fr 1fr;
  background: #111111;
  border: 1px solid #2a2a2a;
  border-radius: 2px;
  padding: 0;
  margin-bottom: 22px;
}

.mode-btn {
  position: relative;
  z-index: 1;
  min-width: 0;
  min-height: 42px;
  padding: 0 12px;
  border: none;
  background: transparent;
  border-radius: 0;
  font: inherit;
  font-size: 14px;
  font-weight: 600;
  line-height: 1;
  color: #9a9a9a;
  cursor: pointer;
  transition: color 0.15s ease;
  display: flex;
  align-items: center;
  justify-content: center;
}

.mode-btn-label {
  line-height: 1;
}

.mode-btn.active {
  color: #f2f2f2;
  font-weight: 700;
}

.mode-indicator {
  position: absolute;
  left: 0;
  bottom: 0;
  width: 50%;
  height: 100%;
  background: #1e1e1e;
  border-bottom: 2px solid #c3f53c;
  box-sizing: border-box;
  pointer-events: none;
  transition: transform 0.2s cubic-bezier(0.4, 0, 0.2, 1);
}

.mode-indicator.right {
  transform: translateX(100%);
}

.auth-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.field-label {
  padding: 0;
  font-size: 12.5px;
  font-weight: 600;
  line-height: 1.4;
  color: #9a9a9a;
}

.optional {
  margin-left: 4px;
  font-size: 12px;
  font-style: normal;
  font-weight: 400;
  color: #6e6e6e;
}

.field-box {
  box-sizing: border-box;
  display: flex;
  align-items: center;
  min-height: 46px;
  background: #111111;
  border: 1px solid #2a2a2a;
  border-radius: 2px;
  padding: 0 14px;
  transition: border-color 0.15s ease;
}

.field-box:focus-within {
  border-color: #c3f53c;
}

.field-icon {
  color: #6e6e6e;
  margin-right: 10px;
  flex-shrink: 0;
  transition: color 0.15s ease;
}

.field-box:focus-within .field-icon {
  color: #c3f53c;
}

.field-box input {
  box-sizing: border-box;
  flex: 1;
  min-width: 0;
  height: 100%;
  padding: 0;
  background: transparent;
  border: none;
  outline: none;
  color: #f2f2f2;
  font: inherit;
  font-size: 15px;
}

.field-box input::placeholder {
  color: #6e6e6e;
  opacity: 1;
}

.field-box input:-webkit-autofill,
.field-box input:-webkit-autofill:hover,
.field-box input:-webkit-autofill:focus,
.field-box input:-webkit-autofill:active {
  -webkit-text-fill-color: #f2f2f2;
  -webkit-box-shadow: 0 0 0 1000px #111111 inset;
  transition: background-color 5000s ease-in-out 0s;
}

.eye-btn {
  position: relative;
  box-sizing: border-box;
  width: 32px;
  height: 32px;
  flex: 0 0 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  margin-left: 6px;
  background: transparent;
  border: none;
  color: #9a9a9a;
  cursor: pointer;
  border-radius: 2px;
}

.eye-btn::after {
  content: "";
  position: absolute;
  inset: -6px;
}

@media (hover: hover) {
  .eye-btn:hover {
    color: #f2f2f2;
    background: #1e1e1e;
  }
}

.mode-btn:focus-visible,
.eye-btn:focus-visible,
.submit-btn:focus-visible {
  outline: 2px solid #c3f53c;
  outline-offset: 2px;
}

/* 密码强度 */
.strength {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 2px;
  padding: 0;
}

.strength-track {
  flex: 1;
  height: 3px;
  background: #242424;
  border-radius: 0;
  overflow: hidden;
}

.strength-fill {
  height: 100%;
  width: 100%;
  border-radius: 0;
  transform: scaleX(0);
  transform-origin: left center;
  transition: transform 0.25s ease, background-color 0.25s ease;
}

.strength-fill.lv-1 { transform: scaleX(0.33); background: #e5534b; }
.strength-fill.lv-2 { transform: scaleX(0.66); background: #9a9a9a; }
.strength-fill.lv-3 { transform: scaleX(1); background: #c3f53c; }

.strength-text {
  font-size: 12px;
  color: #9a9a9a;
  min-width: 16px;
}

.form-error {
  margin: 0;
  padding: 10px 14px;
  border-radius: 2px;
  background: #1e1e1e;
  border: 1px solid rgba(229, 83, 75, 0.55);
  color: #ef6b63;
  font-size: 13px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.closed-notice {
  margin: 0;
  padding: 12px 14px;
  border-radius: 2px;
  background: #1e1e1e;
  border: 1px solid #2a2a2a;
  color: #9a9a9a;
  font-size: 13.5px;
  line-height: 1.5;
  text-align: center;
}

.retry-link {
  background: none;
  border: none;
  padding: 0 8px;
  min-height: 44px;
  color: #c3f53c;
  font: inherit;
  text-decoration: underline;
  cursor: pointer;
}

/* 全页唯一实心荧光绿 #c3f53c 主提交按钮 */
.submit-btn {
  box-sizing: border-box;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  width: 100%;
  height: 46px;
  flex-shrink: 0;
  margin-top: 4px;
  padding: 0 20px;
  background: #c3f53c;
  border: 1px solid #c3f53c;
  border-radius: 2px;
  color: #111111;
  font: inherit;
  font-size: 15px;
  font-weight: 800;
  cursor: pointer;
  transition: background-color 0.15s ease, border-color 0.15s ease;
}

.submit-btn:hover:not(:disabled) {
  background: #d1fa58;
  border-color: #d1fa58;
}

.submit-btn:disabled {
  background: #242424;
  border-color: #2a2a2a;
  color: #6e6e6e;
  cursor: not-allowed;
}

.auth-footnote {
  margin: 20px 0 0;
  text-align: center;
  font-size: 12px;
  color: #6e6e6e;
  line-height: 1.6;
}

@media (max-width: 767px) {
  .auth-page {
    padding: 24px 16px max(24px, env(safe-area-inset-bottom));
  }

  .auth-column {
    max-width: 100%;
    padding: 28px 20px 24px;
  }

  .field-box input {
    font-size: 16px;
  }
}

/* TG 一键登录入口（暗厅次级描边按钮） */
.tg-login {
  margin-top: 16px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.tg-login-divider {
  display: flex;
  align-items: center;
  gap: 12px;
  color: #6e6e6e;
  font-size: 12px;
}

.tg-login-divider::before,
.tg-login-divider::after {
  content: '';
  flex: 1;
  height: 1px;
  background: #242424;
}

.tg-login-btn {
  box-sizing: border-box;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  width: 100%;
  min-height: 46px;
  padding: 0 20px;
  background: #1e1e1e;
  border: 1px solid #2a2a2a;
  border-radius: 2px;
  color: #f2f2f2;
  font-size: 14px;
  font-weight: 600;
  text-decoration: none;
  transition: border-color 0.15s ease;
}

.tg-login-btn:hover {
  border-color: #f2f2f2;
}

.tg-login-btn:focus-visible {
  outline: 2px solid #c3f53c;
  outline-offset: 2px;
}

.tg-login-hint {
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
  color: #9a9a9a;
  text-align: center;
}

@media (prefers-reduced-motion: reduce) {
  *:not(:-webkit-autofill) {
    animation: none !important;
    transition: none !important;
  }
}
</style>