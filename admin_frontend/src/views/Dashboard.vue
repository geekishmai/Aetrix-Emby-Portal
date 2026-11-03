<script setup lang="ts">
/**
 * 仪表盘 — 按 2026-11-03 用户批准的效果图换新（只动本页）
 *
 * 图纸：~/workspace/reports/admin-dashboard-effect-20261103.html
 * 用户口径「不变画面，就变仪表盘」：版式/样式/配色/动效照图纸落地，只换真实数据；
 * 左侧导航与顶栏是 Layout 的全站框架（带 ⌘K 命令搜索），不在本页重做。
 *
 * 版式（与图纸逐格对应）：
 * - KPI 排 4 卡：注册用户 / 当前在播 / 媒体条目 / 今日新增（动态数字 + 迷你走势 + 聚焦光晕）
 * - 主区左：播放趋势双系列渐变面积图 + 分段控制器；右列：系统健康进度环 + 任务进度
 * - 底部行：待处理事项（可展开）+ 上游源状态
 * - 图纸以下保留原有信息区（服务接入 / 各服概况 / 额度驾驶舱 / 交易概览 /
 *   媒体服务 / 排行榜 / 数据明细），换成本页同一套卡片样式，不丢既有信息与深链。
 *
 * 数据口径（全部真实接口，不编造）：
 * - KPI/趋势/明细：沿用仪表盘聚合接口（/dashboard/overview）与回退逐个调用，
 *   趋势序列是现行 /stats/trend 的按日序列（7/14/30 天）；图纸的 24h 段无对应
 *   小时级接口，故分段为 7/14/30 天。双系列 = 播放次数（琥珀，主刻度）+
 *   新增用户（紫，独立刻度，图例已注明）。
 * - 「今日新增入库」没有现行接口（stats/overview、stats/trend、聚合 detail
 *   均无条目按日新增口径），第 4 卡改用同为今日口径的「今日新增用户」
 *   （trend 序列末日），不编造入库数；条目卡的迷你图是按库条目分布柱状，
 *   不是时间趋势（卡内脚注已注明口径）。
 * - 健康环：按后端服务心跳（healthy=1）与面板健康接口（healthy=1 /
 *   degraded=0.5 / unhealthy=0）等权折算的在线率，卡内注明折算口径。
 * - 任务进度：扫描 = 最近一轮成功库数/总库数；刮削 = 聚合 detail 的
 *   已刮/(已刮+待刮) 覆盖率；转存 = 队列 done/全部状态计数。
 * - 上游源状态：苹果 CMS 型源的体检状态（fetchUpstreamSources），右侧数字
 *   是「关联媒体库数」（按库的 upstream_source_ids 数），源接口本身不给
 *   按源条目数，不编造。
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import {
  Film, MessageSquareDashed, Ticket, Users, UserPlus, Wallet,
  Coins, CalendarCheck, TicketCheck, Gift, ArrowRight,
  Server, HardDrive, Play, AlertTriangle, Table2,
  Trophy, Flame, Puzzle, Gauge,
} from 'lucide-vue-next'
import { EmptyState } from '@/components/ui'
import {
  fetchLibraries, fetchOverview, fetchPlaybackStats, fetchRealmOverview,
  fetchServersSummary, fetchStatsTrend, fetchBackendServices, fetchDashboardAggregate, fetchQuotaDashboard,
  fetchPanelHealth, fetchUpstreamSources, fetchTransferJobs, fetchIdentifyReviewTasks,
  type BackendServiceStatus, type DashboardAggregate, type DashboardDetailTables, type QuotaDashboard,
  type PanelHealth, type UpstreamSource } from '@/api/admin'
import { fetchEconomyStats, type EconomyStats } from '@/api/economy'
import type {
  EmbyLibrary, OverviewStats, PlaybackStats, RealmOverview, ServerSummary, TrendStats,
} from '@/types'

const overview = ref<OverviewStats | null>(null)
const playback = ref<PlaybackStats | null>(null)
const detail = ref<DashboardDetailTables | null>(null)
const detailTab = ref('users')

function fmtTime(iso: string): string {
  if (!iso || iso.length < 16) return iso || ''
  return `${iso.slice(5, 10)} ${iso.slice(11, 16)}`
}
const economy = ref<EconomyStats | null>(null)
const trend = ref<TrendStats | null>(null)
const libraries = ref<EmbyLibrary[]>([])
const servers = ref<ServerSummary | null>(null)
const backendServices = ref<BackendServiceStatus[]>([])
const realms = ref<RealmOverview | null>(null)
const loading = ref(true)
const trendLoading = ref(false)
const failedParts = ref<string[]>([])

/** 本页新增软取数：失败只隐藏对应块，不进 failedParts 点名（与额度驾驶舱同一口径） */
const panelHealth = ref<PanelHealth | null>(null)
const upstreamSources = ref<UpstreamSource[]>([])
const transferCounts = ref<Record<string, number> | null>(null)
const identifyPending = ref<number | null>(null)

const days = ref(14)

async function loadTrend() {
  trendLoading.value = true
  try {
    trend.value = await fetchStatsTrend(days.value)
  } catch {
    trend.value = null
  } finally {
    trendLoading.value = false
    animateNums()
  }
}

async function loadAggregate(): Promise<boolean> {
  const d = days.value
  let agg: DashboardAggregate
  try {
    agg = await fetchDashboardAggregate(days.value)
  } catch {
    return false
  }
  const parts = agg?.parts
  if (!parts) return false
  const failed: string[] = []
  const take = <T,>(label: string, v: T | null): T | null => {
    if (v == null) failed.push(label)
    return v
  }
  overview.value = take('概览', parts.overview)
  playback.value = take('播放统计', parts.playback)
  economy.value = take('经营数据', parts.economy)
  if (days.value === d) trend.value = parts.trend
  libraries.value = parts.libraries?.libraries ?? []
  servers.value = parts.servers ?? null
  realms.value = parts.realms ?? null
  backendServices.value = parts.backend_services?.services ?? []
  detail.value = parts.detail ?? null
  failedParts.value = failed
  loading.value = false
  return true
}

async function loadAll() {
  loading.value = true
  if (await loadAggregate()) { animateNums(); return }
  const failed: string[] = []
  const soft = <T,>(label: string, p: Promise<T>) => p.catch(() => { failed.push(label); return null })
  try {
    const [o, p, e, libraryData, serverData, realmData, backendData] = await Promise.all([
      soft('概览', fetchOverview()),
      soft('播放统计', fetchPlaybackStats()),
      soft('经营数据', fetchEconomyStats()),
      fetchLibraries().catch(() => ({ libraries: [] as EmbyLibrary[] })),
      fetchServersSummary().catch(() => null),
      fetchRealmOverview().catch(() => null),
      fetchBackendServices().catch(() => ({ services: [] as BackendServiceStatus[] })),
      loadTrend(),
    ])
    overview.value = o
    playback.value = p
    economy.value = e
    libraries.value = libraryData.libraries
    servers.value = serverData
    realms.value = realmData
    backendServices.value = (backendData as { services?: BackendServiceStatus[] } | null)?.services || []
  } finally {
    failedParts.value = failed
    loading.value = false
    animateNums()
  }
}

/** 额度驾驶舱（只读）：开关关时后端 404，前端直接隐藏本区块 */
const quota = ref<QuotaDashboard | null>(null)
async function loadQuota() {
  try {
    quota.value = await fetchQuotaDashboard()
  } catch {
    quota.value = null
  }
}

/** 图纸新增块的软取数：健康面板 / 上游源 / 转存计数 / 识别审查待处理数 */
async function loadExtras() {
  const [h, u, t, ir] = await Promise.all([
    fetchPanelHealth().catch(() => null),
    fetchUpstreamSources().catch(() => null),
    fetchTransferJobs(1).catch(() => null),
    fetchIdentifyReviewTasks('pending', 1, 1).catch(() => null),
  ])
  panelHealth.value = h
  upstreamSources.value = u?.items ?? []
  transferCounts.value = t?.counts ?? null
  identifyPending.value = ir?.total ?? null
  animateNums()
}

function fmtBytes(v: number | null): string {
  if (v == null) return '未知'
  return `${(v / 1024 ** 3).toFixed(1)} GB`
}

onMounted(() => { loadAll(); loadQuota(); loadExtras() })
watch(days, loadTrend)

// ==================== 数字滚动动画（图纸：动态数字）====================

const disp = reactive<Record<string, number>>({})
const timers = new Map<string, number>()

function countTo(key: string, target: number) {
  const prev = timers.get(key)
  if (prev) window.clearInterval(prev)
  const t0 = performance.now()
  const dur = 1200
  const iv = window.setInterval(() => {
    const p = Math.min((performance.now() - t0) / dur, 1)
    const e = 1 - Math.pow(1 - p, 3)
    disp[key] = Math.round(target * e)
    if (p >= 1) { window.clearInterval(iv); timers.delete(key) }
  }, 16)
  timers.set(key, iv)
}

function fmtDisp(key: string, fallback = 0): string {
  return (disp[key] ?? fallback).toLocaleString('en-US')
}

