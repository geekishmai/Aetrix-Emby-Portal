<template>
  <section
    v-if="!notGranted"
    id="disc-hot"
    class="p2-hot-band"
    aria-label="各库热门"
  >
    <div class="p2-hot-card">
      <div class="p2-hot-head">
        <div class="p2-hot-titles">
          <h2 class="p2-hot-title">各库热门</h2>
          <p class="p2-hot-sub">按分类库筛选高分口碑佳作，点海报查看详情与客户端一键播放</p>
        </div>
        <RouterLink
          :to="activeLibId ? { path: '/library', query: { parentId: activeLibId } } : '/library'"
          class="p2-hot-more"
        >
          <span>{{ activeLibName ? `进入${activeLibName}` : '进入片库' }}</span>
          <ArrowRight :size="15" aria-hidden="true" />
        </RouterLink>
      </div>

      <div v-if="libraries.length > 0" class="p2-lib-tabs" role="tablist" aria-label="选择媒体库">
        <button
          type="button"
          role="tab"
          class="p2-lib-tab"
          :class="{ 'is-active': activeLibId === '' }"
          :aria-selected="activeLibId === ''"
          @click="selectLibrary('')"
        >
          全部热门
        </button>
        <button
          v-for="lib in libraries"
          :key="lib.Id"
          type="button"
          role="tab"
          class="p2-lib-tab"
          :class="{ 'is-active': activeLibId === lib.Id }"
          :aria-selected="activeLibId === lib.Id"
          @click="selectLibrary(lib.Id)"
        >
          {{ lib.Name }}
        </button>
      </div>

      <div v-if="loading" class="p2-poster-row" aria-hidden="true">
        <div v-for="n in 7" :key="n" class="p2-poster-skel">
          <div class="v4-skeleton p2-poster-skel-img"></div>
          <div class="v4-skeleton p2-poster-skel-line"></div>
        </div>
      </div>

      <div v-else-if="error" class="v4-err">
        <span>各库热门加载失败：{{ error }}</span>
        <button type="button" class="v4-retry" @click="() => loadHotItems(true)">重试</button>
      </div>

      <div v-else-if="items.length === 0" class="v4-empty">
        该分类库暂无已刮削条目，可切换其它分类库或<RouterLink to="/library" class="p2-inline-link">浏览全部片库</RouterLink>
      </div>

      <div v-else class="p2-shelf-wrap">
        <button
          v-if="canScrollLeft"
          type="button"
          class="p2-shelf-arrow is-left"
          aria-label="向左查看热门海报"
          @click="scrollByDir(-1)"
        >
          <ChevronLeft :size="18" aria-hidden="true" />
        </button>

        <div
          ref="trackEl"
          class="p2-poster-row"
          :class="{ 'is-dragging': dragging }"
          @scroll.passive="syncScrollState"
          @pointerdown="onPointerDown"
          @pointermove="onPointerMove"
          @pointerup="onPointerUp"
          @pointercancel="onPointerUp"
        >
          <RouterLink
            v-for="item in items"
            :key="item.Id"
            :to="`/media/${encodeURIComponent(item.Id)}`"
            class="p2-poster-card"
            draggable="false"
            @click="onCardClick"
          >
            <div class="p2-poster-frame">
              <img
                v-if="!brokenIds.has(item.Id) && posterOf(item)"
                :src="posterOf(item)"
                :alt="titleOf(item)"
                loading="lazy"
                decoding="async"
                draggable="false"
                @error="brokenIds.add(item.Id)"
              />
              <div v-else class="p2-poster-fallback">
                <span class="p2-ph-title">{{ titleOf(item) }}</span>
                <span class="p2-ph-note">暂无海报</span>
              </div>
              <span v-if="resOf(item)" class="p2-badge-res">{{ resOf(item) }}</span>
              <span v-if="ratingOf(item)" class="p2-badge-rating">★ {{ ratingOf(item) }}</span>
            </div>
            <div class="p2-poster-name" :title="titleOf(item)">{{ titleOf(item) }}</div>
            <div class="p2-poster-meta">
              <span>{{ item.Type === 'Series' ? '剧集' : '电影' }}</span>
              <span v-if="item.ProductionYear">· {{ item.ProductionYear }}</span>
            </div>
          </RouterLink>
        </div>

        <button
          v-if="canScrollRight"
          type="button"
          class="p2-shelf-arrow is-right"
          aria-label="向右查看热门海报"
          @click="scrollByDir(1)"
        >
          <ChevronRight :size="18" aria-hidden="true" />
        </button>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { ArrowRight, ChevronLeft, ChevronRight } from 'lucide-vue-next'
import {
  embyApi,
  ensureEmbyBase,
  httpStatusOf,
  isEmbyAccessNotGranted,
  posterUrl,
  type EmbyItem,
} from '@/api/emby'
import { displayMediaTitle, resolutionBadge } from '@/utils/mediaTitle'
import { humanizeError } from '@/composables/usePanelApi'

