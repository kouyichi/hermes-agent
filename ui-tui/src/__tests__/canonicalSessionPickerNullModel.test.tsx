import { PassThrough } from 'node:stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { expect, it, vi } from 'vitest'

import { ActiveSessionSwitcher } from '../components/activeSessionSwitcher.js'
import { messages } from '../i18n/runtime.js'
import { DEFAULT_THEME } from '../theme.js'

// `sessions.model` stays NULL for a row that never ran a model turn (a route reservation, or a
// `/status` slash command in a channel), and the live feed hands the column over as-is. The row
// renderer called `.replace` on it, so opening the picker with one such row killed the whole
// Ink tree: `TypeError: Cannot read properties of null (reading 'replace')` (#146537).
it('renders a live row whose model is NULL instead of taking the picker down', async () => {
  const request = vi.fn(async (method: string) => {
    if (method !== 'session.list') {
      throw new Error('invalid_params')
    }

    return {
      scope: 'live',
      sessions: [
        {
          id: 'null-model',
          session_id: 'null-model',
          title: 'Channel status',
          started_at: 1,
          message_count: 0,
          model: null
        },
        {
          id: 'owner',
          session_id: 'owner',
          title: 'Owned conversation',
          started_at: 2,
          message_count: 2,
          model: 'vendor/model'
        }
      ]
    }
  })

  const stdin = Object.assign(new PassThrough(), { isTTY: true, setRawMode: vi.fn(), ref: vi.fn(), unref: vi.fn() })
  let output = ''
  const stdout = Object.assign(new PassThrough(), { columns: 100, rows: 30, isTTY: false })
  stdout.on('data', chunk => {
    output += chunk.toString()
  })

  const instance = renderSync(
    <ActiveSessionSwitcher
      currentSessionId="owner"
      gw={{ isCanonical: true, request } as any}
      onCancel={vi.fn()}
      onClose={vi.fn()}
      onNew={vi.fn()}
      onNewPrompt={vi.fn()}
      onResume={vi.fn()}
      onSelect={vi.fn()}
      t={DEFAULT_THEME}
    />,
    { stdin: stdin as any, stdout: stdout as any, stderr: new PassThrough() as any, patchConsole: false }
  )

  try {
    await new Promise(resolve => setImmediate(resolve))
    expect(request.mock.calls[0][0]).toBe('session.list')
    expect(output).toContain('Channel status')
    expect(output).toContain(messages().pickers.session.modelUnknown)
    expect(output).toContain('Owned conversation')
  } finally {
    instance.unmount()
  }
})
