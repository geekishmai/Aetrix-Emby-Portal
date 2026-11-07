<template>
  <div class="v4-scope">
    <main class="v4-page v4-page-wide md-page">
      <div class="md-topbar">
        <button type="button" class="md-back-btn" @click="goBack">
          <ArrowLeft :size="16" aria-hidden="true" />
          <span>返回片库</span>
        </button>
        <RouterLink
          v-if="item?.Type === 'Episode' && item.SeriesId"
          :to="`/media/${encodeURIComponent(item.SeriesId)}`"
          class="md-series-back"
        >
          <span>查看整部剧集{{ item.SeriesName ? ` · ${item.SeriesName}` : '' }}</span>
        </RouterLink>
      </div>

      <div v-if="loading" class="md-hero-card">
        <div class="md-hero-grid">
          <div class="v4-skeleton md-skel-poster"></div>
          <div class="md-skel-body">
            <div class="v4-skeleton" style="height:40px;width:52%"></div>
            <div class="v4-skeleton" style="height:18px;width:36%;margin-top:14px"></div>
            <div class="v4-skeleton" style="height:44px;width:72%;margin-top:20px"></div>
            <div class="v4-skeleton" style="height:92px;width:100%;margin-top:22px"></div>
          </div>
        </div>
      </div>

      <div v-else-if="notGranted" class="md-section-card" role="status">
        <h2 class="md-sec-h">尚未开启片库观影权限</h2>
        <p style="margin:0;font-size:14px;color:#524b42;line-height:1.6">
          前往「我的」激活观影凭证或开通会员后，即可查看完整影片详情并一键唤起播放器点播。
        </p>
        <div style="margin-top:14px;display:flex;gap:10px;flex-wrap:wrap">
          <RouterLink to="/game/account" class="v4-retry" style="text-decoration:none">去我的页面开启权限</RouterLink>
          <RouterLink to="/library" class="md-ghost-link">返回片库</RouterLink>
        </div>
      </div>

      <div v-else-if="error || !item" class="v4-err md-err" role="alert">
        <span>{{ error || '未找到该影片信息' }}</span>
        <div style="margin-top:10px;display:flex;gap:10px">
          <button type="button" class="v4-retry" @click="loadDetail">重试</button>
          <RouterLink to="/library" class="md-ghost-link">去片库浏览</RouterLink>
        </div>
      </div>

      <template v-else>
        <article class="md-hero-card" :class="{ 'has-backdrop': Boolean(backdropSrc && !backdropBroken) }">
          <div v-if="backdropSrc && !backdropBroken" class="md-backdrop">
            <img
              :src="backdropSrc"
              :alt="title"
              decoding="async"
              fetchpriority="high"
              @error="backdropBroken = true"
            />
            <div class="md-backdrop-scrim"></div>
          </div>

          <div class="md-hero-grid">
            <div class="md-poster-col">
              <div class="md-poster-frame">
                <img
                  v-if="posterSrc && !posterBroken"
                  :src="posterSrc"
                  :alt="title"
                  decoding="async"
                  @error="posterBroken = true"
                />
                <div v-else class="md-poster-fallback">
                  <span>{{ title.slice(0, 4) }}</span>
                </div>
              </div>
            </div>

            <div class="md-info-col">
              <div class="md-title-block">
                <img
                  v-if="logoSrc && !logoBroken"
                  :src="logoSrc"
                  :alt="title"
                  class="md-logo-img"
                  decoding="async"
                  @error="logoBroken = true"
                />
                <h1 class="md-title" :class="{ 'has-logo': Boolean(logoSrc && !logoBroken) }">{{ title }}</h1>
                <p v-if="subOriginalTitle" class="md-orig-title">{{ subOriginalTitle }}</p>
              </div>

              <div class="md-meta-pills">
                <span class="md-pill">{{ mediaTypeLabel }}</span>
                <span v-if="episodeBadge" class="md-pill">{{ episodeBadge }}</span>
                <span v-if="item.ProductionYear" class="md-pill">{{ item.ProductionYear }}</span>
                <span v-if="ratingText" class="md-pill md-pill-teal">★ {{ ratingText }}</span>
                <span v-if="runtimeText" class="md-pill">{{ runtimeText }}</span>
                <span v-if="resBadge" class="md-pill">{{ resBadge }}</span>
                <span v-if="item.OfficialRating" class="md-pill">{{ item.OfficialRating }}</span>
              </div>

              <div v-if="item.Genres?.length" class="md-genres">
                <span v-for="g in item.Genres" :key="g" class="md-genre-chip">{{ g }}</span>
              </div>

              <!-- 第 3 期核心：一键用 Infuse、VidHub、SenPlayer 打开（复用现有一键导入 scheme + Infuse 直连流协议） -->
              <div class="md-player-group" role="group" aria-label="用客户端打开">
                <button type="button" class="md-btn-primary" @click="openInInfuse">
                  <Play :size="16" aria-hidden="true" />
                  <span>用 Infuse 打开</span>
                </button>
                <button type="button" class="md-btn-player" @click="openInSchemePlayer('VidHub')">
                  <ExternalLink :size="15" aria-hidden="true" />
                  <span>用 VidHub 打开</span>
                </button>
                <button type="button" class="md-btn-player" @click="openInSchemePlayer('SenPlayer')">
                  <ExternalLink :size="15" aria-hidden="true" />
                  <span>用 SenPlayer 打开</span>
                </button>
                <button
                  type="button"
                  class="md-btn-ghost"
                  :disabled="favBusy"
                  @click="toggleFav"
                >
                  <Heart :size="15" :fill="isFav ? '#36b3ba' : 'none'" aria-hidden="true" />
                  <span>{{ isFav ? '已收藏' : '收藏' }}</span>
                </button>
                <button type="button" class="md-btn-ghost" @click="pickerOpen = true">
                  <span>更多播放器</span>
                </button>
              </div>

              <div class="md-overview-block">
                <h2 class="md-sec-h">剧情简介</h2>
                <p class="md-overview">
                  {{ item.Overview?.trim() || '暂无剧情简介，可直接点击上方客户端按钮在播放器中点播观看。' }}
                </p>
              </div>

              <dl v-if="extraSpecs.length > 0" class="md-specs">
                <div v-for="sp in extraSpecs" :key="sp.label" class="md-spec-item">
                  <dt>{{ sp.label }}</dt>
                  <dd>{{ sp.value }}</dd>
                </div>
              </dl>
            </div>
          </div>
        </article>

        <!-- 演员行（横向头像卡片 + 桌面箭头与拖拽） -->
        <section v-if="castList.length > 0" class="md-section-card" aria-label="演员阵容">
          <div class="md-sec-bar">
            <h2 class="md-sec-h" style="margin:0">演员与主创</h2>
            <span class="md-sec-count">共 {{ castList.length }} 位</span>
          </div>
          <div class="md-cast-shelf">
            <button
              v-if="castCanLeft"
              type="button"
              class="md-cast-arrow is-left"
              aria-label="向左查看演员"
              @click="scrollCast(-1)"
            >
              <ChevronLeft :size="18" aria-hidden="true" />
            </button>
            <div
              ref="castTrackEl"
              class="md-cast-row"
              :class="{ 'is-dragging': castDragging }"
              @scroll.passive="syncCastScroll"
              @pointerdown="onCastPointerDown"
              @pointermove="onCastPointerMove"
              @pointerup="onCastPointerUp"
              @pointercancel="onCastPointerUp"
            >
              <div
                v-for="(p, idx) in castList"
                :key="`${p.Id || idx}-${p.Name}`"
                class="md-actor-card"
              >
                <div class="md-actor-photo">
                  <img
                    v-if="actorPhoto(p) && !brokenActors.has(actorKey(p, idx))"
                    :src="actorPhoto(p)"
                    :alt="p.Name"
                    loading="lazy"
                    decoding="async"
                    draggable="false"
                    @error="brokenActors.add(actorKey(p, idx))"
                  />
                  <div v-else class="md-actor-ph" aria-hidden="true">
                    <span>{{ p.Name.slice(0, 1) }}</span>
                  </div>
                </div>
                <div class="md-actor-name" :title="p.Name">{{ p.Name }}</div>
                <div v-if="p.Role" class="md-actor-role" :title="`饰 ${p.Role}`">饰 {{ p.Role }}</div>
              </div>
            </div>
            <button
              v-if="castCanRight"
              type="button"
              class="md-cast-arrow is-right"
              aria-label="向右查看演员"
              @click="scrollCast(1)"
            >
              <ChevronRight :size="18" aria-hidden="true" />
            </button>
          </div>
        </section>

        <section v-if="seasons.length > 0" class="md-section-card" aria-label="季列表">
          <div class="md-sec-bar">
            <h2 class="md-sec-h" style="margin:0">包含季数（{{ seasons.length }}）</h2>
          </div>
          <div class="md-season-grid">
            <div v-for="s in seasons" :key="s.Id" class="md-season-item">
              <div class="md-season-poster">
                <img
                  v-if="seasonPoster(s) && !brokenSeasons.has(s.Id)"
                  :src="seasonPoster(s)"
                  :alt="s.Name"
                  loading="lazy"
                  decoding="async"
                  @error="brokenSeasons.add(s.Id)"
                />
                <div v-else class="md-season-ph">{{ s.Name }}</div>
              </div>
              <div class="md-season-name">{{ s.Name }}</div>
              <div v-if="typeof s.ChildCount === 'number'" class="md-season-cnt">
                共 {{ s.ChildCount }} 集
              </div>
            </div>
          </div>
        </section>
      </template>

      <PlayerSchemeSheet v-model:open="pickerOpen" :title="title" />
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { ArrowLeft, ChevronLeft, ChevronRight, ExternalLink, Heart, Play } from 'lucide-vue-next'
import {
  backdropUrl,
  embyApi,
  ensureEmbyBase,
  formatDuration,
  httpStatusOf,
  imageUrl,
  isEmbyAccessNotGranted,
  personAvatarUrl,
  posterUrl,
  ticksToSeconds,
  type EmbyItem,
} from '@/api/emby'
import { storageBindingApi } from '@/api/economy'
import PlayerSchemeSheet from '@/components/panel/PlayerSchemeSheet.vue'
import {
  assignPlayerDeepLink,
  buildInfuseLaunchUrl,
  findPlayerImportScheme,
  loadImportSchemes,
  type PlayerScheme,
} from '@/composables/usePlayerSchemes'
import { resolveBindingUiState, shouldShowBindingGuide } from '@/utils/bindingUi'
import { cleanOriginalSubtitle, displayMediaTitle, resolutionBadge } from '@/utils/mediaTitle'
import { humanizeError } from '@/composables/usePanelApi'
import { useToast } from '@/composables/useToast'

