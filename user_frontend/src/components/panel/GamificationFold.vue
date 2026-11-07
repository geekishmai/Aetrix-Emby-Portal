<template>
  <section id="disc-camp" class="p2-camp-band" aria-label="探险营地与互动">
    <div class="p2-camp-shell">
      <button
        type="button"
        class="p2-camp-toggle"
        :aria-expanded="expanded"
        aria-controls="p2-camp-panel"
        @click="toggleExpanded"
      >
        <div class="p2-camp-head-left">
          <span class="p2-camp-title">探险营地与互动</span>
          <span class="p2-camp-sub">
            能量 · 体力 · 每日一封 · 抽一签 · 观影碑 · 到此一游 · 收藏家 · 世界
          </span>
        </div>
        <div class="p2-camp-head-right">
          <span v-if="points !== null" class="p2-camp-pill">能量 {{ points.toLocaleString('zh-CN') }}</span>
          <span v-if="vitality !== null && vitalityMax !== null" class="p2-camp-pill">
            体力 {{ vitality }}/{{ vitalityMax }}
          </span>
          <span v-if="streak !== null && streak > 0" class="p2-camp-pill">连签 {{ streak }} 天</span>
          <span class="p2-camp-cta">
            <span>{{ expanded ? '收起' : '展开' }}</span>
            <ChevronDown :size="16" class="p2-camp-chev" :class="{ 'is-open': expanded }" aria-hidden="true" />
          </span>
        </div>
      </button>

      <div v-if="expanded" id="p2-camp-panel" class="p2-camp-body">
        <div class="p2-camp-shortcuts">
          <RouterLink to="/collector" class="p2-shortcut-card">
            <span class="p2-sc-name">收藏家</span>
            <span class="p2-sc-desc">观影成就与口味画像</span>
          </RouterLink>
          <RouterLink to="/game/checkin" class="p2-shortcut-card">
            <span class="p2-sc-name">每日报到</span>
            <span class="p2-sc-desc">领取能量与恢复体力</span>
          </RouterLink>
          <RouterLink to="/game/ledger" class="p2-shortcut-card">
            <span class="p2-sc-name">能量账本</span>
            <span class="p2-sc-desc">查看能量收支明细</span>
          </RouterLink>
          <button type="button" class="p2-shortcut-card as-btn" @click="goWorld">
            <span class="p2-sc-name">探险世界</span>
            <span class="p2-sc-desc">打开全屏互动地图</span>
          </button>
        </div>

        <div class="p2-camp-grid">
          <div class="p2-camp-col">
            <!-- 每日一封 -->
            <section id="disc-letter" class="p2-camp-card" aria-label="每日一封">
              <div class="p2-sec-head">
                <h3 class="p2-sec-title">每日一封</h3>
                <p class="p2-sec-sub">昨天的观影回顾与今日来信</p>
              </div>
              <div v-if="letter.loading" class="v4-skeleton" aria-hidden="true" style="height:112px"></div>
              <div v-else-if="letter.error" class="v4-err">
                信件加载失败：{{ letter.error }}
                <button type="button" class="v4-retry" @click="() => panel.loadLetter(true)">重试</button>
              </div>
              <WaxLetter v-else-if="hasLetterData" :letter="letterData" />
              <div v-else class="v4-empty">还没有昨日来信</div>
            </section>

            <!-- 今日封存盲盒 -->
            <section id="disc-shelf" class="p2-camp-card" aria-label="今日封存">
              <div class="p2-sec-head">
                <h3 class="p2-sec-title">今日封存</h3>
                <p class="p2-sec-sub">新入库盲盒，点一下拆封看看</p>
              </div>
              <div v-if="parcels.loading" class="v4-skel-row">
                <div class="v4-skeleton" aria-hidden="true" style="width:124px;height:196px;flex:0 0 auto"></div>
                <div class="v4-skeleton" aria-hidden="true" style="width:124px;height:196px;flex:0 0 auto"></div>
              </div>
              <div v-else-if="parcels.error" class="v4-err">
                封存加载失败：{{ parcels.error }}
                <button type="button" class="v4-retry" @click="() => panel.loadParcels(true)">重试</button>
              </div>
              <div v-else-if="parcelList.length === 0" class="v4-empty">今天暂时没有新封存盲盒</div>
              <div v-else class="p2-parcels-row">
                <ParcelBox
                  v-for="p in parcelList"
                  :key="String(p.id)"
                  :parcel="p"
                  :busy="unsealingIds.has(String(p.id))"
                  variant="cover"
                  @unseal="onUnseal"
                  @play="openParcelPicker(p)"
                />
              </div>
            </section>
          </div>

          <!-- 观影碑 / 服主告示牌 / 每日抽一签 / 到此一游 / 世界入口 -->
          <div class="p2-camp-col">
            <DiscoveryRightRail />
          </div>
        </div>
      </div>
    </div>

    <PlayerSchemeSheet v-model:open="parcelPickerOpen" :title="parcelPickerTitle" />
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { ChevronDown } from 'lucide-vue-next'
import { usePanelStore } from '@/stores/panel'
import { useUserStore } from '@/stores/user'
import { checkinApi, vitalityApi } from '@/api/economy'
import { useHomeViewMode } from '@/composables/useHomeViewMode'
import { useToast } from '@/composables/useToast'
import { humanizeError, type ParcelItem, type ParcelsData } from '@/composables/usePanelApi'
import WaxLetter from '@/components/panel/WaxLetter.vue'
import ParcelBox from '@/components/panel/ParcelBox.vue'
import DiscoveryRightRail from '@/components/panel/DiscoveryRightRail.vue'
import PlayerSchemeSheet from '@/components/panel/PlayerSchemeSheet.vue'

