import { ref, watchEffect } from 'vue'

// Identity seam: the id rides the X-User-Id header of every request (src/api.ts).
// When the JWT batch lands, only this module and the header line change —
// components never touch identity directly.
const KEY = 'mini.user'
const id = ref(localStorage.getItem(KEY) ?? 'alice')

watchEffect(() => localStorage.setItem(KEY, id.value))

export function useUser() {
  return {
    id,
    setUser(value: string) {
      id.value = value
    },
  }
}
