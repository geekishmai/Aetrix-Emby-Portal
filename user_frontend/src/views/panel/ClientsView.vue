<template>
  <div class="v4-scope">
    <main class="v4-page v4-page-wide cl-page">
      <!-- 顶部主标题与新手总指南入口 -->
      <header class="cl-head">
        <div class="cl-head-main">
          <div class="cl-eyebrow">
            <span class="cl-eyebrow-tag">原画直放</span>
            <span>支持 16 款全平台 Emby 客户端 · 免密 URL Scheme 一键导入</span>
          </div>
          <h1 class="cl-title">客户端下载与接入教程</h1>
          <p class="cl-sub">
            本站提供 4K HDR / 杜比视界高码原画直放，每台设备任选 1 款播放器安装并连接一次即可同步全库海报墙与播放进度。
          </p>
        </div>
        <div class="cl-head-actions">
          <button type="button" class="cl-btn-guide" @click="openTutorial('readme')">
            <BookOpen :size="15" aria-hidden="true" />
            <span>新手选软件总入门</span>
          </button>
          <RouterLink to="/game/operator" class="cl-btn-cred">
            <KeyRound :size="15" aria-hidden="true" />
            <span>{{ hasEmbyUsername ? '改播放密码 / 播放会话' : '设置专属接线账号' }}</span>
          </RouterLink>
        </div>
      </header>

      <!-- 连接凭据速览条（免翻页直接复制服务器地址与接线用户名，密码绝不进链接） -->
      <section class="cl-cred-bar" aria-label="客户端连接凭据速览">
        <div v-if="cardLoading" class="v4-skeleton" style="height: 56px; width: 100%"></div>
        <div v-else-if="!viewGranted" class="cl-cred-locked">
          <div>
            <strong>尚未开启片库观影权限</strong>
            <span>开启观影权限并设置专属播放账号后，即可查看服务器地址并使用一键导入。</span>
          </div>
          <RouterLink to="/game/operator" class="cl-btn-cta-sm">前往开启权限</RouterLink>
        </div>
        <div v-else class="cl-cred-grid">
          <div class="cl-cred-cell">
            <span class="cl-cred-k">服务器地址（{{ parsedEndpoint ? parsedEndpoint.scheme.toUpperCase() : 'HTTPS' }}）</span>
            <div class="cl-cred-v-wrap">
              <code class="cl-cred-v">{{ displayServerUrl }}</code>
              <button
                v-if="parsedEndpoint"
                type="button"
                class="cl-copy-chip"
                @click="copyValue(parsedEndpoint.rootUrl, '服务器地址')"
              >
                <Check v-if="copiedKey === '服务器地址'" :size="13" />
                <Copy v-else :size="13" />
                <span>{{ copiedKey === '服务器地址' ? '已复制' : '复制' }}</span>
              </button>
            </div>
          </div>

          <div class="cl-cred-cell is-narrow">
            <span class="cl-cred-k">端口（Port）</span>
            <div class="cl-cred-v-wrap">
              <code class="cl-cred-v">{{ parsedEndpoint ? parsedEndpoint.port : 443 }}</code>
              <span class="cl-cred-hint">勿填 8096</span>
            </div>
          </div>

          <div class="cl-cred-cell">
            <span class="cl-cred-k">接线用户名</span>
            <div v-if="hasEmbyUsername" class="cl-cred-v-wrap">
              <code class="cl-cred-v">{{ embyUsername }}</code>
              <button
                type="button"
                class="cl-copy-chip"
                @click="copyValue(embyUsername, '接线用户名')"
              >
                <Check v-if="copiedKey === '接线用户名'" :size="13" />
                <Copy v-else :size="13" />
                <span>{{ copiedKey === '接线用户名' ? '已复制' : '复制' }}</span>
              </button>
            </div>
            <div v-else class="cl-cred-v-wrap">
              <span class="cl-cred-unset">未设置（设置后解锁一键导入）</span>
              <RouterLink to="/game/operator" class="cl-setup-link">立即设置 →</RouterLink>
            </div>
          </div>

          <div class="cl-cred-cell">
            <span class="cl-cred-k">播放密码</span>
            <div class="cl-cred-v-wrap">
              <span class="cl-cred-hint">独立于网页密码 · 一键导入不带密码防泄露</span>
              <RouterLink to="/game/operator" class="cl-setup-link">
                {{ accountCard?.has_password ? '重置密码' : '去设置' }}
              </RouterLink>
            </div>
          </div>
        </div>
      </section>

      <!-- 按设备五端分页签 + 全部 16 款总览 -->
      <div class="cl-tabs-bar">
        <div class="cl-tabs" role="tablist" aria-label="按设备平台筛选播放器">
          <button
            v-for="tab in PLATFORM_TABS"
            :key="tab.key"
            type="button"
            role="tab"
            class="cl-tab"
            :class="{ 'is-active': activePlatform === tab.key }"
            :aria-selected="activePlatform === tab.key"
            @click="selectPlatform(tab.key)"
          >
            <span class="cl-tab-name">{{ tab.label }}</span>
            <span class="cl-tab-cnt">{{ tab.count }}</span>
          </button>
        </div>
        <div class="cl-platform-hint">
          当前设备组：<strong>{{ currentTabSpec.deviceHint }}</strong>
        </div>
      </div>

      <!-- 播放器卡片网格 -->
      <section class="cl-grid" aria-label="支持的播放器列表">
        <article v-for="card in visibleCards" :key="card.id" class="cl-card">
          <div class="cl-card-top">
            <!-- 暗厅几何品牌徽标：纯矢量单字标 + 角标折线，零外链裂图 -->
            <div class="cl-icon-box" :class="{ 'has-scheme': Boolean(card.importTarget) }" aria-hidden="true">
              <svg class="cl-icon-svg" viewBox="0 0 44 44" fill="none">
                <rect x="0.5" y="0.5" width="43" height="43" rx="1.5" stroke="currentColor" stroke-opacity="0.28" />
                <path d="M31 1H43V13" stroke="currentColor" stroke-opacity="0.55" stroke-width="1.5" />
                <circle v-if="card.importTarget" cx="38" cy="6" r="2" fill="#c3f53c" />
              </svg>
              <span class="cl-monogram">{{ card.monogram }}</span>
            </div>

            <div class="cl-title-col">
              <div class="cl-name-row">
                <h2 class="cl-card-name">{{ card.name }}</h2>
                <span v-if="card.tierBadge" class="cl-tier-badge" :class="{ 'is-free': card.isFreeTier }">
                  {{ card.tierBadge }}
                </span>
              </div>
              <p class="cl-card-sub">{{ card.subtitle }}</p>
            </div>
          </div>

          <!-- 价格标签 -->
          <div class="cl-price-row">
            <span class="cl-price-tag" :class="{ 'is-free': card.isFreeTier }">
              {{ card.priceLabel }}
            </span>
            <span v-if="card.importTarget" class="cl-scheme-chip">支持免密一键导入</span>
          </div>

          <p class="cl-card-desc">{{ card.summary }}</p>

          <p v-if="card.caution" class="cl-card-caution">
            {{ card.caution }}
          </p>

          <!-- 操作按钮组：下载 + 一键导入（有 URL Scheme 时高亮） + 教程入口 -->
          <div class="cl-card-actions">
            <button
              v-if="card.importTarget"
              type="button"
              class="cl-btn-import"
              @click="handleImportClick(card)"
            >
              <Zap :size="14" aria-hidden="true" />
              <span>一键导入</span>
            </button>
            <RouterLink
              v-else-if="card.canonicalId === 'infuse'"
              to="/library"
              class="cl-btn-sec"
            >
              <Play :size="14" aria-hidden="true" />
              <span>去片库直连拉起</span>
            </RouterLink>

            <a
              :href="card.downloadUrl"
              target="_blank"
              rel="noopener noreferrer"
              class="cl-btn-sec"
            >
              <Download :size="14" aria-hidden="true" />
              <span>{{ card.downloadLabel }}</span>
            </a>

            <button
              type="button"
              class="cl-btn-tut"
              @click="openTutorial(card.tutorialSlug)"
            >
              <BookOpen :size="14" aria-hidden="true" />
              <span>小白教程</span>
            </button>
          </div>
        </article>
      </section>

      <!-- 底部兼容规范与禁播提醒（对齐参考图红框与说明区） -->
      <section class="cl-rules-grid" aria-label="连接红线与重要说明">
        <div class="cl-rule-card is-warn">
          <h3 class="cl-rule-title">不推荐与已拦截的播放方式</h3>
          <ul class="cl-rule-list">
            <li>
              <strong>禁止开启 Infuse「媒体库模式」</strong>：媒体库模式会全量遍历片库触发网盘风控，服务器已拦截登录（报
              <code>403 该播放器已被服务器停用</code>）；请在「高级」中保持关闭媒体库模式（即直连模式）。
            </li>
            <li>
              <strong>请勿使用 Emby 官方端、魔改客户端或网页裸播</strong>：本站采用原画直放且不提供实时转码，官方端与浏览器缺少完整解码器会报错
              <code>404</code>，请统一使用上方 16 款内置解码播放器。
            </li>
          </ul>
        </div>
        <div class="cl-rule-card">
          <h3 class="cl-rule-title">手动填写四项核对口诀</h3>
          <ul class="cl-rule-list">
            <li><strong>类型必选 Emby</strong>：不要误选成 Jellyfin、Plex 或 WebDAV。</li>
            <li>
              <strong>HTTPS + 纯域名 + 443 端口</strong>：分栏填写时主机只填纯域名（不加
              <code>https://</code>、末尾不加 <code>/emby</code>），端口必须填 <code>443</code>（切勿保留默认
              <code>8096</code>）。
            </li>
            <li>
              <strong>密码安全承诺</strong>：点击本页「一键导入」仅自动传递服务器地址与接线用户名，<strong>播放密码绝不写入跳转链接</strong>，首次连接在播放器内输入一次即可保存。
            </li>
          </ul>
        </div>
      </section>
    </main>

    <!-- 暗厅小白教程抽屉 / 弹窗（渲染 docs/player-tutorials/*.md 原文） -->
    <Teleport to="body">
      <div
        v-if="activeTutorial"
        class="cl-modal-mask"
        @click.self="closeTutorial"
        @keydown.esc.window="closeTutorial"
      >
        <aside
          ref="drawerEl"
          class="cl-drawer"
          role="dialog"
          aria-modal="true"
          :aria-label="activeTutorial.title"
          tabindex="-1"
        >
          <header class="cl-drawer-head">
            <div class="cl-drawer-titles">
              <span class="cl-drawer-tag">小白图文配置指南</span>
              <h2 class="cl-drawer-title">{{ activeTutorial.title }}</h2>
            </div>
            <div class="cl-drawer-head-btns">
              <button
                v-if="activeTutorial.slug !== 'readme'"
                type="button"
                class="cl-copy-chip"
                @click="openTutorial('readme')"
              >
                <span>总目录</span>
              </button>
              <button type="button" class="cl-drawer-close" aria-label="关闭教程" @click="closeTutorial">
                <X :size="18" aria-hidden="true" />
              </button>
            </div>
          </header>

          <!-- 抽屉内嵌快捷复制条：边看教程边一键复制真实域名与用户名 -->
          <div v-if="viewGranted && parsedEndpoint" class="cl-drawer-cred">
            <span>你的服务器：<code>{{ parsedEndpoint.rootUrl }}</code></span>
            <button type="button" class="cl-copy-chip" @click="copyValue(parsedEndpoint.rootUrl, '教程内地址')">
              {{ copiedKey === '教程内地址' ? '已复制' : '复制地址' }}
            </button>
            <span>端口：<code>{{ parsedEndpoint.port }}</code></span>
            <template v-if="hasEmbyUsername">
              <span>用户名：<code>{{ embyUsername }}</code></span>
              <button type="button" class="cl-copy-chip" @click="copyValue(embyUsername, '教程内用户名')">
                {{ copiedKey === '教程内用户名' ? '已复制' : '复制用户名' }}
              </button>
            </template>
          </div>

          <!-- eslint-disable-next-line vue/no-v-html -->
          <div class="cl-drawer-body dh-md-root" @click="onTutorialBodyClick" v-html="renderedTutorialHtml"></div>
        </aside>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { BookOpen, Check, Copy, Download, KeyRound, Play, X, Zap } from 'lucide-vue-next'
import { embyApi, type AccountCard } from '@/api'
import { branding } from '@/composables/useBranding'
import { useToast } from '@/composables/useToast'
import { useClipboard } from '@/composables/useClipboard'
import {
  assignImportScheme,
  buildPasswordlessImportSchemes,
  findPlayerImportScheme,
  listImportSchemes,
  parseEmbyServerAddress,
} from '@/composables/usePlayerSchemes'
import {
  detectDefaultPlatform,
  getCardsForPlatform,
  PLATFORM_TABS,
  type ClientPlatformKey,
  type PlayerCardSpec,
} from '@/utils/playerCatalog'
import { renderTutorialMarkdown } from '@/utils/tutorialMarkdown'
import { PLAYER_TUTORIAL_DOCS, type TutorialDocEntry } from '@/content/playerTutorials'

const route = useRoute()
const router = useRouter()
const toast = useToast()
const { copy } = useClipboard()

const VALID_PLATFORMS = new Set<ClientPlatformKey>(['apple', 'android', 'android_tv', 'windows', 'harmony', 'all'])

function initialPlatform(): ClientPlatformKey {
  const q = String(route.query.platform || '').trim().toLowerCase() as ClientPlatformKey
  if (VALID_PLATFORMS.has(q)) return q
  return detectDefaultPlatform(typeof navigator !== 'undefined' ? navigator.userAgent : '')
}

const activePlatform = ref<ClientPlatformKey>(initialPlatform())
const cardLoading = ref(true)
const accountCard = ref<AccountCard | null>(null)
const copiedKey = ref('')
let copyResetTimer: number | undefined

const activeTutorialSlug = ref<string>('')
const drawerEl = ref<HTMLElement | null>(null)

const currentTabSpec = computed(
  () => PLATFORM_TABS.find((t) => t.key === activePlatform.value) ?? PLATFORM_TABS[0]!,
)

const visibleCards = computed(() => getCardsForPlatform(activePlatform.value))

const viewGranted = computed(() => accountCard.value?.view_permission?.granted !== false)
const embyUsername = computed(() => (accountCard.value?.emby_username || '').trim())
const hasEmbyUsername = computed(() => Boolean(embyUsername.value))

const fallbackOrigin = computed(() =>
  typeof window !== 'undefined' && window.location ? window.location.origin : '',
)

const rawServerUrl = computed(() => (accountCard.value?.base_url || '').trim() || fallbackOrigin.value)
const parsedEndpoint = computed(() => parseEmbyServerAddress(rawServerUrl.value))
const displayServerUrl = computed(() => parsedEndpoint.value?.rootUrl || rawServerUrl.value || '加载中…')

const passwordlessSchemes = computed(() => {
  if (!viewGranted.value || accountCard.value?.external) return []
  const map = buildPasswordlessImportSchemes(
    rawServerUrl.value,
    embyUsername.value,
    branding.site_name || '帆桥影院',
    accountCard.value?.import_schemes,
  )
  return listImportSchemes(map)
})

const activeTutorial = computed<TutorialDocEntry | null>(() => {
  const slug = activeTutorialSlug.value.trim().toLowerCase()
  if (!slug) return null
  return PLAYER_TUTORIAL_DOCS[slug] ?? null
})

const renderedTutorialHtml = computed(() => {
  if (!activeTutorial.value) return ''
  return renderTutorialMarkdown(activeTutorial.value.markdown, {
    serverUrl: parsedEndpoint.value?.rootUrl,
  })
})

function selectPlatform(key: ClientPlatformKey) {
  activePlatform.value = key
  const nextQuery = { ...route.query, platform: key }
  void router.replace({ query: nextQuery })
}

async function copyValue(text: string, key: string) {
  if (!text) return
  const ok = await copy(text, key)
  if (!ok) return
  copiedKey.value = key
  if (copyResetTimer) window.clearTimeout(copyResetTimer)
  copyResetTimer = window.setTimeout(() => {
    copiedKey.value = ''
  }, 2000)
}

function handleImportClick(card: PlayerCardSpec) {
  if (!card.importTarget) return
  if (!viewGranted.value) {
    toast.info('请先开启片库观影权限后再使用一键导入')
    void router.push('/game/operator')
    return
  }
  if (!hasEmbyUsername.value) {
    toast.info('请先设置专属播放用户名与密码，设置后即可一键导入')
    void router.push('/game/operator')
    return
  }
  const matched = findPlayerImportScheme(passwordlessSchemes.value, card.importTarget)
  if (!matched?.url) {
    toast.info('暂未获取到有效服务器地址，请使用手动填写教程连接')
    return
  }
  // 严格遵守安全红线：此处只拉起含「服务器地址 + 用户名」的免密链接，绝不拼入播放密码
  toast.info(`正在拉起 ${card.name}（已自动带入服务器与用户名，进入软件后填一次播放密码即可）`)
  assignImportScheme(matched.url)
}

function openTutorial(slug: string) {
  const clean = slug.trim().toLowerCase()
  if (!PLAYER_TUTORIAL_DOCS[clean]) return
  activeTutorialSlug.value = clean
  document.body.style.overflow = 'hidden'
  void nextTick(() => {
    drawerEl.value?.focus()
    const body = drawerEl.value?.querySelector('.cl-drawer-body')
    if (body) body.scrollTop = 0
  })
}

function closeTutorial() {
  activeTutorialSlug.value = ''
  document.body.style.overflow = ''
}

function onTutorialBodyClick(e: MouseEvent) {
  const target = (e.target as HTMLElement | null)?.closest?.('a[data-tutorial-slug]') as HTMLAnchorElement | null
  if (!target) return
  const slug = target.getAttribute('data-tutorial-slug')
  if (slug && PLAYER_TUTORIAL_DOCS[slug]) {
    e.preventDefault()
    openTutorial(slug)
  }
}

watch(
  () => route.query.tutorial,
  (q) => {
    const slug = String(q || '').trim().toLowerCase()
    if (slug && PLAYER_TUTORIAL_DOCS[slug]) {
      openTutorial(slug)
    }
  },
)

onMounted(async () => {
  const initTut = String(route.query.tutorial || '').trim().toLowerCase()
  if (initTut && PLAYER_TUTORIAL_DOCS[initTut]) {
    openTutorial(initTut)
  }
  cardLoading.value = true
  try {
    accountCard.value = await embyApi.getAccountCard()
  } catch {
    accountCard.value = null
  } finally {
    cardLoading.value = false
  }
})

onUnmounted(() => {
  if (copyResetTimer) window.clearTimeout(copyResetTimer)
  document.body.style.overflow = ''
})
</script>

<style scoped>
.cl-page {
  padding: 28px 24px 68px;
}

.cl-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 24px;
  flex-wrap: wrap;
  padding-bottom: 20px;
  border-bottom: 1px solid #222222;
}

.cl-head-main {
  max-width: 72ch;
  min-width: 0;
}

.cl-eyebrow {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
  font-size: 12px;
  color: #9a9a9a;
}

.cl-eyebrow-tag {
  padding: 2px 7px;
  background: #1e1e1e;
  border: 1px solid #2a2a2a;
  border-radius: 2px;
  color: #c3f53c;
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.04em;
}

.cl-title {
  margin: 0;
  font-size: clamp(26px, 2.5vw, 34px);
  font-weight: 800;
  letter-spacing: -0.02em;
  line-height: 1.15;
  color: #f2f2f2;
}

.cl-sub {
  margin: 8px 0 0;
  font-size: 14.5px;
  line-height: 1.6;
  color: #9a9a9a;
}

.cl-head-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  flex-shrink: 0;
}

.cl-btn-guide,
.cl-btn-cred {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  min-height: 40px;
  padding: 0 16px;
  border-radius: 2px;
  font-size: 13.5px;
  font-weight: 700;
  font-family: inherit;
  text-decoration: none;
  cursor: pointer;
  transition: border-color 0.15s ease, color 0.15s ease, background-color 0.15s ease;
}

.cl-btn-guide {
  background: #1e1e1e;
  border: 1px solid #383838;
  color: #f2f2f2;
}

.cl-btn-guide:hover {
  border-color: #c3f53c;
  color: #c3f53c;
}

.cl-btn-cred {
  background: #171717;
  border: 1px solid #2a2a2a;
  color: #d4d4d4;
}

.cl-btn-cred:hover {
  border-color: #f2f2f2;
  color: #f2f2f2;
}

/* 连接凭据速览条 */
.cl-cred-bar {
  margin-top: 18px;
  background: #171717;
  border: 1px solid #2a2a2a;
  border-radius: 2px;
  padding: 14px 18px;
}