interface PersonEntry {
  Id?: string
  Name: string
  Role?: string
  Type?: string
  PrimaryImageTag?: string
}

type FullEmbyItem = EmbyItem & {
  DisplayTitle?: string
  OriginalTitle?: string
  Aliases?: string[]
  People?: PersonEntry[]
  Countries?: string[]
}

const route = useRoute()
const router = useRouter()
const toast = useToast()

const loading = ref(true)
const error = ref('')
const notGranted = ref(false)
const item = ref<FullEmbyItem | null>(null)
const seasons = ref<EmbyItem[]>([])
const firstEpisodeStreamUrl = ref('')
const resolvedBaseUrl = ref('')
const playerSchemes = ref<PlayerScheme[]>([])
const needsDriveBinding = ref(false)

const pickerOpen = ref(false)
const favBusy = ref(false)

const posterBroken = ref(false)
const backdropBroken = ref(false)
const logoBroken = ref(false)
const brokenSeasons = reactive(new Set<string>())
const brokenActors = reactive(new Set<string>())

const castTrackEl = ref<HTMLElement | null>(null)
const castCanLeft = ref(false)
const castCanRight = ref(false)
const castDragging = ref(false)
let castStartX = 0
let castStartScroll = 0

const itemId = computed(() => String(route.params.id || ''))
const title = computed(() => displayMediaTitle(item.value))
const subOriginalTitle = computed(() =>
  cleanOriginalSubtitle(item.value?.OriginalTitle, title.value),
)

