<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { api } from './api'
import { useUser } from './composables/useUser'

const USERS = ['alice', 'bob', 'carol']

const { id, setUser } = useUser()
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

onMounted(() => {
  void probe()
  timer = window.setInterval(() => void probe(), 30_000)
})
onUnmounted(() => window.clearInterval(timer))
</script>

<template>
  <header class="topbar">
    <RouterLink to="/tasks" class="brand">mini-ai-test-agent-platform</RouterLink>
    <span class="spacer" />
    <span class="health" :class="apiUp ? 'up' : 'down'" :title="apiUp ? 'api up' : 'api unreachable'" />
    <label class="who">
      user
      <select :value="id" @change="setUser(($event.target as HTMLSelectElement).value)">
        <option v-for="u in USERS" :key="u" :value="u">{{ u }}</option>
      </select>
    </label>
  </header>
  <!-- re-key on user switch: list/detail refetch under the new identity -->
  <main><RouterView :key="id" /></main>
</template>