onBeforeUnmount(() => { timers.forEach((iv) => window.clearInterval(iv)); timers.clear() })

// ==================== 服务器接入（信息展示）====================

const SERVER_ICONS: Record<string, unknown> = {
  ea: Server,
  emby: HardDrive,
  moviepilot: Server,
  qbittorrent: Server,
}

const serverTiles = computed(() => {
  const kinds = servers.value?.kinds || {}
  return Object.values(kinds).map((stat) => ({
    stat,
    icon: SERVER_ICONS[stat.kind] || Server,
  }))
})

// ==================== 扫描 / 存储摘要（沿用原口径）====================

const scanSummary = computed(() => {
  const list = libraries.value
  const statusOf = (l: EmbyLibrary) => (l.is_scanning ? 'running' : l.last_scan?.status || '')
  return {
    total: list.length,
    ok: list.filter((l) => statusOf(l) === 'success').length,
    bad: list.filter((l) => statusOf(l) === 'partial' || statusOf(l) === 'failed').length,
    scanning: list.filter((l) => statusOf(l) === 'running').length,
    never: list.filter((l) => !l.last_scan && !l.is_scanning).length,
  }
})

const mountSummary = computed(() => {
  const libs = libraries.value.filter((l) => !l.is_virtual)
  const local = libs.filter((l) => l.local_dirs_readable !== null)
  const failed = local.filter((l) => l.local_dirs_readable === false)
  return {
    total: libs.length,
    ok: local.filter((l) => l.local_dirs_readable === true).length,
    failed: failed.length,
    unchecked: libs.length - local.length,
    blocked: libs.filter((l) => (l.local_dirs_readable === false)
      && (l.path_entries?.length || 0) > 0).length,
    failedNames: failed.slice(0, 2).map((l) => l.name).join('、'),
    failedMore: failed.length > 2,
  }
})

// ==================== KPI（图纸 4 卡）====================

/** trend 序列末日 = 今日（后端按上海自然日补零到今天） */
const todayNewUsers = computed(() => {
  const s = trend.value?.series ?? []
  return s.length ? (s[s.length - 1].new_users ?? 0) : 0
})

/** 迷你走势线：把真实序列归一化进 86×26 的 polyline 点串 */
function sparkPoints(vals: number[]): string {
  if (!vals.length) return ''
  const max = Math.max(...vals, 1)
  const min = Math.min(...vals, 0)
  const span = Math.max(max - min, 1)
  const step = vals.length > 1 ? 86 / (vals.length - 1) : 0
  return vals.map((v, i) => `${(i * step).toFixed(1)},${(23 - ((v - min) / span) * 20).toFixed(1)}`).join(' ')
}

const sparkUsers = computed(() => sparkPoints((trend.value?.series ?? []).map((p) => p.new_users ?? 0)))
const sparkPlays = computed(() => sparkPoints((trend.value?.series ?? []).map((p) => p.plays ?? 0)))

/** 条目卡迷你图：按库条目量分布（柱状），不是时间趋势 */
const libraryBars = computed(() => {
  const rows = detail.value?.library_stats ?? []
  const max = Math.max(...rows.map((r) => r.total), 1)
  return rows.slice(0, 14).map((r) => ({
    name: r.library,
    h: Math.max(18, Math.round((r.total / max) * 100)),
  }))
})

const libraryKindTotals = computed(() => {
  const rows = detail.value?.library_stats ?? []
  return {
    movies: rows.reduce((s, r) => s + r.movies, 0),
    shows: rows.reduce((s, r) => s + r.shows, 0),
    episodes: rows.reduce((s, r) => s + r.episodes, 0),
  }
})

// ==================== 趋势图（图纸：双系列 + 分段控制器）====================

const series = computed(() => trend.value?.series ?? [])
const playsVals = computed(() => series.value.map((p) => Number(p.plays) || 0))
const usersVals = computed(() => series.value.map((p) => Number(p.new_users) || 0))

const chartNums = computed(() => {
  const vals = playsVals.value
  const total = trend.value?.totals.plays ?? vals.reduce((s, v) => s + v, 0)
  return {
    total,
    peak: vals.length ? Math.max(...vals) : 0,
    avg: vals.length ? Math.round(total / vals.length) : 0,
  }
})

const axisRange = computed(() => {
  const s = series.value
  if (!s.length) return ''
  return `${s[0].date.slice(5)} — ${s[s.length - 1].date.slice(5)}`
})

const CHART_W = 720
const CHART_H = 228

/** 图纸同款平滑曲线：Catmull-Rom 转三次贝塞尔 */
function smoothPath(pts: [number, number][]): string {
  if (!pts.length) return ''
  let d = `M ${pts[0][0]},${pts[0][1]}`
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[Math.max(i - 1, 0)]
    const p1 = pts[i]
    const p2 = pts[i + 1]
    const p3 = pts[Math.min(i + 2, pts.length - 1)]
    const c1x = p1[0] + (p2[0] - p0[0]) / 6
    const c1y = p1[1] + (p2[1] - p0[1]) / 6
    const c2x = p2[0] - (p3[0] - p1[0]) / 6
    const c2y = p2[1] - (p3[1] - p1[1]) / 6
    d += ` C ${c1x.toFixed(1)},${c1y.toFixed(1)} ${c2x.toFixed(1)},${c2y.toFixed(1)} ${p2[0]},${p2[1]}`
  }
  return d
}

function seriesPoints(vals: number[]): [number, number][] {
  const n = vals.length
  if (!n) return []
  const max = Math.max(...vals, 1) * 1.12
  const padB = 8
  const padT = 14
  return vals.map((v, i) => {
    const x = n > 1 ? (i / (n - 1)) * CHART_W : CHART_W / 2
    const y = CHART_H - padB - (v / max) * (CHART_H - padT - padB)
    return [x, y] as [number, number]
  })
}

const playsLine = computed(() => smoothPath(seriesPoints(playsVals.value)))
const usersLine = computed(() => smoothPath(seriesPoints(usersVals.value)))
const playsArea = computed(() => (playsLine.value ? `${playsLine.value} L ${CHART_W},${CHART_H} L 0,${CHART_H} Z` : ''))
const usersArea = computed(() => (usersLine.value ? `${usersLine.value} L ${CHART_W},${CHART_H} L 0,${CHART_H} Z` : ''))

const segOptions = [
  { value: 7, label: '7 天' },
  { value: 14, label: '14 天' },
  { value: 30, label: '30 天' },
]

// ==================== 系统健康（图纸：进度环 + 状态行）====================

const SERVICE_STATUS_LABEL: Record<string, string> = {
  healthy: '运行中',
  degraded: '降级',
  redis_unavailable: 'Redis 不可用',
  unreachable: '心跳丢失',
  unknown: '状态未知',
  no_heartbeat: '无心跳',
  stale: '心跳过期',
  error: '读取异常',
}
function serviceStatusLabel(status: string): string {
  return SERVICE_STATUS_LABEL[status] || status
}
const SERVICE_NAME_LABEL: Record<string, string> = {
  'aetrix-api': 'API 服务',
  'aetrix-worker': '后台工人',
}
function serviceNameLabel(name: string): string {
  return SERVICE_NAME_LABEL[name] || name
}

/** 健康分：服务心跳与面板健康等权折算（healthy=1 / degraded=0.5 / 其余=0） */
const healthScore = computed<number | null>(() => {
  const checks: number[] = backendServices.value.map((s) => (s.status === 'healthy' ? 1 : 0))
  const st = panelHealth.value?.status
  if (st) checks.push(st === 'healthy' ? 1 : st === 'degraded' ? 0.5 : 0)
  if (!checks.length) return null
  return Math.round((checks.reduce((s, v) => s + v, 0) / checks.length) * 100)
})

const RING_C = 301.6
const ringOffset = computed(() => {
  const score = disp['health'] ?? 0
  return (RING_C * (1 - score / 100)).toFixed(1)
})

type HealthRow = { dot: 'g' | 'a' | 'r'; label: string; value: string }
const healthRows = computed<HealthRow[]>(() => {
  const rows: HealthRow[] = backendServices.value.map((s) => ({
    dot: (s.status === 'healthy' ? 'g' : s.status === 'degraded' ? 'a' : 'r') as HealthRow['dot'],
    label: serviceNameLabel(s.name),
    value: `${serviceStatusLabel(s.status)}${s.lag_seconds != null ? ` · ${s.lag_seconds}s 前` : ''}`,
  }))
  const ph = panelHealth.value
  if (ph) {
    rows.push({
      dot: ph.status === 'healthy' ? 'g' : ph.status === 'degraded' ? 'a' : 'r',
      label: '面板健康',
      value: ph.status === 'healthy' ? '正常' : ph.status === 'degraded' ? '降级' : '异常',
    })
    rows.push({ dot: 'g', label: '在线用户', value: `${ph.online_users ?? 0} 人` })
    if (ph.emby_server) rows.push({ dot: 'g', label: '媒体服务', value: ph.emby_server })
  }
  return rows
})

// ==================== 任务进度（图纸：三条动态进度）====================

