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
        <div v-for="n in 8" :key="n" class="p2-poster-skel">
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
              <div class="p2-poster-hover" aria-hidden="true">
                <div class="p2-hover-meta">
                  <span>{{ item.Type === 'Series' ? '剧集' : '电影' }}</span>
                  <span v-if="item.ProductionYear">{{ item.ProductionYear }}</span>
                  <span v-if="ratingOf(item)" class="p2-hover-score">★ {{ ratingOf(item) }}</span>
                </div>
                <div class="p2-hover-title">{{ titleOf(item) }}</div>
              </div>
            </div>
            <div class="p2-poster-name" :title="titleOf(item)">{{ titleOf(item) }}</div>
            <div class="p2-poster-meta">
              <span>{{ item.Type === 'Series' ? '剧集' : '电影' }}</span>
              <span v-if="item.ProductionYear">· {{ item.ProductionYear }}</span>
              <span v-if="ratingOf(item)">· ★{{ ratingOf(item) }}</span>
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
      filters: ['HasPrimaryImage'],
      sortBy: 'CommunityRating,DateCreated',
      sortOrder: 'Descending',
      limit: 24,
      enableTotalRecordCount: false,
    })
    const raw = Array.isArray(res?.Items) ? res.Items : []
    // 首页各库热门严格排除无海报条目（不再回退展示 ImageTags 缺失的未刮削行）
    const picked = raw.filter((it) => Boolean(it.ImageTags?.Primary)).slice(0, 14)
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
  width: 100%;
  max-width: min(1720px, 96vw);
  margin: 28px auto 0;
  padding: 0 24px;
  box-sizing: border-box;
}

.p2-hot-card {
  background: transparent;
  border: 0;
  border-radius: 0;
  padding: 0;
}

.p2-hot-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
}

.p2-hot-titles {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px 14px;
  min-width: 0;
}

.p2-hot-title {
  margin: 0;
  font-size: 20px;
  font-weight: 800;
  line-height: 1.25;
  letter-spacing: -0.01em;
  color: #f2f2f2;
}

.p2-hot-sub {
  margin: 0;
  font-size: 13px;
  line-height: 1.45;
  color: #9a9a9a;
}

.p2-hot-more {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-height: 34px;
  padding: 6px 12px;
  border-radius: 2px;
  background: #171717;
  border: 1px solid #2a2a2a;
  color: #f2f2f2;
  font-size: 13px;
  font-weight: 600;
  text-decoration: none;
  white-space: nowrap;
  flex-shrink: 0;
  transition: border-color 0.15s ease, color 0.15s ease;
}

.p2-hot-more:hover {
  border-color: #c3f53c;
  color: #c3f53c;
}

/* 建筑感底边下划线页签：杜绝胶囊圆角堆砌 */
.p2-lib-tabs {
  display: flex;
  gap: 20px;
  overflow-x: auto;
  border-bottom: 1px solid #222222;
  margin-bottom: 16px;
  scrollbar-width: none;
}

.p2-lib-tabs::-webkit-scrollbar {
  display: none;
}

.p2-lib-tab {
  position: relative;
  flex: 0 0 auto;
  min-height: 38px;
  padding: 6px 2px 10px;
  border: 0;
  border-bottom: 2px solid transparent;
  border-radius: 0;
  background: transparent;
  color: #9a9a9a;
  font-size: 14px;
  font-weight: 600;
  font-family: inherit;
  cursor: pointer;
  white-space: nowrap;
  transition: color 0.15s ease, border-color 0.15s ease;
}

.p2-lib-tab:hover {
  color: #f2f2f2;
}

.p2-lib-tab.is-active {
  color: #f2f2f2;
  border-bottom-color: #c3f53c;
  font-weight: 700;
}

.p2-shelf-wrap {
  position: relative;
}

.p2-poster-row {
  display: flex;
  gap: 10px;
  overflow-x: auto;
  scroll-snap-type: x proximity;
  padding-bottom: 4px;
  scrollbar-width: none;
}

.p2-poster-row::-webkit-scrollbar {
  display: none;
}

.p2-poster-row.is-dragging {
  cursor: grabbing;
  scroll-snap-type: none;
  user-select: none;
}

.p2-poster-card,
.p2-poster-skel {
  flex: 0 0 calc((100% - 7 * 10px) / 8);
  width: calc((100% - 7 * 10px) / 8);
  min-width: 148px;
  scroll-snap-align: start;
  text-decoration: none;
  color: inherit;
  display: flex;
  flex-direction: column;
}

