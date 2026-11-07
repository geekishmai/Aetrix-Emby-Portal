<template>
  <div class="v4-scope p2-home-scope">
    <WorldHome v-if="homeWorldActive" />
    <template v-else>
      <header class="p2-home-hero" aria-label="观影导览">
        <div class="p2-hero-inner">
          <div class="p2-hero-titles">
            <h1 class="p2-hero-greeting">{{ greeting }}，{{ nickname }}</h1>
            <p class="p2-hero-sub">
              <span>{{ dateLabel }}</span>
              <span>·</span>
              <span>选一部好片，一键唤起 Infuse、VidHub 或 SenPlayer 流畅直放</span>
            </p>
          </div>
          <div class="p2-hero-actions">
            <RouterLink to="/library" class="p2-btn-primary">浏览片库</RouterLink>
            <RouterLink to="/game/operator" class="p2-btn-ghost">播放器接入</RouterLink>
          </div>
        </div>
      </header>

      <!-- 第 2 期看片优先首屏三联：最新入库海报行 + 继续观看进度行 + 各库热门海报行 -->
      <LatestPosterRow v-if="user.isLoggedIn" />
      <ResumePosterRow v-if="user.isLoggedIn" />
      <LibraryHotRow v-if="user.isLoggedIn" />

      <!-- 合集货架（无合集或总闸关闭时零占位） -->
      <section
        v-if="collectionsLoading || collectionsError || shelfCollections.length > 0"
        id="disc-collections"
        class="p2-band-wrap"
        aria-label="精选合集"
      >
        <div class="p2-surface-card">
          <div class="p2-card-head">
            <div>
              <h2 class="p2-card-title">精选合集</h2>
              <p class="p2-card-sub">系列电影与专题片单</p>
            </div>
            <RouterLink v-if="!collectionsLoading && !collectionsError" to="/collections" class="p2-card-link">
              查看全部 ›
            </RouterLink>
          </div>
          <div v-if="collectionsLoading" class="v4-skeleton" aria-hidden="true" style="height:96px"></div>
          <div v-else-if="collectionsError" class="v4-err">
            合集加载失败：{{ collectionsError }}
            <button type="button" class="v4-retry" @click="loadCollections">重试</button>
          </div>
          <div v-else class="p2-collections-grid">
            <CollectionCard v-for="c in shelfCollections" :key="c.id" :collection="c" />
          </div>
        </div>
      </section>

      <!-- 媒体库速览带 -->
      <LibraryBand v-if="user.isLoggedIn" />

      <!-- 服务器状态与片源实时动态（双栏） -->
      <section v-if="user.isLoggedIn" class="p2-ops-band" aria-label="服务器与入库动态">
        <div class="p2-ops-grid">
          <div class="p2-ops-col">
            <div v-if="visibleCount === 0 && !serversLoading && !serversLoadError" class="p2-surface-card">
              <div class="v4-empty">
                暂无可见服务器，去<RouterLink class="v4-empty-link" to="/game/account">我的页面</RouterLink>恢复显示
              </div>
            </div>
            <TicketCard
              v-else
              :loading="serversLoading"
              :servers="visibleServers"
              :mode="cardMode"
              :watch-strip="watchStrip"
              :error="serversLoadError"
              :skeleton-count="lastServerCount"
              :hide-gamification="true"
              @retry="loadServers"
            />

            <template v-if="homeSections">
              <template v-for="s in homeSections" :key="s.key">
                <RecentUpdates v-if="s.key === 'recently_updated'" :items="s.items as RecentAddedItem[]" />
              </template>
            </template>
          </div>

          <div id="feed" class="p2-surface-card" aria-label="实时动态">
            <div class="p2-card-head">
              <div>
                <h2 class="p2-card-title">实时动态</h2>
                <p class="p2-card-sub">入库 · 刮削 · 播放 · 跟随当前服</p>
              </div>
            </div>
            <div v-if="feed.loading" class="v4-skeleton" style="height:220px" aria-hidden="true"></div>
            <div v-else-if="feed.error" class="v4-err">
              动态加载失败：{{ feed.error }}
              <button type="button" class="v4-retry" @click="() => panel.loadFeed(true)">重试</button>
            </div>
            <div v-else-if="feedItems.length === 0" class="v4-empty">还没有实时动态</div>
            <div v-else class="p2-feed-list">
              <div v-for="it in feedItems.slice(0, 8)" :key="it.key" class="p2-feed-row">
                <span class="p2-feed-time">{{ it.time }}</span>
                <span class="p2-feed-text">{{ it.text }}</span>
                <span v-if="it.library" class="p2-feed-lib">{{ it.library }}</span>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- 游戏化可折叠收纳区：能量 / 体力 / 每日一封 / 抽一签 / 世界 / 到此一游 / 收藏家 / 观影碑；
           后台「站点与品牌 → 显示游戏化与世界组件」关闭时整块隐藏 -->
      <GamificationFold v-if="user.isLoggedIn && gamificationOn" />
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { RouterLink } from 'vue-router'
import { usePanelStore } from '@/stores/panel'
import { useUserStore } from '@/stores/user'
import { branding } from '@/composables/useBranding'
import { useHomeViewMode } from '@/composables/useHomeViewMode'
import { useServerCards } from '@/composables/useServerCards'
import { useServerCardPrefs } from '@/composables/useServerCardPrefs'
import { humanizeError, type FeedItem } from '@/composables/usePanelApi'
import { collectionsApi, embyApi, type HomeSection, type RecentAddedItem, type ShelfCollection } from '@/api'
import { formatFeedText } from '@/utils/mediaTitle'
import WorldHome from '@/components/panel/WorldHome.vue'
import LatestPosterRow from '@/components/panel/LatestPosterRow.vue'
import ResumePosterRow from '@/components/panel/ResumePosterRow.vue'
import LibraryHotRow from '@/components/panel/LibraryHotRow.vue'
import LibraryBand from '@/components/panel/LibraryBand.vue'
import TicketCard from '@/components/panel/TicketCard.vue'
import RecentUpdates from '@/components/panel/RecentUpdates.vue'
import CollectionCard from '@/components/panel/CollectionCard.vue'
import GamificationFold from '@/components/panel/GamificationFold.vue'