const mediaTypeLabel = computed(() => {
  const t = item.value?.Type
  if (t === 'Series') return '剧集'
  if (t === 'Episode') return '单集'
  return '电影'
})

const episodeBadge = computed(() => {
  if (item.value?.Type !== 'Episode') return ''
  const s = item.value.ParentIndexNumber
  const e = item.value.IndexNumber
  if (typeof s === 'number' && typeof e === 'number') {
    return `S${String(s).padStart(2, '0')}E${String(e).padStart(2, '0')}`
  }
  if (typeof e === 'number') return `第 ${e} 集`
  return ''
})

const posterSrc = computed(() => {
  if (!item.value) return ''
  const own = posterUrl(item.value, 520)
  if (own) return own
  if (item.value.SeriesId) return imageUrl(item.value.SeriesId, 'Primary', 520)
  return ''
})

const backdropSrc = computed(() => {
  if (!item.value) return ''
  const own = backdropUrl(item.value, 1600)
  if (own) return own
  if (item.value.SeriesId) return imageUrl(item.value.SeriesId, 'Backdrop', 1600)
  return ''
})

const logoSrc = computed(() => {
  if (!item.value) return ''
  if (item.value.ImageTags?.Logo) return imageUrl(item.value.Id, 'Logo', 640)
  if (item.value.SeriesId) return imageUrl(item.value.SeriesId, 'Logo', 640)
  return ''
})