const loading = ref(true)
const error = ref('')
const notGranted = ref(false)
const libraries = ref<EmbyItem[]>([])
const activeLibId = ref('')
const items = ref<EmbyItem[]>([])
const brokenIds = reactive(new Set<string>())
const tabCache = new Map<string, EmbyItem[]>()

const trackEl = ref<HTMLElement | null>(null)
const canScrollLeft = ref(false)
const canScrollRight = ref(false)

let dragStartX = 0
let dragStartScroll = 0
let dragMoved = false
const dragging = ref(false)

const activeLibName = computed(() => {
  if (!activeLibId.value) return ''
  return libraries.value.find((l) => l.Id === activeLibId.value)?.Name || ''
})

function titleOf(item: EmbyItem): string {
  return displayMediaTitle(item)
}

function posterOf(item: EmbyItem): string {
  return posterUrl(item, 340)
}

function ratingOf(item: EmbyItem): string {
  const r = item.CommunityRating
  return typeof r === 'number' && r > 0 ? r.toFixed(1) : ''
}

function resOf(item: EmbyItem): string {
  return resolutionBadge(item)
}

function syncScrollState() {
  const el = trackEl.value
  if (!el) {
    canScrollLeft.value = false
    canScrollRight.value = false
    return
  }
  canScrollLeft.value = el.scrollLeft > 8
  canScrollRight.value = el.scrollLeft + el.clientWidth < el.scrollWidth - 8
}

function scrollByDir(dir: -1 | 1) {
  const el = trackEl.value
  if (!el) return
  const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  const delta = Math.max(280, Math.round(el.clientWidth * 0.78)) * dir
  el.scrollBy({ left: delta, behavior: reduce ? 'auto' : 'smooth' })
}

function onPointerDown(e: PointerEvent) {
  if (e.pointerType !== 'mouse' || e.button !== 0) return
  const el = trackEl.value
  if (!el) return
  dragging.value = true
  dragMoved = false
  dragStartX = e.clientX
  dragStartScroll = el.scrollLeft
  el.setPointerCapture?.(e.pointerId)
}

function onPointerMove(e: PointerEvent) {
  if (!dragging.value || !trackEl.value) return
  const dx = e.clientX - dragStartX
  if (Math.abs(dx) > 6) dragMoved = true
  if (dragMoved) trackEl.value.scrollLeft = dragStartScroll - dx
}

function onPointerUp(e: PointerEvent) {
  if (!dragging.value) return
  dragging.value = false
  trackEl.value?.releasePointerCapture?.(e.pointerId)
}

function onCardClick(e: MouseEvent) {
  if (dragMoved) {
    e.preventDefault()
    e.stopPropagation()
    dragMoved = false
  }
}

function selectLibrary(libId: string) {
  if (activeLibId.value === libId) return
  activeLibId.value = libId
  if (trackEl.value) trackEl.value.scrollLeft = 0
  void loadHotItems(false)
}

async function loadHotItems(force = false) {
  const cacheKey = activeLibId.value || '__all__'
  if (!force && tabCache.has(cacheKey)) {
    items.value = tabCache.get(cacheKey) || []
    loading.value = false
    await nextTick()
    syncScrollState()
    return
  }
  loading.value = true
  error.value = ''
  notGranted.value = false
  try {
    await ensureEmbyBase()
    if (libraries.value.length === 0) {
      const views = await embyApi.getViews()
      libraries.value = Array.isArray(views) ? views : []
    }
    const res = await embyApi.getItems({
      parentId: activeLibId.value || undefined,
      includeTypes: ['Movie', 'Series'],
      sortBy: 'CommunityRating,DateCreated',
      sortOrder: 'Descending',
      limit: 16,
      enableTotalRecordCount: false,
    })
    const raw = Array.isArray(res?.Items) ? res.Items : []
    const withPoster = raw.filter((it) => Boolean(it.ImageTags?.Primary))
    const picked = (withPoster.length >= 4 ? withPoster : raw).slice(0, 14)
    tabCache.set(cacheKey, picked)
    items.value = picked
  } catch (err: unknown) {
    const status = httpStatusOf(err)
    if (isEmbyAccessNotGranted(err) || status === 401 || status === 403) {
      notGranted.value = true
      items.value = []
    } else {
      error.value = humanizeError(err)
    }
  } finally {
    loading.value = false
    await nextTick()
    syncScrollState()
  }
}

function onResize() {
  syncScrollState()
}

onMounted(() => {
  void loadHotItems()
  window.addEventListener('resize', onResize)
})

onUnmounted(() => {
  window.removeEventListener('resize', onResize)
})
</script>

<style scoped>
.p2-hot-band {
  max-width: min(1480px, 94vw);
  margin: 14px auto 0;
  padding: 0 20px;
}

.p2-hot-card {
  background: #fdfbfa;
  border: 1px solid #d1d1cd;
  border-radius: 16px;
  padding: 18px 22px 20px;
}

.p2-hot-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
}

.p2-hot-titles {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px 12px;
  min-width: 0;
}

