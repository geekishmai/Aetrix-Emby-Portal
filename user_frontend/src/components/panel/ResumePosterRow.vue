<template>
  <section
    v-if="!notGranted"
    id="disc-resume"
    class="p2-watch-band"
    aria-label="继续观看"
  >
    <div class="p2-watch-card" :class="{ 'is-compact': !loading && !error && items.length === 0 }">
      <div class="p2-watch-head">
        <div class="p2-watch-titles">
          <h2 class="p2-watch-title">继续观看</h2>
          <p v-if="items.length > 0" class="p2-watch-sub">接着上次的进度继续看，客户端播放进度自动同步</p>
          <p v-else-if="!loading && !error" class="p2-watch-sub">
            在 Infuse、VidHub 或 SenPlayer 观看影片后，未看完的进度会自动同步到这里接着看
          </p>
        </div>
        <RouterLink v-if="items.length > 0" to="/library" class="p2-watch-more">
          <span>全部片库</span>
          <ArrowRight :size="15" aria-hidden="true" />
        </RouterLink>
      </div>

      <div v-if="loading" class="p2-resume-row" aria-hidden="true">
        <div v-for="n in 4" :key="n" class="p2-resume-skel">
          <div class="v4-skeleton p2-resume-skel-img"></div>
          <div class="v4-skeleton p2-resume-skel-line"></div>
        </div>
      </div>

      <div v-else-if="error" class="v4-err">
        <span>继续观看加载失败：{{ error }}</span>
        <button type="button" class="v4-retry" @click="loadResume">重试</button>
      </div>

      <div v-else-if="items.length > 0" class="p2-shelf-wrap">
        <button
          v-if="canScrollLeft"
          type="button"
          class="p2-shelf-arrow is-left"
          aria-label="向左查看继续观看"
          @click="scrollByDir(-1)"
        >
          <ChevronLeft :size="18" aria-hidden="true" />
        </button>

        <div
          ref="trackEl"
          class="p2-resume-row"
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
            class="p2-resume-card"
            draggable="false"
            @click="onCardClick"
          >
            <div class="p2-resume-frame">
              <img
                v-if="!brokenIds.has(item.Id) && coverOf(item)"
                :src="coverOf(item)"
                :alt="primaryTitleOf(item)"
                loading="lazy"
                decoding="async"
                draggable="false"
                @error="brokenIds.add(item.Id)"
              />
              <div v-else class="p2-resume-fallback">
                <span>{{ primaryTitleOf(item) }}</span>
              </div>
              <span v-if="epCodeOf(item)" class="p2-ep-pill">{{ epCodeOf(item) }}</span>
              <div
                class="p2-progress-track"
                role="progressbar"
                :aria-valuenow="pctOf(item)"
                aria-valuemin="0"
                aria-valuemax="100"
                :aria-label="`已观看 ${pctOf(item)}%`"
              >
                <div class="p2-progress-fill" :style="{ width: `${pctOf(item)}%` }"></div>
              </div>
            </div>
            <div class="p2-resume-name" :title="primaryTitleOf(item)">{{ primaryTitleOf(item) }}</div>
            <div class="p2-resume-meta">
              <span>已看 {{ pctOf(item) }}%</span>
              <span v-if="remainTextOf(item)">· 剩 {{ remainTextOf(item) }}</span>
            </div>
          </RouterLink>
        </div>

        <button
          v-if="canScrollRight"
          type="button"
          class="p2-shelf-arrow is-right"
          aria-label="向右查看继续观看"
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
import {
  backdropUrl,
  embyApi,
  ensureEmbyBase,
  formatDuration,
  httpStatusOf,
  imageUrl,
  isEmbyAccessNotGranted,
  posterUrl,
  progressPercent,
  ticksToSeconds,
  type EmbyItem,
} from '@/api/emby'
import { cleanPtTitle, displayMediaTitle } from '@/utils/mediaTitle'
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

function primaryTitleOf(item: EmbyItem): string {
  if (item.Type === 'Episode' && item.SeriesName) {
    return cleanPtTitle(item.SeriesName) || displayMediaTitle(item)
  }
  return displayMediaTitle(item)
}

function epCodeOf(item: EmbyItem): string {
  if (item.Type !== 'Episode') return ''
  const s = item.ParentIndexNumber
  const e = item.IndexNumber
  if (typeof s === 'number' && typeof e === 'number') {
    return `S${String(s).padStart(2, '0')}E${String(e).padStart(2, '0')}`
  }
  if (typeof e === 'number') return `第 ${e} 集`
  return '单集'
}

function coverOf(item: EmbyItem): string {
  if (item.ImageTags?.Thumb) return imageUrl(item.Id, 'Thumb', 480)
  const bd = backdropUrl(item, 480)
  if (bd) return bd
  if (item.ImageTags?.Primary) return posterUrl(item, 420)
  if (item.SeriesId) return imageUrl(item.SeriesId, 'Backdrop', 480)
  return ''
}

function pctOf(item: EmbyItem): number {
  const pct = progressPercent(item)
  return pct > 0 ? pct : 5
}