const directStreamUrl = computed(() => {
  const ms = item.value?.MediaSources?.[0]
  return ms?.DirectStreamUrl || ms?.DirectPlayUrl || firstEpisodeStreamUrl.value || ''
})

const ratingText = computed(() => {
  const r = item.value?.CommunityRating
  return typeof r === 'number' && r > 0 ? r.toFixed(1) : ''
})

const runtimeText = computed(() => {
  const secs = ticksToSeconds(item.value?.RunTimeTicks)
  return secs > 0 ? formatDuration(secs) : ''
})

const resBadge = computed(() => resolutionBadge(item.value))
const isFav = computed(() => !!item.value?.UserData?.IsFavorite)

const castList = computed((): PersonEntry[] => {
  const raw = item.value?.People
  if (!Array.isArray(raw)) return []
  return raw.filter((p) => p?.Name).slice(0, 18)
})

const extraSpecs = computed(() => {
  const out: Array<{ label: string; value: string }> = []
  const it = item.value
  if (!it) return out
  if (Array.isArray(it.Countries) && it.Countries.length) {
    out.push({ label: '地区', value: it.Countries.join(' / ') })
  }
  const studios = (it.Studios || [])
    .map((s) => (typeof s === 'string' ? s : (s as { Name?: string })?.Name || ''))
    .filter(Boolean)
  if (studios.length) {
    out.push({ label: '出品', value: studios.slice(0, 3).join(' / ') })
  }
  const ms = it.MediaSources?.[0]
  if (ms?.Container) {
    out.push({ label: '容器格式', value: ms.Container.toUpperCase() })
  }
  if (it.ProviderIds?.Tmdb) {
    out.push({ label: 'TMDB ID', value: String(it.ProviderIds.Tmdb) })
  }
  return out
})

function actorKey(p: PersonEntry, idx: number): string {
  return p.Id ? String(p.Id) : `${idx}-${p.Name}`
}

function actorPhoto(p: PersonEntry): string {
  return personAvatarUrl(p, 240)
}

function seasonPoster(s: EmbyItem): string {
  return posterUrl(s, 280)
}

function syncCastScroll() {
  const el = castTrackEl.value
  if (!el) {
    castCanLeft.value = false
    castCanRight.value = false
    return
  }
  castCanLeft.value = el.scrollLeft > 8
  castCanRight.value = el.scrollLeft + el.clientWidth < el.scrollWidth - 8
}

