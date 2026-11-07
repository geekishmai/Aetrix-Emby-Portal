<template>
  <div class="v4-scope">
    <main class="v4-page v4-page-wide lib-wall">
      <header class="lib-head">
        <div class="lib-head-top">
          <div>
            <h1 class="lib-title">片库</h1>
            <p class="lib-sub">
              按分类浏览全部馆藏海报，支持片名/别名搜索与多维排序
              <span v-if="totalCount > 0" class="lib-count">· 当前共 {{ totalCount.toLocaleString('zh-CN') }} 部</span>
            </p>
          </div>
          <div class="lib-search-box">
            <Search :size="16" class="lib-search-ic" aria-hidden="true" />
            <input
              v-model="searchInput"
              type="search"
              class="lib-search-input"
              placeholder="搜中文片名、原名或演员…"
              aria-label="搜索片库"
              @keydown.enter.prevent="flushSearchNow"
            />
            <button
              v-if="searchInput"
              type="button"
              class="lib-search-clear"
              aria-label="清空搜索词"
              @click="clearSearch"
            >✕</button>
          </div>
        </div>

        <div class="lib-controls">
          <div class="lib-tabs" role="tablist" aria-label="媒体库分类">
            <button
              type="button"
              role="tab"
              class="lib-tab"
              :class="{ on: activeLibId === '' }"
              :aria-selected="activeLibId === ''"
              @click="selectLibrary('')"
            >
              全部
            </button>
            <button
              v-for="v in views"
              :key="v.Id"
              type="button"
              role="tab"
              class="lib-tab"
              :class="{ on: activeLibId === v.Id }"
              :aria-selected="activeLibId === v.Id"
              @click="selectLibrary(v.Id)"
            >
              <span>{{ v.Name }}</span>
              <span v-if="typeof v.ChildCount === 'number' && v.ChildCount > 0" class="lib-tab-cnt">
                {{ v.ChildCount }}
              </span>
            </button>
          </div>

          <div class="lib-sorts" role="group" aria-label="排序方式">
            <button
              v-for="opt in sortOptions"
              :key="opt.key"
              type="button"
              class="lib-sort-btn"
              :class="{ on: sortKey === opt.key }"
              :disabled="!!appliedSearch"
              :title="appliedSearch ? '搜索状态下按相关度排序' : opt.label"
              @click="selectSort(opt.key)"
            >
              {{ opt.label }}
            </button>
          </div>
        </div>
      </header>

      <div v-if="notGranted" class="lib-empty-card" role="status">
        <p class="lib-empty-title">尚未开启片库观影权限</p>
        <p class="lib-empty-desc">
          前往「播放器接入」或「我的」激活观影权限后，即可按分类浏览全部馆藏高清海报与影片详情。
        </p>
        <div style="margin-top:14px;display:flex;gap:10px;justify-content:center;flex-wrap:wrap">
          <RouterLink to="/game/operator" class="v4-retry" style="text-decoration:none">前往开启观影权限</RouterLink>
          <RouterLink v-if="branding.gamification_enabled !== false" to="/shop" class="lib-sort-btn" style="text-decoration:none">前往补给站</RouterLink>
        </div>
      </div>

      <div v-else-if="error" class="v4-err lib-err" role="alert">
        <span>片库加载失败：{{ error }}</span>
        <button type="button" class="v4-retry" @click="reloadItems">重试</button>
      </div>
      <div v-else-if="loading && items.length === 0" class="lib-grid" aria-hidden="true">
        <div v-for="n in 18" :key="n" class="lib-card-skel">
          <div class="v4-skeleton lib-skel-poster"></div>
          <div class="v4-skeleton lib-skel-title"></div>
          <div class="v4-skeleton lib-skel-meta"></div>
        </div>
      </div>

      <div v-else-if="items.length === 0" class="lib-empty-card">
        <p class="lib-empty-title">
          {{ appliedSearch ? `没有找到与「${appliedSearch}」相关的影片` : '该媒体库暂时还没有可展示的影片' }}
        </p>
        <p class="lib-empty-desc">
          {{ appliedSearch ? '可以换个译名、年份关键词，或切回「全部」分类再试。' : '新片入库刮削完成后会自动出现在海报墙里。' }}
        </p>
        <button v-if="appliedSearch || activeLibId" type="button" class="v4-retry" @click="resetFilters">
          查看全部馆藏
        </button>
      </div>

      <template v-else>
        <div class="lib-grid">
          <RouterLink
            v-for="item in items"
            :key="item.Id"
            :to="`/media/${encodeURIComponent(item.Id)}`"
            class="lib-card"
          >
            <div class="lib-poster-wrap">
              <img
                v-if="!brokenIds.has(item.Id) && posterOf(item)"
                :src="posterOf(item)"
                :alt="titleOf(item)"
                loading="lazy"
                decoding="async"
                @error="markBroken(item.Id)"
              />
              <div v-else class="lib-poster-placeholder">
                <span class="lib-ph-type">{{ item.Type === 'Series' ? '剧集' : '电影' }}</span>
                <span class="lib-ph-text">{{ titleOf(item) }}</span>
                <span class="lib-ph-foot">暂无海报</span>
              </div>
              <span v-if="resOf(item)" class="lib-badge-res">{{ resOf(item) }}</span>
              <div class="lib-poster-hover" aria-hidden="true">
                <div class="lib-hover-meta">
                  <span>{{ item.Type === 'Series' ? '剧集' : '电影' }}</span>
                  <span v-if="item.ProductionYear">{{ item.ProductionYear }}</span>
                  <span v-if="ratingOf(item)" class="lib-hover-score">★ {{ ratingOf(item) }}</span>
                </div>
                <div class="lib-hover-title">{{ titleOf(item) }}</div>
              </div>
            </div>
            <div class="lib-card-title" :title="titleOf(item)">{{ titleOf(item) }}</div>
            <div class="lib-card-meta">
              <span>{{ item.Type === 'Series' ? '剧集' : '电影' }}</span>
              <span v-if="item.ProductionYear">· {{ item.ProductionYear }}</span>
              <span v-if="ratingOf(item)">· ★ {{ ratingOf(item) }}</span>
            </div>
          </RouterLink>
        </div>

        <div class="lib-footer">
          <button
            v-if="hasMore"
            type="button"
            class="lib-more-btn"
            :disabled="loadingMore"
            @click="loadMore"
          >
            {{ loadingMore ? '正在加载更多…' : `加载更多（已显示 ${items.length} / ${totalCount}）` }}
          </button>
          <span v-else class="lib-end-note">已展示全部 {{ items.length }} 部条目</span>
        </div>
      </template>
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { Search } from 'lucide-vue-next'
import { embyApi, ensureEmbyBase, httpStatusOf, isEmbyAccessNotGranted, posterUrl, type EmbyItem } from '@/api/emby'
import { displayMediaTitle, resolutionBadge } from '@/utils/mediaTitle'
import { humanizeError } from '@/composables/usePanelApi'
import { useToast } from '@/composables/useToast'
import { branding } from '@/composables/useBranding'