function remainTextOf(item: EmbyItem): string {
  const total = item.RunTimeTicks || 0
  const pos = item.UserData?.PlaybackPositionTicks || 0
  if (!total || total <= pos) return ''
  return formatDuration(ticksToSeconds(total - pos))
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
  const delta = Math.max(260, Math.round(el.clientWidth * 0.76)) * dir
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

async function loadResume() {
  loading.value = true
  error.value = ''
  notGranted.value = false
  try {
    await ensureEmbyBase()
    const raw = await embyApi.getResume(12)
    items.value = Array.isArray(raw) ? raw : []
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
  void loadResume()
  window.addEventListener('resize', onResize)
})

onUnmounted(() => {
  window.removeEventListener('resize', onResize)
})
</script>

<style scoped>
.p2-watch-band {
  width: 100%;
  max-width: min(1720px, 96vw);
  margin: 26px auto 0;
  padding: 0 24px;
  box-sizing: border-box;
}

.p2-watch-card {
  background: transparent;
  border: 0;
  border-radius: 0;
  padding: 0;
}

.p2-watch-card.is-compact {
  padding: 14px 18px;
  background: #171717;
  border: 1px solid #2a2a2a;
  border-radius: 2px;
}

.p2-watch-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;
}

.p2-watch-card.is-compact .p2-watch-head {
  margin-bottom: 0;
}

.p2-watch-titles {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px 14px;
  min-width: 0;
}

.p2-watch-title {
  margin: 0;
  font-size: 20px;
  font-weight: 800;
  line-height: 1.25;
  letter-spacing: -0.01em;
  color: #f2f2f2;
}

.p2-watch-sub {
  margin: 0;
  font-size: 13px;
  line-height: 1.45;
  color: #9a9a9a;
}

.p2-watch-more {
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

.p2-watch-more:hover {
  border-color: #c3f53c;
  color: #c3f53c;
}

.p2-shelf-wrap {
  position: relative;
}

.p2-resume-row {
  display: flex;
  gap: 12px;
  overflow-x: auto;
  scroll-snap-type: x proximity;
  padding-bottom: 4px;
  scrollbar-width: none;
}

.p2-resume-row::-webkit-scrollbar {
  display: none;
}

.p2-resume-row.is-dragging {
  cursor: grabbing;
  scroll-snap-type: none;
  user-select: none;
}

.p2-resume-card,
.p2-resume-skel {
  flex: 0 0 calc((100% - 4 * 12px) / 5);
  width: calc((100% - 4 * 12px) / 5);
  min-width: 224px;
  scroll-snap-align: start;
  text-decoration: none;
  color: inherit;
}

.p2-resume-frame {
  position: relative;
  width: 100%;
  aspect-ratio: 16 / 9;
  border-radius: 2px;
  overflow: hidden;
  background: #171717;
  border: 1px solid #222222;
  transition: border-color 0.16s ease;
}

@media (hover: hover) and (pointer: fine) {
  .p2-resume-card:hover .p2-resume-frame {
    border-color: #c3f53c;
  }
}

.p2-resume-frame img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.p2-resume-fallback {
  width: 100%;
  height: 100%;
  padding: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
  background: #171717;
  color: #f2f2f2;
  font-size: 14px;
  font-weight: 700;
}

.p2-ep-pill {
  position: absolute;
  top: 6px;
  left: 6px;
  padding: 1px 6px;
  border-radius: 2px;
  background: rgba(17, 17, 17, 0.88);
  border: 1px solid #2a2a2a;
  color: #f2f2f2;
  font-family: var(--dh-font-mono);
  font-size: 11px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

/* 帆桥暗厅：继续观看进度条 3px 贴底，唯一荧光绿 #c3f53c */
.p2-progress-track {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 3px;
  background: rgba(255, 255, 255, 0.18);
}

.p2-progress-fill {
  height: 100%;
  background: #c3f53c;
}

.p2-resume-name {
  margin-top: 7px;
  font-size: 13.5px;
  font-weight: 600;
  line-height: 1.35;
  color: #e6e6e6;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.p2-resume-meta {
  margin-top: 2px;
  display: flex;
  gap: 5px;
  font-size: 12px;
  color: #9a9a9a;
  font-variant-numeric: tabular-nums;
}

.p2-shelf-arrow {
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

.p2-resume-skel-img {
  width: 100%;
  aspect-ratio: 16 / 9;
  border-radius: 2px;
}

.p2-resume-skel-line {
  height: 14px;
  width: 72%;
  margin-top: 7px;
  border-radius: 2px;
}

@media (max-width: 767px) {
  .p2-watch-band {
    max-width: 100%;
    padding: 0 16px;
    margin-top: 20px;
  }

  .p2-watch-card.is-compact {
    padding: 12px 14px;
  }

  .p2-resume-card,
  .p2-resume-skel {
    flex: 0 0 176px;
    width: 176px;
    min-width: 176px;
  }

  .p2-shelf-arrow {
    display: none;
  }
}
</style>
