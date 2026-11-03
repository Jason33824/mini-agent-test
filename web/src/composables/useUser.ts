import { computed, ref } from 'vue'

// Identity seam (single point for auth — the rest of the app never touches
// credentials): login exchanges seeded credentials for a bearer JWT, kept in
// localStorage; api.ts and sse.ts read the header from here. A 401 anywhere
// upstream means the token died with it — App.vue renders the login panel
// again. No refresh dance: the TTL is the revocation story (ADR 0003).
const TOKEN_KEY = 'mini.token'
const USER_KEY = 'mini.principal'

interface Principal {
  user_id: string
  tenant_id: string
}

const token = ref<string | null>(localStorage.getItem(TOKEN_KEY))
const principal = ref<Principal | null>(
  (() => {
    try {
      return JSON.parse(localStorage.getItem(USER_KEY) ?? 'null') as Principal | null
    } catch {
      return null
    }
  })(),
)

const busy = ref(false)
const error = ref('')

function persist(): void {
  if (token.value) localStorage.setItem(TOKEN_KEY, token.value)
  else localStorage.removeItem(TOKEN_KEY)
  if (principal.value) localStorage.setItem(USER_KEY, JSON.stringify(principal.value))
  else localStorage.removeItem(USER_KEY)
}

export function useUser() {
  return {
    principal: computed(() => principal.value),
    loggedIn: computed(() => token.value !== null && principal.value !== null),
    busy,
    error,
    /** The only place the Authorization header is built (fetch + SSE share it). */
    authHeader(): Record<string, string> {
      return token.value ? { Authorization: `Bearer ${token.value}` } : {}
    },
    async login(username: string, password: string): Promise<boolean> {
      busy.value = true
      error.value = ''
      try {
        const resp = await fetch('/v1/auth/token', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, password }),
        })
        const body = (await resp.json()) as {
          access_token?: string
          user_id?: string
          tenant_id?: string
          error?: { message?: string }
        }
        if (!resp.ok || !body.access_token) {
          error.value = body.error?.message ?? `Login failed (HTTP ${resp.status})`
          return false
        }
        token.value = body.access_token
        principal.value = { user_id: body.user_id!, tenant_id: body.tenant_id! }
        persist()
        return true
      } catch (err) {
        error.value = `Cannot reach the API: ${String(err)}`
        return false
      } finally {
        busy.value = false
      }
    },
    logout(): void {
      token.value = null
      principal.value = null
      persist()
    },
  }
}