type TaskBar = { key: string; title: string; sub: string; pct: number | null; cls: string }
const taskBars = computed<TaskBar[]>(() => {
  const bars: TaskBar[] = []
  const scan = scanSummary.value
  bars.push({
    key: 'scan', title: '扫描任务', cls: 'f-amber',
    sub: scan.total
      ? `${scan.ok}/${scan.total} 个库最近一轮成功${scan.scanning ? ` · 扫描中 ${scan.scanning}` : ''}${scan.bad ? ` · 异常 ${scan.bad}` : ''}`
      : '暂无媒体库',
    pct: scan.total ? Math.round((scan.ok / scan.total) * 100) : null,
  })
  const ep = detail.value?.enrich_progress ?? []
  const enriched = ep.reduce((s, r) => s + r.enriched, 0)
  const pending = ep.reduce((s, r) => s + r.pending, 0)
  bars.push({
    key: 'enrich', title: '刮削补全', cls: 'f-violet',
    sub: ep.length ? `已刮 ${enriched.toLocaleString('en-US')} · 待刮 ${pending.toLocaleString('en-US')}` : '暂无数据',
    pct: enriched + pending > 0 ? Math.round((enriched / (enriched + pending)) * 100) : null,
  })
  const tc = transferCounts.value
  if (tc) {
    const total = Object.values(tc).reduce((s, v) => s + v, 0)
    const done = tc.done ?? 0
    bars.push({
      key: 'transfer', title: '转存队列', cls: 'f-cyan',
      sub: `待跑 ${tc.pending ?? 0} · 进行中 ${tc.running ?? 0} · 失败 ${tc.failed ?? 0}`,
      pct: total > 0 ? Math.round((done / total) * 100) : null,
    })
  }
  return bars
})

/** 进度条与百分比动画：数据到位后把目标值喂给 countTo / 宽度过渡 */
const barsShown = ref(false)
function animateNums() {
  if (overview.value) countTo('users', overview.value.users.total ?? 0)
  if (overview.value) countTo('playing', overview.value.emby.active_sessions ?? 0)
  if (overview.value) countTo('items', overview.value.emby.total_items ?? 0)
  if (trend.value) countTo('todayNew', todayNewUsers.value)
  if (healthScore.value != null) countTo('health', healthScore.value)
  for (const b of taskBars.value) {
    if (b.pct != null) countTo(`pct-${b.key}`, b.pct)
  }
  requestAnimationFrame(() => { barsShown.value = true })
}

// ==================== 待处理事项（图纸：展开卡）====================

type TodoItem = {
  key: string; icon: unknown; bg: string; title: string; cnt: string
  pill: string; pillCls: string; body: string; to: string; act: string
}
const todoItems = computed<TodoItem[]>(() => {
  const list: TodoItem[] = []
  const unhealthy = upstreamSources.value.filter((s) => s.enabled && s.health_status === 'unhealthy')
  if (unhealthy.length) {
    list.push({
      key: 'upstream', icon: AlertTriangle, bg: 'rgba(248,113,113,.14)',
      title: '上游源异常告警', cnt: `${unhealthy.length} 家源体检不健康`,
      pill: '紧急', pillCls: 'red',
      body: `体检不健康的源：${unhealthy.slice(0, 3).map((s) => s.name).join('、')}${unhealthy.length > 3 ? ' 等' : ''}。停用与否请到上游源页逐个确认，本页不自动停用。`,
      to: '/upstream-sources', act: '去处理',
    })
  }
  const scan = scanSummary.value
  const mount = mountSummary.value
  if (scan.bad > 0 || mount.failed > 0) {
    list.push({
      key: 'scanmount', icon: HardDrive, bg: 'rgba(245,165,36,.14)',
      title: '扫描与存储异常', cnt: `${scan.bad ? `扫描异常 ${scan.bad} 个库` : ''}${scan.bad && mount.failed ? ' · ' : ''}${mount.failed ? `目录读不到 ${mount.failed} 条来源` : ''}`,
      pill: '待办', pillCls: 'amber',
      body: `先到媒体库页看异常库的最近一轮扫描结果${mount.failedNames ? `；目录读不到的来源包括 ${mount.failedNames}${mount.failedMore ? ' 等' : ''}` : ''}。`,
      to: '/emby', act: '去查看',
    })
  }
  if (overview.value) {
    const pending = overview.value.media_seeks.pending
    list.push({
      key: 'seeks', icon: MessageSquareDashed, bg: 'rgba(245,165,36,.14)',
      title: '求片待审核', cnt: `${pending} 条新求片待确认`,
      pill: pending > 0 ? '待办' : '已清空', pillCls: pending > 0 ? 'amber' : 'grey',
      body: pending > 0
        ? '点开只看待审核的求片；处理后用户会收到站内通知。'
        : '当前没有待审核的求片。',
      to: '/media-seek?status=pending', act: '去审核',
    })
    const open = overview.value.tickets.open
    list.push({
      key: 'tickets', icon: Ticket, bg: 'rgba(96,165,250,.14)',
      title: '工单待处理', cnt: `${open} 条工单进行中`,
      pill: open > 0 ? '待办' : '已清空', pillCls: open > 0 ? 'amber' : 'grey',
      body: open > 0 ? '点开只看进行中的工单。' : '当前没有待处理的工单。',
      to: '/tickets?status=open', act: '去处理',
    })
  }
  if (identifyPending.value != null) {
    const n = identifyPending.value
    list.push({
      key: 'identify', icon: Puzzle, bg: 'rgba(167,139,250,.14)',
      title: '识别审查待复核', cnt: `${n} 条在审查队列里`,
      pill: n > 0 ? '排队中' : '已清空', pillCls: 'grey',
      body: '识别拿不准的条目会进审查队列等人工确认；不处理不会自动乱绑。',
      to: '/identify-review', act: '查看名单',
    })
  }
  if (economy.value && economy.value.orders.pending > 0) {
    list.push({
      key: 'orders', icon: Wallet, bg: 'rgba(34,211,238,.13)',
      title: '订单待支付', cnt: `${economy.value.orders.pending} 笔待支付`,
      pill: '待办', pillCls: 'amber',
      body: '点开只看待支付订单。',
      to: '/orders?status=pending', act: '去查看',
    })
  }
  return list
})

const openTodoKey = ref<string | null>(null)
const effectiveOpenTodo = computed(() =>
  openTodoKey.value === null ? (todoItems.value[0]?.key ?? '') : openTodoKey.value)
function toggleTodo(key: string) {
  openTodoKey.value = effectiveOpenTodo.value === key ? '' : key
}

// ==================== 上游源状态（图纸：底部右列）====================

type SourceRow = { id: number; name: string; dot: 'g' | 'a' | 'r'; tag: string; tagCls: string; libs: number }
const sourceRows = computed<SourceRow[]>(() => {
  const libCount = new Map<number, number>()
  for (const l of libraries.value) {
    for (const id of l.upstream_source_ids ?? []) {
      libCount.set(id, (libCount.get(id) ?? 0) + 1)
    }
  }
  const rank = (s: UpstreamSource) =>
    !s.enabled ? 3 : s.health_status === 'unhealthy' ? 0 : s.health_status === 'unknown' ? 1 : 2
  return upstreamSources.value
    .filter((s) => s.source_type === 'applecms')
    .slice()
    .sort((a, b) => rank(a) - rank(b))
    .slice(0, 7)
    .map((s) => ({
      id: s.id,
      name: s.name,
      dot: (!s.enabled ? 'r' : s.health_status === 'healthy' ? 'g' : s.health_status === 'unhealthy' ? 'r' : 'a') as SourceRow['dot'],
      tag: !s.enabled ? '已停用' : s.health_status === 'healthy' ? '正常' : s.health_status === 'unhealthy' ? '异常' : '未体检',
      tagCls: !s.enabled ? 'down' : s.health_status === 'healthy' ? 'ok' : s.health_status === 'unhealthy' ? 'down' : 'warn',
      libs: libCount.get(s.id) ?? 0,
    }))
})

// ==================== 聚焦光晕（图纸：Spotlight）====================

function onSpotMove(e: MouseEvent) {
  const el = e.currentTarget as HTMLElement
  const r = el.getBoundingClientRect()
  const x = Math.round(e.clientX - r.left)
  const y = Math.round(e.clientY - r.top)
  el.style.background =
    `radial-gradient(340px circle at ${x}px ${y}px, rgba(245,165,36,.13), transparent 65%), ` +
    'linear-gradient(180deg, rgba(255,255,255,.028), rgba(255,255,255,0) 46%), #15151b'
}
function onSpotLeave(e: MouseEvent) {
  ;(e.currentTarget as HTMLElement).style.background = ''
}

function fmtMoney(v: number): string {
  return `¥${v.toFixed(2)}`
}

function libraryStatus(library: EmbyLibrary): string {
  if (library.is_scanning) return '扫描中'
  if (!library.is_enabled) return '已停用'
  return '正常'
}

const greeting = computed(() => {
  const h = new Date().getHours()
  return h < 6 ? '夜深了' : h < 12 ? '上午好' : h < 14 ? '中午好' : h < 18 ? '下午好' : '晚上好'
})
</script>

