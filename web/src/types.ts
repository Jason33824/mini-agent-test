export type TaskStatus =
  | 'queued'
  | 'running'
  | 'awaiting_gate'
  | 'completed'
  | 'failed'
  | 'cancelled'

export const TERMINAL_STATUSES: ReadonlySet<string> = new Set([
  'completed',
  'failed',
  'cancelled',
])

export interface Task {
  id: string
  user_id: string
  title: string
  prd: string
  status: TaskStatus
  current_gate: string | null
  outcome: string | null
  error: string | null
  cancel_requested: boolean
  created_at: string
  updated_at: string
}

export type ArtifactKind = 'design' | 'evidence' | 'cases' | 'report'

export interface Artifact {
  id?: string
  task_id?: string
  kind: ArtifactKind
  version: string
  idempotency_key: string
  content: Record<string, unknown>
  created_at?: string
}

export interface GateDecisionIn {
  approved: boolean
  feedback: string
  drop_areas?: string[]
}

export interface TimelineEvent {
  seq: number
  type: string
  payload: Record<string, unknown>
  receivedAt: string
}

export const MAX_PRD_BYTES = 64 * 1024
