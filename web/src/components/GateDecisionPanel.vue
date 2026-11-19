<script setup lang="ts">
import { computed, ref } from 'vue'
import type { Artifact, DispositionItem, GateDecisionIn } from '../types'

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

const matrix = computed<{ area?: string; risk?: string }[]>(
  () => (underReview.value?.content.coverage_matrix as { area?: string; risk?: string }[] | undefined) ?? [],
)

// design's answer to each evidence item (DESIGN §12), filled at the merge
// fan-in and shipped inside the design artifact the gate reviews
const dispositions = computed<DispositionItem[]>(() => {
  if (props.gate !== 'design') return []
  return (underReview.value?.content.dispositions as DispositionItem[] | undefined) ?? []
})

// the human arbitrates only the contested rows still awaiting review. Group
// findings by area because drop_areas is an area-level veto; evidence_id stays
// the unique key for each finding's reason inside a group.
const contested = computed(() =>
  dispositions.value.filter((d) => d.decision === 'rebut' && d.review === 'pending'),
)
const contestedAreas = computed(() => {
  const groups = new Map<string, DispositionItem[]>()
  for (const item of contested.value) groups.set(item.source, [...(groups.get(item.source) ?? []), item])
  return [...groups.entries()].map(([source, items]) => ({ source, items }))
})
const integratedCount = computed(() => dispositions.value.filter((d) => d.decision === 'integrate').length)
const reviewedCount = computed(
  () => dispositions.value.filter((d) => d.review === 'kept' || d.review === 'dropped').length,
)

// fallback for a legacy artifact without dispositions: veto any evidence area
const areas = computed<string[]>(() => {
  if (props.gate !== 'design' || dispositions.value.length) return []
  return matrix.value.map((row) => row.area ?? '').filter(Boolean)
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
    // upholding a rebuttal = veto the area; on approval the orchestrator drops
    // it from the design that flows forward (no revise round needed)
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

    <div v-if="contestedAreas.length" class="disagreement">
      <span class="hint">
        Agent disagreement — design rebutted {{ contested.length }} evidence finding(s).
        Arbitrate the contested areas: uphold a rebuttal to drop the area, or approve to keep it.
      </span>
      <div v-for="group in contestedAreas" :key="group.source" class="contested">
        <label class="check">
          <input type="checkbox" :checked="dropAreas.includes(group.source)" @change="toggle(group.source)" />
          <strong>{{ group.source }}</strong>
          <span v-if="group.items.length > 1">({{ group.items.length }} findings)</span>
          — uphold rebuttal (drop area)
        </label>
        <p v-for="d in group.items" :key="d.evidence_id" class="hint">{{ d.reason }}</p>
      </div>
    </div>
    <p v-else-if="reviewedCount" class="hint">
      {{ reviewedCount }} contested finding(s) already arbitrated — see the Disposition card for the audit record.
    </p>
    <p v-else-if="integratedCount" class="hint">
      Agents agree — all {{ integratedCount }} evidence row(s) integrated; no contested rows to arbitrate.
    </p>
    <div v-else-if="areas.length" class="veto">
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