.cl-cred-locked {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.cl-cred-locked strong {
  display: block;
  font-size: 14.5px;
  color: #f2f2f2;
}

.cl-cred-locked span {
  font-size: 13px;
  color: #9a9a9a;
}

.cl-btn-cta-sm {
  display: inline-flex;
  align-items: center;
  height: 36px;
  padding: 0 16px;
  border-radius: 2px;
  background: #c3f53c;
  color: #111111;
  font-size: 13px;
  font-weight: 800;
  text-decoration: none;
}

.cl-cred-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.4fr) auto minmax(0, 1fr) minmax(0, 1.15fr);
  gap: 14px 22px;
  align-items: center;
}

.cl-cred-cell {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.cl-cred-k {
  font-size: 11.5px;
  font-weight: 600;
  color: #9a9a9a;
}

.cl-cred-v-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  flex-wrap: wrap;
}

.cl-cred-v {
  font-family: var(--dh-font-mono);
  font-size: 13.5px;
  font-weight: 700;
  color: #f2f2f2;
  font-variant-numeric: tabular-nums;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cl-cred-hint {
  font-size: 12px;
  color: #9a9a9a;
}

.cl-cred-unset {
  font-size: 13px;
  color: #9a9a9a;
}

.cl-setup-link {
  font-size: 12.5px;
  font-weight: 700;
  color: #c3f53c;
  text-decoration: none;
  white-space: nowrap;
}

.cl-copy-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 26px;
  padding: 0 8px;
  border-radius: 2px;
  border: 1px solid #2e2e2e;
  background: #1e1e1e;
  color: #d4d4d4;
  font-size: 11.5px;
  font-weight: 600;
  font-family: inherit;
  cursor: pointer;
  flex-shrink: 0;
}

