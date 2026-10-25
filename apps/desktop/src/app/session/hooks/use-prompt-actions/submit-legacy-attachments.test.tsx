import { cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { getLatestSessionMessages } from '@/hermes'
import type { ComposerAttachment } from '@/store/composer'

import { actRender, Harness, type HarnessHandle, RUNTIME_SESSION_ID } from './index-test-utils'

vi.mock('@/hermes', () => ({
  getLatestSessionMessages: vi.fn(async () => ({ messages: [], session_id: 'session' })),
  getProfiles: vi.fn(async () => ({ profiles: [] })),
  getSession: vi.fn(),
  PROMPT_SUBMIT_REQUEST_TIMEOUT_MS: 1_800_000,
  setApiRequestProfile: vi.fn(),
  transcribeAudio: vi.fn()
}))

vi.mock('@/store/gateway', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  requestGatewayForAgent: vi.fn()
}))

// #144186: Desktop talking to a remote `hermes serve` (legacy `/api/ws`) sends
// `attachments` on `prompt.submit`, which the legacy contract forbids. Images are
// already staged into the session before submit, so the field is a canonical-only
// admission payload — the legacy downgrade (which already drops `submission_id`)
// must drop it too, or the retry is refused and the turn never runs.
const stagedImage = (): ComposerAttachment => ({
  attachedSessionId: RUNTIME_SESSION_ID,
  id: 'image:shot.png',
  kind: 'image',
  label: 'shot.png',
  mime: 'image/png',
  path: '/remote/work/.hermes/attachments/shot.png'
})

const extraInput = (key: string) =>
  Object.assign(
    new Error(
      `invalid params for prompt.submit: ${key}: Extra inputs are not permitted — the client and the Hermes backend are out of sync (different versions); run \`hermes update\` and restart both`
    ),
    { code: 4000 }
  )

beforeEach(() => {
  window.localStorage.removeItem('hermes.desktop.preparedSubmissions.v1')
  window.localStorage.removeItem('hermes.desktop.composerQueue.v1')
  vi.mocked(getLatestSessionMessages).mockReset()
  vi.mocked(getLatestSessionMessages).mockImplementation(async () => ({ messages: [], session_id: 'session' }))
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('legacy prompt.submit downgrade', () => {
  it('drops attachments together with submission_id so an image send reaches a legacy backend', async () => {
    const submits: Record<string, unknown>[] = []

    // A legacy `hermes serve` validates params against the strict legacy contract:
    // any unknown key is refused as 4000 before a handler runs. Both canonical-only
    // keys therefore refuse, which is exactly the second error users hit (#144186).
    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method !== 'prompt.submit') {
        return {} as never
      }

      submits.push(params!)

      if ('submission_id' in params!) {
        throw extraInput('submission_id')
      }

      if ('attachments' in params!) {
        throw extraInput('attachments')
      }

      return { status: 'streaming' } as never
    })

    let handle: HarnessHandle | null = null
    await actRender(
      <Harness
        onReady={h => (handle = h)}
        rawAdmissionReceipts
        refreshSessions={async () => undefined}
        requestGateway={requestGateway}
      />
    )

    expect(await handle!.submitText('what is in this image?', { attachments: [stagedImage()] })).toBe(true)

    expect(submits).toHaveLength(2)
    // The canonical-shaped first attempt still carries the admission payload …
    expect(submits[0]).toMatchObject({
      attachments: [{ mime: 'image/png', path: '/remote/work/.hermes/attachments/shot.png' }]
    })
    // … and the legacy retry carries neither canonical-only key.
    expect(submits[1]).not.toHaveProperty('submission_id')
    expect(submits[1]).not.toHaveProperty('attachments')
    expect(submits[1]).toMatchObject({ text: 'what is in this image?' })
  })

  it('keeps attachments on a canonical admission', async () => {
    const submits: Record<string, unknown>[] = []

    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method !== 'prompt.submit') {
        return {} as never
      }

      submits.push(params!)

      return {
        admission_id: params?.submission_id,
        session_id: RUNTIME_SESSION_ID,
        status: 'queued',
        submission_id: params?.submission_id
      } as never
    })

    let handle: HarnessHandle | null = null
    await actRender(
      <Harness
        onReady={h => (handle = h)}
        rawAdmissionReceipts
        refreshSessions={async () => undefined}
        requestGateway={requestGateway}
      />
    )

    expect(await handle!.submitText('what is in this image?', { attachments: [stagedImage()] })).toBe(true)

    expect(submits).toHaveLength(1)
    expect(submits[0]).toMatchObject({
      attachments: [{ mime: 'image/png', path: '/remote/work/.hermes/attachments/shot.png' }],
      text: 'what is in this image?'
    })
    expect(submits[0]).toHaveProperty('submission_id')
  })
})
