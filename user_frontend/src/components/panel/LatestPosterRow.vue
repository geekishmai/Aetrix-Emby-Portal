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
        <div v-for="n in 8" :key="n" class="p1-poster-skel">
          <div class="v4-skeleton p1-poster-skel-img"></div>
          <div class="v4-skeleton p1-poster-skel-line"></div>
        </div>
      </div>

      <div v-else-if="notGranted" class="v4-empty p1-unlock-guide">
        <span>开启观影权限后，即可在这里直接浏览最新入库海报与影片详情。</span>
        <RouterLink to="/game/operator" class="p1-inline-link">前往开启权限 →</RouterLink>
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
              <div class="p1-poster-hover" aria-hidden="true">
                <div class="p1-hover-meta">
                  <span>{{ item.Type === 'Series' ? '剧集' : '电影' }}</span>
                  <span v-if="item.ProductionYear">{{ item.ProductionYear }}</span>
                  <span v-if="ratingOf(item)" class="p1-hover-score">★ {{ ratingOf(item) }}</span>
                </div>
                <div class="p1-hover-title">{{ titleOf(item) }}</div>
              </div>
            </div>
            <div class="p1-poster-name" :title="titleOf(item)">{{ titleOf(item) }}</div>
            <div class="p1-poster-meta">
              <span>{{ item.Type === 'Series' ? '剧集' : '电影' }}</span>
              <span v-if="item.ProductionYear">· {{ item.ProductionYear }}</span>
              <span v-if="ratingOf(item)">· ★ {{ ratingOf(item) }}</span>
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
  max-width: min(1720px, 96vw);
  margin: 0 auto;
  padding: 28px 24px 0;
  box-sizing: border-box;
}

/* 无外框贴边舞台：海报直接生长于 #111111 暗厅底面 */
.p1-latest-card {
  background: transparent;
  border: 0;
  border-radius: 0;
  padding: 0;
}

.p1-latest-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;
}

.p1-latest-titles {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px 14px;
  min-width: 0;
}

.p1-latest-title {
  margin: 0;
  font-size: 20px;
  font-weight: 800;
  line-height: 1.25;
  letter-spacing: -0.01em;
  color: #f2f2f2;
}

.p1-latest-sub {
  margin: 0;
  font-size: 13px;
  line-height: 1.45;
  color: #9a9a9a;
}

.p1-latest-more {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 6px 12px;
  min-height: 34px;
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

.p1-latest-more:hover {
  border-color: #c3f53c;
  color: #c3f53c;
}

.p1-inline-link {
  color: #c3f53c;
  text-decoration: underline;
  text-underline-offset: 3px;
  margin-left: 4px;
}

.p1-shelf-wrap {
  position: relative;
}

.p1-poster-row {
  display: flex;
  gap: 10px;
  overflow-x: auto;
  scroll-snap-type: x proximity;
  -webkit-overflow-scrolling: touch;
  scrollbar-width: none;
  padding: 0 0 4px;
  user-select: none;
}

.p1-poster-row::-webkit-scrollbar {
  display: none;
}

.p1-poster-row.is-dragging {
  cursor: grabbing;
  scroll-snap-type: none;
}

/* 宽屏高密一行 8 张（≥1600px）/ 7 张（1280-1599px）/ 6 张（768-1279px）整幅对齐 */
.p1-poster-card,
.p1-poster-skel {
  flex: 0 0 calc((100% - 7 * 10px) / 8);
  width: calc((100% - 7 * 10px) / 8);
  min-width: 148px;
  scroll-snap-align: start;
  text-decoration: none;
  color: #f2f2f2;
  display: flex;
  flex-direction: column;
}

@media (min-width: 1280px) and (max-width: 1599.98px) {
  .p1-poster-card,
  .p1-poster-skel {
    flex-basis: calc((100% - 6 * 10px) / 7);
    width: calc((100% - 6 * 10px) / 7);
  }
}

@media (min-width: 768px) and (max-width: 1279.98px) {
  .p1-poster-card,
  .p1-poster-skel {
    flex-basis: calc((100% - 5 * 10px) / 6);
    width: calc((100% - 5 * 10px) / 6);
    min-width: 136px;
  }
}

.p1-poster-frame {
  position: relative;
  width: 100%;
  aspect-ratio: 2 / 3;
  border-radius: 2px;
  overflow: hidden;
  background: #171717;
  border: 1px solid #222222;
  transition: border-color 0.16s ease;
}

.p1-poster-frame img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
  transition: transform 0.22s ease;
}

