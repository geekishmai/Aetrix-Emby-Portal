<template>
  <div class="v4-scope">
    <main class="v4-page v4-page-wide md-page">
      <div class="md-topbar">
        <button type="button" class="md-back-btn" @click="goBack">
          <ArrowLeft :size="16" aria-hidden="true" />
          <span>返回片库</span>
        </button>
        <RouterLink
          v-if="(item?.Type === 'Episode' || item?.Type === 'Season') && item.SeriesId"
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
        <p style="margin:0;font-size:14px;color:#9a9a9a;line-height:1.6">
          前往「播放器接入」或「我的」激活观影权限后，即可查看完整影片详情并一键唤起播放器点播。
        </p>
        <div style="margin-top:14px;display:flex;gap:10px;flex-wrap:wrap">
          <RouterLink to="/game/operator" class="v4-retry" style="text-decoration:none">前往开启观影权限</RouterLink>
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

              <!-- 播放操作区：剧集/季自动落续看集或第 1 集真实流地址；无可用媒体源时显式禁用并给出提示 -->
              <div class="md-player-group" role="group" aria-label="用客户端打开">
                <button
                  type="button"
                  class="md-btn-primary"
                  :disabled="sourceResolving || !hasPlayableSource"
                  @click="openInInfuse"
                >
                  <Play :size="16" aria-hidden="true" />
                  <span>{{ primaryPlayLabel }}</span>
                </button>
                <button
                  type="button"
                  class="md-btn-player"
                  :disabled="sourceResolving || !hasPlayableSource"
                  @click="openClientPlayGuide('VidHub')"
                >
                  <ExternalLink :size="15" aria-hidden="true" />
                  <span>{{ isContainerItem ? 'VidHub 选集点播' : '在 VidHub 中点播' }}</span>
                </button>
                <button
                  type="button"
                  class="md-btn-player"
                  :disabled="sourceResolving || !hasPlayableSource"
                  @click="openClientPlayGuide('SenPlayer')"
                >
                  <ExternalLink :size="15" aria-hidden="true" />
                  <span>{{ isContainerItem ? 'SenPlayer 选集点播' : '在 SenPlayer 中点播' }}</span>
                </button>
                <button
                  type="button"
                  class="md-btn-ghost"
                  :disabled="favBusy"
                  @click="toggleFav"
                >
                  <Heart :size="15" :fill="isFav ? '#c3f53c' : 'none'" :color="isFav ? '#c3f53c' : 'currentColor'" aria-hidden="true" />
                  <span>{{ isFav ? '已收藏' : '收藏' }}</span>
                </button>
                <button type="button" class="md-btn-ghost" @click="router.push('/clients')">
                  <span>客户端与教程</span>
                </button>
              </div>

              <p v-if="!sourceResolving && !hasPlayableSource" class="md-source-notice" role="status">
                {{ unplayableReason }}
              </p>
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


        <!-- 剧集/季选集面板：让用户可直接点进具体单集或查看进度 -->
        <section v-if="episodes.length > 0" class="md-section-card" aria-label="剧集选集">
          <div class="md-sec-bar">
            <h2 class="md-sec-h" style="margin:0">选集点播</h2>
            <span class="md-sec-count">共 {{ filteredEpisodes.length }} 集</span>
          </div>
          <div v-if="seasons.length > 1" class="md-ep-tabs" role="tablist" aria-label="选择季">
            <button
              type="button"
              role="tab"
              class="md-ep-tab"
              :class="{ 'is-active': activeSeasonFilter === '' }"
              :aria-selected="activeSeasonFilter === ''"
              @click="activeSeasonFilter = ''"
            >
              全部
            </button>
            <button
              v-for="s in seasons"
              :key="s.Id"
              type="button"
              role="tab"
              class="md-ep-tab"
              :class="{ 'is-active': activeSeasonFilter === s.Id }"
              :aria-selected="activeSeasonFilter === s.Id"
              @click="activeSeasonFilter = s.Id"
            >
              {{ s.Name }}
            </button>
          </div>
          <div class="md-ep-grid">
            <RouterLink
              v-for="ep in filteredEpisodes"
              :key="ep.Id"
              :to="`/media/${encodeURIComponent(ep.Id)}`"
              class="md-ep-card"
              :class="{ 'is-target': targetEpisodeInfo?.episode.Id === ep.Id }"
            >
              <div class="md-ep-top">
                <span class="md-ep-code">{{ formatEpisodeCode(ep) }}</span>
                <span v-if="(ep.UserData?.PlaybackPositionTicks ?? 0) > 0 && !ep.UserData?.Played" class="md-ep-state is-resume">续看</span>
                <span v-else-if="ep.UserData?.Played" class="md-ep-state">已看</span>
              </div>
              <div class="md-ep-name" :title="displayMediaTitle(ep)">{{ displayMediaTitle(ep) }}</div>
            </RouterLink>
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

      <!-- VidHub / SenPlayer 选集与点播引导弹窗（避免下发无密码 import 协议触发客户端「加载失败」） -->
      <Teleport to="body">
        <div
          v-if="clientGuideTarget"
          class="md-guide-mask"
          @click.self="clientGuideTarget = ''"
          @keydown.esc.window="clientGuideTarget = ''"
        >
          <div class="md-guide-card" role="dialog" aria-modal="true" :aria-label="`在 ${clientGuideTarget} 中点播`">
            <h3 class="md-guide-title">在 {{ clientGuideTarget }} 中{{ isContainerItem ? '选集播放' : '点播' }}</h3>
            <p class="md-guide-desc">
              片名「<strong>{{ title }}</strong>」已自动复制到剪贴板。{{ clientGuideTarget }} 官方未开放网页直传单视频流协议，请直接打开已添加本站服务器的
              {{ clientGuideTarget }}，粘贴片名即可{{ isContainerItem ? '进入剧集选集播放' : '立即原画点播' }}。
            </p>
            <div class="md-guide-actions">
              <RouterLink to="/clients" class="md-btn-primary" style="text-decoration:none" @click="clientGuideTarget = ''">
                <span>首次使用？去客户端页导入服务器</span>
              </RouterLink>
              <button type="button" class="md-btn-ghost" @click="clientGuideTarget = ''">
                <span>我知道了</span>
              </button>
            </div>
          </div>
        </div>
      </Teleport>
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
  buildStrictInfusePlayUrl,
  formatEpisodeCode,
  pickTargetEpisodeForContainer,
  type ResolvedContainerEpisode,
} from '@/composables/usePlayerSchemes'
import { resolveBindingUiState, shouldShowBindingGuide } from '@/utils/bindingUi'
import { cleanOriginalSubtitle, displayMediaTitle, resolutionBadge } from '@/utils/mediaTitle'
import { humanizeError } from '@/composables/usePanelApi'
import { useToast } from '@/composables/useToast'
import { useClipboard } from '@/composables/useClipboard'
import { pageTitle } from '@/composables/useBranding'

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
const { copy } = useClipboard()

