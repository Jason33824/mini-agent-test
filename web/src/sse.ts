import { useUser } from './composables/useUser'
import { TERMINAL_STATUSES } from './types'

export interface SseCallbacks {
  onEvent: (type: string, payload: Record<string, unknown>) => void
  /** Reconnect = missed events. The API has no event log, so the consumer
      must refetch a task snapshot BEFORE the next subscription. */
  onResync: () => void
}

const TERMINAL_EVENT_TYPES = new Set(['task.completed'])

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

/**
 * Hand-rolled SSE consumer (fetch + ReadableStream).
 *
 * Why not EventSource: it can neither carry the X-User-Id auth header nor
 * control reconnection. Terminal events close the stream for good; anything
 * else reconnects with exponential backoff and asks the consumer to resync
 * from a snapshot first.
 */
export class TaskEventStream {
  private controller: AbortController | null = null
  private closed = false
  private retry = 0
  private sawTerminal = false

  constructor(
    private readonly taskId: string,
    private readonly callbacks: SseCallbacks,
  ) {}

  start(): void {
    this.closed = false
    void this.loop()
  }

  stop(): void {
    this.closed = true
    this.controller?.abort()
    this.controller = null
  }

  private async loop(): Promise<void> {
    while (!this.closed) {
      if (this.retry > 0) {
        this.callbacks.onResync()
        await sleep(Math.min(2 ** this.retry * 500, 8000))
      }
      this.controller = new AbortController()
      this.sawTerminal = false
      try {
        await this.consume()
        if (this.sawTerminal || this.closed) return // clean end: server closed a terminal stream
        this.retry += 1
      } catch {
        if (this.closed) return // stop() aborted us — not an error
        this.retry += 1
      }
    }
  }

  private async consume(): Promise<void> {
    const headers: Record<string, string> = {
      'X-User-Id': useUser().id.value,
      Accept: 'text/event-stream',
    }
    const resp = await fetch(`/v1/tasks/${this.taskId}/events`, {
      headers,
      signal: this.controller!.signal,
    })
    if (!resp.ok || !resp.body) throw new Error(`SSE ${resp.status}`)

    const reader = resp.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let boundary = buffer.indexOf('\n\n')
      while (boundary !== -1) {
        this.handleFrame(buffer.slice(0, boundary))
        buffer = buffer.slice(boundary + 2)
        boundary = buffer.indexOf('\n\n')
      }
    }
  }

  private handleFrame(frame: string): void {
    let type = 'message'
    const dataLines: string[] = []
    for (const line of frame.split('\n')) {
      if (line.startsWith(':')) continue // keepalive comment frame
      if (line.startsWith('event:')) type = line.slice(6).trim()
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
    }
    if (dataLines.length === 0) return

    const payload = JSON.parse(dataLines.join('\n')) as Record<string, unknown>
    const status = typeof payload.status === 'string' ? payload.status : null
    const terminal =
      TERMINAL_EVENT_TYPES.has(type) || (status !== null && TERMINAL_STATUSES.has(status))

    this.callbacks.onEvent(type, payload)
    if (terminal) {
      this.sawTerminal = true
      this.stop()
    }
  }
}
