<template>
  <div class="v4-eco">
    <div ref="plantsEl" class="plants">
      <div v-for="p in plants" :key="p.library_id" class="plant">
        <div class="ct">{{ fmtCount(p.count) }}</div>
        <div class="stem-track">
          <div class="stem" :style="{ height: p.height + 'px' }"></div>
        </div>
        <div class="nm" :title="p.name">{{ p.name }}</div>
      </div>
    </div>
    <p class="eco-foot">共 {{ plants.length }} 个分类库 · 累计 {{ totalCountText }} 部影视</p>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import type { EcoItem } from '@/composables/usePanelApi'

const props = defineProps<{ eco: EcoItem[] }>()

const totalCountText = computed(() => {
  const sum = (props.eco || []).reduce((acc, e) => acc + (Number(e.count) || 0), 0)
  return sum.toLocaleString('zh-CN')
})
// R41：脏数据（NaN / Infinity）不展示，兜底 '0'
function fmtCount(n: number): string {
  if (!Number.isFinite(n)) return '0'
  if (n >= 10000) {
    const w = n / 10000
    return w >= 100 ? `${Math.round(w)}万` : `${w.toFixed(1)}万`
  }
  return `${n}`
}

const MAX_H = 110

const plants = computed(() => {
  const list = (props.eco || []).slice(0, 12)
  const max = Math.max(1, ...list.map((e) => e.count || 0))
  return list.map((e) => ({
    ...e,
    height: Math.max(10, Math.round(((e.count || 0) / max) * MAX_H)),
  }))
})

const plantsEl = ref<HTMLElement | null>(null)
/** 内容超出可横滑时才显示右侧渐隐，避免无滚动时误导 */
function updateScrollHint() {
  const el = plantsEl.value
  if (!el) return
  el.classList.toggle('can-scroll', el.scrollWidth > el.clientWidth + 1)
}
onMounted(() => {
  updateScrollHint()
  window.addEventListener('resize', updateScrollHint)
})
onUnmounted(() => {
  window.removeEventListener('resize', updateScrollHint)
})
watch(plants, () => {
  requestAnimationFrame(updateScrollHint)
})
</script>

<style scoped>
/* 取自设计稿 .eco / .plants / .plant */
.v4-eco {
  background: var(--paper-card);
  border: 1px solid #d1d1cd;
  border-radius: 14px;
  padding: 16px;
}
.plants {
  display: flex;
  align-items: flex-end;
  justify-content: flex-start;
  justify-content: safe space-around;
  min-height: 142px;
  border-bottom: 1px solid #d1d1cd;
  padding-bottom: 10px;
  margin-bottom: 10px;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  gap: 8px;
}
.plants.can-scroll {
  mask-image: linear-gradient(90deg, #000 92%, transparent);
  -webkit-mask-image: linear-gradient(90deg, #000 92%, transparent);
}
.plant {
  text-align: center;
  min-width: 56px;
  flex: 1;
}
.stem-track {
  height: 112px;
  display: flex;
  align-items: flex-end;
  justify-content: center;
  margin-top: 6px;
}
.plant .stem {
  width: 18px;
  margin: 0 auto;
  border-radius: 6px 6px 2px 2px;
  background: #016a71;
}
.plant .nm {
  font-size: 11px;
  margin-top: 6px;
  color: var(--ink2);
  line-height: 1.4;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.plant .ct {
  font-size: 13px;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
}
.eco-foot {
  font-size: 11px;
  color: var(--ink2);
  text-align: center;
  margin: 8px 0 0;
}
</style>
