import type { GatewayClient } from '../gatewayClient.js'

import { asRpcResult } from './rpc.js'
import { detectTerminalGraphics, type TerminalGraphics } from './terminalGraphics.js'

export interface PetMetaResult {
  enabled?: boolean
  scale?: number
  slug?: string
  spritesheetRevision?: string
}

interface PetUpdate<TCells> {
  cells: TCells | null
  meta: PetMetaResult
}

type PetGateway = Pick<GatewayClient, 'request'>

/**
 * This process's terminal, detected once: `pet.cells` is answered by the gateway
 * owner, which has no `TERM`/`TERM_PROGRAM` of its own, so `render_mode: auto`
 * only reaches native pixels when the client says what it is attached to.
 */
const CLIENT_GRAPHICS = detectTerminalGraphics()

/**
 * Suppress overlapping cosmetic polls so a slow gateway can never accumulate
 * a queue of pet requests. Returning false tells callers that an existing
 * probe is still in flight.
 */
export function createPetSingleFlight() {
  let active = false

  return async (operation: () => Promise<void>): Promise<boolean> => {
    if (active) {
      return false
    }

    active = true

    try {
      await operation()

      return true
    } finally {
      active = false
    }
  }
}

/**
 * Probe cheap pet metadata on the gateway reader thread, then request the
 * expensive frame payload only when the active selection/state is not cached.
 * This deliberately bypasses the transcript-logging RPC wrapper: pet display
 * is cosmetic, so an unavailable gateway must not print an error.
 *
 * `protocol` is this client's own terminal detection: the gateway owner has no
 * `TERM`, so without it `render_mode: auto` degrades to half-blocks everywhere
 * (#144619). It defaults to this process's own answer and always rides the
 * request, so the owner never has to guess.
 */
export async function requestPetUpdate<TCells>(
  gateway: PetGateway,
  state: string,
  graphics: boolean,
  needsCells: (meta: PetMetaResult) => boolean,
  protocol: TerminalGraphics = CLIENT_GRAPHICS
): Promise<PetUpdate<TCells> | null> {
  try {
    const meta = asRpcResult(await gateway.request('pet.info.meta')) as PetMetaResult | null

    if (!meta) {
      return null
    }

    if (!meta.enabled || !needsCells(meta)) {
      return { cells: null, meta }
    }

    const cells = asRpcResult(await gateway.request('pet.cells', { graphics, protocol, state })) as TCells | null

    return { cells, meta }
  } catch {
    return null
  }
}
