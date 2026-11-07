<template>
  <div class="v4-scope">
    <div class="v4-page">
      <header class="v4-loghead">
        <h1>观影帮助</h1>
        <p>客户端连接教程、常见播放说明与在线问答支持。</p>
        <div v-if="available" id="guide-quota" class="v4-fire">{{ quotaText }}</div>
        <div v-else-if="loading" class="v4-skeleton" style="height:28px;width:48%;margin-top:10px"></div>
      </header>

      <div v-if="loading" class="v4-sec"><div class="v4-skeleton" style="height:180px"></div></div>

      <section v-else-if="!available" class="v4-sec">
        <div class="guide-card">
          <div class="guide-ic" aria-hidden="true"><Compass :size="22" /></div>
          <div class="who">
            <div class="nm">客户端接入与播放速查 <span class="tag">{{ netError ? '离线模式' : '使用指南' }}</span></div>
            <div class="motto">推荐使用第三方原生播放器直连服务器，获得完整的 4K HDR / 杜比视界解码体验。</div>
          </div>
        </div>

        <div class="faq-grid">
          <article class="faq-item">
            <h3>1. 如何获取服务器地址与播放密码？</h3>
            <p>前往「播放器接入」页面即可查看当前服务器 HTTPS 地址与专属播放用户名，或一键导入至 VidHub、SenPlayer。</p>
          </article>
          <article class="faq-item">
            <h3>2. 推荐使用哪些播放器客户端？</h3>
            <p>Apple 端推荐 Infuse（添加为文件来源并关闭媒体库模式，缓存设为 Legacy）、VidHub、SenPlayer；Android / Windows 端推荐 VidHub、Hills、AfuseKt。</p>
          </article>
          <article class="faq-item">
            <h3>3. 为什么不在浏览器内直接播放？</h3>
            <p>本站采用原盘直连不转码架构以保留最高音画质量，请在上述原生客户端中点播或从影片详情页一键唤起。</p>
          </article>
        </div>

        <div class="faq-actions">
          <button type="button" class="v4-retry" @click="router.push('/game/operator')">前往播放器接入</button>
          <button type="button" class="faq-ghost-btn" @click="goSignal">提交工单反馈</button>
        </div>
      </section>

      <template v-else>
        <section class="v4-sec">
          <div class="guide-card">
            <div class="guide-ic" aria-hidden="true"><Sparkles :size="20" /></div>
            <div class="who">
              <div class="nm">智能观影助手 <span class="tag">在线</span></div>
              <div class="motto">支持查询近期新片、客户端配置方法与账号权益问题。</div>
            </div>
            <span class="dot"></span>
          </div>
        </section>

        <section class="v4-sec">
          <div class="v4-sec-title">常见问题</div>
          <div class="chips">
            <button
              v-for="s in SUGGESTIONS"
              :key="s"
              class="chip"
              type="button"
              :disabled="sending || exhausted"
              @click="ask(s)"
            >{{ s }}</button>
          </div>
        </section>

        <section class="v4-sec">
          <div ref="scroller" class="chat" role="log" tabindex="0" aria-label="问答记录">
            <div v-for="b in bubbles" :key="b.id" class="msg" :class="b.role">
              <div v-if="b.role === 'guide'" class="who">观影助手</div>
              <p>{{ b.content }}</p>
            </div>
            <div v-if="sending" class="msg guide">
              <div class="who">观影助手</div>
              <span class="sr-only" role="status">正在生成回复…</span>
              <span class="typing" aria-hidden="true"><i></i><i></i><i></i></span>
            </div>
          </div>
        </section>

        <div class="inputbar">
          <input
            v-model="question"
            type="text"
            :disabled="exhausted"
            :placeholder="exhausted ? '今日提问次数已用完，请明日再试' : '输入你想了解的片库或播放问题…'"
            aria-label="输入问题"
            aria-describedby="guide-quota"
            @keyup.enter="ask()"
          >
          <button :disabled="sending || exhausted || !question.trim()" @click="ask()">
            {{ sending ? '生成中' : '发送' }}
          </button>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Compass, Sparkles } from 'lucide-vue-next'
import { aiApi, type AiStatus } from '@/api'
import { humanizeError } from '@/composables/usePanelApi'
const router = useRouter()
/** R45：「向导不在」时求救信号文字按钮 → 跳求救信号页（router 里已声明 /game/signal） */
function goSignal() {
  router.push('/game/signal')
}

interface Bubble {
  id: number
  role: 'me' | 'guide' | 'error'
  content: string
}

const loading = ref(true)
const status = ref<AiStatus | null>(null)
const question = ref('')
const sending = ref(false)
const bubbles = ref<Bubble[]>([])
const MAX_BUBBLES = 80
let bubbleSeq = 0
function trimBubbles() {
  if (bubbles.value.length > MAX_BUBBLES) bubbles.value.splice(0, bubbles.value.length - MAX_BUBBLES)
}
/** R107-2：气泡带唯一 id（:key 不再用索引）—— trimBubbles 从头部删时，索引 key 会让 Vue 错位复用节点 */
function addBubble(role: Bubble['role'], content: string) {
  bubbles.value.push({ id: ++bubbleSeq, role, content })
  trimBubbles()
}
const scroller = ref<HTMLElement | null>(null)
/** true=请求抛错（网络开小差）；false=明确未启用（管理员还没请向导） */
const netError = ref(false)

