/**
 * Bedrock crossplay store.
 *
 * One snapshot per server, ref-counted polling. While a setup/teardown worker
 * is running the poll runs fast (phase updates every step); otherwise it idles
 * so the settings/overview pages cost almost nothing. `enable`/`disable`
 * return the fresh snapshot themselves, so the UI reacts before the next tick.
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { disableBedrock, enableBedrock, getBedrockStatus } from '../api/bedrock'
import { t } from '../i18n'

const ACTIVE_POLL_MS = 1500
const IDLE_POLL_MS = 10000

export const useBedrockStore = defineStore('bedrock', () => {
  // ---------- State ----------
  const serverId = ref(null)
  const snapshot = ref(null)
  // Last transport/refusal error. Worker failures arrive in the snapshot's
  // `error` field instead; the getter below merges both for the UI.
  const error = ref(null)
  const busy = ref(false)

  // Internal — ref-counted poller and an in-flight guard so a fast page mount
  // can't stack two requests.
  const _consumers = ref(0)
  let _timer = null
  let _inFlight = null

  // ---------- Getters ----------
  const supported = computed(() => snapshot.value?.supported === true)
  const enabled = computed(() => snapshot.value?.enabled === true)
  const active = computed(() => snapshot.value?.active === true)
  const phase = computed(() => snapshot.value?.phase ?? null)
  const plugin = computed(() => snapshot.value?.plugin ?? null)
  const pluginVersion = computed(() => snapshot.value?.plugin_version ?? null)
  const errorMessage = computed(() => error.value || snapshot.value?.error || null)
  const address = computed(() => snapshot.value?.address ?? null)
  const reason = computed(() => snapshot.value?.reason ?? null)
  const reasonMessage = computed(() => snapshot.value?.reason_message ?? null)
  const serverRunning = computed(() => snapshot.value?.server_running === true)
  const playitStatus = computed(() => snapshot.value?.playit_status ?? null)
  const playitReady = computed(() => snapshot.value?.playit_ready === true)
  const clientPort = computed(() => snapshot.value?.client_port ?? 19132)
  // The backend already folds "supported / not working / not running" into
  // `manageable`; `busy` only covers the request that has not answered yet.
  const canToggle = computed(() => supported.value && snapshot.value?.manageable === true && !busy.value)

  // ---------- Internals ----------
  function _applySnapshot(data) {
    if (!data || typeof data !== 'object') return
    snapshot.value = data
  }

  function _schedule() {
    if (_timer !== null) {
      clearTimeout(_timer)
      _timer = null
    }
    const delay = active.value ? ACTIVE_POLL_MS : IDLE_POLL_MS
    _timer = setTimeout(async () => {
      _timer = null
      await fetch()
      if (_consumers.value > 0) _schedule()
    }, delay)
  }

  // ---------- Actions ----------
  async function fetch() {
    const id = serverId.value
    if (!id) return
    if (_inFlight) return _inFlight
    const promise = (async () => {
      try {
        const data = await getBedrockStatus(id)
        if (serverId.value === id) {
          _applySnapshot(data)
          error.value = null
        }
      } catch (err) {
        // Keep the last good snapshot — a hiccup should not blank the panel.
        if (serverId.value === id) error.value = err?.message || t('errors.networkShort')
      } finally {
        _inFlight = null
      }
    })()
    _inFlight = promise
    return promise
  }

  /** Point the store at a server. Cheap no-op when it is already current. */
  async function setServer(id) {
    const next = id ? String(id) : null
    if (next === serverId.value) {
      if (next && !snapshot.value) await fetch()
      return
    }
    serverId.value = next
    snapshot.value = null
    error.value = null
    if (next) await fetch()
  }

  async function enable() {
    if (!serverId.value || busy.value) return false
    busy.value = true
    error.value = null
    try {
      _applySnapshot(await enableBedrock(serverId.value))
      _schedule()
      return true
    } catch (err) {
      error.value = err?.message || t('errors.networkShort')
      return false
    } finally {
      busy.value = false
    }
  }

  async function disable() {
    if (!serverId.value || busy.value) return false
    busy.value = true
    error.value = null
    try {
      _applySnapshot(await disableBedrock(serverId.value))
      _schedule()
      return true
    } catch (err) {
      error.value = err?.message || t('errors.networkShort')
      return false
    } finally {
      busy.value = false
    }
  }

  /**
   * Subscribe to polling. Returns an unsubscribe function — every component
   * that subscribes must release it (onUnmounted) or the poller outlives it.
   */
  function subscribe() {
    _consumers.value += 1
    if (_consumers.value === 1) {
      if (serverId.value) fetch()
      _schedule()
    }
    let released = false
    return function unsubscribe() {
      if (released) return
      released = true
      _consumers.value = Math.max(0, _consumers.value - 1)
      if (_consumers.value === 0 && _timer !== null) {
        clearTimeout(_timer)
        _timer = null
      }
    }
  }

  return {
    // state
    serverId, snapshot, error, busy,
    // getters
    supported, enabled, active, phase, plugin, pluginVersion, errorMessage,
    address, reason, reasonMessage, serverRunning, playitStatus, playitReady,
    clientPort, canToggle,
    // actions
    fetch, setServer, enable, disable, subscribe,
  }
})
