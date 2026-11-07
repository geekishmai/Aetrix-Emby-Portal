<template>
  <section class="dh-hero" aria-label="首屏精选巨幕">
    <!-- 加载态：暗厅等高静默骨架，避免布局跳动 -->
    <div v-if="loading" class="dh-hero-stage is-loading" aria-hidden="true">
      <div class="dh-hero-inner">
        <div class="dh-hero-copy">
          <div class="v4-skeleton" style="width: 148px; height: 14px"></div>
          <div class="v4-skeleton" style="width: min(420px, 78vw); height: 48px; margin-top: 16px"></div>
          <div class="v4-skeleton" style="width: min(520px, 86vw); height: 42px; margin-top: 18px"></div>
          <div class="dh-hero-actions" style="margin-top: 26px">
            <div class="v4-skeleton" style="width: 152px; height: 44px"></div>
            <div class="v4-skeleton" style="width: 124px; height: 44px"></div>
          </div>
        </div>
      </div>
    </div>

    <!-- 主视觉巨幕：全宽无字剧照 + 透明 Logo / 粗黑大字 + 唯一 #c3f53c 主操作按钮 -->
    <div v-else-if="featured" class="dh-hero-stage">
      <div class="dh-hero-media" aria-hidden="true">
        <img
          v-if="heroBackdrop && !brokenBackdropIds.has(featured.Id)"
          :key="featured.Id"
          :src="heroBackdrop"
          :alt="''"
          class="dh-hero-img"
          decoding="async"
          fetchpriority="high"
          @error="brokenBackdropIds.add(featured.Id)"
        />
        <div class="dh-hero-scrim"></div>
      </div>

      <div class="dh-hero-inner">
        <div class="dh-hero-copy">
          <div class="dh-hero-eyebrow">
            <span class="dh-eyebrow-tag">{{ featured.Type === 'Series' ? '剧集' : '电影' }}</span>
            <span v-if="featured.ProductionYear" class="dh-eyebrow-num">{{ featured.ProductionYear }}</span>
            <span v-if="featuredRating" class="dh-eyebrow-num">★ {{ featuredRating }}</span>
            <span v-if="featuredRes" class="dh-eyebrow-spec">{{ featuredRes }}</span>
            <span v-if="featuredGenres" class="dh-eyebrow-genre">{{ featuredGenres }}</span>
          </div>

          <div class="dh-hero-brand">
            <img
              v-if="heroLogo && !brokenLogoIds.has(featured.Id)"
              :key="`logo-${featured.Id}`"
              :src="heroLogo"
              :alt="featuredTitle"
              class="dh-hero-logo"
              decoding="async"
              @error="brokenLogoIds.add(featured.Id)"
            />
            <h1
              class="dh-hero-title"
              :class="{ 'is-sub': Boolean(heroLogo && !brokenLogoIds.has(featured.Id)) }"
            >
              {{ featuredTitle }}
            </h1>
          </div>

          <p class="dh-hero-overview">
            {{ featuredOverview }}
          </p>

          <div class="dh-hero-actions">
            <RouterLink
              :to="`/media/${encodeURIComponent(featured.Id)}`"
              class="dh-btn-cta"
            >
              <Play :size="16" fill="currentColor" aria-hidden="true" />
              <span>立即点播详情</span>
            </RouterLink>
            <RouterLink to="/library" class="dh-btn-secondary">
              <span>浏览全部片库</span>
              <ArrowUpRight :size="16" aria-hidden="true" />
            </RouterLink>
            <RouterLink to="/clients" class="dh-btn-ghost">
              <span>播放器接入</span>
            </RouterLink>
          </div>
        </div>

        <!-- 右下极简切页导轨（最多 5 部精选横图） -->
        <div
          v-if="slides.length > 1"
          class="dh-hero-rail"
          role="tablist"
          aria-label="切换首屏精选影片"
        >
          <button
            v-for="(sl, idx) in slides"
            :key="sl.Id"
            type="button"
            role="tab"
            class="dh-rail-item"
            :class="{ 'is-active': idx === activeIdx }"
            :aria-selected="idx === activeIdx"
            :aria-label="`切换到 ${titleOf(sl)}`"
            @click="selectSlide(idx)"
          >
            <span class="dh-rail-idx">0{{ idx + 1 }}</span>
            <span class="dh-rail-name">{{ titleOf(sl) }}</span>
            <span class="dh-rail-bar" aria-hidden="true"></span>
          </button>
        </div>
      </div>
    </div>

    <!-- 未授权或空片库兜底：保持暗厅硬朗横幅，不退回暖纸问候条 -->
    <div v-else class="dh-hero-stage is-fallback">
      <div class="dh-hero-inner">
        <div class="dh-hero-copy">
          <div class="dh-hero-eyebrow">
            <span class="dh-eyebrow-tag">帆桥影院</span>
            <span class="dh-eyebrow-num">{{ dateLabel }}</span>
          </div>
          <h1 class="dh-hero-title">{{ greeting }}，{{ nickname }}</h1>
          <p class="dh-hero-overview">
            {{
              notGranted
                ? '开启观影权限后即可浏览高清片库，一键唤起 Infuse、VidHub 或 SenPlayer 原盘直连播放。'
                : '选一部高分佳作，支持 Infuse、VidHub、SenPlayer 多端进度同步与原画直放。'
            }}
          </p>
          <div class="dh-hero-actions">
            <RouterLink :to="notGranted ? '/game/operator' : '/library'" class="dh-btn-cta">
              <Play :size="16" fill="currentColor" aria-hidden="true" />
              <span>{{ notGranted ? '开启观影权限' : '浏览片库' }}</span>
            </RouterLink>
            <RouterLink to="/clients" class="dh-btn-secondary">
              <span>播放器接入</span>
            </RouterLink>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { ArrowUpRight, Play } from 'lucide-vue-next'
