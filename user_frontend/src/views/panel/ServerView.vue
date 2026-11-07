<template>
  <div class="v4-scope">
    <div class="v4-page v4-server-page">
      <header class="v4-loghead">
        <h1>服务器</h1>
        <p>服务运行状态、后台调度任务、馆藏分类规模与近 7 日入库统计</p>
      </header>

      <!-- 顶层错误卡：失败时各区收起，只留这一张 -->
      <div v-if="server.error" class="v4-sec">
        <div class="v4-err" role="alert">{{ server.error }}<br><button class="v4-retry" :disabled="server.loading" @click="retryServer">{{ server.loading ? '正在重连…' : '重试' }}</button></div>
      </div>

      <div v-if="!server.error" class="v4-server-grid">
      <!-- 连接状态（可视化仍是 LifeOrb） -->
      <section class="v4-sec">
        <div class="v4-sec-title">服务运行状态</div>
        <div class="v4-sec-sub">流媒体网关连通性与后台处理负载</div>
        <div v-if="server.loading" class="v4-skeleton" style="height: 290px"></div>
        <LifeOrb v-else-if="data?.vitals" :vitals="data.vitals" />
        <div v-else class="v4-empty">暂未获取到服务器运行状态<br><span class="v4-empty-hint">可点击下方按钮重新检测节点响应</span><button class="v4-retry" @click="retryServer">重新检查</button></div>
      </section>

      <!-- 任务（可视化仍是 RadarCanvas） -->
      <section class="v4-sec">
        <div class="v4-sec-title">后台调度任务</div>
        <div class="v4-sec-sub">片库扫描、元数据识别与媒体探针状态</div>
        <div v-if="server.loading" class="v4-skeleton" style="height: 275px"></div>
        <RadarCanvas v-else-if="data?.radar_tasks?.length" :tasks="data.radar_tasks" />
        <div v-else class="v4-empty">当前暂无运行中的后台任务<br><span class="v4-empty-hint">扫描或整理任务启动后将在此实时显示</span><button class="v4-retry" @click="retryServer">重新检查</button></div>
      </section>

      <!-- 媒体库（可视化仍是 EcoGarden） -->
      <section class="v4-sec">
        <div class="v4-sec-title">馆藏分类分布</div>
        <div class="v4-sec-sub">各分类库当前可点播条目统计</div>
        <div v-if="server.loading" class="v4-skeleton" style="height: 190px"></div>
        <EcoGarden v-else-if="data?.eco?.length" :eco="data.eco" />
        <div v-else class="v4-empty">暂无已生效的媒体库统计<br><span class="v4-empty-hint">片库完成首次入库后将在此展示分类分布</span><button class="v4-retry" @click="retryServer">重新检查</button></div>
      </section>

      <!-- 入库趋势（可视化仍是 GrowthChart） -->
      <!-- R100-2：safe-area 只由 .v4-page 统一处理（76px + env），这里不再重复加 env，否则刘海机型底部多出一个安全区高度的空白 -->
      <section class="v4-sec" style="padding-bottom: 20px">
        <div class="v4-sec-title">入库趋势</div>
        <div class="v4-sec-sub">近 7 日新增上架影视数量</div>
        <div v-if="server.loading" class="v4-skeleton" style="height: 190px"></div>
        <GrowthChart v-else-if="data?.growth?.length" :growth="data.growth" />
        <div v-else class="v4-empty">近 7 日暂无新增入库记录<br><span class="v4-empty-hint">新片上架后将按日展示入库数量</span><button class="v4-retry" @click="retryServer">重新检查</button></div>
      </section>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { storeToRefs } from 'pinia'
import { usePanelStore } from '@/stores/panel'
import type { ServerData } from '@/composables/usePanelApi'
import LifeOrb from '@/components/panel/LifeOrb.vue'
import RadarCanvas from '@/components/panel/RadarCanvas.vue'
import EcoGarden from '@/components/panel/EcoGarden.vue'
import GrowthChart from '@/components/panel/GrowthChart.vue'

const panel = usePanelStore()
const { server } = storeToRefs(panel)

const data = computed(() => (server.value.data || null) as ServerData | null)

onMounted(() => {
  void panel.loadServer()
})

function retryServer() {
  void panel.loadServer(true)
}
</script>

<style scoped>
/* 生长记录区无额外样式，复用 v4-sec */
/* 空态第二行指引：小一号、弱化，告诉用户"正常还是故障" */
/* R100-1：硬编码冷灰 #6b7280 改走 --ink2（暖灰 #6B6257），与纸面色系统一 */
.v4-empty-hint {
  display: block;
  margin-top: 6px;
  font-size: 13px;
  color: var(--ink2);
}
</style>