const panel = usePanelStore()
const user = useUserStore()
const { worldActive: homeWorldActive } = useHomeViewMode()
const { today, feed } = storeToRefs(panel)

const gamificationOn = computed(() => branding.gamification_enabled !== false)

const collectionsLoading = ref(false)
const collectionsError = ref('')
const shelfCollections = ref<ShelfCollection[]>([])
async function loadCollections() {
  if (collectionsLoading.value) return
  collectionsLoading.value = true
  collectionsError.value = ''
  try {
    const data = await collectionsApi.getShelves()
    shelfCollections.value = Array.isArray(data?.collections) ? data.collections : []
  } catch (err: unknown) {
    const status = (err as { response?: { status?: number } })?.response?.status
    if (status === 404) {
      shelfCollections.value = []
    } else {
      collectionsError.value = humanizeError(err)
    }
  } finally {
    collectionsLoading.value = false
  }
}

const serverCards = useServerCards()
const cardPrefs = useServerCardPrefs()
const { mode, isVisible } = cardPrefs
const {
  loading: serversLoading,
  loadError: serversLoadError,
  servers: serverList,
  watchStrip,
  load: loadServers,
} = serverCards

const visibleServers = computed(() => serverList.value.filter((s) => isVisible(s.id)))
const visibleCount = computed(() => visibleServers.value.length)

const LAST_SERVER_COUNT_KEY = 'aetrix:lastServerCount:v2'
const lastServerCount = ref(1)
try {
  const n = Number(localStorage.getItem(LAST_SERVER_COUNT_KEY))
  if (Number.isInteger(n) && n > 0) lastServerCount.value = n
} catch {
  /* ignore */
}
watch(visibleServers, (v) => {
  if (v.length > 0) {
    lastServerCount.value = v.length
    try {
      localStorage.setItem(LAST_SERVER_COUNT_KEY, String(v.length))
    } catch {
      /* ignore */
    }
  }
})

const cardMode = computed<'cards' | 'overview'>(() => {
  if (mode.value !== 'auto') return mode.value
  return visibleCount.value === 1 ? 'cards' : 'overview'
})

const homeSections = ref<HomeSection[] | null>(null)
async function loadHomeSections() {
  if (!user.isLoggedIn) return
  try {
    const d = await embyApi.getHomeSections()
    homeSections.value = Array.isArray(d?.sections) ? d.sections : null
  } catch {
    homeSections.value = null
  }
}

const todayData = computed(() => (today.value.data || {}) as Record<string, unknown>)
const dateLabel = computed(() => {
  const raw = todayData.value.date as string | undefined
  if (raw) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(raw)
    if (m) return `${Number(m[2])}月${Number(m[3])}日`
  }
  const d = new Date()
  return `${d.getMonth() + 1}月${d.getDate()}日`
})

const greeting = computed(() => {
  const g = todayData.value.greeting as string | undefined
  if (g) return g
  const h = new Date().getHours()
  if (h < 5) return '夜深了'
  if (h < 9) return '早上好'
  if (h < 12) return '上午好'
  if (h < 14) return '中午好'
  if (h < 18) return '下午好'
  return '晚上好'
})

const nickname = computed(() => {
  const n = todayData.value.nickname as string | undefined
  if (n) return n
  const u = user.user as Record<string, unknown> | null
  return (u?.nickname as string) || (u?.username as string) || '观影人'
})

function fmtFeedTime(at: string | null): string {
  if (!at) return ''
  const d = new Date(at)
  if (Number.isNaN(d.getTime())) return ''
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const now = new Date()
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate()
  return sameDay ? `${hh}:${mm}` : `${d.getMonth() + 1}-${d.getDate()} ${hh}:${mm}`
}