.p2-hot-title {
  margin: 0;
  font-size: 18px;
  font-weight: 500;
  line-height: 1.35;
  letter-spacing: 0;
  color: #27251e;
}

.p2-hot-sub {
  margin: 0;
  font-size: 14px;
  line-height: 1.45;
  color: #524b42;
}

.p2-hot-more {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 36px;
  padding: 4px 12px;
  border-radius: 6px;
  border: 1px solid #d1d1cd;
  color: #016a71;
  font-size: 14px;
  font-weight: 500;
  text-decoration: none;
  white-space: nowrap;
  flex-shrink: 0;
  transition: opacity 0.16s ease, transform 0.16s ease;
}

.p2-hot-more:hover {
  transform: translateY(-1px);
  opacity: 0.88;
}

.p2-lib-tabs {
  display: flex;
  gap: 8px;
  overflow-x: auto;
  padding-bottom: 6px;
  margin-bottom: 12px;
  scrollbar-width: none;
}

.p2-lib-tabs::-webkit-scrollbar {
  display: none;
}

.p2-lib-tab {
  flex: 0 0 auto;
  min-height: 34px;
  padding: 5px 14px;
  border-radius: 9999px;
  border: 1px solid #d1d1cd;
  background: #faf8f5;
  color: #524b42;
  font-size: 14px;
  font-weight: 500;
  font-family: inherit;
  cursor: pointer;
  white-space: nowrap;
  transition: opacity 0.16s ease, transform 0.16s ease;
}

.p2-lib-tab:hover {
  color: #27251e;
}

.p2-lib-tab.is-active {
  background: #016a71;
  border-color: #016a71;
  color: #ffffff;
}

.p2-shelf-wrap {
  position: relative;
}

.p2-poster-row {
  display: flex;
  gap: 14px;
  overflow-x: auto;
  scroll-snap-type: x proximity;
  padding-bottom: 4px;
  scrollbar-width: thin;
}

.p2-poster-row.is-dragging {
  cursor: grabbing;
  scroll-snap-type: none;
  user-select: none;
}

.p2-poster-card {
  flex: 0 0 156px;
  width: 156px;
  scroll-snap-align: start;
  text-decoration: none;
  color: inherit;
  transition: transform 0.16s ease;
}

.p2-poster-card:hover {
  transform: translateY(-2px);
}

.p2-poster-frame {
  position: relative;
  width: 100%;
  aspect-ratio: 2 / 3;
  border-radius: 12px;
  overflow: hidden;
  background: #f0ece4;
  border: 1px solid #d1d1cd;
}

.p2-poster-frame img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.p2-poster-fallback {
  width: 100%;
  height: 100%;
  padding: 14px 12px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  background: linear-gradient(160deg, #f5f1e8 0%, #eae3d5 100%);
  color: #27251e;
}

.p2-ph-title {
  font-size: 14px;
  font-weight: 500;
  line-height: 1.4;
  display: -webkit-box;
  -webkit-line-clamp: 4;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.p2-ph-note {
  font-size: 12px;
  color: #524b42;
}

.p2-badge-res {
  position: absolute;
  top: 8px;
  left: 8px;
  padding: 2px 7px;
  border-radius: 9999px;
  background: rgba(250, 248, 245, 0.92);
  border: 1px solid #d1d1cd;
  color: #27251e;
  font-size: 11px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.p2-badge-rating {
  position: absolute;
  bottom: 8px;
  right: 8px;
  padding: 2px 7px;
  border-radius: 9999px;
  background: #016a71;
  color: #ffffff;
  font-size: 11px;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
}

.p2-poster-name {
  margin-top: 8px;
  font-size: 14px;
  font-weight: 500;
  line-height: 1.38;
  color: #27251e;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.p2-poster-meta {
  margin-top: 2px;
  display: flex;
  gap: 5px;
  font-size: 13px;
  color: #524b42;
  font-variant-numeric: tabular-nums;
}

.p2-shelf-arrow {
  position: absolute;
  top: 40%;
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

.p2-shelf-arrow.is-left {
  left: -10px;
}

.p2-shelf-arrow.is-right {
  right: -10px;
}

.p2-inline-link {
  color: #016a71;
  font-weight: 500;
  text-decoration: none;
  margin-left: 4px;
}

.p2-poster-skel {
  flex: 0 0 156px;
  width: 156px;
}

.p2-poster-skel-img {
  width: 100%;
  aspect-ratio: 2 / 3;
  border-radius: 12px;
}

.p2-poster-skel-line {
  height: 14px;
  width: 80%;
  margin-top: 8px;
  border-radius: 6px;
}

@media (max-width: 767px) {
  .p2-hot-band {
    max-width: 100%;
    padding: 0 16px;
    margin-top: 12px;
  }

  .p2-hot-card {
    padding: 14px 16px 16px;
  }

  .p2-poster-card,
  .p2-poster-skel {
    flex: 0 0 128px;
    width: 128px;
  }

  .p2-lib-tab {
    min-height: 40px;
    padding: 6px 14px;
  }

  .p2-shelf-arrow {
    display: none;
  }
}
</style>
