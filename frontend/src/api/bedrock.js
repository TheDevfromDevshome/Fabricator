/**
 * Bedrock crossplay API (Geyser + Floodgate + playit.gg NetherNet).
 *
 * `enable`/`disable` answer with the same snapshot `getBedrockStatus` returns,
 * so the toggle handler can paint immediately and then let the poller follow
 * the worker thread through its phases.
 */

import { get, post } from './client'

export function getBedrockStatus(serverId) {
  return get(`/api/servers/${serverId}/bedrock`)
}

/** Worker progress only — the status snapshot already carries phase/error. */
export function getBedrockProgress(serverId) {
  return get(`/api/servers/${serverId}/bedrock/progress`)
}

export function enableBedrock(serverId) {
  return post(`/api/servers/${serverId}/bedrock/enable`)
}

export function disableBedrock(serverId) {
  return post(`/api/servers/${serverId}/bedrock/disable`)
}