import {
  backdropUrl,
  embyApi,
  ensureEmbyBase,
  httpStatusOf,
  imageUrl,
  isEmbyAccessNotGranted,
  posterUrl,
  type EmbyItem,
} from '@/api/emby'
import { displayMediaTitle, hasCjk, resolutionBadge } from '@/utils/mediaTitle'

defineProps<{
  greeting: string
  nickname: string
  dateLabel: string
}>()

const loading = ref(true)
const notGranted = ref(false)
const slides = ref<EmbyItem[]>([])
const activeIdx = ref(0)
const brokenBackdropIds = reactive(new Set<string>())
const brokenLogoIds = reactive(new Set<string>())

let rotateTimer: number | undefined

const featured = computed<EmbyItem | null>(() => slides.value[activeIdx.value] ?? slides.value[0] ?? null)

function titleOf(item: EmbyItem): string {
  return displayMediaTitle(item)
}

const featuredTitle = computed(() => (featured.value ? titleOf(featured.value) : ''))

const featuredRating = computed(() => {
  const r = featured.value?.CommunityRating
  return typeof r === 'number' && r > 0 ? r.toFixed(1) : ''
})

const featuredRes = computed(() => (featured.value ? resolutionBadge(featured.value) : ''))

const featuredGenres = computed(() => {
  const gs = featured.value?.Genres
  return Array.isArray(gs) && gs.length > 0 ? gs.slice(0, 3).join(' / ') : ''
})

const featuredOverview = computed(() => {
  const raw = featured.value?.Overview?.trim()
  if (raw) return raw
  return '高清原盘直连片库现已就绪，点击查看完整演职员表并一键唤起 Infuse、VidHub 或 SenPlayer 流畅播放。'
})

const heroBackdrop = computed(() => {
  const it = featured.value
  if (!it) return ''
  const bd = backdropUrl(it, 1920)
  if (bd) return bd
  if (it.ImageTags?.Thumb) return imageUrl(it.Id, 'Thumb', 1600)
  return posterUrl(it, 1280)
})

const heroLogo = computed(() => {
  const it = featured.value
  if (!it || !it.ImageTags?.Logo) return ''
  return imageUrl(it.Id, 'Logo', 720)
})

function selectSlide(idx: number) {
  activeIdx.value = idx
  restartAutoRotate()
}

function restartAutoRotate() {
  if (rotateTimer) {
    window.clearInterval(rotateTimer)
    rotateTimer = undefined
  }
  if (slides.value.length <= 1) return
  if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return
  rotateTimer = window.setInterval(() => {
    activeIdx.value = (activeIdx.value + 1) % slides.value.length
  }, 8000)
}