type SortKey = 'latest' | 'rating' | 'year'

const sortOptions: Array<{ key: SortKey; label: string; sortBy: string; sortOrder: 'Ascending' | 'Descending' }> = [
  { key: 'latest', label: '最新入库', sortBy: 'DateCreated', sortOrder: 'Descending' },
  { key: 'rating', label: '最高评分', sortBy: 'CommunityRating', sortOrder: 'Descending' },
  { key: 'year', label: '上映年份', sortBy: 'ProductionYear', sortOrder: 'Descending' },
]

const PAGE_SIZE = 42

const route = useRoute()
const router = useRouter()
const toast = useToast()

const views = ref<EmbyItem[]>([])
const activeLibId = ref<string>(typeof route.query.lib === 'string' ? route.query.lib : '')
const sortKey = ref<SortKey>(
  route.query.sort === 'rating' || route.query.sort === 'year' ? route.query.sort : 'latest',
)
const searchInput = ref<string>(typeof route.query.q === 'string' ? route.query.q : '')
const appliedSearch = ref<string>(searchInput.value.trim())

const items = ref<EmbyItem[]>([])
const totalCount = ref(0)
const loading = ref(true)
const loadingMore = ref(false)
const error = ref('')
const notGranted = ref(false)
const brokenIds = reactive(new Set<string>())

let searchTimer: number | undefined
let activeController: AbortController | null = null

const hasMore = computed(() => items.value.length < totalCount.value)

