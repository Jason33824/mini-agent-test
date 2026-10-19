<script setup lang="ts">
import { computed } from 'vue'
import type { Artifact, ArtifactKind } from '../types'

const props = defineProps<{ artifacts: Artifact[] }>()

interface CoverageRow {
  area?: string
  risk?: string
  covered_by?: string[]
}
interface EvidenceItem {
  source?: string
  finding?: string
  risk_hint?: string
}
interface CaseItem {
  title?: string
  priority?: string
  data?: string
  assertion?: string
  design_ref?: string
}
interface ReportResult {
  title?: string
  passed?: boolean
  detail?: string
}

// latest attempt per kind wins (revise rounds append rows with :a{n} keys)
const latest = computed<Partial<Record<ArtifactKind, Artifact>>>(() => {
  const byKind: Partial<Record<ArtifactKind, Artifact>> = {}
  for (const a of props.artifacts) byKind[a.kind] = a
  return byKind
})

const design = computed(
  () => latest.value.design?.content as { scope?: string[]; risks?: string[]; coverage_matrix?: CoverageRow[] } | undefined,
)
const evidence = computed(
  () => latest.value.evidence?.content as { items?: EvidenceItem[] } | undefined,
)
const cases = computed(
  () => latest.value.cases?.content as { items?: CaseItem[] } | undefined,
)
const report = computed(
  () => latest.value.report?.content as { results?: ReportResult[] } | undefined,
)

const hasAny = computed(() => Object.keys(latest.value).length > 0)
</script>

<template>
  <p v-if="!hasAny" class="hint">No artifacts yet — they appear as agents produce them.</p>

  <div v-if="design" class="artifact">
    <h3>Design <span class="hint">test-design agent</span></h3>
    <p><strong>scope:</strong> {{ (design.scope ?? []).join(', ') }}</p>
    <p><strong>risks:</strong> {{ (design.risks ?? []).join(', ') }}</p>
    <table v-if="design.coverage_matrix?.length">
      <thead><tr><th>area</th><th>risk</th><th>covered_by</th></tr></thead>
      <tbody>
        <tr v-for="row in design.coverage_matrix" :key="row.area">
          <td>{{ row.area }}</td><td>{{ row.risk }}</td>
          <td>{{ (row.covered_by ?? []).join(', ') || '—' }}</td>
        </tr>
      </tbody>
    </table>
  </div>

  <div v-if="evidence" class="artifact">
    <h3>Whitebox evidence <span class="hint">whitebox agent</span></h3>
    <table v-if="evidence.items?.length">
      <thead><tr><th>source</th><th>finding</th><th>risk hint</th></tr></thead>
      <tbody>
        <tr v-for="(item, i) in evidence.items" :key="i">
          <td>{{ item.source }}</td><td>{{ item.finding }}</td><td>{{ item.risk_hint }}</td>
        </tr>
      </tbody>
    </table>
  </div>

  <div v-if="cases" class="artifact">
    <h3>Test cases <span class="hint">test-case agent — every row links back to a design anchor</span></h3>
    <table v-if="cases.items?.length">
      <thead><tr><th>title</th><th>P</th><th>data</th><th>assertion</th><th>design_ref</th></tr></thead>
      <tbody>
        <tr v-for="c in cases.items" :key="c.title">
          <td>{{ c.title }}</td><td>{{ c.priority }}</td><td>{{ c.data }}</td>
          <td>{{ c.assertion }}</td><td><code>{{ c.design_ref }}</code></td>
        </tr>
      </tbody>
    </table>
  </div>

  <div v-if="report" class="artifact">
    <h3>Execution report <span class="hint">test-execution agent</span></h3>
    <table v-if="report.results?.length">
      <thead><tr><th>title</th><th>passed</th><th>detail</th></tr></thead>
      <tbody>
        <tr v-for="(r, i) in report.results" :key="i">
          <td>{{ r.title }}</td>
          <td :class="r.passed ? 'pass' : 'fail'">{{ r.passed ? 'PASS' : 'FAIL' }}</td>
          <td>{{ r.detail }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