const FOLD_KEY = 'aetrix:camp-fold:v1'

const panel = usePanelStore()
const user = useUserStore()
const route = useRoute()
const router = useRouter()
const { isDesktop, setMode } = useHomeViewMode()
const { parcels, letter } = storeToRefs(panel)

const expanded = ref(false)
const points = ref<number | null>(null)
const streak = ref<number | null>(null)
const vitality = ref<number | null>(null)
const vitalityMax = ref<number | null>(null)

const unsealingIds = ref(new Set<string>())
const parcelPickerOpen = ref(false)
const parcelPickerTitle = ref('')

const letterData = computed(() => {
  const d = letter.value.data
  return d !== null && typeof d === 'object' && !Array.isArray(d)
    ? (d as Record<string, unknown>)
    : {}
})
const hasLetterData = computed(() => Object.keys(letterData.value).length > 0)

const parcelList = computed((): ParcelItem[] => {
  const d = parcels.value.data as ParcelsData | null
  return Array.isArray(d?.parcels) ? d.parcels : []
})

function toggleExpanded() {
  expanded.value = !expanded.value
  try {
    localStorage.setItem(FOLD_KEY, expanded.value ? '1' : '0')
  } catch {
    /* ignore */
  }
  if (expanded.value) {
    void panel.loadHome()
  }
}

function goWorld() {
  if (!isDesktop.value) {
    void router.push('/game')
    return
  }
  setMode('world')
}

function openParcelPicker(p: ParcelItem) {
  parcelPickerTitle.value = p.name || '神秘包裹'
  parcelPickerOpen.value = true
}

async function onUnseal(id: string | number) {
  const key = String(id)
  if (unsealingIds.value.has(key)) return
  unsealingIds.value.add(key)
  try {
    await panel.unseal(id)
  } catch (e) {
    useToast().errorWithRetry(`拆封失败：${humanizeError(e)}`, () => void onUnseal(id))
  } finally {
    unsealingIds.value.delete(key)
  }
}