function scrollCast(dir: -1 | 1) {
  const el = castTrackEl.value
  if (!el) return
  const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  const delta = Math.max(240, Math.round(el.clientWidth * 0.72)) * dir
  el.scrollBy({ left: delta, behavior: reduce ? 'auto' : 'smooth' })
}

function onCastPointerDown(e: PointerEvent) {
  if (e.pointerType !== 'mouse' || e.button !== 0) return
  const el = castTrackEl.value
  if (!el) return
  castDragging.value = true
  castStartX = e.clientX
  castStartScroll = el.scrollLeft
  el.setPointerCapture?.(e.pointerId)
}

function onCastPointerMove(e: PointerEvent) {
  if (!castDragging.value || !castTrackEl.value) return
  const dx = e.clientX - castStartX
  if (Math.abs(dx) > 5) {
    castTrackEl.value.scrollLeft = castStartScroll - dx
  }
}

function onCastPointerUp(e: PointerEvent) {
  if (!castDragging.value) return
  castDragging.value = false
  castTrackEl.value?.releasePointerCapture?.(e.pointerId)
}

async function silentCopyTitle() {
  const t = title.value.trim()
  if (!t) return
  try {
    await navigator.clipboard?.writeText(t)
  } catch {
    /* ignore */
  }
}

async function ensureSchemesReady(): Promise<PlayerScheme[]> {
  if (playerSchemes.value.length > 0) return playerSchemes.value
  const loaded = await loadImportSchemes()
  playerSchemes.value = loaded
  return loaded
}

async function openInInfuse() {
  if (needsDriveBinding.value) {
    pickerOpen.value = true
    return
  }
  await silentCopyTitle()
  const baseOrigin =
    resolvedBaseUrl.value || (typeof window !== 'undefined' ? window.location.origin : '')
  const url = buildInfuseLaunchUrl(directStreamUrl.value, baseOrigin)
  toast.info(
    directStreamUrl.value
      ? '正在唤起 Infuse 直接播放…'
      : '正在打开 Infuse，片名已复制可直接粘贴搜索',
  )
  assignPlayerDeepLink(url)
}

async function openInSchemePlayer(target: 'VidHub' | 'SenPlayer') {
  if (needsDriveBinding.value) {
    pickerOpen.value = true
    return
  }
  const schemes = await ensureSchemesReady()
  const matched = findPlayerImportScheme(schemes, target)
  if (!matched) {
    toast.info(`请先在「接线员」页设置播放密码后再一键打开 ${target}`)
    void router.push('/game/operator')
    return
  }
  await silentCopyTitle()
  toast.info(`正在打开 ${target}（片名已复制，导入后可直接搜索点播）`)
  assignPlayerDeepLink(matched.url)
}

function goBack() {
  if (window.history.length > 1) router.back()
  else void router.push('/library')
}

async function toggleFav() {
  if (!item.value || favBusy.value) return
  favBusy.value = true
  try {
    const next = !isFav.value
    const ud = await embyApi.setFavorite(item.value.Id, next)
    item.value = { ...item.value, UserData: { ...item.value.UserData, ...ud } }
    toast.success(next ? '已加入收藏' : '已取消收藏')
  } catch (err: unknown) {
    toast.error(humanizeError(err))
  } finally {
    favBusy.value = false
  }
}

async function refreshBindingAndSchemes() {
  const [schemesRes, bindingsRes, providersRes] = await Promise.allSettled([
    loadImportSchemes(),
    storageBindingApi.list(),
    storageBindingApi.providers(),
  ])
  if (schemesRes.status === 'fulfilled') {
    playerSchemes.value = schemesRes.value
  }
  const ui = resolveBindingUiState(
    providersRes.status === 'fulfilled' ? providersRes.value : undefined,
  )
  if (bindingsRes.status === 'fulfilled') {
    const bindings = bindingsRes.value?.bindings ?? []
    needsDriveBinding.value = shouldShowBindingGuide(
      ui.enabled,
      bindings.some((b) => b.status === 'active'),
    )
  } else {
    needsDriveBinding.value = false
  }
}

