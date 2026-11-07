<!-- 桌面发现页底部 · 媒体库速览带（C档第2批）
     数据源：/api/panel/server 的 eco（与服务器页片库生态同口径）——
     真实库名 + 真实条数（movie/series 口径）。eco 只含非空库；
     有条数的库卡链到服务器页（已有页面），0 条的库不设“去看看”假按钮，只写暂无内容。 -->
<template>
  <section v-if="libs.length > 0" id="disc-libs" class="d3-band" aria-label="媒体库速览">
    <div class="d3-sec-head">
      <div>
        <div class="d3-sec-title">媒体库速览</div>
        <div class="d3-sec-sub">{{ libs.length }} 个库 · 全部直挂可播</div>
      </div>
      <RouterLink to="/library" class="d3-sec-link">进入片库 <span aria-hidden="true">→</span></RouterLink>
    </div>
    <div class="d3-lib-grid">
      <template v-for="lib in libs" :key="lib.library_id">
        <RouterLink
          v-if="lib.count > 0"
          :to="{ path: '/library', query: { name: lib.name } }"
          class="d3-card d3-lib-card d3-hover"
        >
          <div class="ln">{{ lib.name }}</div>
          <div class="lc">{{ lib.count.toLocaleString('zh-CN') }} 部 · 点击浏览海报墙</div>
        </RouterLink>
        <div v-else class="d3-card d3-lib-card">
          <div class="ln">{{ lib.name }}</div>
          <div class="lc">暂无内容</div>
        </div>
      </template>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { storeToRefs } from 'pinia'
import { RouterLink } from 'vue-router'
import { usePanelStore } from '@/stores/panel'
import { useUserStore } from '@/stores/user'
import type { EcoItem } from '@/composables/usePanelApi'

const panel = usePanelStore()
const user = useUserStore()
const { server } = storeToRefs(panel)

const libs = computed((): EcoItem[] => {
  const d = (server.value.data || {}) as Record<string, unknown>
  const eco = d.eco as EcoItem[] | undefined
  return Array.isArray(eco) ? eco : []
})

onMounted(() => {
  if (user.isLoggedIn) void panel.loadServer()
})
</script>

<style scoped>
.d3-band {
  max-width: min(1720px, 96vw);
  margin: 28px auto 0;
  padding: 0 24px;
  box-sizing: border-box;
}
.d3-sec-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}
.d3-sec-title {
  font-size: 20px;
  font-weight: 800;
  letter-spacing: -0.01em;
  color: #f2f2f2;
}
.d3-sec-sub {
  font-size: 13px;
  color: #9a9a9a;
  margin-top: 2px;
  font-variant-numeric: tabular-nums;
}
.d3-sec-link {
  font-size: 13px;
  font-weight: 600;
  color: #9a9a9a;
  text-decoration: none;
}
.d3-sec-link:hover {
  color: #c3f53c;
}
.d3-lib-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 10px;
}
@media (max-width: 767px) {
  .d3-band {
    max-width: 100%;
    padding: 0 16px;
    margin-top: 22px;
  }
}
</style>