/** 向导是否在岗（能力来自 aiApi.status，与原 AI 助手页一致） */
const available = computed(() => status.value?.enabled === true)
const quotaText = computed(() => {
  const s = status.value
  if (!s?.enabled) return ''
  if (!s.daily_limit || typeof s.remaining !== 'number') return '在线问答已就绪'
  return `今日剩余提问 ${s.remaining} 次`
})
const exhausted = computed(() => available.value && status.value?.remaining === 0)

const SUGGESTIONS = [
  '近期有哪些高分新片入库？',
  '如何在 Infuse 或 VidHub 中添加服务器？',
  '4K 杜比视界播放有哪些注意事项？',
]

async function load() {
  loading.value = true
  netError.value = false
  await refreshStatus()
  loading.value = false
}

/** R37 P2-1：只刷新向导状态，不碰页面级 loading —— ask() 错误路径
 *  （配额用尽/被关闭）用这个刷新，聊天 UI 不卸载重挂、错误气泡可见、滚动不归零 */
async function refreshStatus() {
  try {
    status.value = await aiApi.status()
    netError.value = false
  } catch {
    status.value = null
    netError.value = true
  }
}

async function scrollToEnd() {
  await nextTick()
  if (scroller.value) {
    scroller.value.scrollTo({
      top: scroller.value.scrollHeight,
      behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
    })
  }
}

async function ask(text?: string) {
  const content = (text ?? question.value).trim()
  if (!content || sending.value) return
  question.value = ''
  addBubble('me', content)
  sending.value = true
  scrollToEnd()
  try {
    // 只把 me / guide 轮次交给后端（它也会再过滤一次）；当前提问已作为 content 单独传，历史里去掉刚 push 的那条
    const history = bubbles.value
      .filter((b) => b.role !== 'error')
      .map((b): { role: 'user' | 'assistant'; content: string } => ({
        role: b.role === 'me' ? 'user' : 'assistant',
        content: b.content,
      }))
      .slice(0, -1)
      .slice(-8)
    const res = await aiApi.ask(content, history)
    addBubble('guide', res.answer)
    status.value = { ...(status.value as AiStatus), ...res }
  } catch (err) {
    addBubble('error', humanizeError(err))
    // R107-1：发送失败把问题还回输入框（只在输入框为空时，避免覆盖等待期间用户新打的字）
    if (!question.value) question.value = content
    // 配额用尽 / 被关闭时刷新一次状态，界面提示随之更新
    // R37 P2-1：只刷新 status，不调 load() —— 全页 skeleton 不闪，聊天停留在原位置
    void refreshStatus()
  } finally {
    sending.value = false
    scrollToEnd()
  }
}

onMounted(() => {
  load()
  addBubble('guide', '你好，欢迎使用观影助手。你可以询问片库新片推荐、播放器配置步骤或账号权益说明。')
})
</script>

