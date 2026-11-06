<template>
  <section
    id="disc-latest"
    class="p1-latest-band"
    aria-label="最新入库海报墙"
  >
    <div class="p1-latest-card">
      <div class="p1-latest-head">
        <div class="p1-latest-titles">
          <h2 class="p1-latest-title">最新入库</h2>
          <p class="p1-latest-sub">近期整理上架的电影与剧集，点海报查看详情或去播放器点播</p>
        </div>
        <RouterLink to="/library" class="p1-latest-more">
          <span>浏览片库</span>
          <ArrowRight :size="15" aria-hidden="true" />
        </RouterLink>
      </div>

      <div v-if="loading" class="p1-poster-row" aria-hidden="true">
        <div v-for="n in 7" :key="n" class="p1-poster-skel">
          <div class="v4-skeleton p1-poster-skel-img"></div>
          <div class="v4-skeleton p1-poster-skel-line"></div>
        </div>
      </div>

      <div v-else-if="notGranted" class="v4-empty p1-unlock-guide">
        <span>开启观影权限后，即可在这里直接浏览最新入库海报与影片详情。</span>
        <RouterLink to="/game/account" class="p1-inline-link">去我的页面开启权限 →</RouterLink>
      </div>

      <div v-else-if="error" class="v4-err">
        <span>最新入库加载失败：{{ error }}</span>
        <button type="button" class="v4-retry" @click="loadLatest">重试</button>
      </div>

      <div v-else-if="items.length === 0" class="v4-empty">
        片库正在入库刮削中，稍后回来看看，或直接<RouterLink to="/library" class="p1-inline-link">打开片库</RouterLink>
      </div>
      <div v-else class="p1-shelf-wrap">
        <button
          v-if="canScrollLeft"
          type="button"
          class="p1-shelf-arrow is-left"
          aria-label="向左查看海报"
          @click="scrollByDir(-1)"
        >
          <ChevronLeft :size="18" aria-hidden="true" />
        </button>

        <div
          ref="trackEl"
          class="p1-poster-row"
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
            class="p1-poster-card"
            draggable="false"
            @click="onCardClick"
          >
            <div class="p1-poster-frame">
              <img
                v-if="!brokenIds.has(item.Id) && posterOf(item)"
                :src="posterOf(item)"
                :alt="titleOf(item)"
                loading="lazy"
                decoding="async"
                draggable="false"
                @error="markBroken(item.Id)"
              />
              <div v-else class="p1-poster-fallback">
                <span class="p1-ph-title">{{ titleOf(item) }}</span>
                <span class="p1-ph-note">暂无海报</span>
              </div>
              <span v-if="resOf(item)" class="p1-badge-res">{{ resOf(item) }}</span>
              <span v-if="ratingOf(item)" class="p1-badge-rating">★ {{ ratingOf(item) }}</span>
            </div>
            <div class="p1-poster-name" :title="titleOf(item)">{{ titleOf(item) }}</div>
            <div class="p1-poster-meta">
              <span>{{ item.Type === 'Series' ? '剧集' : '电影' }}</span>
              <span v-if="item.ProductionYear">· {{ item.ProductionYear }}</span>
            </div>
          </RouterLink>
        </div>

        <button
          v-if="canScrollRight"
          type="button"
          class="p1-shelf-arrow is-right"
          aria-label="向右查看海报"
          @click="scrollByDir(1)"
        >
          <ChevronRight :size="18" aria-hidden="true" />
        </button>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, reactive, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { ArrowRight, ChevronLeft, ChevronRight } from 'lucide-vue-next'
import { embyApi, ensureEmbyBase, httpStatusOf, isEmbyAccessNotGranted, posterUrl, type EmbyItem } from '@/api/emby'
import { displayMediaTitle, hasCjk, resolutionBadge } from '@/utils/mediaTitle'
import { humanizeError } from '@/composables/usePanelApi'

const loading = ref(true)
const error = ref('')
const notGranted = ref(false)
const items = ref<EmbyItem[]>([])
const brokenIds = reactive(new Set<string>())

const trackEl = ref<HTMLElement | null>(null)
const canScrollLeft = ref(false)
const canScrollRight = ref(false)

let dragStartX = 0
let dragStartScroll = 0
let dragMoved = false
const dragging = ref(false)

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