<template>
  <div class="admin-page dashboard">
    <p class="dash-subline">
      {{ greeting }}，管理员 · <b>生产环境运行中</b> · 每个数字都能点进带好筛选的明细页
      <RouterLink to="/health" class="dash-subline-link">服务健康 <ArrowRight :size="12" /></RouterLink>
    </p>

    <!-- 首屏骨架：与图纸同形（KPI 排骨架 + 趋势块骨架），不再是一行「加载中…」 -->
    <div v-if="loading" class="dash-skel-wrap" aria-busy="true" aria-label="加载中">
      <div class="dash-skel-kpis">
        <div v-for="n in 4" :key="n" class="dash-card dash-skel-card">
          <div class="skel" style="height:38px;width:38px;border-radius:11px" />
          <div class="skel" style="height:26px;width:58%;margin-top:12px" />
          <div class="skel" style="height:12px;width:78%;margin-top:8px" />
        </div>
      </div>
      <div class="dash-card dash-skel-wide"><div class="skel" style="height:100%" /></div>
    </div>

    <template v-else>
      <el-alert
        v-if="failedParts.length"
        type="warning"
        :closable="false"
        show-icon
        :title="`部分数据没拿到：${failedParts.join('、')}。下面对应的数字暂按 0 显示。`"
      >
        <template #default>
          <el-button size="small" :icon="AlertTriangle" @click="loadAll">重新加载</el-button>
        </template>
      </el-alert>

      <!-- ======== KPI 排（图纸 4 卡） ======== -->
      <section class="dash-kpis dash-fade" aria-label="关键指标">
        <RouterLink to="/users" class="dash-card spot dash-kpi" @mousemove="onSpotMove" @mouseleave="onSpotLeave">
          <div class="dash-kpi-top">
            <div class="dash-chip amber"><Users :size="19" /></div>
            <span class="dash-delta up">今日 +{{ todayNewUsers }}</span>
          </div>
          <div class="dash-num">{{ fmtDisp('users', overview?.users.total ?? 0) }}</div>
          <div class="dash-lbl">注册用户</div>
          <svg class="dash-spark" width="86" height="26" viewBox="0 0 86 26" aria-hidden="true">
            <polyline fill="none" stroke="#f5a524" stroke-width="2" :points="sparkUsers" />
          </svg>
          <div class="dash-foot">活跃 {{ overview?.users.active ?? 0 }} 人</div>
        </RouterLink>

        <RouterLink to="/health" class="dash-card spot dash-kpi" @mousemove="onSpotMove" @mouseleave="onSpotLeave">
          <div class="dash-kpi-top">
            <div class="dash-chip blue"><Play :size="19" /></div>
            <span class="dash-delta blue">今日 {{ playback?.today.plays ?? 0 }} 次</span>
          </div>
          <div class="dash-num">{{ fmtDisp('playing', overview?.emby.active_sessions ?? 0) }}</div>
          <div class="dash-lbl">当前在播</div>
          <svg class="dash-spark" width="86" height="26" viewBox="0 0 86 26" aria-hidden="true">
            <polyline fill="none" stroke="#60a5fa" stroke-width="2" :points="sparkPlays" />
          </svg>
          <div class="dash-foot">今日 {{ playback?.today.users ?? 0 }} 人播放过</div>
        </RouterLink>

        <RouterLink to="/emby" class="dash-card spot dash-kpi" @mousemove="onSpotMove" @mouseleave="onSpotLeave">
          <div class="dash-kpi-top">
            <div class="dash-chip violet"><Film :size="19" /></div>
            <span class="dash-delta violet">{{ libraries.length }} 个库</span>
          </div>
          <div class="dash-num">{{ fmtDisp('items', overview?.emby.total_items ?? 0) }}</div>
          <div class="dash-lbl">媒体条目</div>
          <div class="dash-bars" aria-hidden="true">
            <i v-for="(b, i) in libraryBars" :key="i" :style="{ height: `${Math.round(b.h * 0.24)}px` }" :title="b.name" />
          </div>
          <div class="dash-foot">
            电影 {{ libraryKindTotals.movies.toLocaleString('en-US') }} · 剧集 {{ libraryKindTotals.shows.toLocaleString('en-US') }} · 集数 {{ libraryKindTotals.episodes.toLocaleString('en-US') }}
          </div>
        </RouterLink>

        <RouterLink to="/users" class="dash-card spot dash-kpi" @mousemove="onSpotMove" @mouseleave="onSpotLeave">
          <div class="dash-kpi-top">
            <div class="dash-chip cyan"><UserPlus :size="19" /></div>
            <span class="dash-delta cyan">近 {{ days }} 天 +{{ trend?.totals.new_users ?? 0 }}</span>
          </div>
          <div class="dash-num">{{ fmtDisp('todayNew', todayNewUsers) }}</div>
          <div class="dash-lbl">今日新增用户</div>
          <svg class="dash-spark" width="86" height="26" viewBox="0 0 86 26" aria-hidden="true">
            <polyline fill="none" stroke="#22d3ee" stroke-width="2" :points="sparkUsers" />
          </svg>
          <div class="dash-foot">按上海自然日统计 · 与趋势图同源</div>
        </RouterLink>
      </section>

      <!-- ======== 主区：趋势图 + 右列（健康 / 任务） ======== -->
      <section class="dash-grid dash-fade">
        <div class="dash-card spot dash-chart-card" @mousemove="onSpotMove" @mouseleave="onSpotLeave">
          <div class="dash-card-h">
            <h3>播放趋势</h3><span class="dash-hint">全站起播次数（按日）</span>
            <div class="dash-seg" role="tablist" aria-label="趋势时间范围">
              <button
                v-for="o in segOptions"
                :key="o.value"
                :class="{ on: days === o.value }"
                role="tab"
                :aria-selected="days === o.value"
                @click="days = o.value"
              >{{ o.label }}</button>
            </div>
          </div>
          <div class="dash-chart-num">
            <div><b>{{ chartNums.total.toLocaleString('en-US') }}</b><span>区间总起播</span></div>
            <div><b>{{ chartNums.peak.toLocaleString('en-US') }}</b><span>单日峰值</span></div>
            <div><b>{{ chartNums.avg.toLocaleString('en-US') }}</b><span>日均起播</span></div>
          </div>
          <div class="dash-chart-wrap" v-loading="trendLoading">
            <svg class="dash-chart" viewBox="0 0 720 228" preserveAspectRatio="none" role="img" aria-label="播放与新增用户趋势">
              <defs>
                <linearGradient id="dashGa" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0" stop-color="#f5a524" stop-opacity=".38" />
                  <stop offset="1" stop-color="#f5a524" stop-opacity=".02" />
                </linearGradient>
                <linearGradient id="dashGb" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0" stop-color="#a78bfa" stop-opacity=".4" />
                  <stop offset="1" stop-color="#a78bfa" stop-opacity=".03" />
                </linearGradient>
              </defs>
              <line v-for="g in 3" :key="g" x1="0" :y1="(228 / 4) * g" x2="720" :y2="(228 / 4) * g" stroke="rgba(255,255,255,.05)" />
              <path :d="usersArea" fill="url(#dashGb)" class="dash-fade-path" />
              <path :d="playsArea" fill="url(#dashGa)" class="dash-fade-path" />
              <path :d="usersLine" fill="none" stroke="#a78bfa" stroke-width="2.2" stroke-linecap="round" class="dash-draw" />
              <path :d="playsLine" fill="none" stroke="#f5a524" stroke-width="2.4" stroke-linecap="round" class="dash-draw" />
            </svg>
          </div>
          <div class="dash-legend">
            <span><i style="background:#f5a524" />本地＋无界播放（主刻度）</span>
            <span><i style="background:#a78bfa" />新增用户（独立刻度）</span>
            <span class="dash-axis">{{ axisRange }}</span>
          </div>
        </div>

        <div class="dash-sidecol">
          <div class="dash-card spot" @mousemove="onSpotMove" @mouseleave="onSpotLeave">
            <div class="dash-card-h">
              <h3>系统健康</h3>
              <RouterLink to="/health" class="dash-more">查看详情 ›</RouterLink>
            </div>
            <div v-if="healthScore === null && !healthRows.length" class="dash-empty">健康数据暂不可用</div>
            <div v-else class="dash-health-body">
              <div class="dash-ring-box">
                <svg width="112" height="112">
                  <circle cx="56" cy="56" r="48" fill="none" stroke="rgba(255,255,255,.07)" stroke-width="10" />
                  <circle
                    cx="56" cy="56" r="48" fill="none" stroke="url(#dashRg)" stroke-width="10"
                    stroke-linecap="round" :stroke-dasharray="RING_C" :stroke-dashoffset="ringOffset"
                    class="dash-ring-fg"
                  />
                  <defs>
                    <linearGradient id="dashRg" x1="0" y1="0" x2="1" y2="1">
                      <stop offset="0" stop-color="#ffcf5c" /><stop offset="1" stop-color="#f5a524" />
                    </linearGradient>
                  </defs>
                </svg>
                <div class="dash-ring-c"><b>{{ fmtDisp('health', healthScore ?? 0) }}</b><span>综合健康分</span></div>
              </div>
              <div class="dash-health-rows">
                <div v-for="r in healthRows" :key="r.label" class="dash-hrow">
                  <span class="dash-stdot" :class="r.dot" /><span class="dash-hlabel">{{ r.label }}</span><span class="dash-st">{{ r.value }}</span>
                </div>
                <p class="dash-health-note">按服务心跳与健康接口等权折算</p>
              </div>
            </div>
          </div>

          <div class="dash-card spot" @mousemove="onSpotMove" @mouseleave="onSpotLeave">
            <div class="dash-card-h">
              <h3>任务进度</h3><span class="dash-hint">后台队列实时状态</span>
              <RouterLink to="/tasks" class="dash-more">全部队列 ›</RouterLink>
            </div>
            <div style="height:6px" />
            <div v-for="b in taskBars" :key="b.key" class="dash-task">
              <div class="dash-task-top">
                <b>{{ b.title }}</b><span class="dash-task-sub">· {{ b.sub }}</span>
                <span class="dash-task-pct"><b>{{ b.pct === null ? '—' : `${fmtDisp(`pct-${b.key}`, b.pct)}%` }}</b></span>
              </div>
              <div class="dash-bar"><i :class="b.cls" :style="{ width: barsShown && b.pct !== null ? `${b.pct}%` : '0%' }" /></div>
            </div>
          </div>
        </div>
      </section>

      <!-- ======== 底部行：待处理事项 + 上游源状态（图纸） ======== -->
      <section class="dash-grid2 dash-fade">
        <div class="dash-card">
          <div class="dash-card-h">
            <h3>待处理事项</h3><span class="dash-hint">点标题展开详情</span>
          </div>
          <div style="height:6px" />
          <div v-if="!todoItems.length" class="dash-empty">暂无待处理事项</div>
          <div
            v-for="t in todoItems"
            :key="t.key"
            class="dash-todo"
            :class="{ open: effectiveOpenTodo === t.key }"
          >
            <div class="dash-todo-head" @click="toggleTodo(t.key)">
              <div class="dash-todo-ic" :style="{ background: t.bg }"><component :is="t.icon" :size="15" /></div>
              <b>{{ t.title }}</b><span class="dash-cnt">{{ t.cnt }}</span>
              <span class="dash-pill" :class="t.pillCls">{{ t.pill }}</span>
              <span class="dash-chev">▾</span>
            </div>
            <div class="dash-todo-body">
              <p>{{ t.body }} <RouterLink :to="t.to" class="dash-act">{{ t.act }} ›</RouterLink></p>
            </div>
          </div>
        </div>

        <div class="dash-card">
          <div class="dash-card-h">
            <h3>上游源状态</h3><span class="dash-hint">无界片库供源（苹果 CMS）</span>
            <RouterLink to="/upstream-sources" class="dash-more">全部源 ›</RouterLink>
          </div>
          <div style="height:4px" />
          <div v-if="!sourceRows.length" class="dash-empty">暂无上游源数据</div>
          <div v-for="s in sourceRows" :key="s.id" class="dash-src">
            <span class="dash-stdot" :class="s.dot" /><b>{{ s.name }}</b>
            <span class="dash-tag" :class="s.tagCls">{{ s.tag }}</span>
            <span class="dash-src-cnt">关联 {{ s.libs }} 个库</span>
          </div>
        </div>
      </section>

      <!-- ======== 以下为原有信息区（换本页卡片样式，内容与深链不变） ======== -->

      <!-- 服务接入：接了什么、几台能用、当前用哪台 -->
      <section v-if="serverTiles.length" class="dash-card dash-fade">
        <div class="dash-card-h">
          <h3>服务器接入</h3><span class="dash-hint">明细在「服务器与线路」页</span>
          <RouterLink to="/servers" class="dash-more">服务器与线路 ›</RouterLink>
        </div>
        <div style="height:6px" />
        <div v-for="tile in serverTiles" :key="tile.stat.kind" class="dash-src">
          <span class="dash-stdot" :class="tile.stat.reachable > 0 ? 'g' : 'r'" />
          <b>{{ tile.stat.short }}</b>
          <span v-if="tile.stat.active_name" class="dash-tag ok">当前 · {{ tile.stat.active_name }}</span>
          <span class="dash-src-cnt">{{ tile.stat.total }} 台 · 可用 {{ tile.stat.reachable }}</span>
        </div>
      </section>

      <!-- 各服概况 -->
      <section v-if="realms?.realms.length" class="dash-card dash-fade">
        <div class="dash-card-h">
          <h3>各服概况</h3>
          <span class="dash-hint">{{ realms.summary.total_realms }} 个服 · 有效订阅 {{ realms.summary.active_subscriptions }} · 播放节点在线 {{ realms.summary.nodes_online }}/{{ realms.summary.nodes }}</span>
          <RouterLink to="/realms" class="dash-more">服管理 ›</RouterLink>
        </div>
        <div style="height:6px" />
        <div class="dash-rows">
          <RouterLink
            v-for="r in realms.realms"
            :key="r.id"
            to="/realms"
            class="dash-row"
            :class="{ current: r.id === realms.active_realm_id, off: !r.is_active }"
          >
            <div class="dash-row-name">
              <strong>{{ r.name }}</strong>
              <span class="dash-tag grey">{{ r.slug }}</span>
              <span v-if="r.id === realms.active_realm_id" class="dash-tag ok">当前服</span>
              <span v-if="r.is_default" class="dash-tag warn">默认服</span>
            </div>
            <div class="dash-row-stats">
              <span>媒体库 <b>{{ r.stats.libraries }}</b></span>
              <span>条目 <b>{{ r.stats.items }}</b></span>
              <span>挂载 <b>{{ r.stats.mounts }}</b></span>
              <span>套餐 <b>{{ r.stats.plans }}</b></span>
              <span>有效订阅 <b>{{ r.stats.active_subscriptions }}</b></span>
              <span :class="{ warn: r.stats.nodes_online < r.stats.nodes }">节点 <b>{{ r.stats.nodes_online }}/{{ r.stats.nodes }}</b></span>
              <span :class="{ warn: r.stats.pending_requests > 0 }">待审求片 <b>{{ r.stats.pending_requests }}</b></span>
            </div>
          </RouterLink>
        </div>
      </section>

      <!-- 额度驾驶舱（只读）：开关关时不渲染 -->
      <section v-if="quota" class="dash-card dash-fade">
        <div class="dash-card-h">
          <h3>额度驾驶舱</h3><span class="dash-hint">只读 · 不触发转存</span>
          <span class="dash-more-static"><Gauge :size="13" /> SA 与转存</span>
        </div>
        <div class="dash-row-stats pad">
          <span>SA 账号 <b>{{ quota.sa_pool.count ?? '未知' }}</b></span>
          <span>转存待跑 <b>{{ quota.transfer_queue.counts.pending }}</b></span>
          <span>转存进行中 <b>{{ quota.transfer_queue.counts.running }}</b></span>
          <span>转存失败 <b>{{ quota.transfer_queue.counts.failed }}</b></span>
        </div>
        <div v-if="quota.user_disks.items.length" class="dash-rows">
          <div v-for="d in quota.user_disks.items" :key="d.binding_id" class="dash-row">
            <div class="dash-row-name">
              <strong>{{ d.account_label }}</strong>
              <span class="dash-tag grey">用户 {{ d.user_id }}</span>
              <span v-if="d.status !== 'ok'" class="dash-tag warn">配额未知</span>
            </div>
            <div class="dash-row-stats">
              <span>剩余 <b>{{ fmtBytes(d.free) }}</b></span>
              <span>已用 <b>{{ fmtBytes(d.usage) }}</b></span>
              <span>上限 <b>{{ fmtBytes(d.limit) }}</b></span>
            </div>
          </div>
        </div>
        <div v-if="quota.transfer_queue.recent_failed.length" class="dash-rows">
          <div v-for="f in quota.transfer_queue.recent_failed" :key="f.job_id" class="dash-row">
            <div class="dash-row-name">
              <strong>转存失败 #{{ f.job_id }}</strong>
              <span class="dash-tag grey">{{ f.error_kind || '未知原因' }}</span>
            </div>
            <div class="dash-row-stats"><span>{{ f.error_detail }}</span></div>
          </div>
        </div>
      </section>

      <!-- 交易概览：每个数字都能点进它自己的明细页（带筛选深链） -->
      <section class="dash-tiles dash-fade" aria-label="交易概览">
        <RouterLink to="/orders?status=paid" class="dash-card dash-mini">
          <div class="dash-mini-top"><Wallet :size="16" /><span>累计营收</span></div>
          <div class="dash-mini-num accent">{{ fmtMoney(economy?.orders.revenue ?? 0) }}</div>
          <div class="dash-foot">{{ economy?.orders.pending ?? 0 }} 笔待支付</div>
        </RouterLink>
        <RouterLink to="/invitations" class="dash-card dash-mini">
          <div class="dash-mini-top"><Coins :size="16" /><span>积分存量</span></div>
          <div class="dash-mini-num">{{ (economy?.total_points ?? 0).toLocaleString('en-US') }}</div>
          <div class="dash-foot">全站用户持有</div>
        </RouterLink>
        <RouterLink to="/users" class="dash-card dash-mini">
          <div class="dash-mini-top"><CalendarCheck :size="16" /><span>今日签到</span></div>
          <div class="dash-mini-num">{{ economy?.checkins_today ?? 0 }}</div>
          <div class="dash-foot">人已签到</div>
        </RouterLink>
        <RouterLink to="/codes?tab=exchange" class="dash-card dash-mini">
          <div class="dash-mini-top"><TicketCheck :size="16" /><span>兑换码</span></div>
          <div class="dash-mini-num">{{ economy?.exchange_codes.used ?? 0 }}<small> / {{ economy?.exchange_codes.total ?? 0 }}</small></div>
          <div class="dash-foot">已核销 / 已生成</div>
        </RouterLink>
        <RouterLink to="/invitations" class="dash-card dash-mini">
          <div class="dash-mini-top"><Gift :size="16" /><span>邀请关系</span></div>
          <div class="dash-mini-num">{{ economy?.invitations ?? 0 }}</div>
          <div class="dash-foot">累计成功邀请</div>
        </RouterLink>
      </section>

      <!-- 媒体服务运行态 -->
      <section v-if="libraries.length" class="dash-card dash-fade">
        <div class="dash-card-h">
          <h3>媒体服务</h3><span class="dash-hint">条目 {{ overview?.emby.total_items ?? 0 }}</span>
          <RouterLink to="/emby" class="dash-more">管理媒体库 ›</RouterLink>
        </div>
        <div style="height:6px" />
        <div v-for="library in libraries.slice(0, 5)" :key="library.id" class="dash-src">
          <span
            class="dash-stdot"
            :class="library.is_scanning ? 'a' : !library.is_enabled ? 'r' : 'g'"
          />
          <b>{{ library.name }}</b>
          <span class="dash-src-cnt">{{ library.item_count }} 个条目 · {{ libraryStatus(library) }}</span>
        </div>
      </section>

      <!-- 排行榜 -->
      <section v-if="playback" class="dash-two-col dash-fade">
        <div class="dash-card">
          <div class="dash-card-h"><h3>用户播放排行</h3><span class="dash-hint">近 7 天</span><Trophy :size="15" class="dash-h-icon" /></div>
          <div style="height:6px" />
          <EmptyState v-if="playback.user_ranking.length === 0" compact :icon="Users" title="暂无播放数据" description="有人播放后这里会出现排行；如果已有人在看却没数据，去「一键体检」查一下。" />
          <div v-for="(u, i) in playback.user_ranking" :key="u.username" class="dash-rank-row">
            <span class="dash-rank-index" :class="{ top: i < 3 }">{{ i + 1 }}</span>
            <span class="dash-rank-name">{{ u.username }}</span>
            <span class="dash-rank-value">{{ u.plays }} 次</span>
          </div>
        </div>
        <div class="dash-card">
          <div class="dash-card-h"><h3>热门内容</h3><span class="dash-hint">近 7 天</span><Flame :size="15" class="dash-h-icon" /></div>
          <div style="height:6px" />
          <EmptyState v-if="playback.item_ranking.length === 0" compact :icon="Film" title="暂无播放数据" description="有人播放后这里会出现排行；如果已有人在看却没数据，去「一键体检」查一下。" />
          <div v-for="(it, i) in playback.item_ranking" :key="it.name" class="dash-rank-row">
            <span class="dash-rank-index" :class="{ top: i < 3 }">{{ i + 1 }}</span>
            <span class="dash-rank-name">
              {{ it.name }}
              <span class="dash-rank-type">{{ it.type === 'episode' ? '剧集' : it.type === 'movie' ? '电影' : it.type }}</span>
            </span>
            <span class="dash-rank-value">{{ it.plays }} 次</span>
          </div>
        </div>
      </section>

      <!-- 数据明细：7 张表集中在这一个区里页签切换（2026-10-23 用户要求补表，不铺开） -->
      <section v-if="detail" class="dash-card dash-fade">
        <div class="dash-card-h"><h3>数据明细</h3><span class="dash-hint">点页签切换</span><Table2 :size="15" class="dash-h-icon" /></div>
        <el-tabs v-model="detailTab" class="dash-detail-tabs">
          <el-tab-pane label="最近注册" name="users">
            <EmptyState v-if="detail.recent_users.length === 0" compact :icon="Users" title="暂无用户" description="" />
            <table v-else class="dash-table">
              <thead><tr><th>用户</th><th>注册时间</th><th>渠道</th><th>类型</th></tr></thead>
              <tbody>
                <tr v-for="u in detail.recent_users" :key="u.username + u.created_at">
                  <td>{{ u.username }}</td>
                  <td>{{ fmtTime(u.created_at) }}</td>
                  <td>{{ u.channel || '—' }}</td>
                  <td>{{ u.is_welfare ? '公益' : '付费' }}</td>
                </tr>
              </tbody>
            </table>
          </el-tab-pane>
          <el-tab-pane label="最近订单" name="orders">
            <EmptyState v-if="detail.recent_orders.length === 0" compact :icon="Wallet" title="暂无订单" description="" />
            <table v-else class="dash-table">
              <thead><tr><th>用户</th><th>项目</th><th>类别</th><th>金额</th><th>状态</th><th>时间</th></tr></thead>
              <tbody>
                <tr v-for="(o, i) in detail.recent_orders" :key="i">
                  <td>{{ o.username }}</td>
                  <td>{{ o.item }}</td>
                  <td>{{ o.kind }}</td>
                  <td>¥{{ o.amount }}</td>
                  <td>{{ o.status }}</td>
                  <td>{{ fmtTime(o.created_at) }}</td>
                </tr>
              </tbody>
            </table>
          </el-tab-pane>
          <el-tab-pane label="库内容" name="libraries">
            <EmptyState v-if="detail.library_stats.length === 0" compact :icon="Film" title="暂无媒体库" description="" />
            <table v-else class="dash-table">
              <thead><tr><th>媒体库</th><th>电影</th><th>剧集</th><th>集数</th><th>合计</th></tr></thead>
              <tbody>
                <tr v-for="r in detail.library_stats" :key="r.library">
                  <td>{{ r.library }}</td>
                  <td>{{ r.movies }}</td>
                  <td>{{ r.shows }}</td>
                  <td>{{ r.episodes }}</td>
                  <td>{{ r.total }}</td>
                </tr>
              </tbody>
            </table>
          </el-tab-pane>
          <el-tab-pane label="刮削进度" name="enrich">
            <EmptyState v-if="detail.enrich_progress.length === 0" compact :icon="Film" title="暂无媒体库" description="" />
            <table v-else class="dash-table">
              <thead><tr><th>媒体库</th><th>已刮</th><th>待刮</th><th>覆盖率</th></tr></thead>
              <tbody>
                <tr v-for="r in detail.enrich_progress" :key="r.library">
                  <td>{{ r.library }}</td>
                  <td>{{ r.enriched }}</td>
                  <td>{{ r.pending }}</td>
                  <td>{{ r.enriched + r.pending === 0 ? '—' : `${Math.round((r.enriched / (r.enriched + r.pending)) * 100)}%` }}</td>
                </tr>
              </tbody>
            </table>
          </el-tab-pane>
          <el-tab-pane label="积分流水" name="points">
            <EmptyState v-if="detail.points_flow.length === 0" compact :icon="Coins" title="暂无流水" description="" />
            <table v-else class="dash-table">
              <thead><tr><th>用户</th><th>类型</th><th>增减</th><th>说明</th><th>时间</th></tr></thead>
              <tbody>
                <tr v-for="(p, i) in detail.points_flow" :key="i">
                  <td>{{ p.username }}</td>
                  <td>{{ p.type }}</td>
                  <td :class="p.amount >= 0 ? 'amt-in' : 'amt-out'">{{ p.amount >= 0 ? `+${p.amount}` : p.amount }}</td>
                  <td class="desc-cell">{{ p.description }}</td>
                  <td>{{ fmtTime(p.created_at) }}</td>
                </tr>
              </tbody>
            </table>
          </el-tab-pane>
          <el-tab-pane label="求片热度" name="seeks">
            <EmptyState v-if="detail.seek_hot.length === 0" compact :icon="MessageSquareDashed" title="暂无求片" description="" />
            <table v-else class="dash-table">
              <thead><tr><th>片名</th><th>类型</th><th>求片次数</th><th>待处理</th></tr></thead>
              <tbody>
                <tr v-for="s in detail.seek_hot" :key="s.title + s.media_type">
                  <td>{{ s.title }}</td>
                  <td>{{ s.media_type === 'movie' ? '电影' : s.media_type === 'tv' ? '剧集' : s.media_type }}</td>
                  <td>{{ s.count }}</td>
                  <td>{{ s.pending }}</td>
                </tr>
              </tbody>
            </table>
          </el-tab-pane>
          <el-tab-pane label="近 7 天活跃" name="activity">
            <table class="dash-table">
              <thead><tr><th>日期</th><th>活跃用户</th><th>播放次数</th><th>新增用户</th></tr></thead>
              <tbody>
                <tr v-for="d in detail.activity_days" :key="d.date">
                  <td>{{ d.date.slice(5) }}</td>
                  <td>{{ d.active_users }}</td>
                  <td>{{ d.plays }}</td>
                  <td>{{ d.new_users }}</td>
                </tr>
              </tbody>
            </table>
          </el-tab-pane>
        </el-tabs>
      </section>
    </template>
  </div>
