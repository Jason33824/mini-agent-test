<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api, describeError } from '../api'
import { TaskEventStream } from '../sse'
import type { Artifact, GateDecisionIn, Task, TimelineEvent } from '../types'
import GateDecisionPanel from '../components/GateDecisionPanel.vue'
import ArtifactPanel from '../components/ArtifactPanel.vue'

const props = defineProps<{ id: string }>()

const task = ref<Task | null>(null)
const artifacts = ref<Artifact[]>([])
const timeline = ref<TimelineEvent[]>([])
const error = ref('')
let stream: TaskEventStream | null = null
let seq = 0

const isTerminal = computed(
  () => task.value !== null && ['completed', 'failed', 'cancelled'].includes(task.value.status),
)

async function snapshot(): Promise<void> {
  try {
    task.value = (await api.getTask(props.id)).task
    artifacts.value = (await api.listArtifacts(props.id)).artifacts
    error.value = ''
  } catch (err) {
    error.value = describeError(err)
  }
}

function upsertArtifact(incoming: Artifact): void {
  const rest = artifacts.value.filter((a) => a.idempotency_key !== incoming.idempotency_key)
  rest.push(incoming)
  artifacts.value = rest
}

async function refreshArtifacts(): Promise<void> {
  try {
    artifacts.value = (await api.listArtifacts(props.id)).artifacts
  } catch {
    // transient listing failure is non-fatal: the next resync retries it
  }
}

function onEvent(type: string, payload: Record<string, unknown>): void {
  timeline.value.push({ seq: seq++, type, payload, receivedAt: new Date().toISOString() })
  if (type === 'task.status' && task.value && typeof payload.status === 'string') {
    task.value.status = payload.status as Task['status']
  }
  if (type === 'gate.request') {
    // the worker ships the artifact under review inside the event payload
    const artifact = payload.artifact as Artifact | undefined
    if (artifact && task.value) {
      task.value.status = 'awaiting_gate'
      task.value.current_gate = typeof payload.stage === 'string' ? payload.stage : null
      upsertArtifact(artifact)
    }
  }
  if (type === 'artifact.created') void refreshArtifacts()
  if (type === 'task.completed' && task.value) {
    task.value.status = 'completed'
    task.value.current_gate = null
    task.value.outcome = typeof payload.outcome === 'string' ? payload.outcome : null
  }
}

async function decide(gate: string, decision: GateDecisionIn): Promise<void> {
  try {
    await api.decideGate(props.id, gate, decision)
    await snapshot() // decision recorded → resuming; SSE carries the rest
  } catch (err) {
    error.value = describeError(err)
  }
}

async function cancel(): Promise<void> {
  try {
    await api.cancel(props.id)
    await snapshot()
  } catch (err) {
    error.value = describeError(err)
  }
}

onMounted(() => {
  void snapshot() // first paint from state; the stream only carries deltas
  stream = new TaskEventStream(props.id, {
    onEvent,
    onResync: () => void snapshot(),
  })
  stream.start()
})
onUnmounted(() => stream?.stop())
</script>

<template>
  <section v-if="task" class="grid two">
    <div class="card">
      <h2>
        {{ task.title }}
        <span class="badge" :class="task.status">{{ task.status }}</span>
        <span v-if="task.current_gate" class="badge pending">{{ task.current_gate }} gate</span>
      </h2>
      <p v-if="task.outcome" class="hint">outcome: {{ task.outcome }}</p>
      <p v-if="task.error" class="error">{{ task.error }}</p>

      <GateDecisionPanel
        v-if="task.status === 'awaiting_gate' && task.current_gate"
        :gate="task.current_gate"
        :artifacts="artifacts"
        @decide="decide"
      />

      <button
        v-if="!isTerminal && !task.cancel_requested"
        class="danger"
        @click="cancel"
      >
        Cancel task
      </button>
      <p v-else-if="task.cancel_requested" class="hint">
        Cancellation requested — stopping at the next boundary…
      </p>

      <p v-if="error" class="error">{{ error }}</p>
    </div>

    <div class="card">
      <h2>Timeline <span class="hint">live via SSE</span></h2>
      <p v-if="timeline.length === 0" class="hint">Waiting for events…</p>
      <ol class="timeline">
        <li v-for="e in timeline" :key="e.seq">
          <code>{{ e.type }}</code>
          <span class="hint">{{ e.receivedAt.slice(11, 19) }}</span>
          <pre>{{ JSON.stringify(e.payload, null, 2) }}</pre>
        </li>
      </ol>
    </div>

    <div class="card wide">
      <h2>Artifacts</h2>
      <ArtifactPanel :artifacts="artifacts" />
    </div>
  </section>
</template>
