import { describe, expect, it } from 'vitest'

import { linuxOzoneBackend } from './hud-windowing'
import { needsWaylandIme, wslgLaunchArgs } from './wslg-launch'

const env = { WSL_DISTRO_NAME: 'Ubuntu', WAYLAND_DISPLAY: 'wayland-0', DISPLAY: ':0' }

// Every relaunch that selects the Wayland ozone backend also carries the Wayland IME switch
// (#143425): the backend alone leaves Chromium's text-input client off.
const waylandRelaunch = (...argv: string[]): string[] => [...argv, '--ozone-platform=wayland', '--enable-wayland-ime']

describe('WSLg launch arguments', () => {
  it('preserves the invocation and restarts only once with native Wayland', () => {
    const args = ['.', '--inspect=9229', 'hermes://session/example']
    const next = wslgLaunchArgs(args, env, 'linux')!

    expect(next).toEqual(waylandRelaunch(...args))
    expect(wslgLaunchArgs(next, env, 'linux')).toBeNull()
    expect(args).toEqual(['.', '--inspect=9229', 'hermes://session/example'])
  })

  it('respects explicit backends and the existing config-bridged X11 hint', () => {
    for (const args of [['--ozone-platform=x11'], ['--ozone-platform', 'x11']]) {
      expect(wslgLaunchArgs(args, env, 'linux')).toBeNull()
    }

    expect(wslgLaunchArgs(['--ozone-platform-hint=x11'], env, 'linux')).toEqual([
      '--ozone-platform-hint=x11',
      '--ozone-platform=x11'
    ])
    expect(wslgLaunchArgs([], { ...env, HERMES_DESKTOP_DISABLE_GPU: 'true' }, 'linux')).toEqual(waylandRelaunch())
    expect(wslgLaunchArgs([], { ...env, ELECTRON_OZONE_PLATFORM_HINT: 'x11' }, 'linux')).toEqual([
      '--ozone-platform=x11'
    ])
  })

  it('leaves other platforms, missing Wayland displays and forwarded sessions alone', () => {
    expect(wslgLaunchArgs([], env, 'win32')).toBeNull()
    expect(wslgLaunchArgs([], env, 'darwin')).toBeNull()
    expect(wslgLaunchArgs([], { DISPLAY: ':0' }, 'linux')).toBeNull()
    expect(wslgLaunchArgs([], { WSL_DISTRO_NAME: 'Ubuntu' }, 'linux')).toBeNull()
    expect(wslgLaunchArgs([], { ...env, SSH_CONNECTION: 'remote' }, 'linux')).toBeNull()
    expect(wslgLaunchArgs([], { ...env, DISPLAY: 'localhost:10.0' }, 'linux')).toBeNull()
  })

  // main.ts keys the WSL GPU-blocklist override off linuxOzoneBackend: forcing GPU
  // compositing on WSLg's Wayland ozone segfaults the GPU process (no DRM render node).
  it('is the backend main.ts sees after the relaunch', () => {
    expect(linuxOzoneBackend(env, wslgLaunchArgs([], env, 'linux')!)).toBe('wayland')

    const x11 = { ...env, ELECTRON_OZONE_PLATFORM_HINT: 'x11' }
    expect(linuxOzoneBackend(x11, wslgLaunchArgs([], x11, 'linux')!)).toBe('x11')
  })
})

const nativeWayland = { XDG_SESSION_TYPE: 'wayland', WAYLAND_DISPLAY: 'wayland-0', DISPLAY: ':0' }

describe('native Wayland launch arguments', () => {
  it('appends wayland on a native Linux Wayland session when the user did not choose a platform', () => {
    expect(wslgLaunchArgs(['.'], nativeWayland, 'linux')).toEqual(waylandRelaunch('.'))
    expect(wslgLaunchArgs([], { WAYLAND_DISPLAY: 'wayland-0', DISPLAY: ':0' }, 'linux')).toEqual(waylandRelaunch())
    expect(wslgLaunchArgs([], { XDG_SESSION_TYPE: 'wayland', DISPLAY: ':0' }, 'linux')).toEqual(waylandRelaunch())
    expect(wslgLaunchArgs(['--ozone-platform=wayland'], nativeWayland, 'linux')).toBeNull()
  })

  it('keeps an explicit x11 platform already on the command line', () => {
    expect(wslgLaunchArgs(['--ozone-platform=x11'], nativeWayland, 'linux')).toBeNull()
    expect(wslgLaunchArgs(['--ozone-platform', 'x11'], nativeWayland, 'linux')).toBeNull()
  })

  it('lets an x11 hint win over the automatic wayland platform', () => {
    expect(wslgLaunchArgs([], { ...nativeWayland, ELECTRON_OZONE_PLATFORM_HINT: 'x11' }, 'linux')).toEqual([
      '--ozone-platform=x11'
    ])
    expect(wslgLaunchArgs(['--ozone-platform-hint=x11'], nativeWayland, 'linux')).toEqual([
      '--ozone-platform-hint=x11',
      '--ozone-platform=x11'
    ])
  })

  it('lets desktop.electron_flags choose the ozone platform instead of forcing wayland', () => {
    expect(wslgLaunchArgs([], nativeWayland, 'linux', ['--ozone-platform=x11'])).toEqual(['--ozone-platform=x11'])
    expect(wslgLaunchArgs(['.'], nativeWayland, 'linux', ['--disable-gpu'])).toEqual(waylandRelaunch('.'))
    expect(wslgLaunchArgs([], { XDG_SESSION_TYPE: 'x11', DISPLAY: ':0' }, 'linux')).toBeNull()
    expect(wslgLaunchArgs([], { ...nativeWayland, SSH_CONNECTION: 'remote' }, 'linux')).toBeNull()
  })

  // #126013: the NVIDIA proprietary driver's GPU process dies on Wayland ozone.
  it('defaults the NVIDIA proprietary driver to x11 unless the user chose wayland', () => {
    expect(wslgLaunchArgs(['.'], nativeWayland, 'linux', [], true)).toEqual(['.', '--ozone-platform=x11'])
    expect(wslgLaunchArgs([], { ...nativeWayland, ELECTRON_OZONE_PLATFORM_HINT: 'auto' }, 'linux', [], true)).toEqual([
      '--ozone-platform=x11'
    ])

    expect(
      wslgLaunchArgs([], { ...nativeWayland, ELECTRON_OZONE_PLATFORM_HINT: 'wayland' }, 'linux', [], true)
    ).toEqual(waylandRelaunch())
    expect(wslgLaunchArgs([], nativeWayland, 'linux', ['--ozone-platform-hint=wayland'], true)).toEqual(
      waylandRelaunch()
    )
    expect(wslgLaunchArgs([], nativeWayland, 'linux', ['--ozone-platform=wayland'], true)).toEqual(waylandRelaunch())
    expect(wslgLaunchArgs(['--ozone-platform=wayland'], nativeWayland, 'linux', [], true)).toBeNull()
    expect(wslgLaunchArgs([], { XDG_SESSION_TYPE: 'x11', DISPLAY: ':0' }, 'linux', [], true)).toBeNull()
  })
})