<style scoped>
/* 向导名片：设计稿 game-guide.html 原值 */
.guide-card {
  display: flex;
  align-items: center;
  gap: 12px;
  background: var(--paper-card);
  border: 1px solid #d1d1cd;
  border-radius: 16px;
  padding: 16px 18px;
  position: relative;
}
.guide-ic {
  width: 42px;
  height: 42px;
  border-radius: 12px;
  background: rgba(1, 106, 113, 0.1);
  color: #016a71;
  display: flex;
  align-items: center;
  justify-content: center;
  flex: none;
}
.guide-card .who { flex: 1; min-width: 0; }
.guide-card .nm { font-size: 16px; font-weight: 500; color: #27251e; }
.guide-card .tag {
  font-size: 12px;
  font-weight: 500;
  color: #016a71;
  background: rgba(1, 106, 113, 0.08);
  border: 1px solid rgba(1, 106, 113, 0.28);
  border-radius: 9999px;
  padding: 2px 10px;
  margin-left: 6px;
  white-space: nowrap;
}
.guide-card .motto { font-size: 14px; color: #524b42; margin-top: 4px; }
.faq-grid {
  margin-top: 14px;
  display: grid;
  gap: 12px;
}
.faq-item {
  padding: 16px 18px;
  border-radius: 16px;
  border: 1px solid #d1d1cd;
  background: #fdfbfa;
}
.faq-item h3 {
  margin: 0 0 6px;
  font-size: 15px;
  font-weight: 500;
  color: #27251e;
}
.faq-item p {
  margin: 0;
  font-size: 14px;
  line-height: 1.65;
  color: #524b42;
}
.faq-actions {
  margin-top: 16px;
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}
.faq-ghost-btn {
  min-height: 40px;
  padding: 0 16px;
  border-radius: 6px;
  border: 1px solid #d1d1cd;
  background: #fdfbfa;
  color: #27251e;
  font-size: 14px;
  font-weight: 500;
  font-family: inherit;
  cursor: pointer;
}
.guide-card .dot {
  width: 10px;
  height: 10px;
  border-radius: var(--paper-r-full, 50%);
  background: var(--green);
  flex: none;
}

.v4-retry:disabled { opacity: 0.5; cursor: default; }

/* 快捷问 chips */
.chips { display: flex; flex-wrap: wrap; gap: 8px; }
.chip {
  border: 1.5px solid var(--line);
  background: var(--paper-card);
  color: var(--ink);
  border-radius: var(--paper-r-2xl, 20px);
  padding: 8px 14px;
  font-size: 13px;
  min-height: 44px;
  cursor: pointer;
}
.chip:active { transform: scale(0.96); border-color: var(--red); }
.chip:disabled { opacity: 0.5; cursor: default; }

/* 聊天区 */
.chat {
  background: var(--paper-card);
  border: 1.5px solid var(--line);
  border-radius: var(--paper-r-lg, 12px);
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 200px;
  max-height: 52vh;
  overflow-y: auto;
}
/* R41：.chat-empty 已删（onMounted 同步 push 欢迎气泡，空态永不可达），样式一并清掉 */
.msg { max-width: 82%; }
.msg p { margin: 0; font-size: 13.5px; line-height: 1.7; white-space: pre-wrap; overflow-wrap: break-word; }
.msg.guide {
  align-self: flex-start;
  background: var(--paper2);
  border: 1px solid var(--line);
  border-radius: 4px 12px 12px 12px;
  padding: 10px 13px;
}
.msg.me {
  align-self: flex-end;
  background: #FBE9E2;
  border: 1px solid var(--red);
  border-radius: 12px 4px 12px 12px;
  padding: 10px 13px;
}
.msg.error {
  align-self: stretch;
  background: #FBE9E2;
  border: 1px dashed var(--red);
  border-radius: var(--paper-r-md, 10px);
  padding: 10px 13px;
  color: var(--red);
  font-size: 13px;
}
.msg .who { font-size: 11px; color: var(--ink2); margin-bottom: 4px; }

/* 思考中省略号：只用 opacity（60fps） */
.typing { display: inline-flex; gap: 5px; padding: 6px 0; }
.typing i {
  width: 7px;
  height: 7px;
  border-radius: var(--paper-r-full, 50%);
  background: var(--ink2);
  animation: blink 1.2s infinite;
}
.typing i:nth-child(2) { animation-delay: 0.2s; }
.typing i:nth-child(3) { animation-delay: 0.4s; }
@keyframes blink { 0%, 100% { opacity: 0.25; } 50% { opacity: 1; } }

/* 输入条 */
.inputbar {
  display: flex;
  gap: 8px;
  margin: 14px 20px 0;
}
.inputbar input {
  flex: 1;
  min-width: 0;
  font-size: 16px;
  padding: 12px 14px;
  border: 1.5px solid var(--line);
  border-radius: var(--paper-r-md, 10px);
  background: var(--paper-card);
  color: var(--ink);
}
.inputbar button {
  flex-shrink: 0;
  background: var(--red);
  color: var(--on-dark, #fff);
  border: none;
  font-size: 14px;
  font-weight: 800;
  padding: 0 18px;
  /* R41：补上 min-height:44px —— 原只有 padding，瘦机型上高度差一截 */
  min-height: 44px;
  border-radius: var(--paper-r-md, 10px);
  box-shadow: 0 3px 0 var(--red-deep, #a33319);
  cursor: pointer;
}
.inputbar button:active { transform: translateY(2px); box-shadow: none; }
.inputbar button:disabled { background: var(--ink2); box-shadow: none; cursor: default; }

.tip { font-size: 12px; color: var(--ink2); }
/* R45/R54：求救信号入口（跳 /game/signal）；独立整行按钮块 + 44px 触控高度，不再用负 margin 挤压相邻文案 */
.signal-link {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  min-height: 44px;
  margin-top: 6px;
  background: none;
  border: none;
  color: var(--gold);
  font-size: 13px;
  font-weight: 700;
  text-decoration: underline;
  text-underline-offset: 2px;
  cursor: pointer;
}

.chip:focus-visible,
.v4-retry:focus-visible,
.signal-link:focus-visible,
.inputbar input:focus-visible,
.inputbar button:focus-visible {
  outline: 2px solid var(--red);
  outline-offset: 2px;
}

/* 减弱动效：按下不再位移 */
@media (prefers-reduced-motion: reduce) {
  .chip:active, .inputbar button:active { transform: none; }
  /* R41：打字省略号呼吸动画在减弱动态下关闭，留半透明静态点 */
  .typing i { animation: none; opacity: 0.6; }
}

/* R107-3：读屏播报节点（视觉隐藏），口径同 CalendarView R48B-14 */
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
</style>
