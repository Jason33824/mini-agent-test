<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { api } from './api'
import { useUser } from './composables/useUser'

const { principal, loggedIn, busy, error, login, logout } = useUser()
const username = ref('')
const password = ref('')

const apiUp = ref(true)
let timer: number | undefined

async function probe(): Promise<void> {
  try {
    await api.health()
    apiUp.value = true
  } catch {
    apiUp.value = false
  }
}

async function submit(): Promise<void> {
  if (username.value && password.value) await login(username.value, password.value)
}

onMounted(() => {
  void probe()
  timer = window.setInterval(() => void probe(), 30_000)
})
onUnmounted(() => window.clearInterval(timer))
</script>

<template>
  <!-- auth gate: the SPA renders only with a live token (ADR 0003) -->
  <main v-if="!loggedIn" class="login-wrap">
    <form class="login-card" @submit.prevent="submit">
      <h1 class="brand">mini-agent-test</h1>
      <p class="hint">
        {{ apiUp ? 'sign in — seeded users: alice / bob / carol' : 'API unreachable' }}
      </p>
      <input v-model="username" placeholder="username" autocomplete="username" />
      <input v-model="password" type="password" placeholder="password" autocomplete="current-password" />
      <button :disabled="busy || !username || !password">{{ busy ? 'signing in…' : 'sign in' }}</button>
      <p v-if="error" class="error">{{ error }}</p>
    </form>
  </main>

  <template v-else>
    <header class="topbar">
      <RouterLink to="/tasks" class="brand">mini-agent-test</RouterLink>
      <span class="spacer" />
      <span class="health" :class="apiUp ? 'up' : 'down'" :title="apiUp ? 'api up' : 'api unreachable'" />
      <span class="who">{{ principal!.user_id }}@{{ principal!.tenant_id }}</span>
      <button class="ghost" @click="logout">logout</button>
    </header>
    <!-- re-key on identity: list/detail refetch under the new principal -->
    <main><RouterView :key="principal!.user_id" /></main>
  </template>
</template>
