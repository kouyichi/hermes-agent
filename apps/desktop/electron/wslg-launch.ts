import { detectRemoteDisplay } from './bootstrap-platform'
import { linuxOzoneBackend } from './hud-windowing'

export const WAYLAND_IME_SWITCH = '--enable-wayland-ime'

/**
 * Whether a launch that is already past the relaunch — `entry.ts`'s in-process
 * path, where the ozone platform is on argv and no child is spawned — still has
 * to append `--enable-wayland-ime` (#143425).
 *
 * The Wayland ozone backend does NOT imply Chromium's Wayland text-input
 * client: without the switch there is no preedit and no candidate window, so a
 * fcitx5/ibus user gets no CJK input at all and nothing in the UI says why. It
 * is needed only where the IME is the Wayland one — an XWayland launch (the
 * documented `--ozone-platform=x11` / `desktop.ozone_platform_hint: x11`
 * escape hatch) keeps the GTK/Qt input path, which is also why that stays the
 * opt-out for a compositor with a broken input-popup surface.
 */
export function needsWaylandIme(argv: readonly string[], env: NodeJS.ProcessEnv, platform: NodeJS.Platform): boolean {
  if (platform !== 'linux' || argv.includes(WAYLAND_IME_SWITCH)) {
    return false
  }

  // A forwarded display renders through the software path, where there is no
  // local input-method surface to bind to (same gate as the relaunch below).
  const displayEnv = { ...env, HERMES_DESKTOP_DISABLE_GPU: undefined }

  if (detectRemoteDisplay({ env: displayEnv, platform })) {
    return false
  }

  return linuxOzoneBackend(env, argv) === 'wayland'
}

/** The relaunch argv, carrying the Wayland IME switch when the backend is Wayland. */
function withWaylandIme(args: readonly string[], backend: string): string[] {
  if (backend !== 'wayland' || args.includes(WAYLAND_IME_SWITCH)) {
    return [...args]
  }

  return [...args, WAYLAND_IME_SWITCH]
}

// Ozone is selected before application JavaScript. Never appendSwitch here:
// that leaves the browser on X11 while GPU children receive Wayland.
export function wslgLaunchArgs(
  argv: readonly string[],
  env: NodeJS.ProcessEnv,
  platform: NodeJS.Platform,
  electronFlags: readonly string[] = [],
  nvidiaProprietaryDriver = false
): string[] | null {
  const displayEnv = { ...env, HERMES_DESKTOP_DISABLE_GPU: undefined }

  // Remote/forwarded displays stay on the software-rendering path. A local
  // Wayland session — native Linux or WSLg — otherwise defaults to XWayland
  // unless the platform is on argv before application JavaScript.
  if (platform !== 'linux' || detectRemoteDisplay({ env: displayEnv, platform })) {
    return null
  }

  // WSLg always exports WAYLAND_DISPLAY, so it is covered by the same check.
  const wayland = env.XDG_SESSION_TYPE === 'wayland' || Boolean(env.WAYLAND_DISPLAY)

  if (!wayland || hasOzonePlatform(argv)) {
    return null
  }

  // desktop.electron_flags are not always on argv yet (.desktop entry, source
  // launches). An explicit platform there wins over the automatic default, and
  // must be on the relaunch command line or appendSwitch applies it too late.
  const flagged = explicitOzonePlatform(electronFlags)

  if (flagged !== null) {
    return flagged ? withWaylandIme([...argv, `--ozone-platform=${flagged}`], flagged) : null
  }

  const hintArg = findOzoneHint(argv) ?? findOzoneHint(electronFlags)
  const hint = hintArg ?? env.ELECTRON_OZONE_PLATFORM_HINT

  const backend = defaultBackend(hint, nvidiaProprietaryDriver)

  return withWaylandIme([...argv, `--ozone-platform=${backend}`], backend)
}

// An explicit x11/wayland hint always wins. Without one, the NVIDIA proprietary
// driver stays on XWayland: its GPU process dies on Wayland ozone under the
// bundled Chromium (615.x, #126013) while x11 launches. Everyone else gets
// native Wayland (#83578).
function defaultBackend(hint: string | undefined, nvidiaProprietaryDriver: boolean): 'wayland' | 'x11' {
  if (hint === 'x11' || hint === 'wayland') {
    return hint
  }

  return nvidiaProprietaryDriver ? 'x11' : 'wayland'
}

function hasOzonePlatform(args: readonly string[]): boolean {
  return args.some(arg => arg === '--ozone-platform' || arg.startsWith('--ozone-platform='))
}

function explicitOzonePlatform(args: readonly string[]): string | null {
  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i] ?? ''

    if (arg === '--ozone-platform') {
      const next = args[i + 1]

      return next && !next.startsWith('-') ? next : ''
    }

    if (arg.startsWith('--ozone-platform=')) {
      return arg.slice('--ozone-platform='.length)
    }
  }

  return null
}

function findOzoneHint(args: readonly string[]): string | undefined {
  const hintArg = args.findLast(arg => arg.startsWith('--ozone-platform-hint='))

  return hintArg?.slice('--ozone-platform-hint='.length)
}