async function loadDetail() {
  if (!itemId.value) return
  loading.value = true
  error.value = ''
  notGranted.value = false
  posterBroken.value = false
  backdropBroken.value = false
  logoBroken.value = false
  seasons.value = []
  firstEpisodeStreamUrl.value = ''
  try {
    resolvedBaseUrl.value = await ensureEmbyBase()
    const fetched = (await embyApi.getItem(itemId.value)) as FullEmbyItem
    item.value = fetched
    document.title = `${displayMediaTitle(fetched)} - 影片详情`
    if (fetched.Type === 'Series') {
      const [seasonList, epList] = await Promise.allSettled([
        embyApi.getSeasons(fetched.Id),
        embyApi.getEpisodes(fetched.Id),
      ])
      if (seasonList.status === 'fulfilled') {
        seasons.value = seasonList.value
      }
      if (epList.status === 'fulfilled' && epList.value.length > 0) {
        try {
          const firstEp = await embyApi.getItem(epList.value[0].Id)
          const epMs = firstEp.MediaSources?.[0]
          firstEpisodeStreamUrl.value = epMs?.DirectStreamUrl || epMs?.DirectPlayUrl || ''
        } catch {
          firstEpisodeStreamUrl.value = ''
        }
      }
    }
    void refreshBindingAndSchemes()
  } catch (err: unknown) {
    const status = httpStatusOf(err)
    if (isEmbyAccessNotGranted(err) || status === 401 || status === 403) {
      notGranted.value = true
    } else {
      error.value = humanizeError(err)
    }
  } finally {
    loading.value = false
    await nextTick()
    syncCastScroll()
  }
}

watch(itemId, () => {
  void loadDetail()
})

onMounted(() => {
  void loadDetail()
  window.addEventListener('resize', syncCastScroll)
})

onUnmounted(() => {
  window.removeEventListener('resize', syncCastScroll)
})
</script>

<style scoped>
.md-page {
  padding: 20px 24px 64px;
}

.md-topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}

.md-back-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 38px;
  padding: 6px 14px;
  border-radius: 6px;
  border: 1px solid #d1d1cd;
  background: #fdfbfa;
  color: #27251e;
  font-size: 14px;
  font-family: inherit;
  cursor: pointer;
}

.md-back-btn:hover {
  border-color: #016a71;
  color: #016a71;
}

.md-series-back {
  font-size: 14px;
  font-weight: 500;
  color: #016a71;
  text-decoration: none;
}

.md-hero-card {
  position: relative;
  background: #171411;
  border: 1px solid #2e2923;
  border-radius: 16px;
  overflow: hidden;
  color: #f6f1e7;
}

.md-hero-card.has-backdrop {
  min-height: 420px;
}

.md-backdrop {
  position: absolute;
  inset: 0;
  overflow: hidden;
  background: #171411;
  pointer-events: none;
}

.md-backdrop img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  object-position: center 22%;
  opacity: 0.78;
  display: block;
}

.md-backdrop-scrim {
  position: absolute;
  inset: 0;
  background:
    linear-gradient(90deg, rgba(20, 17, 14, 0.95) 0%, rgba(20, 17, 14, 0.82) 48%, rgba(20, 17, 14, 0.34) 100%),
    linear-gradient(180deg, rgba(20, 17, 14, 0.18) 0%, rgba(20, 17, 14, 0.62) 55%, rgba(20, 17, 14, 0.96) 100%);
}

.md-hero-grid {
  position: relative;
  z-index: 1;
  display: grid;
  grid-template-columns: 236px minmax(0, 1fr);
  gap: 28px;
  padding: 36px 32px 32px;
  align-items: end;
}

.md-poster-frame {
  width: 100%;
  aspect-ratio: 2 / 3;
  border-radius: 14px;
  overflow: hidden;
  background: #2b2620;
  border: 1px solid rgba(255, 253, 248, 0.22);
}