const loading = ref(true)
const sourceResolving = ref(false)
const error = ref('')
const notGranted = ref(false)
const item = ref<FullEmbyItem | null>(null)
const seasons = ref<EmbyItem[]>([])
const episodes = ref<EmbyItem[]>([])
const activeSeasonFilter = ref('')
const targetEpisodeInfo = ref<ResolvedContainerEpisode<EmbyItem> | null>(null)
const firstEpisodeStreamUrl = ref('')
const resolvedBaseUrl = ref('')
const needsDriveBinding = ref(false)

const pickerOpen = ref(false)
const clientGuideTarget = ref<'VidHub' | 'SenPlayer' | ''>('')
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

const isContainerItem = computed(() => item.value?.Type === 'Series' || item.value?.Type === 'Season')

const mediaTypeLabel = computed(() => {
  const t = item.value?.Type
  if (t === 'Series') return '剧集'
  if (t === 'Season') return '分季'
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
  if (item.value.ImageTags?.Primary) return posterUrl(item.value, 520)
  if (item.value.SeriesId) return imageUrl(item.value.SeriesId, 'Primary', 520)
  return posterUrl(item.value, 520)
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
  if (isContainerItem.value) {
    return firstEpisodeStreamUrl.value || ''
  }
  const ms = item.value?.MediaSources?.[0]
  return ms?.DirectStreamUrl || ms?.DirectPlayUrl || ''
})