onMounted(() => {
  try {
    if (
      localStorage.getItem(FOLD_KEY) === '1' ||
      ['#disc-camp', '#disc-letter', '#disc-world', '#disc-shelf'].includes(route.hash)
    ) {
      expanded.value = true
      void panel.loadHome()
    }
  } catch {
    /* ignore */
  }

  if (!user.isLoggedIn) return
  checkinApi
    .status()
    .then((st) => {
      if (typeof st.points === 'number') points.value = st.points
      if (typeof st.streak === 'number') streak.value = st.streak
    })
    .catch(() => {})
  vitalityApi
    .status()
    .then((st) => {
      if (typeof st.vitality === 'number') vitality.value = st.vitality
      if (typeof st.max === 'number') vitalityMax.value = st.max
    })
    .catch(() => {})
})
</script>

<style scoped>
.p2-camp-band {
  max-width: min(1480px, 94vw);
  margin: 16px auto 0;
  padding: 0 20px;
}

.p2-camp-shell {
  background: #fdfbfa;
  border: 1px solid #d1d1cd;
  border-radius: 16px;
  overflow: hidden;
}

.p2-camp-toggle {
  width: 100%;
  min-height: 58px;
  padding: 16px 22px;
  border: 0;
  background: transparent;
  color: #27251e;
  font-family: inherit;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  text-align: left;
  cursor: pointer;
}

.p2-camp-toggle:hover {
  opacity: 0.92;
}

.p2-camp-head-left {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.p2-camp-title {
  font-size: 16px;
  font-weight: 500;
  color: #27251e;
}

.p2-camp-sub {
  font-size: 14px;
  color: #524b42;
}

.p2-camp-head-right {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
  flex-shrink: 0;
}

.p2-camp-pill {
  padding: 4px 10px;
  border-radius: 9999px;
  border: 1px solid #d1d1cd;
  background: #faf8f5;
  color: #27251e;
  font-size: 12px;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
}

.p2-camp-cta {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 5px 12px;
  border-radius: 6px;
  border: 1px solid #d1d1cd;
  color: #016a71;
  font-size: 14px;
  font-weight: 500;
}

.p2-camp-chev {
  transition: transform 0.18s ease;
}

.p2-camp-chev.is-open {
  transform: rotate(180deg);
}

.p2-camp-body {
  padding: 18px 22px 22px;
  border-top: 1px solid #d1d1cd;
  background: #faf8f5;
}

.p2-camp-shortcuts {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}

.p2-shortcut-card {
  padding: 12px 14px;
  border-radius: 12px;
  border: 1px solid #d1d1cd;
  background: #fdfbfa;
  color: #27251e;
  text-decoration: none;
  font-family: inherit;
  text-align: left;
  display: flex;
  flex-direction: column;
  gap: 3px;
  cursor: pointer;
  transition: transform 0.16s ease;
}

.p2-shortcut-card:hover {
  transform: translateY(-2px);
}

.p2-sc-name {
  font-size: 15px;
  font-weight: 500;
  color: #27251e;
}

.p2-sc-desc {
  font-size: 13px;
  color: #524b42;
}

.p2-camp-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  align-items: start;
}

.p2-camp-col {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}

.p2-camp-card {
  padding: 18px 20px;
  border-radius: 16px;
  border: 1px solid #d1d1cd;
  background: #fdfbfa;
}

.p2-sec-head {
  margin-bottom: 12px;
}

.p2-sec-title {
  margin: 0;
  font-size: 16px;
  font-weight: 500;
  color: #27251e;
}

.p2-sec-sub {
  margin: 2px 0 0;
  font-size: 14px;
  color: #524b42;
}

.p2-parcels-row {
  display: flex;
  gap: 12px;
  overflow-x: auto;
  padding-bottom: 4px;
}

@media (max-width: 1023px) {
  .p2-camp-shortcuts {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .p2-camp-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 767px) {
  .p2-camp-band {
    max-width: 100%;
    padding: 0 16px;
    margin-top: 12px;
  }

  .p2-camp-toggle {
    padding: 14px 16px;
    flex-direction: column;
    align-items: flex-start;
  }

  .p2-camp-head-right {
    width: 100%;
    justify-content: space-between;
  }

  .p2-camp-body {
    padding: 14px 16px 18px;
  }
}
</style>
