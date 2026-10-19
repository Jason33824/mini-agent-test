<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, describeError } from '../api'
import { MAX_PRD_BYTES, type Task, type TaskStatus } from '../types'

const router = useRouter()
const tasks = ref<Task[]>([])
const title = ref('')
const prd = ref('')
const error = ref('')
const busy = ref(false)
const prdBytes = ref(0)

const BADGE: Record<TaskStatus, string> = {
  queued: 'badge queued',
  running: 'badge running',
  awaiting_gate: 'badge gate',
  completed: 'badge completed',
  failed: 'badge failed',
  cancelled: 'badge cancelled',
}

function measure(): void {
  prdBytes.value = new Blob([prd.value]).size
}

async function refresh(): Promise<void> {
  try {
    tasks.value = (await api.listTasks()).tasks
  } catch (err) {
    error.value = describeError(err)
  }
}

async function submit(): Promise<void> {
  error.value = ''
  if (!title.value.trim()) {
    error.value = 'Title is required'
    return
  }
  if (prdBytes.value > MAX_PRD_BYTES) {
    error.value = `PRD exceeds the ${MAX_PRD_BYTES / 1024} KiB cap`
    return
  }
  busy.value = true
  try {
    const created = await api.submitTask(title.value.trim(), prd.value)
    await router.push(`/tasks/${created.task_id}`)
  } catch (err) {
    error.value = describeError(err)
  } finally {
    busy.value = false
  }
}

onMounted(refresh)
</script>

<template>
  <section class="grid two">
    <div class="card">
      <h2>Submit a task</h2>
      <label>Title
        <input v-model="title" placeholder="checkout refund edge cases" />
      </label>
      <label>PRD
        <textarea
          v-model="prd"
          rows="8"
          placeholder="Paste the requirement text…"
          @input="measure"
        ></textarea>
      </label>
      <p class="hint" :class="{ over: prdBytes > MAX_PRD_BYTES }">
        {{ prdBytes }} / {{ MAX_PRD_BYTES }} bytes
      </p>
      <p v-if="error" class="error">{{ error }}</p>
      <button :disabled="busy" @click="submit">{{ busy ? 'Submitting…' : 'Submit' }}</button>
    </div>

    <div class="card">
      <h2>My tasks</h2>
      <p v-if="tasks.length === 0" class="hint">Nothing yet — submit the first task.</p>
      <ul class="tasklist">
        <li v-for="t in tasks" :key="t.id">
          <RouterLink :to="`/tasks/${t.id}`" class="taskrow">
            <span class="title">{{ t.title }}</span>
            <span :class="BADGE[t.status]">{{ t.status }}</span>
            <span v-if="t.current_gate" class="badge pending">{{ t.current_gate }} awaiting</span>
          </RouterLink>
        </li>
      </ul>
    </div>
  </section>
</template>
