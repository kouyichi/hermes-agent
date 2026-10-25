import { describe, expect, it } from 'vitest'

import { detectTerminalGraphics } from '../lib/terminalGraphics.js'

const detect = (env: Record<string, string | undefined>) => detectTerminalGraphics(env)

describe('detectTerminalGraphics', () => {
  it('answers for the terminal the client actually runs in', () => {
    expect(detect({ TERM: 'xterm-ghostty' })).toBe('kitty')
    expect(detect({ TERM_PROGRAM: 'ghostty' })).toBe('kitty')
    expect(detect({ TERM: 'xterm-kitty' })).toBe('kitty')
    expect(detect({ KITTY_WINDOW_ID: '1' })).toBe('kitty')
    expect(detect({ TERM_PROGRAM: 'WezTerm', WEZTERM_PANE: '3' })).toBe('kitty')
    expect(detect({ TERM_PROGRAM: 'iTerm.app' })).toBe('iterm')
    expect(detect({ ITERM_SESSION_ID: 'w0t0p0' })).toBe('iterm')
    expect(detect({ TERM: 'xterm-256color', TERM_PROGRAM: 'mintty' })).toBe('sixel')
    expect(detect({ TERM: 'foot' })).toBe('sixel')
    expect(detect({ TERM: 'xterm-256color' })).toBe('unicode')
    expect(detect({})).toBe('unicode')
  })

  it('refuses the inherited session markers of an xterm.js host', () => {
    // VS Code/Cursor set TERM_PROGRAM=vscode but inherit ITERM_SESSION_ID /
    // KITTY_WINDOW_ID from the shell that launched them.
    expect(detect({ TERM_PROGRAM: 'vscode', KITTY_WINDOW_ID: '1' })).toBe('unicode')
    expect(detect({ TERM_PROGRAM: 'VSCode', ITERM_SESSION_ID: 'w0t0p0' })).toBe('unicode')
  })
})
