import { useUser } from './composables/useUser'
import type { Artifact, GateDecisionIn, Task } from './types'

export class ApiRequestError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message)
  }
}

export function describeError(err: unknown): string {
  if (err instanceof ApiRequestError) {
    if (err.code === 'task_not_found') return 'No such task for this user'
    if (err.code === 'gate_not_open') return 'Gate state changed — refresh and retry'
    if (err.code === 'prd_too_large') return 'PRD exceeds the 64 KiB cap'
    if (err.status === 429) return 'Quota exceeded — try again later'
    return err.message
  }
  return String(err)
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('X-User-Id', useUser().id.value)
  if (init.body != null && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  const resp = await fetch(path, { ...init, headers })
  if (!resp.ok) {
    // unified error envelope: {"error": {code, message, details}}
    let code = 'http_error'
    let message = `HTTP ${resp.status}`
    try {
      const body = (await resp.json()) as { error?: { code?: string; message?: string } }
      if (body?.error?.code) code = body.error.code
      if (body?.error?.message) message = body.error.message
    } catch {
      // non-JSON error body — keep the defaults
    }
    throw new ApiRequestError(resp.status, code, message)
  }
  return (await resp.json()) as T
}

export const api = {
  health: () => request<{ status: string }>('/healthz'),

  listTasks: () => request<{ tasks: Task[] }>('/v1/tasks'),

  getTask: (id: string) => request<{ task: Task }>(`/v1/tasks/${id}`),

  listArtifacts: (id: string) =>
    request<{ artifacts: Artifact[] }>(`/v1/tasks/${id}/artifacts`),

  submitTask: (title: string, prd: string) =>
    request<{ task_id: string; status: string }>('/v1/tasks', {
      method: 'POST',
      body: JSON.stringify({ title, prd }),
    }),

  decideGate: (id: string, gate: string, decision: GateDecisionIn) =>
    request<{ task_id: string; gate: string; decision_id: string; status: string }>(
      `/v1/tasks/${id}/gates/${gate}`,
      { method: 'POST', body: JSON.stringify(decision) },
    ),

  cancel: (id: string) =>
    request<{ task_id: string; result: string }>(`/v1/tasks/${id}/cancel`, {
      method: 'POST',
    }),
}