@media (min-width: 1280px) and (max-width: 1599.98px) {
  .p2-poster-card,
  .p2-poster-skel {
    flex-basis: calc((100% - 6 * 10px) / 7);
    width: calc((100% - 6 * 10px) / 7);
  }
}

@media (min-width: 768px) and (max-width: 1279.98px) {
  .p2-poster-card,
  .p2-poster-skel {
    flex-basis: calc((100% - 5 * 10px) / 6);
    width: calc((100% - 5 * 10px) / 6);
    min-width: 136px;
  }
}

.p2-poster-frame {
  position: relative;
  width: 100%;
  aspect-ratio: 2 / 3;
  border-radius: 2px;
  overflow: hidden;
  background: #171717;
  border: 1px solid #222222;
  transition: border-color 0.16s ease;
}

.p2-poster-frame img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
  transition: transform 0.22s ease;
}

.p2-poster-fallback {
  width: 100%;
  height: 100%;
  padding: 14px 12px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  background: #171717;
  color: #f2f2f2;
}

.p2-ph-title {
  font-size: 14px;
  font-weight: 700;
  line-height: 1.35;
  display: -webkit-box;
  -webkit-line-clamp: 4;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.p2-ph-note {
  font-size: 11px;
  color: #9a9a9a;
}

.p2-badge-res {
  position: absolute;
  top: 6px;
  left: 6px;
  padding: 1px 6px;
  border-radius: 2px;
  background: rgba(17, 17, 17, 0.88);
  border: 1px solid #2a2a2a;
  color: #f2f2f2;
  font-family: var(--dh-font-mono);
  font-size: 10px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.p2-poster-hover {
  position: absolute;
  inset: auto 0 0 0;
  padding: 24px 10px 10px;
  background: linear-gradient(180deg, transparent 0%, rgba(17, 17, 17, 0.94) 46%, #111111 100%);
  opacity: 0;
  transform: translateY(4px);
  transition: opacity 0.16s ease, transform 0.16s ease;
  pointer-events: none;
}

.p2-hover-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: #9a9a9a;
  font-variant-numeric: tabular-nums;
}

.p2-hover-score {
  color: #c3f53c;
  font-weight: 700;
}

.p2-hover-title {
  margin-top: 3px;
  font-size: 13px;
  font-weight: 700;
  line-height: 1.3;
  color: #f2f2f2;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

@media (hover: hover) and (pointer: fine) {
  .p2-poster-card:hover .p2-poster-frame {
    border-color: #c3f53c;
  }
  .p2-poster-card:hover .p2-poster-frame img {
    transform: scale(1.025);
  }
  .p2-poster-card:hover .p2-poster-hover {
    opacity: 1;
    transform: translateY(0);
  }
}

.p2-poster-name {
  margin-top: 7px;
  font-size: 13.5px;
  font-weight: 600;
  line-height: 1.35;
  color: #e6e6e6;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.p2-poster-meta {
  margin-top: 2px;
  display: flex;
  gap: 4px;
  font-size: 12px;
  color: #9a9a9a;
  font-variant-numeric: tabular-nums;
  overflow: hidden;
  white-space: nowrap;
}

.p2-shelf-arrow {
  position: absolute;
  top: 40%;
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

.p2-shelf-arrow:hover {
  border-color: #c3f53c;
  color: #c3f53c;
}

.p2-shelf-arrow.is-left {
  left: -10px;
}

.p2-shelf-arrow.is-right {
  right: -10px;
}

.p2-inline-link {
  color: #c3f53c;
  font-weight: 600;
  text-decoration: none;
  margin-left: 4px;
}

.p2-poster-skel-img {
  width: 100%;
  aspect-ratio: 2 / 3;
  border-radius: 2px;
}

.p2-poster-skel-line {
  height: 14px;
  width: 80%;
  margin-top: 7px;
  border-radius: 2px;
}

@media (max-width: 767px) {
  .p2-hot-band {
    max-width: 100%;
    padding: 0 16px;
    margin-top: 22px;
  }

  .p2-hot-sub {
    display: none;
  }

  .p2-poster-row {
    gap: 8px;
  }

  .p2-poster-card,
  .p2-poster-skel {
    flex: 0 0 112px;
    width: 112px;
    min-width: 112px;
  }

  .p2-poster-meta {
    font-size: 11px;
    gap: 2px;
  }

  .p2-lib-tab {
    min-height: 38px;
    padding: 6px 2px 8px;
  }

  .p2-shelf-arrow {
    display: none;
  }
}
</style>