.p1-poster-fallback {
  width: 100%;
  height: 100%;
  padding: 36px 12px 12px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  background: #171717;
  color: #f2f2f2;
}

.p1-ph-title {
  font-size: 14px;
  font-weight: 700;
  line-height: 1.35;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
  word-break: break-word;
}

.p1-ph-note {
  font-size: 11px;
  color: #9a9a9a;
}

.p1-badge-res {
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
  letter-spacing: 0.04em;
  line-height: 1.4;
}

/* 悬停浮现信息层（桌面精确指针触发） */
.p1-poster-hover {
  position: absolute;
  inset: auto 0 0 0;
  padding: 24px 10px 10px;
  background: linear-gradient(180deg, transparent 0%, rgba(17, 17, 17, 0.94) 46%, #111111 100%);
  opacity: 0;
  transform: translateY(4px);
  transition: opacity 0.16s ease, transform 0.16s ease;
  pointer-events: none;
}

.p1-hover-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: #9a9a9a;
  font-variant-numeric: tabular-nums;
}

.p1-hover-score {
  color: #c3f53c;
  font-weight: 700;
}

.p1-hover-title {
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
  .p1-poster-card:hover .p1-poster-frame {
    border-color: #c3f53c;
  }
  .p1-poster-card:hover .p1-poster-frame img {
    transform: scale(1.025);
  }
  .p1-poster-card:hover .p1-poster-hover {
    opacity: 1;
    transform: translateY(0);
  }
}

.p1-poster-name {
  margin-top: 7px;
  font-size: 13.5px;
  font-weight: 600;
  line-height: 1.35;
  color: #e6e6e6;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.p1-poster-meta {
  margin-top: 2px;
  font-size: 12px;
  line-height: 1.35;
  color: #9a9a9a;
  display: flex;
  gap: 4px;
  font-variant-numeric: tabular-nums;
  overflow: hidden;
  white-space: nowrap;
}

.p1-shelf-arrow {
  position: absolute;
  top: 40%;
  transform: translateY(-50%);
  width: 34px;
  height: 34px;
  border-radius: 2px;
  background: rgba(23, 23, 23, 0.94);
  border: 1px solid #333333;
  color: #f2f2f2;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  z-index: 3;
  transition: border-color 0.15s ease, color 0.15s ease;
}

.p1-shelf-arrow:hover {
  border-color: #c3f53c;
  color: #c3f53c;
}

.p1-shelf-arrow.is-left {
  left: -10px;
}

.p1-shelf-arrow.is-right {
  right: -10px;
}

.p1-poster-skel-img {
  width: 100%;
  aspect-ratio: 2 / 3;
  border-radius: 2px;
}

.p1-poster-skel-line {
  height: 14px;
  width: 76%;
  margin-top: 7px;
}

@media (max-width: 767px) {
  .p1-latest-band {
    max-width: 100%;
    padding: 20px 16px 0;
  }
  .p1-latest-sub {
    display: none;
  }
  .p1-poster-row {
    gap: 8px;
  }
  .p1-poster-card,
  .p1-poster-skel {
    flex: 0 0 112px;
    width: 112px;
    min-width: 112px;
  }
  .p1-shelf-arrow {
    display: none;
  }
}
</style>