async function loadHero() {
  loading.value = true
  notGranted.value = false
  try {
    await ensureEmbyBase()
    const res = await embyApi.getItems({
      includeTypes: ['Movie', 'Series'],
      sortBy: 'DateCreated',
      sortOrder: 'Descending',
      limit: 30,
      recursive: true,
      enableTotalRecordCount: false,
    })
    const raw = Array.isArray(res?.Items) ? res.Items : []
    // 严格优先挑选带无字宽幅剧照（BackdropImageTags）+ 透明 Logo + 中文片名的条目
    const candidates = raw.filter(
      (it) => (it.BackdropImageTags && it.BackdropImageTags.length > 0) || !!it.ImageTags?.Primary,
    )
    candidates.sort((a, b) => {
      const aScore =
        (a.BackdropImageTags?.length ? 4 : 0) +
        (a.ImageTags?.Logo ? 2 : 0) +
        (hasCjk(displayMediaTitle(a)) ? 2 : 0) +
        ((a.CommunityRating || 0) >= 7.5 ? 1 : 0)
      const bScore =
        (b.BackdropImageTags?.length ? 4 : 0) +
        (b.ImageTags?.Logo ? 2 : 0) +
        (hasCjk(displayMediaTitle(b)) ? 2 : 0) +
        ((b.CommunityRating || 0) >= 7.5 ? 1 : 0)
      return bScore - aScore
    })
    slides.value = candidates.slice(0, 5)
    activeIdx.value = 0
    restartAutoRotate()
  } catch (err: unknown) {
    const status = httpStatusOf(err)
    if (isEmbyAccessNotGranted(err) || status === 401 || status === 403) {
      notGranted.value = true
    }
    slides.value = []
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void loadHero()
})

onUnmounted(() => {
  if (rotateTimer) window.clearInterval(rotateTimer)
})
</script>

<style scoped>
.dh-hero {
  position: relative;
  width: 100%;
  background: #111111;
  border-bottom: 1px solid #222222;
}

.dh-hero-stage {
  position: relative;
  width: 100%;
  max-width: 1920px;
  min-height: clamp(420px, 50vw, 600px);
  margin: 0 auto;
  display: flex;
  align-items: flex-end;
  overflow: hidden;
  background: #111111;
}

.dh-hero-stage.is-fallback {
  min-height: 280px;
  background: #141414;
}

.dh-hero-media {
  position: absolute;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  background: #111111;
}

.dh-hero-img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  object-position: center 20%;
  display: block;
  opacity: 0.86;
}