function markBroken(id: string) {
  brokenIds.add(id)
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
  trackEl.value.scrollLeft = dragStartScroll - dx
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

async function loadLatest() {
  loading.value = true
  error.value = ''
  notGranted.value = false
  try {
    await ensureEmbyBase()
    const res = await embyApi.getItems({
      includeTypes: ['Movie', 'Series'],
      sortBy: 'DateCreated',
      sortOrder: 'Descending',
      limit: 36,
      recursive: true,
      enableTotalRecordCount: false,
    })
    const raw = Array.isArray(res?.Items) ? res.Items : []
    const withPoster = raw.filter((it) => !!it.ImageTags?.Primary)
    // 优先排有中文标题的已刮削条目，其次其余有海报条目
    withPoster.sort((a, b) => {
      const ac = hasCjk(displayMediaTitle(a)) ? 1 : 0
      const bc = hasCjk(displayMediaTitle(b)) ? 1 : 0
      return bc - ac
    })
    items.value = withPoster.slice(0, 18)
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

onMounted(() => {
  void loadLatest()
  window.addEventListener('resize', syncScrollState)
})

onUnmounted(() => {
  window.removeEventListener('resize', syncScrollState)
})
</script>

<style scoped>
.p1-latest-band {
  width: 100%;
  max-width: min(1480px, 94vw);
  margin: 0 auto;
  padding: 20px 24px 0;
  box-sizing: border-box;
}

.p1-latest-card {
  background: #fdfbfa;
  border: 1px solid #d1d1cd;
  border-radius: 16px;
  padding: 20px;
}

.p1-latest-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}

.p1-latest-titles {
  min-width: 0;
}

.p1-latest-title {
  margin: 0;
  font-size: 18px;
  font-weight: 500;
  line-height: 1.35;
  color: #27251e;
}

.p1-latest-sub {
  margin: 4px 0 0;
  font-size: 14px;
  line-height: 1.5;
  color: #524b42;
}

.p1-latest-more {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 14px;
  min-height: 38px;
  border-radius: 9999px;
  background: #016a71;
  color: #ffffff;
  font-size: 14px;
  font-weight: 500;
  text-decoration: none;
  white-space: nowrap;
  flex-shrink: 0;
  transition: opacity 0.16s ease;
}

.p1-latest-more:hover {
  opacity: 0.9;
}

.p1-inline-link {
  color: #016a71;
  text-decoration: underline;
  text-underline-offset: 3px;
  margin-left: 4px;
}

.p1-shelf-wrap {
  position: relative;
}

.p1-poster-row {
  display: flex;
  gap: 16px;
  overflow-x: auto;
  scroll-snap-type: x proximity;
  -webkit-overflow-scrolling: touch;
  scrollbar-width: none;
  padding: 2px 2px 6px;
  user-select: none;
}

.p1-poster-row::-webkit-scrollbar {
  display: none;
}

.p1-poster-row.is-dragging {
  cursor: grabbing;
  scroll-snap-type: none;
}

.p1-poster-card {
  flex: 0 0 auto;
  width: 152px;
  scroll-snap-align: start;
  text-decoration: none;
  color: #27251e;
  display: flex;
  flex-direction: column;
  transition: transform 0.18s ease;
}

@media (hover: hover) and (pointer: fine) {
  .p1-poster-card:hover {
    transform: translateY(-2px);
  }
}

.p1-poster-frame {
  position: relative;
  width: 100%;
  aspect-ratio: 2 / 3;
  border-radius: 12px;
  overflow: hidden;
  background: #efe9df;
  border: 1px solid #d1d1cd;
}

.p1-poster-frame img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.p1-poster-fallback {
  width: 100%;
  height: 100%;
  padding: 38px 12px 12px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  background: linear-gradient(160deg, #eae2d3 0%, #d8ccb8 100%);
  color: #27251e;
}

.p1-ph-title {
  font-size: 14px;
  font-weight: 500;
  line-height: 1.4;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
  word-break: break-word;
}

.p1-ph-note {
  font-size: 12px;
  color: #6b6257;
}

.p1-badge-rating {
  position: absolute;
  top: 8px;
  right: 8px;
  padding: 2px 8px;
  border-radius: 9999px;
  background: rgba(28, 25, 22, 0.84);
  color: #fdfbfa;
  font-size: 12px;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
  line-height: 1.4;
}

.p1-badge-res {
  position: absolute;
  top: 8px;
  left: 8px;
  padding: 2px 7px;
  border-radius: 6px;
  background: rgba(28, 25, 22, 0.82);
  color: #fdfbfa;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.02em;
  line-height: 1.4;
}

.p1-poster-name {
  margin-top: 8px;
  font-size: 14px;
  font-weight: 500;
  line-height: 1.4;
  color: #27251e;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  word-break: break-word;
}

.p1-poster-meta {
  margin-top: 2px;
  font-size: 13px;
  line-height: 1.4;
  color: #524b42;
  display: flex;
  gap: 4px;
  font-variant-numeric: tabular-nums;
}

.p1-shelf-arrow {
  position: absolute;
  top: 38%;
  transform: translateY(-50%);
  width: 38px;
  height: 38px;
  border-radius: 9999px;
  background: rgba(253, 251, 250, 0.96);
  border: 1px solid #d1d1cd;
  color: #27251e;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  z-index: 3;
  transition: opacity 0.16s ease;
}

.p1-shelf-arrow:hover {
  opacity: 0.88;
}

.p1-shelf-arrow.is-left {
  left: -14px;
}

.p1-shelf-arrow.is-right {
  right: -14px;
}

.p1-poster-skel {
  flex: 0 0 auto;
  width: 152px;
}

.p1-poster-skel-img {
  width: 100%;
  aspect-ratio: 2 / 3;
  border-radius: 12px;
}

.p1-poster-skel-line {
  height: 16px;
  width: 78%;
  margin-top: 8px;
}

@media (max-width: 767px) {
  .p1-latest-band {
    max-width: min(480px, 100vw);
    padding: 16px 16px 0;
  }
  .p1-latest-card {
    padding: 16px;
  }
  .p1-latest-sub {
    display: none;
  }
  .p1-poster-card,
  .p1-poster-skel {
    width: 124px;
  }
  .p1-shelf-arrow {
    display: none;
  }
}

@media (min-width: 768px) and (max-width: 1279.98px) {
  .p1-latest-band {
    max-width: min(1040px, 94vw);
    padding: 20px 20px 0;
  }
}
</style>