.md-poster-frame img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.md-poster-fallback {
  width: 100%;
  height: 100%;
  padding: 24px 16px;
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
  background: linear-gradient(155deg, #322c25, #221e19);
  color: #f6f1e7;
  font-size: 24px;
  font-weight: 500;
}

.md-logo-img {
  max-height: 88px;
  max-width: min(400px, 100%);
  width: auto;
  object-fit: contain;
  margin-bottom: 10px;
  display: block;
  filter: drop-shadow(0 2px 14px rgba(0, 0, 0, 0.68));
}

.md-title {
  margin: 0;
  font-size: 28px;
  font-weight: 500;
  line-height: 1.3;
  letter-spacing: 0;
  color: #faf8f5;
}

.md-title.has-logo {
  font-size: 19px;
  color: #e9e2d5;
}

.md-orig-title {
  margin: 4px 0 0;
  font-size: 14px;
  color: #cfc6b8;
}

.md-meta-pills {
  margin-top: 14px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.md-pill {
  padding: 4px 12px;
  border-radius: 9999px;
  border: 1px solid rgba(255, 253, 248, 0.22);
  background: rgba(20, 17, 14, 0.56);
  color: #f4efe6;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}

.md-pill-teal {
  border-color: #36b3ba;
  background: rgba(1, 106, 113, 0.52);
  color: #d4f8fa;
  font-weight: 500;
}

.md-genres {
  margin-top: 12px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.md-genre-chip {
  font-size: 13px;
  color: #e5ddd0;
  background: rgba(255, 253, 248, 0.12);
  padding: 3px 10px;
  border-radius: 9999px;
}

.md-player-group {
  margin-top: 20px;
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.md-btn-primary {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: 44px;
  padding: 0 20px;
  border-radius: 12px;
  border: 1px solid #018088;
  background: #016a71;
  color: #ffffff;
  font-size: 14px;
  font-weight: 500;
  font-family: inherit;
  cursor: pointer;
  transition: transform 0.16s ease, opacity 0.16s ease;
}

.md-btn-primary:hover {
  transform: translateY(-1px);
  opacity: 0.94;
}

.md-btn-player {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  min-height: 44px;
  padding: 0 16px;
  border-radius: 12px;
  border: 1px solid rgba(255, 253, 248, 0.34);
  background: rgba(250, 248, 245, 0.14);
  color: #faf8f5;
  font-size: 14px;
  font-weight: 500;
  font-family: inherit;
  cursor: pointer;
  transition: transform 0.16s ease, opacity 0.16s ease;
}

.md-btn-player:hover {
  transform: translateY(-1px);
  opacity: 0.9;
}

.md-btn-ghost {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 44px;
  padding: 0 14px;
  border-radius: 6px;
  border: 1px solid rgba(255, 253, 248, 0.22);
  background: rgba(20, 17, 14, 0.45);
  color: #efe9de;
  font-size: 14px;
  font-weight: 500;
  font-family: inherit;
  cursor: pointer;
  transition: transform 0.16s ease, opacity 0.16s ease;
}

.md-btn-ghost:hover {
  transform: translateY(-1px);
  opacity: 0.9;
}

.md-overview-block {
  margin-top: 22px;
  padding-top: 16px;
  border-top: 1px solid rgba(255, 253, 248, 0.16);
}

.md-hero-card .md-sec-h {
  color: #faf8f5;
}

.md-sec-bar {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}

.md-sec-h {
  margin: 0 0 10px;
  font-size: 17px;
  font-weight: 500;
  color: #27251e;
}

.md-sec-count {
  font-size: 13px;
  color: #524b42;
  font-variant-numeric: tabular-nums;
}

.md-overview {
  margin: 0;
  max-width: 72ch;
  font-size: 15px;
  line-height: 1.7;
  color: #eae3d6;
  white-space: pre-line;
}

.md-specs {
  margin: 18px 0 0;
  padding-top: 14px;
  border-top: 1px solid rgba(255, 253, 248, 0.16);
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px 20px;
}

.md-spec-item dt {
  font-size: 13px;
  color: #c8beae;
}

.md-spec-item dd {
  margin: 3px 0 0;
  font-size: 14px;
  font-weight: 500;
  color: #faf8f5;
  font-variant-numeric: tabular-nums;
}

.md-section-card {
  margin-top: 20px;
  background: #fdfbfa;
  border: 1px solid #d1d1cd;
  border-radius: 16px;
  padding: 22px 24px;
}

/* 演员横向行 */
.md-cast-shelf {
  position: relative;
}

.md-cast-row {
  display: flex;
  gap: 14px;
  overflow-x: auto;
  scroll-snap-type: x proximity;
  padding-bottom: 4px;
  scrollbar-width: thin;
}

.md-cast-row.is-dragging {
  cursor: grabbing;
  scroll-snap-type: none;
  user-select: none;
}

.md-actor-card {
  flex: 0 0 112px;
  width: 112px;
  scroll-snap-align: start;
}

.md-actor-photo {
  width: 100%;
  aspect-ratio: 3 / 4;
  border-radius: 12px;
  overflow: hidden;
  border: 1px solid #d1d1cd;
  background: #efe9df;
}

.md-actor-photo img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.md-actor-ph {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(155deg, #f4efe6 0%, #e7dfd0 100%);
  color: #524b42;
  font-size: 26px;
  font-weight: 500;
}

.md-actor-name {
  margin-top: 8px;
  font-size: 14px;
  font-weight: 500;
  color: #27251e;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.md-actor-role {
  margin-top: 2px;
  font-size: 13px;
  color: #524b42;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.md-cast-arrow {
  position: absolute;
  top: 38%;
  transform: translateY(-50%);
  z-index: 2;
  width: 36px;
  height: 36px;
  border-radius: 9999px;
  border: 1px solid #d1d1cd;
  background: rgba(253, 251, 250, 0.95);
  color: #27251e;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
}

.md-cast-arrow.is-left {
  left: -10px;
}

.md-cast-arrow.is-right {
  right: -10px;
}

.md-season-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(136px, 1fr));
  gap: 16px;
}

.md-season-poster {
  aspect-ratio: 2 / 3;
  border-radius: 12px;
  overflow: hidden;
  border: 1px solid #d1d1cd;
  background: #efe9df;
}

.md-season-poster img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.md-season-ph {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 10px;
  text-align: center;
  font-size: 14px;
  color: #524b42;
}

.md-season-name {
  margin-top: 6px;
  font-size: 14px;
  font-weight: 500;
  color: #27251e;
}

.md-season-cnt {
  font-size: 13px;
  color: #524b42;
}

.md-skel-poster {
  width: 100%;
  aspect-ratio: 2 / 3;
  border-radius: 14px;
}

.md-ghost-link {
  display: inline-flex;
  align-items: center;
  padding: 8px 16px;
  border-radius: 12px;
  border: 1px solid #d1d1cd;
  color: #27251e;
  text-decoration: none;
  font-size: 14px;
}

@media (max-width: 767px) {
  .md-page {
    padding: 14px 16px calc(84px + env(safe-area-inset-bottom));
  }

  .md-hero-card.has-backdrop {
    min-height: 0;
  }

  /* DESIGN.md §6.2：手机端主视觉图高 240–320px，半透明背板保证文字清晰 */
  .md-backdrop {
    height: 256px;
    bottom: auto;
  }

  .md-backdrop-scrim {
    background: linear-gradient(180deg, rgba(23, 20, 17, 0.22) 0%, rgba(23, 20, 17, 0.88) 76%, #171411 100%);
  }

  .md-hero-grid {
    grid-template-columns: 1fr;
    gap: 16px;
    padding: 136px 16px 20px;
    align-items: start;
  }

  .md-hero-card:not(.has-backdrop) .md-hero-grid {
    padding-top: 18px;
  }

  .md-poster-col {
    width: 132px;
  }

  .md-logo-img {
    max-height: 64px;
  }

  .md-title {
    font-size: 22px;
  }

  .md-player-group {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .md-btn-primary {
    grid-column: 1 / -1;
    justify-content: center;
  }

  .md-btn-player,
  .md-btn-ghost {
    justify-content: center;
    padding: 0 10px;
    font-size: 13px;
    gap: 5px;
    white-space: nowrap;
  }

  .md-section-card {
    padding: 16px;
  }

  .md-actor-card {
    flex: 0 0 96px;
    width: 96px;
  }

  .md-cast-arrow {
    display: none;
  }
}
</style>
