import type * as HermesSdk from '@hermes/plugin-sdk'
import { host } from '@hermes/plugin-sdk'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { WritableAtom } from 'nanostores'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { $canonicalGroupBindings } from './canonical-group-registry'
import { $botMeta } from './data'
import { $groupChats, $groupChatWorkspace } from './group-chat'
import type * as GroupChatParts from './group-chat-parts'
import { GroupChatWorkspace } from './group-chat-view'
import { translateBots } from './i18n-test-helper'

// #143241: the gate's Start button stayed armed after a successful create, so a
// second click sent another `groups.create` and made a second room. The create
// handler bound the room by its `canonical:` key only — it never published the
// room for the NAME this tab was opened with, so this tab kept rendering the
// gate with `lookup.status === 'missing'`.
const { request, notify, openWorkspace, catalog } = vi.hoisted(() => ({
  request: vi.fn(), notify: vi.fn(), openWorkspace: vi.fn(), catalog: { patch: null as null | ((t: Record<string, unknown>) => unknown) }
}))
vi.mock('@hermes/plugin-sdk', async importOriginal => {
  const sdk = await importOriginal<typeof HermesSdk>()
  const { en } = await import('@/i18n/en')

  return {
    ...sdk,
    gatewayActivationEpoch: () => 1,
    host: {
      ...sdk.host, requestProfile: request, notify, openWorkspace,
      request: (method: string, params?: Record<string, unknown>) => request(null, method, params),
      connections: vi.fn(async () => []),
      state: {
        ...sdk.host.state,
        connectionId: sdk.atom<string | null>('local'),
        profile: sdk.atom('default'),
        gateway: sdk.atom('open')
      }
    },
    useI18n: () => ({ locale: 'en', t: catalog.patch ? catalog.patch(en as unknown as Record<string, unknown>) : en }),
    usePluginI18n: () => translateBots
  }
})
vi.mock('./group-chat-parts', async importOriginal => ({
  ...await importOriginal<typeof GroupChatParts>(),
  GroupImageControls: () => null
}))

const state = {
  connectionId: host.state.connectionId as WritableAtom<string | null>,
  profile: host.state.profile as WritableAtom<string>,
  gateway: host.state.gateway as WritableAtom<string>
}

const roster = [{ name: 'alpha', connectionId: 'local' }, { name: 'beta', connectionId: 'local' }]
const members = roster.map(m => ({ member_id: m.name, profile: m.name, handle: m.name }))
const startButton = () => screen.queryByRole('button', { name: 'Start gateway group' }) as HTMLButtonElement | null

beforeEach(() => {
  catalog.patch = null
  state.connectionId.set('local')
  state.profile.set('default')
  state.gateway.set('open')
  $canonicalGroupBindings.set({})
  $groupChats.set({})
  $groupChatWorkspace.set(null)
  $botMeta.set({})
  request.mockReset()
  notify.mockReset()
  openWorkspace.mockReset().mockReturnValue(() => undefined)
  Element.prototype.scrollIntoView = vi.fn()
})
afterEach(() => {
  cleanup()
  localStorage.clear()
  vi.restoreAllMocks()
})

/** A gateway that holds no room named "Team" (the legacy-room gate), records the
 *  room ids `groups.create` was asked for, and can hold a create answer back. */
function gateway({ list = () => ({ rooms: [], next_offset: null }) as unknown, hold = false } = {}) {
  const created: string[] = []
  let release: (room: unknown) => void = () => undefined
  const held = new Promise(resolve => { release = resolve })
  request.mockImplementation(async (_route, method, params) => {
    if (method === 'groups.capabilities') {return { driver: true, persistent_process: true }}

    if (method === 'groups.list') {return list()}

    if (method === 'groups.create') {
      const room = { room_id: params.room_id, name: params.name, members: params.members }

      created.push(params.room_id)

      return hold ? held : { room }
    }

    if (method === 'groups.state') {return { room: { room_id: params.room_id, name: 'Team', members, latest_seq: 0 }, driver_status: {} }}

    if (method === 'groups.log') {return { events: [] }}

    throw new Error(`Unexpected RPC: ${method} ${String(params)}`)
  })

  return { created, release: (room: unknown) => release({ room }) }
}

const methods = () => request.mock.calls.map(call => call[1])
const roomStates = () => request.mock.calls.filter(call => call[1] === 'groups.state').map(call => call[2]?.room_id)

it('a successful Start turns this tab into the room and stops offering Start', async () => {
  const { created } = gateway()
  await act(async () => { render(<GroupChatWorkspace group="Team" members={roster} />) })

  const start = startButton()
  expect(start?.disabled).toBe(false)
  await act(async () => { fireEvent.click(start as HTMLButtonElement) })

  expect(created).toHaveLength(1)
  // The tab followed the room it created: the gate is gone and the room is rendering.
  expect(startButton()).toBeNull()
  expect(roomStates()).toEqual([created[0]])
})

it('a second Start cannot make a second room', async () => {
  const { created } = gateway()
  await act(async () => { render(<GroupChatWorkspace group="Team" members={roster} />) })

  const first = startButton()
  await act(async () => { fireEvent.click(first as HTMLButtonElement) })
  // A user who pressed Start again before the tab repainted got a second room.
  const second = startButton()

  if (second) {await act(async () => { fireEvent.click(second) })}
  expect(created).toHaveLength(1)
})

it('a create that lands after the connection changed publishes no binding for the old route', async () => {
  const { created, release } = gateway({ hold: true })
  await act(async () => { render(<GroupChatWorkspace group="Team" members={roster} />) })
  await act(async () => { fireEvent.click(startButton() as HTMLButtonElement) })
  expect(created).toHaveLength(1)

  await act(async () => { state.connectionId.set('remote') })
  await act(async () => { release({ room_id: created[0], name: 'Team', members }) })

  expect(Object.values($canonicalGroupBindings.get()).some(binding => binding.connectionId === 'local')).toBe(false)
  // The new source's gate is still the authority on its own connection.
  expect((startButton() as HTMLButtonElement | null)?.disabled).toBe(false)
})

it('a name that already has a gateway room is still adopted instead of created', async () => {
  const { created } = gateway({ list: () => ({ rooms: [{ room_id: 'room-original', name: 'Team', members }], next_offset: null }) })
  await act(async () => { render(<GroupChatWorkspace group="Team" members={roster} />) })

  expect(startButton()).toBeNull()
  expect(created).toHaveLength(0)
  expect(roomStates()).toEqual(['room-original'])
})
