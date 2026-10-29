import type * as HermesSdk from '@hermes/plugin-sdk'
import { host } from '@hermes/plugin-sdk'
import { act, cleanup, render, screen } from '@testing-library/react'
import type { WritableAtom } from 'nanostores'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { $canonicalGroupBindings, CanonicalGroupList } from './canonical-group-registry'
import { translateBots } from './i18n-test-helper'

// #145410: the group list's discovery effect depends on the connection, profile, refresh counter and
// the list revision — never on the gateway socket. A list mounted while the local gateway was still
// starting therefore issued groups.capabilities against a closed socket, recorded the failure, and
// kept that startup error after the SAME gateway opened, because nothing re-ran the effect.
const { request, notify, openWorkspace, catalog } = vi.hoisted(() => ({
  request: vi.fn(),
  notify: vi.fn(),
  openWorkspace: vi.fn(),
  catalog: { patch: null as null | ((t: Record<string, unknown>) => unknown) }
}))
vi.mock('@hermes/plugin-sdk', async importOriginal => {
  const sdk = await importOriginal<typeof HermesSdk>()
  const { en } = await import('@/i18n/en')

  return {
    ...sdk,
    gatewayActivationEpoch: () => 1,
    useGatewayActivationEpoch: () => 1,
    host: {
      ...sdk.host,
      requestProfile: request,
      notify,
      openWorkspace,
      request: (method: string, params?: Record<string, unknown>) => request(null, method, params),
      connections: vi.fn(async () => []),
      state: {
        ...sdk.host.state,
        connectionId: sdk.atom<string | null>('local'),
        profile: sdk.atom('default'),
        // Mounted the way Bot mode mounts: before the gateway socket is open.
        gateway: sdk.atom('connecting')
      }
    },
    useI18n: () => ({ locale: 'en', t: catalog.patch ? catalog.patch(en as unknown as Record<string, unknown>) : en }),
    usePluginI18n: () => translateBots
  }
})

const state = { gateway: host.state.gateway as WritableAtom<string> }
const members = [{ member_id: 'alpha', profile: 'alpha', handle: 'alpha' }]
const methods = () => request.mock.calls.map(call => call[1])

beforeEach(() => {
  catalog.patch = null
  state.gateway.set('connecting')
  $canonicalGroupBindings.set({})
  request.mockReset()
  notify.mockReset()
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

it('does not read groups while the gateway is not open, and loads them once it opens', async () => {
  request.mockImplementation(async (_route, method) => {
    if (method === 'groups.capabilities') {
      return { driver: true, persistent_process: true }
    }

    if (method === 'groups.list') {
      return { rooms: [{ room_id: 'room-team', name: 'Team', members }], next_offset: null }
    }

    throw new Error(`Unexpected RPC: ${method}`)
  })

  await act(async () => {
    render(<CanonicalGroupList onOpen={() => undefined} />)
  })
  // A closed socket is not a discovery failure: no RPC, so nothing to report either.
  expect(methods()).toEqual([])
  expect(screen.queryByRole('alert')).toBeNull()

  // Opening the SAME connection and profile is what the effect used to ignore.
  await act(async () => {
    state.gateway.set('open')
  })
  expect(methods()).toContain('groups.capabilities')
  expect(methods()).toContain('groups.list')
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.getByText('Team')).toBeTruthy()
})
