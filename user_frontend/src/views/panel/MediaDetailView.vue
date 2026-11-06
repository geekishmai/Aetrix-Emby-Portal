<template>
  <div class="v4-scope">
    <main class="v4-page v4-page-wide md-page">
      <div class="md-topbar">
        <button type="button" class="md-back-btn" @click="goBack">
          <ArrowLeft :size="16" aria-hidden="true" />
          <span>返回片库</span>
        </button>
      </div>

      <div v-if="loading" class="md-hero-card">
        <div class="md-hero-grid">
          <div class="v4-skeleton md-skel-poster"></div>
          <div class="md-skel-body">
            <div class="v4-skeleton" style="height:32px;width:55%"></div>
            <div class="v4-skeleton" style="height:18px;width:35%;margin-top:12px"></div>
            <div class="v4-skeleton" style="height:96px;width:100%;margin-top:20px"></div>
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
          <RouterLink to="/shop" class="md-ghost-link">前往补给站</RouterLink>
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
        <article class="md-hero-card">
          <div v-if="backdropSrc && !backdropBroken" class="md-backdrop">
            <img
              :src="backdropSrc"
              :alt="title"
              decoding="async"
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
                  <span>{{ title.slice(0, 2) }}</span>
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
                <h1 class="md-title" :class="{ 'has-logo': logoSrc && !logoBroken }">{{ title }}</h1>
                <p v-if="subOriginalTitle" class="md-orig-title">{{ subOriginalTitle }}</p>
              </div>

              <div class="md-meta-pills">
                <span class="md-pill">{{ item.Type === 'Series' ? '剧集' : '电影' }}</span>
                <span v-if="item.ProductionYear" class="md-pill">{{ item.ProductionYear }}</span>
                <span v-if="ratingText" class="md-pill md-pill-teal">★ {{ ratingText }}</span>
                <span v-if="runtimeText" class="md-pill">{{ runtimeText }}</span>
                <span v-if="resBadge" class="md-pill">{{ resBadge }}</span>
                <span v-if="item.OfficialRating" class="md-pill">{{ item.OfficialRating }}</span>
              </div>

              <div v-if="item.Genres?.length" class="md-genres">
                <span v-for="g in item.Genres" :key="g" class="md-genre-chip">{{ g }}</span>
              </div>

              <div class="md-actions">
                <button type="button" class="md-btn-primary" @click="pickerOpen = true">
                  <Play :size="17" aria-hidden="true" />
                  <span>在播放器中打开</span>
                </button>
                <button
                  type="button"
                  class="md-btn-ghost"
                  :disabled="favBusy"
                  @click="toggleFav"
                >
                  <Heart :size="16" :fill="isFav ? '#016a71' : 'none'" aria-hidden="true" />
                  <span>{{ isFav ? '已收藏' : '加入收藏' }}</span>
                </button>
              </div>

              <div class="md-overview-block">
                <h2 class="md-sec-h">剧情简介</h2>
                <p class="md-overview">
                  {{ item.Overview?.trim() || '暂无剧情简介，可直接在客户端播放器中点播观看。' }}
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

        <section v-if="seasons.length > 0" class="md-section-card" aria-label="季列表">
          <h2 class="md-sec-h">包含季数（{{ seasons.length }}）</h2>
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

        <section v-if="castList.length > 0" class="md-section-card" aria-label="演职员">
          <h2 class="md-sec-h">主要演职员</h2>
          <div class="md-cast-grid">
            <div v-for="p in castList" :key="`${p.Name}-${p.Role || ''}`" class="md-cast-chip">
              <span class="md-cast-name">{{ p.Name }}</span>
              <span v-if="p.Role" class="md-cast-role">饰 {{ p.Role }}</span>
            </div>
          </div>
        </section>
      </template>

      <PlayerSchemeSheet v-model:open="pickerOpen" :title="title" />
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { ArrowLeft, Heart, Play } from 'lucide-vue-next'
import {
  backdropUrl,
  embyApi,
  ensureEmbyBase,
  formatDuration,
  httpStatusOf,
  imageUrl,
  isEmbyAccessNotGranted,
  posterUrl,
  ticksToSeconds,
  type EmbyItem,
} from '@/api/emby'
import PlayerSchemeSheet from '@/components/panel/PlayerSchemeSheet.vue'
import { cleanOriginalSubtitle, displayMediaTitle, resolutionBadge } from '@/utils/mediaTitle'
import { humanizeError } from '@/composables/usePanelApi'
import { useToast } from '@/composables/useToast'