function titleOf(item: EmbyItem): string {
  return displayMediaTitle(item)
}

function posterOf(item: EmbyItem): string {
  return posterUrl(item, 360)
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

function syncQueryUrl() {
  const q: Record<string, string> = {}
  if (activeLibId.value) q.lib = activeLibId.value
  if (sortKey.value !== 'latest') q.sort = sortKey.value
  if (appliedSearch.value) q.q = appliedSearch.value
  void router.replace({ path: '/library', query: q })
}

function currentSortSpec() {
  return sortOptions.find((s) => s.key === sortKey.value) ?? sortOptions[0]!
}

async function fetchPage(reset: boolean) {
  if (reset) {
    activeController?.abort()
    loading.value = true
    error.value = ''
    notGranted.value = false
  } else {
    if (loadingMore.value || loading.value) return
    loadingMore.value = true
  }

  const ctrl = new AbortController()
  if (reset) activeController = ctrl
  const spec = currentSortSpec()

  try {
    await ensureEmbyBase()
    const res = await embyApi.getItems({
      parentId: activeLibId.value || undefined,
      includeTypes: ['Movie', 'Series'],
      recursive: true,
      searchTerm: appliedSearch.value || undefined,
      sortBy: spec.sortBy,
      sortOrder: spec.sortOrder,
      startIndex: reset ? 0 : items.value.length,
      limit: PAGE_SIZE,
      signal: ctrl.signal,
    })
    if (ctrl.signal.aborted) return
    const nextBatch = Array.isArray(res?.Items) ? res.Items : []
    totalCount.value = typeof res?.TotalRecordCount === 'number' ? res.TotalRecordCount : nextBatch.length
    items.value = reset ? nextBatch : [...items.value, ...nextBatch]
  } catch (err: unknown) {
    if (ctrl.signal.aborted) return
    const status = httpStatusOf(err)
    if (isEmbyAccessNotGranted(err) || status === 401 || status === 403) {
      notGranted.value = true
      items.value = []
      totalCount.value = 0
      return
    }
    const msg = humanizeError(err)
    if (reset) {
      error.value = msg
    } else {
      toast.error(msg)
    }
  } finally {
    if (reset && activeController === ctrl) {
      loading.value = false
    }
    if (!reset) {
      loadingMore.value = false
    }
  }
}

function reloadItems() {
  void fetchPage(true)
}

function loadMore() {
  void fetchPage(false)
}

function selectLibrary(libId: string) {
  if (activeLibId.value === libId) return
  activeLibId.value = libId
  syncQueryUrl()
  void fetchPage(true)
}

function selectSort(key: SortKey) {
  if (sortKey.value === key) return
  sortKey.value = key
  syncQueryUrl()
  void fetchPage(true)
}

function flushSearchNow() {
  if (searchTimer) window.clearTimeout(searchTimer)
  const next = searchInput.value.trim()
  if (next === appliedSearch.value) return
  appliedSearch.value = next
  syncQueryUrl()
  void fetchPage(true)
}

function clearSearch() {
  searchInput.value = ''
  flushSearchNow()
}

function resetFilters() {
  activeLibId.value = ''
  searchInput.value = ''
  appliedSearch.value = ''
  sortKey.value = 'latest'
  syncQueryUrl()
  void fetchPage(true)
}

watch(searchInput, (val) => {
  if (searchTimer) window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(() => {
    const trimmed = val.trim()
    if (trimmed !== appliedSearch.value) {
      appliedSearch.value = trimmed
      syncQueryUrl()
      void fetchPage(true)
    }
  }, 280)
})

async function initPage() {
  try {
    await ensureEmbyBase()
    const rawViews = await embyApi.getViews()
    views.value = rawViews.filter((v) => v.CollectionType !== 'boxsets')
    // 支持从首页媒体库速览按库名 ?name=华语电影 跳转定位对应 Tab
    const wantedName = typeof route.query.name === 'string' ? route.query.name.trim() : ''
    if (!activeLibId.value && wantedName) {
      const matched = views.value.find((v) => v.Name === wantedName)
      if (matched) activeLibId.value = matched.Id
    }
  } catch {
    views.value = []
  }
  await fetchPage(true)
}

onMounted(() => {
  void initPage()
})

onUnmounted(() => {
  if (searchTimer) window.clearTimeout(searchTimer)
  activeController?.abort()
})
</script>

<style scoped>
.lib-wall {
  padding: 28px 24px 64px;
}

.lib-head {
  background: transparent;
  border: 0;
  border-bottom: 1px solid #222222;
  border-radius: 0;
  padding: 0 0 18px;
  margin-bottom: 22px;
}

.lib-head-top {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 20px;
  flex-wrap: wrap;
}

.lib-title {
  margin: 0;
  font-size: clamp(26px, 2.4vw, 34px);
  font-weight: 800;
  color: #f2f2f2;
  letter-spacing: -0.02em;
  line-height: 1.15;
}

.lib-sub {
  margin: 6px 0 0;
  font-size: 14px;
  color: #9a9a9a;
  line-height: 1.5;
}

.lib-count {
  font-variant-numeric: tabular-nums;
}

.lib-search-box {
  position: relative;
  display: flex;
  align-items: center;
  width: min(360px, 100%);
  flex-shrink: 0;
}

.lib-search-ic {
  position: absolute;
  left: 13px;
  color: #9a9a9a;
  pointer-events: none;
}

.lib-search-input {
  width: 100%;
  height: 42px;
  padding: 0 36px 0 38px;
  border-radius: 2px;
  border: 1px solid #2a2a2a;
  background: #171717;
  color: #f2f2f2;
  font-size: 14px;
  font-family: inherit;
  outline: none;
  transition: border-color 0.15s ease;
}

.lib-search-input::placeholder {
  color: #6e6e6e;
}

.lib-search-input:focus {
  border-color: #c3f53c;
}

.lib-search-clear {
  position: absolute;
  right: 8px;
  width: 28px;
  height: 28px;
  border: none;
  border-radius: 2px;
  background: transparent;
  color: #9a9a9a;
  font-size: 13px;
  cursor: pointer;
}

.lib-search-clear:hover {
  color: #f2f2f2;
}

.lib-controls {
  margin-top: 18px;
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.lib-tabs {
  display: flex;
  align-items: center;
  gap: 20px;
  overflow-x: auto;
  scrollbar-width: none;
  max-width: 100%;
}

.lib-tabs::-webkit-scrollbar {
  display: none;
}

.lib-tab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
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
  white-space: nowrap;
  cursor: pointer;
  transition: color 0.15s ease, border-color 0.15s ease;
}

.lib-tab:hover {
  color: #f2f2f2;
}

.lib-tab.on {
  color: #f2f2f2;
  border-bottom-color: #c3f53c;
  font-weight: 700;
}

.lib-tab-cnt {
  font-family: var(--dh-font-mono);
  font-size: 11px;
  color: #6e6e6e;
  font-variant-numeric: tabular-nums;
}

.lib-tab.on .lib-tab-cnt {
  color: #c3f53c;
}

.lib-sorts {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}

.lib-sort-btn {
  min-height: 32px;
  padding: 4px 12px;
  border-radius: 2px;
  border: 1px solid #2a2a2a;
  background: #171717;
  color: #9a9a9a;
  font-size: 13px;
  font-weight: 600;
  font-family: inherit;
  cursor: pointer;
  transition: color 0.15s ease, border-color 0.15s ease;
}

.lib-sort-btn:hover:not(:disabled) {
  color: #f2f2f2;
  border-color: #444444;
}

.lib-sort-btn.on {
  border-color: #c3f53c;
  color: #f2f2f2;
  background: #1e1e1e;
  font-weight: 700;
}

.lib-sort-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

/* 高密度无外框海报墙：手机 3 列，平板 5 列，1280+ 7 列，1600+ 8 列 */
.lib-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px 10px;
}