</template>

<style scoped>
/* ===== 图纸调色（效果图 2026-11-03 定稿，只作用于本页） =====
 * 数值直接写图纸色值：仓库令牌契约要求 var() 引用必须定义在全局样式表里，
 * 本页不新增全局令牌，故不用页面私有 CSS 变量。 */
.dashboard {
  gap: 16px;
  color: #f2f0eb;
  position: relative;
}
/* 图纸的背景三色微光晕（固定层，不挡交互） */
.dashboard::before {
  content: ""; position: fixed; inset: 0; pointer-events: none; z-index: 0;
  background:
    radial-gradient(720px 340px at 78% -80px, rgba(245, 165, 36, .13), transparent 62%),
    radial-gradient(620px 320px at 8% -60px, rgba(167, 139, 250, .09), transparent 60%),
    radial-gradient(500px 300px at 55% 110%, rgba(34, 211, 238, .05), transparent 60%);
}
.dashboard > * { position: relative; z-index: 1; }

.dash-subline { color: #6d6a78; font-size: 12.5px; margin: 0; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.dash-subline b { color: #9a97a3; font-weight: 500; }
.dash-subline-link { margin-left: auto; display: inline-flex; align-items: center; gap: 3px; color: #ffcf5c; text-decoration: none; font-size: 12.5px; }
.dash-subline-link:hover { color: #f5a524; }

/* ===== 卡片基座 + 聚焦光晕 ===== */
.dash-card {
  background: linear-gradient(180deg, rgba(255, 255, 255, .028), rgba(255, 255, 255, 0) 46%), #15151b;
  border: 1px solid rgba(255, 255, 255, .07); border-radius: 18px;
  position: relative; overflow: hidden;
}
/* 聚焦光晕（Spotlight）由 onSpotMove 直接把径向渐变写到卡片背景上：
 * 令牌契约不允许页面私自定义属性（--mx/--my），故不用伪元素 + var() 方案，
 * 视觉与图纸一致（340px 琥珀光晕跟随鼠标、离开即还原）。 */
.dash-card-h { display: flex; align-items: center; gap: 10px; padding: 16px 18px 6px; }
.dash-card-h h3 { font-size: 15px; font-weight: 650; color: #f2f0eb; margin: 0; }
.dash-hint { font-size: 12px; color: #6d6a78; }
.dash-more { margin-left: auto; font-size: 12px; color: #6d6a78; text-decoration: none; white-space: nowrap; }
.dash-more:hover { color: #ffcf5c; }
.dash-more-static { margin-left: auto; font-size: 12px; color: #6d6a78; display: inline-flex; align-items: center; gap: 4px; }
.dash-h-icon { margin-left: auto; color: #6d6a78; }
.dash-empty { padding: 14px 18px 18px; color: #6d6a78; font-size: 12.5px; }
.dash-fade { animation: dashFade .4s ease; }
@keyframes dashFade { from { opacity: 0 } to { opacity: 1 } }

/* ===== 骨架屏（图纸同款闪烁） ===== */
.dash-skel-kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 16px; }
.dash-skel-card { padding: 16px 18px; }
.dash-skel-wide { height: 240px; padding: 16px; }
.skel { position: relative; overflow: hidden; background: rgba(255, 255, 255, .055); border-radius: 8px; }
.skel::after {
  content: ""; position: absolute; inset: 0;
  background: linear-gradient(100deg, transparent 30%, rgba(255, 255, 255, .09) 50%, transparent 70%);
  animation: dashShim 1.1s infinite;
}
@keyframes dashShim { from { transform: translateX(-100%) } to { transform: translateX(100%) } }

/* ===== KPI ===== */
.dash-kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }
.dash-kpi { padding: 16px 18px 14px; text-decoration: none; color: inherit; display: block; }
.dash-kpi-top { display: flex; align-items: flex-start; gap: 10px; }
.dash-chip { width: 40px; height: 40px; border-radius: 12px; display: grid; place-items: center; flex: 0 0 40px; color: #f2f0eb; }
.dash-chip.amber { background: rgba(245, 165, 36, .15); box-shadow: inset 0 0 0 1px rgba(245, 165, 36, .3); color: #ffcf5c; }
.dash-chip.blue { background: rgba(96, 165, 250, .14); box-shadow: inset 0 0 0 1px rgba(96, 165, 250, .3); color: #60a5fa; }
.dash-chip.violet { background: rgba(167, 139, 250, .14); box-shadow: inset 0 0 0 1px rgba(167, 139, 250, .3); color: #a78bfa; }
.dash-chip.cyan { background: rgba(34, 211, 238, .13); box-shadow: inset 0 0 0 1px rgba(34, 211, 238, .3); color: #22d3ee; }
.dash-delta { margin-left: auto; font-size: 12px; font-weight: 700; padding: 3px 8px; border-radius: 20px; white-space: nowrap; }
.dash-delta.up { color: #34d399; background: rgba(52, 211, 153, .12); }
.dash-delta.blue { color: #60a5fa; background: rgba(96, 165, 250, .12); }
.dash-delta.violet { color: #a78bfa; background: rgba(167, 139, 250, .12); }
.dash-delta.cyan { color: #22d3ee; background: rgba(34, 211, 238, .12); }
.dash-num { font-size: 31px; font-weight: 750; letter-spacing: .2px; margin-top: 10px; font-variant-numeric: tabular-nums; color: #f2f0eb; }
.dash-lbl { color: #9a97a3; font-size: 13px; margin-top: 1px; }
.dash-spark { position: absolute; right: 14px; bottom: 34px; opacity: .9; }
.dash-bars { position: absolute; right: 14px; bottom: 34px; display: flex; align-items: flex-end; gap: 2.5px; height: 26px; opacity: .9; }
.dash-bars i { display: block; width: 4.5px; border-radius: 2px; background: linear-gradient(180deg, #a78bfa, rgba(167, 139, 250, .35)); }
.dash-foot { font-size: 11.5px; color: #6d6a78; margin-top: 6px; line-height: 1.5; padding-right: 92px; }

/* ===== 主区网格 ===== */
.dash-grid { display: grid; grid-template-columns: minmax(0, 1fr) 342px; gap: 16px; }
.dash-grid2 { display: grid; grid-template-columns: minmax(0, 1fr) 342px; gap: 16px; }
.dash-sidecol { display: flex; flex-direction: column; gap: 16px; min-width: 0; }

/* 分段控制器（图纸） */
.dash-seg { margin-left: auto; display: flex; background: rgba(255, 255, 255, .05); border: 1px solid rgba(255, 255, 255, .07); border-radius: 11px; padding: 3px; gap: 2px; }
.dash-seg button { border: none; background: transparent; color: #9a97a3; font-size: 12.5px; padding: 5px 13px; border-radius: 8px; cursor: pointer; font-family: inherit; transition: .18s; }
.dash-seg button.on { background: linear-gradient(135deg, #ffcf5c, #f5a524); color: #191204; font-weight: 700; box-shadow: 0 3px 10px rgba(245, 165, 36, .3); }
.dash-chart-num { display: flex; gap: 22px; padding: 8px 18px 0; }
.dash-chart-num div b { font-size: 19px; font-weight: 700; display: block; font-variant-numeric: tabular-nums; color: #f2f0eb; }
.dash-chart-num div span { font-size: 11.5px; color: #6d6a78; }
.dash-chart-card { display: flex; flex-direction: column; }
.dash-chart-wrap { padding: 8px 10px 4px; flex: 1; display: flex; }
.dash-chart { width: 100%; height: 228px; min-height: 228px; display: block; flex: 1; }
.dash-legend { display: flex; gap: 18px; padding: 2px 18px 12px; color: #9a97a3; font-size: 12px; flex-wrap: wrap; }
.dash-legend i { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
.dash-axis { margin-left: auto; color: #6d6a78; font-variant-numeric: tabular-nums; }
.dash-fade-path { animation: dashFd .7s ease; }
@keyframes dashFd { from { opacity: 0 } to { opacity: 1 } }
.dash-draw { stroke-dasharray: 2600; stroke-dashoffset: 2600; animation: dashDr 1.1s ease forwards; }
@keyframes dashDr { to { stroke-dashoffset: 0 } }

/* ===== 健康环 ===== */
.dash-health-body { display: flex; gap: 16px; align-items: center; padding: 12px 18px 16px; }
.dash-ring-box { position: relative; width: 112px; height: 112px; flex: 0 0 112px; }
.dash-ring-box svg { transform: rotate(-90deg); }
.dash-ring-fg { transition: stroke-dashoffset 1.2s cubic-bezier(.22, .8, .3, 1); }
.dash-ring-c { position: absolute; inset: 0; display: grid; place-content: center; text-align: center; }
.dash-ring-c b { font-size: 23px; font-weight: 750; line-height: 1; color: #f2f0eb; }
.dash-ring-c span { font-size: 11px; color: #6d6a78; margin-top: 3px; }
.dash-health-rows { flex: 1; display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.dash-hrow { display: flex; align-items: center; gap: 8px; font-size: 12.5px; color: #9a97a3; min-width: 0; }
.dash-hlabel { flex-shrink: 0; white-space: nowrap; }
.dash-hrow .dash-st { margin-left: auto; font-weight: 650; color: #f2f0eb; font-variant-numeric: tabular-nums; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
.dash-health-note { font-size: 11px; color: #6d6a78; margin: 0; }
.dash-stdot { width: 7px; height: 7px; border-radius: 50%; flex: 0 0 7px; }
.dash-stdot.g { background: #34d399; box-shadow: 0 0 7px rgba(52, 211, 153, .8); }
.dash-stdot.a { background: #f5a524; box-shadow: 0 0 7px rgba(245, 165, 36, .8); }
.dash-stdot.r { background: #f87171; box-shadow: 0 0 7px rgba(248, 113, 113, .8); }

/* ===== 任务进度 ===== */
.dash-task { padding: 5px 18px 9px; }
.dash-task:last-child { padding-bottom: 16px; }
.dash-task-top { display: flex; align-items: baseline; font-size: 12.5px; color: #9a97a3; margin-bottom: 6px; min-width: 0; }
.dash-task-top > b { color: #f2f0eb; font-weight: 600; white-space: nowrap; flex-shrink: 0; }
.dash-task-sub { margin-left: 5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
.dash-task-pct { margin-left: auto; font-variant-numeric: tabular-nums; flex-shrink: 0; padding-left: 8px; }
.dash-task-pct b { color: #f2f0eb; }
.dash-bar { height: 8px; border-radius: 6px; background: rgba(255, 255, 255, .07); overflow: hidden; }
.dash-bar i { display: block; height: 100%; width: 0; border-radius: 6px; transition: width 1.15s cubic-bezier(.22, .8, .3, 1); }
.dash-bar .f-amber { background: linear-gradient(90deg, #d97f06, #ffcf5c); }
.dash-bar .f-violet { background: linear-gradient(90deg, #7c5cf0, #a78bfa); }
.dash-bar .f-cyan { background: linear-gradient(90deg, #0891b2, #22d3ee); }

/* ===== 待处理事项（图纸：展开卡） ===== */
.dash-todo { border-top: 1px solid rgba(255, 255, 255, .07); }
.dash-todo-head { display: flex; align-items: center; gap: 10px; padding: 12px 18px; cursor: pointer; user-select: none; }
.dash-todo-head:hover { background: rgba(255, 255, 255, .02); }
.dash-todo-ic { width: 30px; height: 30px; border-radius: 9px; display: grid; place-items: center; flex: 0 0 30px; color: #f2f0eb; }
.dash-todo-head b { font-size: 13.5px; font-weight: 600; color: #f2f0eb; }
.dash-todo-head .dash-cnt { font-size: 11.5px; color: #6d6a78; }
.dash-chev { margin-left: auto; color: #6d6a78; transition: transform .22s; font-size: 12px; }
.dash-todo.open .dash-chev { transform: rotate(180deg); }
.dash-todo-body { max-height: 0; overflow: hidden; transition: max-height .3s ease; }
.dash-todo.open .dash-todo-body { max-height: 150px; }
.dash-todo-body p { padding: 0 18px 13px 58px; color: #9a97a3; font-size: 12.5px; line-height: 1.65; margin: 0; }
.dash-act { color: #ffcf5c; font-weight: 600; text-decoration: none; white-space: nowrap; }
.dash-pill { font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 14px; }
.dash-pill.red { color: #ffb4b4; background: rgba(248, 113, 113, .13); }
.dash-pill.amber { color: #ffcf5c; background: rgba(245, 165, 36, .13); }
.dash-pill.grey { color: #9a97a3; background: rgba(255, 255, 255, .08); }

/* ===== 源状态 / 通用行 ===== */
.dash-src { display: flex; align-items: center; gap: 10px; padding: 10px 18px; border-top: 1px solid rgba(255, 255, 255, .07); font-size: 13px; color: #f2f0eb; }
.dash-src b { font-weight: 600; }
.dash-src-cnt { margin-left: auto; color: #6d6a78; font-size: 12px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.dash-tag { font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 14px; white-space: nowrap; }
.dash-tag.ok { color: #7bf0c3; background: rgba(52, 211, 153, .12); }
.dash-tag.warn { color: #ffcf5c; background: rgba(245, 165, 36, .12); }
.dash-tag.down { color: #ffb4b4; background: rgba(248, 113, 113, .12); }
.dash-tag.grey { color: #9a97a3; background: rgba(255, 255, 255, .08); }

/* ===== 保留区：服/额度行、交易小卡、排行、明细表 ===== */
.dash-rows { display: flex; flex-direction: column; gap: 6px; padding: 0 12px 14px; }
.dash-row {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  padding: 10px 12px; border-radius: 12px;
  border: 1px solid rgba(255, 255, 255, .07); background: rgba(255, 255, 255, .02);
  text-decoration: none;
}
a.dash-row:hover { border-color: rgba(255, 255, 255, .12); background: rgba(255, 255, 255, .04); }
.dash-row.current { border-color: rgba(245, 165, 36, .4); }
.dash-row.off { opacity: .6; }
.dash-row-name { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; min-width: min(190px, 100%); }
.dash-row-name strong { color: #f2f0eb; font-size: 13px; }
.dash-row-stats { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; margin-left: auto; }
.dash-row-stats.pad { padding: 4px 18px 14px; margin-left: 0; }
.dash-row-stats span { color: #6d6a78; font-size: 12px; }
.dash-row-stats b { color: #9a97a3; font-variant-numeric: tabular-nums; }
.dash-row-stats span.warn b { color: #ffcf5c; }

.dash-tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; }
.dash-mini { padding: 14px 18px; text-decoration: none; color: inherit; display: block; }
.dash-mini-top { display: flex; align-items: center; gap: 8px; color: #9a97a3; font-size: 13px; }
.dash-mini-num { font-size: 24px; font-weight: 750; margin-top: 8px; font-variant-numeric: tabular-nums; color: #f2f0eb; }
.dash-mini-num.accent { color: #ffcf5c; }
.dash-mini-num small { font-size: 14px; color: #6d6a78; }
.dash-mini .dash-foot { padding-right: 0; }

.dash-two-col { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 320px), 1fr)); gap: 16px; }
.dash-rank-row { display: flex; align-items: center; gap: 12px; padding: 9px 18px; border-top: 1px solid rgba(255, 255, 255, .07); }
.dash-rank-index {
  width: 24px; height: 24px; flex-shrink: 0; display: flex; align-items: center; justify-content: center;
  border-radius: 7px; background: rgba(255, 255, 255, .06); color: #6d6a78;
  font-size: 12px; font-weight: 700; font-variant-numeric: tabular-nums;
}
.dash-rank-index.top { background: rgba(245, 165, 36, .16); color: #ffcf5c; }
.dash-rank-name { flex: 1; font-size: 13px; color: #f2f0eb; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dash-rank-type { font-size: 11px; color: #6d6a78; margin-left: 6px; }
.dash-rank-value { font-size: 12px; color: #9a97a3; font-variant-numeric: tabular-nums; }

.dash-detail-tabs { padding: 0 18px 10px; }
.dash-table { width: 100%; border-collapse: collapse; font-size: 13px; color: #f2f0eb; }
.dash-table th { text-align: left; font-weight: 500; color: #9a97a3; padding: 6px 8px; border-bottom: 1px solid rgba(255, 255, 255, .07); }
.dash-table td { padding: 7px 8px; border-bottom: 1px solid rgba(255, 255, 255, .07); white-space: nowrap; }
.dash-table .desc-cell { white-space: normal; max-width: 260px; overflow: hidden; text-overflow: ellipsis; }
.dash-table .amt-in { color: #34d399; }
.dash-table .amt-out { color: #f87171; }

/* ===== 响应式（图纸同款断点） ===== */
@media (max-width: 1180px) {
  .dash-kpis { grid-template-columns: repeat(2, 1fr); }
  .dash-skel-kpis { grid-template-columns: repeat(2, 1fr); }
  .dash-grid, .dash-grid2 { grid-template-columns: 1fr; }
}
@media (max-width: 760px) {
  .dash-kpis { grid-template-columns: 1fr; }
  .dash-skel-kpis { grid-template-columns: 1fr; }
  .dash-spark, .dash-bars { display: none; }
  .dash-foot { padding-right: 0; }
  .dash-chart { height: 180px; }
  .dash-health-body { flex-direction: column; align-items: flex-start; }
  .dash-todo-body p { padding-left: 18px; }
}
</style>