interface PersonEntry {
  Name: string
  Role?: string
  Type?: string
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
const pickerOpen = ref(false)
const favBusy = ref(false)

const posterBroken = ref(false)
const backdropBroken = ref(false)
const logoBroken = ref(false)
const brokenSeasons = reactive(new Set<string>())

const itemId = computed(() => String(route.params.id || ''))
const title = computed(() => displayMediaTitle(item.value))
const subOriginalTitle = computed(() =>
  cleanOriginalSubtitle(item.value?.OriginalTitle, title.value),
)

const posterSrc = computed(() => (item.value ? posterUrl(item.value, 520) : ''))
const backdropSrc = computed(() => (item.value ? backdropUrl(item.value, 1400) : ''))
const logoSrc = computed(() => {
  if (!item.value?.ImageTags?.Logo) return ''
  return imageUrl(item.value.Id, 'Logo', 480)
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

const castList = computed(() => {
  const raw = item.value?.People
  if (!Array.isArray(raw)) return []
  return raw.filter((p) => p?.Name).slice(0, 14)
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

function seasonPoster(s: EmbyItem): string {
  return posterUrl(s, 280)
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

async function loadDetail() {
  if (!itemId.value) return
  loading.value = true
  error.value = ''
  notGranted.value = false
  backdropBroken.value = false
  logoBroken.value = false
  seasons.value = []
  try {
    await ensureEmbyBase()
    const fetched = (await embyApi.getItem(itemId.value)) as FullEmbyItem
    item.value = fetched
    document.title = `${displayMediaTitle(fetched)} - 影片详情`
    if (fetched.Type === 'Series') {
      try {
        seasons.value = await embyApi.getSeasons(fetched.Id)
      } catch {
        seasons.value = []
      }
    }
  } catch (err: unknown) {
    const status = httpStatusOf(err)
    if (isEmbyAccessNotGranted(err) || status === 401 || status === 403) {
      notGranted.value = true
    } else {
      error.value = humanizeError(err)
    }
  } finally {
    loading.value = false
  }
}

watch(itemId, () => {
  void loadDetail()
})

onMounted(() => {
  void loadDetail()
})
</script>

<style scoped>
.md-page {
  padding: 20px 24px 64px;
}

.md-topbar {
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

.md-hero-card {
  position: relative;
  background: #1a1714;
  border: 1px solid #2f2a24;
  border-radius: 16px;
  overflow: hidden;
  color: #f6f1e7;
}

.md-backdrop {
  position: absolute;
  inset: 0;
  overflow: hidden;
  background: #1a1714;
  pointer-events: none;
}

.md-backdrop img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  opacity: 0.52;
  display: block;
}

.md-backdrop-scrim {
  position: absolute;
  inset: 0;
  background:
    linear-gradient(90deg, rgba(22, 19, 16, 0.94) 0%, rgba(22, 19, 16, 0.80) 50%, rgba(22, 19, 16, 0.58) 100%),
    linear-gradient(180deg, rgba(22, 19, 16, 0.36) 0%, rgba(22, 19, 16, 0.94) 100%);
}

.md-hero-grid {
  position: relative;
  z-index: 1;
  display: grid;
  grid-template-columns: 240px minmax(0, 1fr);
  gap: 28px;
  padding: 32px;
  align-items: start;
}

.md-poster-frame {
  width: 100%;
  aspect-ratio: 2 / 3;
  border-radius: 14px;
  overflow: hidden;
  background: #2b2620;
  border: 1px solid rgba(255, 253, 248, 0.18);
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
  font-size: 26px;
  font-weight: 500;
}

.md-logo-img {
  max-height: 76px;
  max-width: min(360px, 100%);
  width: auto;
  object-fit: contain;
  margin-bottom: 12px;
  display: block;
  filter: drop-shadow(0 2px 12px rgba(0, 0, 0, 0.62));
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
  font-size: 22px;
  color: #ebe4d8;
}

.md-orig-title {
  margin: 6px 0 0;
  font-size: 15px;
  color: #c9c0b2;
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
  border: 1px solid rgba(255, 253, 248, 0.2);
  background: rgba(255, 253, 248, 0.08);
  color: #f4efe6;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}

.md-pill-teal {
  border-color: #36b3ba;
  background: rgba(1, 106, 113, 0.42);
  color: #c4f5f8;
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
  color: #dcd4c6;
  background: rgba(255, 253, 248, 0.1);
  padding: 3px 10px;
  border-radius: 9999px;
}

.md-actions {
  margin-top: 20px;
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}

.md-btn-primary {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: 44px;
  padding: 0 22px;
  border-radius: 12px;
  border: none;
  background: #018088;
  color: #ffffff;
  font-size: 15px;
  font-weight: 500;
  font-family: inherit;
  cursor: pointer;
}

.md-btn-primary:hover {
  background: #01929b;
}

.md-btn-ghost {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: 44px;
  padding: 0 18px;
  border-radius: 12px;
  border: 1px solid rgba(255, 253, 248, 0.24);
  background: rgba(255, 253, 248, 0.08);
  color: #faf8f5;
  font-size: 14px;
  font-weight: 500;
  font-family: inherit;
  cursor: pointer;
}

.md-overview-block {
  margin-top: 24px;
  padding-top: 18px;
  border-top: 1px solid rgba(255, 253, 248, 0.14);
}

.md-hero-card .md-sec-h {
  color: #faf8f5;
}

.md-sec-h {
  margin: 0 0 10px;
  font-size: 16px;
  font-weight: 500;
  color: #27251e;
}

.md-overview {
  margin: 0;
  max-width: 72ch;
  font-size: 15px;
  line-height: 1.7;
  color: #e6dfd2;
  white-space: pre-line;
}

.md-specs {
  margin: 20px 0 0;
  padding-top: 16px;
  border-top: 1px solid rgba(255, 253, 248, 0.14);
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px 20px;
}

.md-spec-item dt {
  font-size: 13px;
  color: #bfb5a5;
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

.md-season-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(136px, 1fr));
  gap: 16px;
  margin-top: 12px;
}

.md-season-poster {
  aspect-ratio: 2 / 3;
  border-radius: 10px;
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

.md-cast-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 12px;
}

.md-cast-chip {
  padding: 8px 14px;
  border-radius: 12px;
  border: 1px solid #d1d1cd;
  background: #faf8f5;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.md-cast-name {
  font-size: 14px;
  font-weight: 500;
  color: #27251e;
}

.md-cast-role {
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
  .md-backdrop {
    height: 180px;
  }
  .md-hero-grid {
    grid-template-columns: 1fr;
    gap: 18px;
    padding: 18px;
  }
  .md-poster-col {
    width: 156px;
  }
  .md-backdrop + .md-hero-grid {
    margin-top: -72px;
  }
  .md-title {
    font-size: 22px;
  }
}
</style>