@media (min-width: 640px) {
  .lib-grid {
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 16px 12px;
  }
}

@media (min-width: 1024px) {
  .lib-grid {
    grid-template-columns: repeat(6, minmax(0, 1fr));
    gap: 18px 12px;
  }
}

@media (min-width: 1280px) {
  .lib-grid {
    grid-template-columns: repeat(7, minmax(0, 1fr));
    gap: 20px 12px;
  }
}

@media (min-width: 1600px) {
  .lib-grid {
    grid-template-columns: repeat(8, minmax(0, 1fr));
    gap: 22px 12px;
  }
}

.lib-card {
  display: flex;
  flex-direction: column;
  text-decoration: none;
  color: #f2f2f2;
  min-width: 0;
}

.lib-poster-wrap {
  position: relative;
  width: 100%;
  aspect-ratio: 2 / 3;
  border-radius: 2px;
  overflow: hidden;
  background: #171717;
  border: 1px solid #222222;
  transition: border-color 0.16s ease;
}

.lib-poster-wrap img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
  transition: transform 0.22s ease;
}

.lib-poster-placeholder {
  width: 100%;
  height: 100%;
  padding: 34px 12px 12px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  background: #171717;
  color: #f2f2f2;
}

.lib-ph-type {
  font-size: 11px;
  font-weight: 600;
  color: #9a9a9a;
}