const hasPlayableSource = computed(() => Boolean(directStreamUrl.value.trim()))

const primaryPlayLabel = computed(() => {
  if (sourceResolving.value) return '正在解析片源…'
  if (!hasPlayableSource.value) return '暂无可直连片源'
  if (isContainerItem.value && targetEpisodeInfo.value) {
    const verb = targetEpisodeInfo.value.isResume ? '续播' : '播放'
    return `用 Infuse ${verb} ${targetEpisodeInfo.value.codeLabel}`
  }
  return '用 Infuse 打开'
})

const unplayableReason = computed(() => {
  if (isContainerItem.value) {
    return '该剧集分集正在整理入库中，暂无可直连唤起的单集流地址；整理完成后即可一键点播，或在已连接的播放器内浏览选集。'
  }
  return '该条目暂无可用媒体源，暂时无法发起播放。'
})

const filteredEpisodes = computed(() => {
  if (!activeSeasonFilter.value) return episodes.value
  const seasonObj = seasons.value.find((s) => s.Id === activeSeasonFilter.value)
  return episodes.value.filter(
    (ep) =>
      ep.SeasonId === activeSeasonFilter.value ||
      (seasonObj && typeof seasonObj.IndexNumber === 'number' && ep.ParentIndexNumber === seasonObj.IndexNumber),
  )
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

async function openInInfuse() {
  if (sourceResolving.value) return
  if (!hasPlayableSource.value) {
    toast.info(unplayableReason.value)
    return
  }
  if (needsDriveBinding.value) {
    pickerOpen.value = true
    return
  }
  const baseOrigin =
    resolvedBaseUrl.value || (typeof window !== 'undefined' ? window.location.origin : '')
  const launchUrl = buildStrictInfusePlayUrl(directStreamUrl.value, baseOrigin)
  if (!launchUrl) {
    toast.info(unplayableReason.value)
    return
  }
  toast.info(
    isContainerItem.value && targetEpisodeInfo.value
      ? `正在唤起 Infuse ${targetEpisodeInfo.value.isResume ? '续播' : '播放'} ${targetEpisodeInfo.value.codeLabel}…`
      : '正在唤起 Infuse 直接播放…',
  )
  assignPlayerDeepLink(launchUrl)
}

async function openClientPlayGuide(target: 'VidHub' | 'SenPlayer') {
  if (sourceResolving.value) return
  if (!hasPlayableSource.value) {
    toast.info(unplayableReason.value)
    return
  }
  if (needsDriveBinding.value) {
    pickerOpen.value = true
    return
  }
  const t = title.value.trim()
  if (t) {
    await copy(t, '片名')
  }
  clientGuideTarget.value = target
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

async function refreshBindingState() {
  const [bindingsRes, providersRes] = await Promise.allSettled([
    storageBindingApi.list(),
    storageBindingApi.providers(),
  ])
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
  sourceResolving.value = false
  error.value = ''
  notGranted.value = false
  posterBroken.value = false
  backdropBroken.value = false
  logoBroken.value = false
  seasons.value = []
  episodes.value = []
  activeSeasonFilter.value = ''
  targetEpisodeInfo.value = null
  firstEpisodeStreamUrl.value = ''
  clientGuideTarget.value = ''
  try {
    resolvedBaseUrl.value = await ensureEmbyBase()
    const fetched = (await embyApi.getItem(itemId.value)) as FullEmbyItem
    item.value = fetched
    document.title = pageTitle(displayMediaTitle(fetched))
    if (fetched.Type === 'Series' || fetched.Type === 'Season') {
      sourceResolving.value = true
      try {
        const seriesGuid = fetched.Type === 'Season' ? fetched.SeriesId || fetched.Id : fetched.Id
        const seasonFilterGuid = fetched.Type === 'Season' ? fetched.Id : undefined
        const [seasonList, epList] = await Promise.allSettled([
          fetched.Type === 'Series' ? embyApi.getSeasons(seriesGuid) : Promise.resolve([] as EmbyItem[]),
          embyApi.getEpisodes(seriesGuid, seasonFilterGuid),
        ])
        if (seasonList.status === 'fulfilled') {
          seasons.value = seasonList.value
        }
        if (epList.status === 'fulfilled' && epList.value.length > 0) {
          episodes.value = epList.value
          const chosen = pickTargetEpisodeForContainer(epList.value)
          targetEpisodeInfo.value = chosen
          if (chosen) {
            const fullEp = await embyApi.getItem(chosen.episode.Id)
            const epMs = fullEp.MediaSources?.[0]
            firstEpisodeStreamUrl.value = epMs?.DirectStreamUrl || epMs?.DirectPlayUrl || ''
          }
        }
      } catch {
        firstEpisodeStreamUrl.value = ''
      } finally {
        sourceResolving.value = false
      }
    }
    void refreshBindingState()
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
  padding: 22px 24px 64px;
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
  min-height: 36px;
  padding: 6px 14px;
  border-radius: 2px;
  border: 1px solid #2a2a2a;
  background: #171717;
  color: #f2f2f2;
  font-size: 13.5px;
  font-weight: 600;
  font-family: inherit;
  cursor: pointer;
  transition: border-color 0.15s ease, color 0.15s ease;
}

.md-back-btn:hover {
  border-color: #c3f53c;
  color: #c3f53c;
}

.md-series-back {
  font-size: 13.5px;
  font-weight: 600;
  color: #c3f53c;
  text-decoration: none;
}

.md-hero-card {
  position: relative;
  background: #171717;
  border: 1px solid #2a2a2a;
  border-radius: 2px;
  overflow: hidden;
  color: #f2f2f2;
}

.md-hero-card.has-backdrop {
  min-height: 440px;
}

.md-backdrop {
  position: absolute;
  inset: 0;
  overflow: hidden;
  background: #111111;
  pointer-events: none;
}

.md-backdrop img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  object-position: center 20%;
  opacity: 0.76;
  display: block;
}

.md-backdrop-scrim {
  position: absolute;
  inset: 0;
  background:
    linear-gradient(90deg, rgba(17, 17, 17, 0.96) 0%, rgba(17, 17, 17, 0.86) 48%, rgba(17, 17, 17, 0.42) 100%),
    linear-gradient(180deg, rgba(17, 17, 17, 0.22) 0%, rgba(17, 17, 17, 0.68) 58%, #171717 100%);
}

.md-hero-grid {
  position: relative;
  z-index: 1;
  display: grid;
  grid-template-columns: 232px minmax(0, 1fr);
  gap: 32px;
  padding: 36px 32px 32px;
  align-items: end;
}

.md-poster-frame {
  width: 100%;
  aspect-ratio: 2 / 3;
  border-radius: 2px;
  overflow: hidden;
  background: #111111;
  border: 1px solid #2a2a2a;
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
  background: #111111;
  color: #f2f2f2;
  font-size: 24px;
  font-weight: 800;
}

.md-logo-img {
  max-height: 88px;
  max-width: min(400px, 100%);
  width: auto;
  object-fit: contain;
  margin-bottom: 10px;
  display: block;
}

.md-title {
  margin: 0;
  font-size: clamp(26px, 2.8vw, 38px);
  font-weight: 800;
  line-height: 1.16;
  letter-spacing: -0.02em;
  color: #f2f2f2;
}

.md-title.has-logo {
  font-size: 18px;
  font-weight: 600;
  letter-spacing: 0;
  color: #9a9a9a;
}

.md-orig-title {
  margin: 5px 0 0;
  font-size: 14px;
  color: #9a9a9a;
}

.md-meta-pills {
  margin-top: 14px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.md-pill {
  padding: 3px 9px;
  border-radius: 2px;
  border: 1px solid #2a2a2a;
  background: rgba(17, 17, 17, 0.85);
  color: #f2f2f2;
  font-family: var(--dh-font-mono);
  font-size: 12px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.md-pill-teal {
  border-color: #3a3a3a;
  background: #1e1e1e;
  color: #c3f53c;
  font-weight: 700;
}

.md-genres {
  margin-top: 10px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.md-genre-chip {
  font-size: 12.5px;
  color: #9a9a9a;
  background: #1e1e1e;
  border: 1px solid #2a2a2a;
  padding: 2px 9px;
  border-radius: 2px;
}

.md-player-group {
  margin-top: 22px;
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

/* 唯一主操作按钮：荧光绿 #c3f53c + 黑字 */
.md-btn-primary {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: 44px;
  padding: 0 22px;
  border-radius: 2px;
  border: 1px solid #c3f53c;
  background: #c3f53c;
  color: #111111;
  font-size: 14px;
  font-weight: 800;
  font-family: inherit;
  cursor: pointer;
  transition: background-color 0.15s ease, border-color 0.15s ease;
}

.md-btn-primary:hover {
  background: #d1fa58;
  border-color: #d1fa58;
}

.md-btn-player {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  min-height: 44px;
  padding: 0 16px;
  border-radius: 2px;
  border: 1px solid #333333;
  background: #1e1e1e;
  color: #f2f2f2;
  font-size: 14px;
  font-weight: 600;
  font-family: inherit;
  cursor: pointer;
  transition: border-color 0.15s ease, background-color 0.15s ease;
}

.md-btn-player:hover {
  border-color: #f2f2f2;
  background: #242424;
}

.md-btn-ghost {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 44px;
  padding: 0 14px;
  border-radius: 2px;
  border: 1px solid #2a2a2a;
  background: rgba(17, 17, 17, 0.72);
  color: #d0d0d0;
  font-size: 14px;
  font-weight: 500;
  font-family: inherit;
  cursor: pointer;
  transition: border-color 0.15s ease, color 0.15s ease;
}

.md-btn-ghost:hover {
  border-color: #4a4a4a;
  color: #f2f2f2;
}

.md-btn-primary:disabled,
.md-btn-player:disabled {
  opacity: 0.42;
  cursor: not-allowed;
  background: #242424;
  border-color: #2f2f2f;
  color: #8c8c8c;
}

.md-source-notice {
  margin: 12px 0 0;
  padding: 10px 14px;
  background: rgba(23, 23, 23, 0.9);
  border: 1px solid #2a2a2a;
  border-left: 2px solid #e5534b;
  border-radius: 2px;
  color: #c8c8c8;
  font-size: 13.5px;
  line-height: 1.55;
}

.md-ep-tabs {
  display: flex;
  gap: 18px;
  overflow-x: auto;
  border-bottom: 1px solid #242424;
  margin-bottom: 14px;
  scrollbar-width: none;
}

.md-ep-tabs::-webkit-scrollbar {
  display: none;
}

.md-ep-tab {
  padding: 6px 2px 9px;
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: #9a9a9a;
  font-size: 13.5px;
  font-weight: 600;
  font-family: inherit;
  white-space: nowrap;
  cursor: pointer;
}

.md-ep-tab.is-active {
  color: #f2f2f2;
  border-bottom-color: #c3f53c;
  font-weight: 700;
}

.md-ep-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(188px, 1fr));
  gap: 10px;
}

.md-ep-card {
  padding: 10px 12px;
  background: #141414;
  border: 1px solid #262626;
  border-radius: 2px;
  text-decoration: none;
  color: #f2f2f2;
  display: flex;
  flex-direction: column;
  gap: 4px;
  transition: border-color 0.15s ease;
}

.md-ep-card:hover,
.md-ep-card.is-target {
  border-color: #c3f53c;
}

.md-ep-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}

.md-ep-code {
  font-family: var(--dh-font-mono);
  font-size: 12px;
  font-weight: 700;
  color: #c3f53c;
  font-variant-numeric: tabular-nums;
}

.md-ep-state {
  font-size: 11px;
  color: #9a9a9a;
}

.md-ep-state.is-resume {
  color: #c3f53c;
  font-weight: 700;
}

.md-ep-name {
  font-size: 13.5px;
  font-weight: 600;
  color: #e6e6e6;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.md-guide-mask {
  position: fixed;
  inset: 0;
  z-index: 105;
  background: rgba(0, 0, 0, 0.78);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
}

.md-guide-card {
  width: min(460px, 100%);
  padding: 22px 24px;
  background: #171717;
  border: 1px solid #2a2a2a;
  border-radius: 2px;
  color: #f2f2f2;
}

.md-guide-title {
  margin: 0 0 10px;
  font-size: 18px;
  font-weight: 800;
}

.md-guide-desc {
  margin: 0;
  font-size: 14px;
  line-height: 1.65;
  color: #c0c0c0;
}

.md-guide-desc strong {
  color: #f2f2f2;
}

.md-guide-actions {
  margin-top: 18px;
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.md-overview-block {
  margin-top: 22px;
  padding-top: 16px;
  border-top: 1px solid #2a2a2a;
}

.md-hero-card .md-sec-h {
  color: #f2f2f2;
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
  font-size: 18px;
  font-weight: 800;
  letter-spacing: -0.01em;
  color: #f2f2f2;
}

.md-sec-count {
  font-size: 13px;
  color: #9a9a9a;
  font-variant-numeric: tabular-nums;
}

.md-overview {
  margin: 0;
  max-width: 72ch;
  font-size: 15.5px;
  line-height: 1.7;
  color: #cccccc;
  white-space: pre-line;
}

.md-specs {
  margin: 18px 0 0;
  padding-top: 14px;
  border-top: 1px solid #2a2a2a;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px 20px;
}

.md-spec-item dt {
  font-size: 12px;
  color: #9a9a9a;
}

.md-spec-item dd {
  margin: 3px 0 0;
  font-size: 14px;
  font-weight: 600;
  color: #f2f2f2;
  font-variant-numeric: tabular-nums;
}

.md-section-card {
  margin-top: 20px;
  background: #171717;
  border: 1px solid #2a2a2a;
  border-radius: 2px;
  padding: 22px 24px;
}

/* 演员横向行 */
.md-cast-shelf {
  position: relative;
}

.md-cast-row {
  display: flex;
  gap: 12px;
  overflow-x: auto;
  scroll-snap-type: x proximity;
  padding-bottom: 4px;
  scrollbar-width: none;
}

.md-cast-row::-webkit-scrollbar {
  display: none;
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
  border-radius: 2px;
  overflow: hidden;
  border: 1px solid #2a2a2a;
  background: #1e1e1e;
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
  background: #1e1e1e;
  color: #9a9a9a;
  font-size: 24px;
  font-weight: 700;
}

.md-actor-name {
  margin-top: 8px;
  font-size: 13.5px;
  font-weight: 600;
  color: #f2f2f2;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.md-actor-role {
  margin-top: 2px;
  font-size: 12px;
  color: #9a9a9a;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.md-cast-arrow {
  position: absolute;
  top: 38%;
  transform: translateY(-50%);
  z-index: 2;
  width: 34px;
  height: 34px;
  border-radius: 2px;
  border: 1px solid #333333;
  background: rgba(23, 23, 23, 0.94);
  color: #f2f2f2;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
}

.md-cast-arrow:hover {
  border-color: #c3f53c;
  color: #c3f53c;
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
  gap: 14px;
}

.md-season-poster {
  aspect-ratio: 2 / 3;
  border-radius: 2px;
  overflow: hidden;
  border: 1px solid #2a2a2a;
  background: #1e1e1e;
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
  color: #9a9a9a;
}

.md-season-name {
  margin-top: 6px;
  font-size: 13.5px;
  font-weight: 600;
  color: #f2f2f2;
}

.md-season-cnt {
  font-size: 12px;
  color: #9a9a9a;
  font-variant-numeric: tabular-nums;
}

.md-skel-poster {
  width: 100%;
  aspect-ratio: 2 / 3;
  border-radius: 2px;
}

.md-ghost-link {
  display: inline-flex;
  align-items: center;
  padding: 8px 16px;
  border-radius: 2px;
  border: 1px solid #2a2a2a;
  background: #1e1e1e;
  color: #f2f2f2;
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

  .md-backdrop {
    height: 256px;
    bottom: auto;
  }

  .md-backdrop-scrim {
    background: linear-gradient(180deg, rgba(17, 17, 17, 0.2) 0%, rgba(17, 17, 17, 0.88) 76%, #171717 100%);
  }

  .md-hero-grid {
    grid-template-columns: 1fr;
    gap: 16px;
    padding: 132px 16px 20px;
    align-items: start;
  }

  .md-hero-card:not(.has-backdrop) .md-hero-grid {
    padding-top: 18px;
  }

  .md-poster-col {
    width: 128px;
  }

  .md-logo-img {
    max-height: 60px;
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