.cl-copy-chip:hover {
  border-color: #c3f53c;
  color: #c3f53c;
}

/* 五端设备分页签 */
.cl-tabs-bar {
  margin-top: 24px;
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  border-bottom: 1px solid #222222;
}

.cl-tabs {
  display: flex;
  align-items: center;
  gap: 22px;
  overflow-x: auto;
  scrollbar-width: none;
  max-width: 100%;
}

.cl-tabs::-webkit-scrollbar {
  display: none;
}

.cl-tab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 42px;
  padding: 6px 2px 11px;
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: #9a9a9a;
  font-size: 15px;
  font-weight: 600;
  font-family: inherit;
  white-space: nowrap;
  cursor: pointer;
  transition: color 0.15s ease, border-color 0.15s ease;
}

.cl-tab:hover {
  color: #f2f2f2;
}

.cl-tab.is-active {
  color: #f2f2f2;
  border-bottom-color: #c3f53c;
  font-weight: 800;
}

.cl-tab-cnt {
  font-family: var(--dh-font-mono);
  font-size: 11px;
  font-weight: 700;
  color: #6e6e6e;
  font-variant-numeric: tabular-nums;
}

.cl-tab.is-active .cl-tab-cnt {
  color: #c3f53c;
}

.cl-platform-hint {
  padding-bottom: 10px;
  font-size: 12.5px;
  color: #9a9a9a;
}

