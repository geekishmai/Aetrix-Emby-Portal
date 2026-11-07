<script setup lang="ts">
import { RouterLink, useRouter } from 'vue-router'
import { ArrowLeft, Clapperboard, Home } from 'lucide-vue-next'
import { branding } from '@/composables/useBranding'

const router = useRouter()

function goBack() {
  if (window.history.length > 1) router.back()
  else void router.push('/')
}
</script>

<template>
  <main class="v4-scope nf-page">
    <div class="nf-card">
      <div class="nf-code" aria-hidden="true">404</div>
      <h1 class="nf-title">未找到所访问的页面</h1>
      <p class="nf-desc">
        你访问的链接不存在或已变更位置，可返回上一页或前往{{ branding.site_name }}首页与片库浏览。
      </p>
      <div class="nf-actions">
        <button type="button" class="nf-btn nf-btn-ghost" @click="goBack">
          <ArrowLeft :size="16" aria-hidden="true" />
          <span>返回上一页</span>
        </button>
        <RouterLink to="/" class="nf-btn nf-btn-primary">
          <Home :size="16" aria-hidden="true" />
          <span>回首页</span>
        </RouterLink>
        <RouterLink to="/library" class="nf-btn nf-btn-ghost">
          <Clapperboard :size="16" aria-hidden="true" />
          <span>浏览片库</span>
        </RouterLink>
      </div>
    </div>
  </main>
</template>

<style scoped>
.nf-page {
  min-height: calc(100vh - 64px);
  min-height: calc(100dvh - 64px);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 48px 20px calc(64px + env(safe-area-inset-bottom));
  background: #faf8f5;
}

.nf-card {
  width: min(520px, 100%);
  background: #fdfbfa;
  border: 1px solid #d1d1cd;
  border-radius: 16px;
  padding: 40px 32px;
  text-align: center;
}

.nf-code {
  margin: 0 0 10px;
  font-size: clamp(3.5rem, 12vw, 5.25rem);
  font-weight: 500;
  line-height: 1;
  color: #016a71;
  font-variant-numeric: tabular-nums;
}

.nf-title {
  margin: 0 0 10px;
  font-size: 22px;
  font-weight: 500;
  line-height: 1.35;
  color: #27251e;
}

.nf-desc {
  max-width: 38ch;
  margin: 0 auto 28px;
  font-size: 15px;
  line-height: 1.6;
  color: #524b42;
}

.nf-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 10px;
}

.nf-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  min-height: 44px;
  padding: 0 18px;
  border-radius: 12px;
  font-size: 14px;
  font-weight: 500;
  font-family: inherit;
  text-decoration: none;
  cursor: pointer;
  transition: opacity 0.16s ease;
}

.nf-btn-primary {
  background: #016a71;
  color: #ffffff;
  border: 1px solid #016a71;
}

.nf-btn-primary:hover {
  opacity: 0.9;
}

.nf-btn-ghost {
  background: #faf8f5;
  color: #27251e;
  border: 1px solid #d1d1cd;
}

.nf-btn-ghost:hover {
  border-color: #016a71;
  color: #016a71;
}

@media (prefers-reduced-motion: reduce) {
  .nf-btn {
    transition: none;
  }
}
</style>