.lib-ph-text {
  font-size: 14px;
  font-weight: 700;
  line-height: 1.35;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
  word-break: break-word;
}

.lib-ph-foot {
  font-size: 11px;
  color: #6e6e6e;
}

.lib-badge-res {
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

.lib-poster-hover {
  position: absolute;
  inset: auto 0 0 0;
  padding: 24px 10px 10px;
  background: linear-gradient(180deg, transparent 0%, rgba(17, 17, 17, 0.94) 46%, #111111 100%);
  opacity: 0;
  transform: translateY(4px);
  transition: opacity 0.16s ease, transform 0.16s ease;
  pointer-events: none;
}

.lib-hover-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: #9a9a9a;
  font-variant-numeric: tabular-nums;
}

.lib-hover-score {
  color: #c3f53c;
  font-weight: 700;
}

.lib-hover-title {
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
  .lib-card:hover .lib-poster-wrap {
    border-color: #c3f53c;
  }
  .lib-card:hover .lib-poster-wrap img {
    transform: scale(1.025);
  }
  .lib-card:hover .lib-poster-hover {
    opacity: 1;
    transform: translateY(0);
  }
}

.lib-card-title {
  margin-top: 7px;
  font-size: 13.5px;
  font-weight: 600;
  line-height: 1.35;
  color: #e6e6e6;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.lib-card-meta {
  margin-top: 2px;
  font-size: 12px;
  color: #9a9a9a;
  display: flex;
  gap: 4px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-variant-numeric: tabular-nums;
}

.lib-skel-poster {
  width: 100%;
  aspect-ratio: 2 / 3;
  border-radius: 2px;
}

.lib-skel-title {
  height: 14px;
  width: 82%;
  margin-top: 7px;
}

.lib-skel-meta {
  height: 12px;
  width: 54%;
  margin-top: 5px;
}

.lib-empty-card {
  background: #171717;
  border: 1px solid #2a2a2a;
  border-radius: 2px;
  padding: 48px 24px;
  text-align: center;
}

.lib-empty-title {
  margin: 0;
  font-size: 18px;
  font-weight: 700;
  color: #f2f2f2;
}

.lib-empty-desc {
  margin: 8px auto 16px;
  max-width: 44ch;
  font-size: 14px;
  color: #9a9a9a;
}

.lib-footer {
  margin-top: 32px;
  display: flex;
  justify-content: center;
}

.lib-more-btn {
  min-height: 44px;
  padding: 10px 28px;
  border-radius: 2px;
  border: 1px solid #2a2a2a;
  background: #171717;
  color: #f2f2f2;
  font-size: 14px;
  font-weight: 600;
  font-family: inherit;
  font-variant-numeric: tabular-nums;
  cursor: pointer;
  transition: border-color 0.15s ease, color 0.15s ease;
}

.lib-more-btn:hover:not(:disabled) {
  border-color: #c3f53c;
  color: #c3f53c;
}

.lib-end-note {
  font-size: 13px;
  color: #9a9a9a;
  font-variant-numeric: tabular-nums;
}

@media (max-width: 767px) {
  .lib-wall {
    padding: 16px 16px calc(84px + env(safe-area-inset-bottom));
  }
  .lib-head {
    padding-bottom: 14px;
    margin-bottom: 16px;
  }
  .lib-search-box {
    width: 100%;
  }
  .lib-card-title {
    font-size: 12.5px;
  }
  .lib-card-meta {
    font-size: 11px;
  }
}
</style>