// #143425: Chromium keeps its Wayland text-input client off unless the switch is on the
// command line, so a native Wayland session had no preedit/candidate window (CJK input dead)
// until the user found `desktop.electron_flags`. The switch rides the same launch that picks
// the wayland ozone backend; XWayland keeps the GTK/Qt IME path and needs nothing.
describe('Wayland IME switch', () => {
  it('rides the relaunch that selects the wayland backend', () => {
    expect(wslgLaunchArgs(['.'], nativeWayland, 'linux')).toEqual([
      '.',
      '--ozone-platform=wayland',
      '--enable-wayland-ime'
    ])
    expect(wslgLaunchArgs([], { ...nativeWayland, ELECTRON_OZONE_PLATFORM_HINT: 'wayland' }, 'linux')).toEqual([
      '--ozone-platform=wayland',
      '--enable-wayland-ime'
    ])
    expect(wslgLaunchArgs([], nativeWayland, 'linux', ['--ozone-platform=wayland'])).toEqual([
      '--ozone-platform=wayland',
      '--enable-wayland-ime'
    ])
    expect(wslgLaunchArgs([], env, 'linux')).toEqual(['--ozone-platform=wayland', '--enable-wayland-ime'])
  })

  it('stays off an XWayland relaunch, a forwarded session and other platforms', () => {
    expect(wslgLaunchArgs(['.'], nativeWayland, 'linux', [], true)).toEqual(['.', '--ozone-platform=x11'])
    expect(wslgLaunchArgs([], { ...nativeWayland, ELECTRON_OZONE_PLATFORM_HINT: 'x11' }, 'linux')).toEqual([
      '--ozone-platform=x11'
    ])
    expect(wslgLaunchArgs(['--ozone-platform-hint=x11'], nativeWayland, 'linux')).toEqual([
      '--ozone-platform-hint=x11',
      '--ozone-platform=x11'
    ])
    expect(wslgLaunchArgs([], { ...nativeWayland, SSH_CONNECTION: 'remote' }, 'linux')).toBeNull()
  })

  it('does not duplicate the switch when the caller already passed it', () => {
    expect(wslgLaunchArgs([], nativeWayland, 'linux', ['--enable-wayland-ime'])).toEqual([
      '--ozone-platform=wayland',
      '--enable-wayland-ime'
    ])
    expect(wslgLaunchArgs(['--enable-wayland-ime'], nativeWayland, 'linux')).toEqual([
      '--enable-wayland-ime',
      '--ozone-platform=wayland'
    ])
  })

  // A launch that already carries the ozone platform never relaunches; entry.ts appends the
  // switch itself, so the decision has to be reachable from the argv alone.
  it('covers an in-process launch that already carries the wayland platform', () => {
    expect(needsWaylandIme(['--ozone-platform=wayland'], nativeWayland, 'linux')).toBe(true)
    expect(needsWaylandIme(['--ozone-platform=wayland'], { WAYLAND_DISPLAY: 'wayland-0' }, 'linux')).toBe(true)
    expect(needsWaylandIme([], { ...nativeWayland, ELECTRON_OZONE_PLATFORM_HINT: 'wayland' }, 'linux')).toBe(true)
  })

  it('leaves every launch that is not a local native Wayland one alone', () => {
    expect(needsWaylandIme(['--ozone-platform=wayland', '--enable-wayland-ime'], nativeWayland, 'linux')).toBe(false)
    expect(needsWaylandIme(['--ozone-platform=x11'], nativeWayland, 'linux')).toBe(false)
    expect(needsWaylandIme([], { XDG_SESSION_TYPE: 'x11', DISPLAY: ':0' }, 'linux')).toBe(false)
    expect(needsWaylandIme(['--ozone-platform=wayland'], { ...nativeWayland, SSH_CONNECTION: 'remote' }, 'linux')).toBe(
      false
    )
    expect(needsWaylandIme(['--ozone-platform=wayland'], nativeWayland, 'darwin')).toBe(false)
    expect(needsWaylandIme(['--ozone-platform=wayland'], nativeWayland, 'win32')).toBe(false)
  })
})
