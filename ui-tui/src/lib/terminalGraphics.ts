/**
 * Which image protocol this terminal speaks — asked in the process that owns it.
 *
 * `pet.cells` is answered by the gateway owner, a long-lived daemon (a systemd
 * user service, or the shared gateway), so its environment carries no `TERM` /
 * `TERM_PROGRAM` and its own detection answers `unicode` for every terminal.
 * The Ink client runs inside the real terminal, so it sends its answer and the
 * owner trusts it for `display.pet.render_mode: auto`; an explicitly pinned
 * render mode still wins on the server.
 *
 * Mirrors `agent/pet/render.py::detect_terminal_graphics` — environment only,
 * never a DA1/OSC query that could hang a pipe.
 */

export type TerminalGraphics = 'kitty' | 'iterm' | 'sixel' | 'unicode'

const lower = (value: string | undefined) => (value ?? '').trim().toLowerCase()

const isWezTerm = (env: NodeJS.ProcessEnv) => lower(env.TERM_PROGRAM) === 'wezterm' || Boolean(env.WEZTERM_PANE)

export function detectTerminalGraphics(env: NodeJS.ProcessEnv = process.env): TerminalGraphics {
  const term = lower(env.TERM)
  const termProgram = lower(env.TERM_PROGRAM)

  // VS Code/Cursor advertise TERM_PROGRAM=vscode but inherit ITERM_SESSION_ID /
  // KITTY_WINDOW_ID from the shell that launched them; trusting those emits
  // escapes xterm.js renders as a blank frame (the same carve-out the Python
  // detector makes).
  if (termProgram === 'vscode') {
    return 'unicode'
  }

  if (
    env.KITTY_WINDOW_ID ||
    term.includes('kitty') ||
    term.includes('ghostty') ||
    termProgram === 'ghostty' ||
    isWezTerm(env)
  ) {
    return 'kitty' // WezTerm speaks kitty and iterm; kitty has richer placement
  }

  if (termProgram === 'iterm.app' || env.ITERM_SESSION_ID) {
    return 'iterm'
  }

  if (termProgram === 'mintty' || term.includes('foot') || term.includes('mlterm') || term.includes('sixel')) {
    return 'sixel'
  }

  return 'unicode'
}