/* 纯黑影院定向暗角遮罩：只做左/底向 #111111 画布自然消隐，绝不染色 */
.dh-hero-scrim {
  position: absolute;
  inset: 0;
  background:
    linear-gradient(90deg, #111111 0%, rgba(17, 17, 17, 0.9) 34%, rgba(17, 17, 17, 0.34) 68%, rgba(17, 17, 17, 0.62) 100%),
    linear-gradient(180deg, rgba(17, 17, 17, 0.42) 0%, rgba(17, 17, 17, 0.08) 38%, rgba(17, 17, 17, 0.78) 80%, #111111 100%);
}

.dh-hero-inner {
  position: relative;
  z-index: 1;
  width: 100%;
  max-width: min(1720px, 96vw);
  margin: 0 auto;
  padding: 56px 24px 40px;
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 36px;
  box-sizing: border-box;
}

.dh-hero-copy {
  max-width: min(640px, 100%);
  min-width: 0;
}

.dh-hero-eyebrow {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
  font-size: 12px;
  line-height: 1.2;
  color: #9a9a9a;
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.06em;
}

.dh-eyebrow-tag {
  padding: 3px 7px;
  background: #1e1e1e;
  border: 1px solid #2a2a2a;
  border-radius: 2px;
  color: #f2f2f2;
  font-family: var(--dh-font-mono);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
}

.dh-eyebrow-spec {
  padding: 2px 6px;
  border: 1px solid #3a3a3a;
  border-radius: 2px;
  color: #f2f2f2;
  font-family: var(--dh-font-mono);
  font-size: 11px;
  font-weight: 600;
}

.dh-eyebrow-num {
  color: #f2f2f2;
  font-weight: 600;
}

.dh-eyebrow-genre {
  color: #9a9a9a;
  letter-spacing: 0.02em;
}

.dh-hero-brand {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
}

.dh-hero-logo {
  max-height: clamp(54px, 6.2vw, 92px);
  max-width: min(420px, 82vw);
  width: auto;
  object-fit: contain;
  display: block;
}

.dh-hero-title {
  margin: 0;
  font-family: var(--dh-font-sans);
  font-size: clamp(30px, 3.8vw, 52px);
  font-weight: 800;
  line-height: 1.12;
  letter-spacing: -0.02em;
  color: #f2f2f2;
  word-break: break-word;
}

.dh-hero-title.is-sub {
  font-size: 16px;
  font-weight: 600;
  letter-spacing: 0;
  color: #9a9a9a;
}

.dh-hero-overview {
  margin: 14px 0 0;
  max-width: 54ch;
  font-size: 15px;
  line-height: 1.65;
  color: #b8b8b8;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.dh-hero-actions {
  margin-top: 24px;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}

/* 全屏唯一 #c3f53c 荧光绿主操作按钮 */
.dh-btn-cta {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  height: 44px;
  padding: 0 22px;
  border-radius: 2px;
  background: #c3f53c;
  border: 1px solid #c3f53c;
  color: #111111;
  font-size: 14px;
  font-weight: 800;
  letter-spacing: 0.01em;
  text-decoration: none;
  white-space: nowrap;
  transition: background-color 0.15s ease, border-color 0.15s ease;
}

.dh-btn-cta:hover {
  background: #d1fa58;
  border-color: #d1fa58;
}

.dh-btn-secondary {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 44px;
  padding: 0 18px;
  border-radius: 2px;
  background: rgba(23, 23, 23, 0.92);
  border: 1px solid #333333;
  color: #f2f2f2;
  font-size: 14px;
  font-weight: 600;
  text-decoration: none;
  white-space: nowrap;
  transition: border-color 0.15s ease, background-color 0.15s ease;
}

.dh-btn-secondary:hover {
  background: #1e1e1e;
  border-color: #f2f2f2;
}

.dh-btn-ghost {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 44px;
  padding: 0 16px;
  border-radius: 2px;
  background: transparent;
  border: 1px solid #2a2a2a;
  color: #9a9a9a;
  font-size: 14px;
  font-weight: 500;
  text-decoration: none;
  white-space: nowrap;
  transition: color 0.15s ease, border-color 0.15s ease;
}

.dh-btn-ghost:hover {
  color: #f2f2f2;
  border-color: #4a4a4a;
}

/* 右下极简切页导轨：无胶囊，2px 锋利进度条 */
.dh-hero-rail {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 220px;
  flex-shrink: 0;
}

.dh-rail-item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  min-height: 36px;
  padding: 6px 10px 8px;
  background: rgba(23, 23, 23, 0.72);
  border: 1px solid #242424;
  border-radius: 2px;
  color: #9a9a9a;
  font-family: inherit;
  font-size: 13px;
  text-align: left;
  cursor: pointer;
  transition: color 0.15s ease, border-color 0.15s ease, background-color 0.15s ease;
}

.dh-rail-item:hover {
  color: #f2f2f2;
  border-color: #3a3a3a;
}

.dh-rail-item.is-active {
  color: #f2f2f2;
  background: #171717;
  border-color: #363636;
}

.dh-rail-idx {
  font-family: var(--dh-font-mono);
  font-size: 11px;
  font-weight: 700;
  color: #6e6e6e;
  font-variant-numeric: tabular-nums;
}

.dh-rail-item.is-active .dh-rail-idx {
  color: #c3f53c;
}

.dh-rail-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 600;
}

.dh-rail-bar {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 2px;
  background: transparent;
}

.dh-rail-item.is-active .dh-rail-bar {
  background: #c3f53c;
}

@media (max-width: 1023px) {
  .dh-hero-rail {
    display: none;
  }
}

@media (max-width: 767px) {
  .dh-hero-stage {
    min-height: 360px;
  }

  .dh-hero-scrim {
    background:
      linear-gradient(180deg, rgba(17, 17, 17, 0.25) 0%, rgba(17, 17, 17, 0.78) 62%, #111111 100%);
  }

  .dh-hero-inner {
    max-width: 100%;
    padding: 132px 16px 24px;
  }

  .dh-hero-title {
    font-size: 26px;
  }

  .dh-hero-overview {
    font-size: 14px;
    margin-top: 10px;
  }

  .dh-hero-actions {
    margin-top: 18px;
    width: 100%;
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
  }

  .dh-btn-cta {
    grid-column: 1 / -1;
  }

  .dh-btn-secondary,
  .dh-btn-ghost {
    padding: 0 12px;
    font-size: 13px;
  }
}
</style>