.cl-platform-hint strong {
  color: #f2f2f2;
  font-weight: 600;
}

/* 播放器卡片矩阵 */
.cl-grid {
  margin-top: 20px;
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
}

@media (max-width: 1180px) {
  .cl-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .cl-cred-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

.cl-card {
  display: flex;
  flex-direction: column;
  padding: 18px 20px;
  background: #171717;
  border: 1px solid #2a2a2a;
  border-radius: 2px;
  min-width: 0;
  transition: border-color 0.16s ease;
}

@media (hover: hover) and (pointer: fine) {
  .cl-card:hover {
    border-color: #3d3d3d;
  }
}

.cl-card-top {
  display: flex;
  align-items: center;
  gap: 14px;
}

.cl-icon-box {
  position: relative;
  width: 44px;
  height: 44px;
  flex: 0 0 44px;
  border-radius: 2px;
  background: #111111;
  color: #9a9a9a;
  display: flex;
  align-items: center;
  justify-content: center;
}

.cl-icon-box.has-scheme {
  color: #f2f2f2;
}

.cl-icon-svg {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
}

.cl-monogram {
  position: relative;
  z-index: 1;
  font-family: var(--dh-font-mono);
  font-size: 14px;
  font-weight: 800;
  letter-spacing: 0.04em;
  color: #f2f2f2;
}

.cl-title-col {
  flex: 1;
  min-width: 0;
}

.cl-name-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.cl-card-name {
  margin: 0;
  font-size: 17px;
  font-weight: 800;
  letter-spacing: -0.01em;
  color: #f2f2f2;
  line-height: 1.25;
}

.cl-tier-badge {
  padding: 1px 6px;
  border-radius: 2px;
  background: #1e1e1e;
  border: 1px solid #383838;
  color: #f2f2f2;
  font-size: 11px;
  font-weight: 700;
}

.cl-tier-badge.is-free {
  color: #c3f53c;
  border-color: rgba(195, 245, 60, 0.35);
}

.cl-card-sub {
  margin: 3px 0 0;
  font-size: 12.5px;
  color: #9a9a9a;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cl-price-row {
  margin-top: 12px;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}

.cl-price-tag {
  padding: 3px 8px;
  border-radius: 2px;
  background: #1e1e1e;
  border: 1px solid #2a2a2a;
  color: #d4d4d4;
  font-size: 12px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.cl-price-tag.is-free {
  color: #f2f2f2;
  border-color: #363636;
}

.cl-scheme-chip {
  padding: 3px 7px;
  border-radius: 2px;
  background: #141414;
  border: 1px solid #2a2a2a;
  color: #9a9a9a;
  font-size: 11px;
  font-weight: 600;
}

.cl-card-desc {
  margin: 10px 0 0;
  font-size: 13.5px;
  line-height: 1.6;
  color: #c6c6c6;
  flex: 1;
}

.cl-card-caution {
  margin: 10px 0 0;
  padding: 8px 10px;
  background: #131313;
  border-left: 2px solid #c3f53c;
  color: #b8b8b8;
  font-size: 12px;
  line-height: 1.55;
}

.cl-card-actions {
  margin-top: 16px;
  padding-top: 14px;
  border-top: 1px solid #222222;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

/* 一键导入按钮：本卡片唯一荧光绿主操作 */
.cl-btn-import {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  min-height: 36px;
  padding: 0 13px;
  border-radius: 2px;
  background: #c3f53c;
  border: 1px solid #c3f53c;
  color: #111111;
  font-size: 13px;
  font-weight: 800;
  font-family: inherit;
  cursor: pointer;
  white-space: nowrap;
  transition: background-color 0.15s ease, border-color 0.15s ease;
}

.cl-btn-import:hover {
  background: #d1fa58;
  border-color: #d1fa58;
}

.cl-btn-sec,
.cl-btn-tut {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  min-height: 36px;
  padding: 0 12px;
  border-radius: 2px;
  font-size: 13px;
  font-weight: 600;
  font-family: inherit;
  text-decoration: none;
  cursor: pointer;
  white-space: nowrap;
  transition: border-color 0.15s ease, color 0.15s ease, background-color 0.15s ease;
}

.cl-btn-sec {
  background: #1e1e1e;
  border: 1px solid #2f2f2f;
  color: #f2f2f2;
}

.cl-btn-sec:hover {
  border-color: #f2f2f2;
}

.cl-btn-tut {
  margin-left: auto;
  background: transparent;
  border: 1px solid #2a2a2a;
  color: #b8b8b8;
}

.cl-btn-tut:hover {
  border-color: #c3f53c;
  color: #c3f53c;
}

/* 底部规则说明双栏 */
.cl-rules-grid {
  margin-top: 28px;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}

.cl-rule-card {
  padding: 18px 20px;
  background: #171717;
  border: 1px solid #2a2a2a;
  border-radius: 2px;
}

.cl-rule-card.is-warn {
  border-left: 2px solid #e5534b;
}

.cl-rule-title {
  margin: 0 0 10px;
  font-size: 15px;
  font-weight: 800;
  color: #f2f2f2;
}

.cl-rule-list {
  margin: 0;
  padding-left: 18px;
  font-size: 13.5px;
  line-height: 1.65;
  color: #9a9a9a;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.cl-rule-list strong {
  color: #f2f2f2;
}

.cl-rule-list code {
  padding: 1px 5px;
  background: #111111;
  border: 1px solid #2a2a2a;
  border-radius: 2px;
  font-family: var(--dh-font-mono);
  font-size: 12px;
  color: #f2f2f2;
}

/* 教程阅读侧栏抽屉 */
.cl-modal-mask {
  position: fixed;
  inset: 0;
  z-index: 110;
  background: rgba(0, 0, 0, 0.78);
  display: flex;
  justify-content: flex-end;
}

.cl-drawer {
  width: min(760px, 100vw);
  height: 100%;
  background: #141414;
  border-left: 1px solid #2a2a2a;
  display: flex;
  flex-direction: column;
  outline: none;
}

.cl-drawer-head {
  padding: 18px 24px;
  background: #171717;
  border-bottom: 1px solid #2a2a2a;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
}

.cl-drawer-titles {
  min-width: 0;
}

.cl-drawer-tag {
  font-size: 11px;
  font-weight: 700;
  color: #c3f53c;
  letter-spacing: 0.05em;
}

.cl-drawer-title {
  margin: 2px 0 0;
  font-size: 18px;
  font-weight: 800;
  color: #f2f2f2;
}

.cl-drawer-head-btns {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.cl-drawer-close {
  width: 34px;
  height: 34px;
  border-radius: 2px;
  border: 1px solid #2a2a2a;
  background: #1e1e1e;
  color: #f2f2f2;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
}

.cl-drawer-close:hover {
  border-color: #c3f53c;
  color: #c3f53c;
}

.cl-drawer-cred {
  padding: 10px 24px;
  background: #111111;
  border-bottom: 1px solid #242424;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  font-size: 12.5px;
  color: #9a9a9a;
}

.cl-drawer-cred code {
  font-family: var(--dh-font-mono);
  color: #f2f2f2;
  font-weight: 700;
}

.cl-drawer-body {
  flex: 1;
  overflow-y: auto;
  padding: 22px 26px 48px;
  color: #d8d8d8;
  font-size: 15px;
  line-height: 1.72;
}

/* 教程 Markdown 排版（scoped :deep） */
.dh-md-root :deep(.dh-md-h1) {
  display: none;
}

.dh-md-root :deep(.dh-md-h2) {
  margin: 26px 0 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid #262626;
  font-size: 18px;
  font-weight: 800;
  color: #f2f2f2;
}

.dh-md-root :deep(.dh-md-h2:first-child) {
  margin-top: 0;
}

.dh-md-root :deep(.dh-md-h3),
.dh-md-root :deep(.dh-md-h4) {
  margin: 18px 0 8px;
  font-size: 15.5px;
  font-weight: 700;
  color: #f2f2f2;
}

.dh-md-root :deep(.dh-md-p) {
  margin: 10px 0;
  color: #d0d0d0;
}

.dh-md-root :deep(.dh-md-hr) {
  border: 0;
  border-top: 1px solid #222222;
  margin: 20px 0;
}

.dh-md-root :deep(.dh-md-quote) {
  margin: 14px 0;
  padding: 10px 14px;
  background: #191919;
  border: 1px solid #2a2a2a;
  border-left: 2px solid #c3f53c;
  border-radius: 2px;
  color: #b8b8b8;
  font-size: 13.5px;
}

.dh-md-root :deep(.dh-md-quote p) {
  margin: 0;
}

.dh-md-root :deep(.dh-md-ol),
.dh-md-root :deep(.dh-md-ul) {
  margin: 10px 0;
  padding-left: 22px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.dh-md-root :deep(.dh-md-sublist) {
  margin-top: 6px;
  padding-left: 18px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  color: #b8b8b8;
  font-size: 14px;
}

.dh-md-root :deep(strong) {
  color: #f2f2f2;
  font-weight: 700;
}

.dh-md-root :deep(.dh-md-code) {
  padding: 1px 6px;
  background: #1e1e1e;
  border: 1px solid #2e2e2e;
  border-radius: 2px;
  font-family: var(--dh-font-mono);
  font-size: 13px;
  color: #c3f53c;
}

.dh-md-root :deep(.dh-md-link) {
  color: #c3f53c;
  text-decoration: underline;
  text-underline-offset: 3px;
}

.dh-md-root :deep(.dh-md-table-wrap) {
  margin: 14px 0;
  overflow-x: auto;
  border: 1px solid #2a2a2a;
  border-radius: 2px;
}

.dh-md-root :deep(.dh-md-table) {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  line-height: 1.5;
}

.dh-md-root :deep(.dh-md-table th),
.dh-md-root :deep(.dh-md-table td) {
  padding: 9px 12px;
  border-bottom: 1px solid #242424;
  text-align: left;
  vertical-align: top;
}

.dh-md-root :deep(.dh-md-table th) {
  background: #1b1b1b;
  color: #f2f2f2;
  font-weight: 700;
  white-space: nowrap;
}

@media (max-width: 767px) {
  .cl-page {
    padding: 16px 16px calc(88px + env(safe-area-inset-bottom));
  }

  .cl-head-actions {
    width: 100%;
  }

  .cl-btn-guide,
  .cl-btn-cred {
    flex: 1 1 0;
    justify-content: center;
    padding: 0 10px;
    font-size: 13px;
  }

  .cl-cred-grid,
  .cl-grid,
  .cl-rules-grid {
    grid-template-columns: 1fr;
  }

  .cl-platform-hint {
    display: none;
  }

  .cl-card {
    padding: 16px;
  }

  .cl-btn-tut {
    margin-left: 0;
  }

  .cl-drawer-head,
  .cl-drawer-cred,
  .cl-drawer-body {
    padding-left: 16px;
    padding-right: 16px;
  }
}
</style>