const feedItems = computed((): Array<{ key: string; time: string; text: string; library: string | null }> => {
  const d = feed.value.data as { items?: FeedItem[] } | null
  const items = Array.isArray(d?.items) ? d.items : []
  return items.map((it, i) => ({
    key: `${i}-${it.at || ''}-${String(it.text ?? '').slice(0, 12)}`,
    time: fmtFeedTime(it.at),
    text:
      typeof it.text === 'string' && it.text.trim()
        ? formatFeedText(it.text)
        : '片库已同步更新',
    library: typeof it.library === 'string' && it.library ? it.library : null,
  }))
})

onMounted(() => {
  void panel.loadHome()
  void panel.loadFeed()
  void loadServers()
  if (branding.collections_enabled) void loadCollections()
  if (branding.home_sections_enabled) void loadHomeSections()
})
</script>

<style scoped>
.p2-home-scope {
  padding-bottom: 48px;
}

.p2-home-hero {
  max-width: min(1480px, 94vw);
  margin: 12px auto 0;
  padding: 0 20px;
}

.p2-hero-inner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 4px 10px;
  border-bottom: 1px solid #d1d1cd;
}

.p2-hero-titles {
  min-width: 0;
}

.p2-hero-greeting {
  margin: 0;
  font-size: 22px;
  font-weight: 500;
  line-height: 1.3;
  letter-spacing: 0;
  color: #27251e;
}

.p2-hero-sub {
  margin: 4px 0 0;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  font-size: 14px;
  color: #524b42;
}

.p2-hero-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}

.p2-btn-primary {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 40px;
  padding: 0 18px;
  border-radius: 12px;
  background: #016a71;
  color: #ffffff;
  font-size: 14px;
  font-weight: 500;
  text-decoration: none;
  white-space: nowrap;
  transition: opacity 0.16s ease, transform 0.16s ease;
}

.p2-btn-primary:hover {
  transform: translateY(-1px);
  opacity: 0.92;
}

.p2-btn-ghost {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 40px;
  padding: 0 14px;
  border-radius: 6px;
  border: 1px solid #d1d1cd;
  background: #fdfbfa;
  color: #27251e;
  font-size: 14px;
  font-weight: 500;
  text-decoration: none;
  white-space: nowrap;
  transition: opacity 0.16s ease, transform 0.16s ease;
}

.p2-btn-ghost:hover {
  transform: translateY(-1px);
  opacity: 0.88;
}

.p2-band-wrap,
.p2-ops-band {
  max-width: min(1480px, 94vw);
  margin: 16px auto 0;
  padding: 0 20px;
}

.p2-ops-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1.25fr);
  gap: 16px;
  align-items: start;
}

.p2-ops-col {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}

.p2-surface-card {
  background: #fdfbfa;
  border: 1px solid #d1d1cd;
  border-radius: 16px;
  padding: 18px 22px;
  min-width: 0;
}

.p2-card-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.p2-card-title {
  margin: 0;
  font-size: 17px;
  font-weight: 500;
  color: #27251e;
}

.p2-card-sub {
  margin: 2px 0 0;
  font-size: 14px;
  color: #524b42;
}

.p2-card-link {
  color: #016a71;
  font-size: 14px;
  font-weight: 500;
  text-decoration: none;
  white-space: nowrap;
}

.p2-collections-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 12px;
}

.p2-feed-list {
  display: flex;
  flex-direction: column;
}

.p2-feed-row {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 9px 0;
  border-top: 1px solid #e7e4de;
  font-size: 14px;
}

.p2-feed-row:first-child {
  border-top: 0;
  padding-top: 2px;
}

.p2-feed-time {
  flex: 0 0 auto;
  font-size: 13px;
  color: #524b42;
  font-variant-numeric: tabular-nums;
}

.p2-feed-text {
  flex: 1 1 auto;
  min-width: 0;
  color: #27251e;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.p2-feed-lib {
  flex: 0 0 auto;
  padding: 2px 8px;
  border-radius: 9999px;
  border: 1px solid #d1d1cd;
  background: #faf8f5;
  color: #524b42;
  font-size: 12px;
}

.v4-empty-link {
  display: inline-flex;
  align-items: center;
  min-height: 44px;
  padding: 4px 12px;
  margin: -4px -12px;
  color: #016a71;
  text-decoration: none;
}

@media (max-width: 1023px) {
  .p2-ops-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 767px) {
  .p2-home-scope {
    padding-bottom: calc(84px + env(safe-area-inset-bottom));
  }

  .p2-home-hero,
  .p2-band-wrap,
  .p2-ops-band {
    max-width: 100%;
    padding: 0 16px;
  }

  .p2-hero-inner {
    flex-direction: column;
    align-items: flex-start;
    gap: 12px;
  }

  .p2-hero-greeting {
    font-size: 20px;
  }

  .p2-hero-actions {
    width: 100%;
  }

  .p2-btn-primary,
  .p2-btn-ghost {
    min-height: 44px;
    flex: 1 1 0;
  }

  .p2-surface-card {
    padding: 14px 16px;
  }
}
</style>
