<script setup lang="ts">
import { computed, ref } from 'vue'
import type { Artifact, GateDecisionIn } from '../types'

const props = defineProps<{ gate: string; artifacts: Artifact[] }>()
const emit = defineEmits<{ decide: [gate: string, decision: GateDecisionIn] }>()

const approved = ref(true)
const feedback = ref('')
const dropAreas = ref<string[]>([])

// the artifact under review: SSE gate.request ships it, artifact.created
// refresh lands it in the same list — match by the idempotency key segment
const underReview = computed(() =>
  props.artifacts.find(
    (a) => a.idempotency_key.includes(`:${props.gate}:`) || a.idempotency_key.endsWith(`:${props.gate}`),
  ),
)

// veto applies to the design gate: evidence areas the human refuses to
// design for (persistently dropped — revise rounds cannot resurrect them)
const areas = computed<string[]>(() => {
  if (props.gate !== 'design') return []
  const matrix = underReview.value?.content.coverage_matrix as
    | { area?: string }[]
    | undefined
  return (matrix ?? []).map((row) => row.area ?? '').filter(Boolean)
})

function toggle(area: string): void {
  dropAreas.value = dropAreas.value.includes(area)
    ? dropAreas.value.filter((a) => a !== area)
    : [...dropAreas.value, area]
}

function submit(): void {
  emit('decide', props.gate, {
    approved: approved.value,
    feedback: feedback.value,
    drop_areas: approved.value ? dropAreas.value : [],
  })
}
</script>

<template>
  <div class="gatepanel">
    <h3>Gate: {{ gate }}</h3>
    <p v-if="!underReview" class="hint">
      Artifact payload not in this stream — decide from the artifact cards below.
    </p>

    <div v-if="areas.length" class="veto">
      <span class="hint">Veto evidence areas (persistently dropped from the design):</span>
      <label v-for="area in areas" :key="area" class="check">
        <input type="checkbox" :checked="dropAreas.includes(area)" @change="toggle(area)" />
        {{ area }}
      </label>
    </div>

    <label class="radio">
      <input v-model="approved" type="radio" :value="true" /> Approve
    </label>
    <label class="radio">
      <input v-model="approved" type="radio" :value="false" />
      Reject — bounded revise, feedback goes back to the agent
    </label>
    <textarea
      v-model="feedback"
      rows="3"
      :placeholder="approved ? 'Optional note' : 'What must change?'"
    ></textarea>
    <button @click="submit">Submit decision</button>
  </div>
</template>
