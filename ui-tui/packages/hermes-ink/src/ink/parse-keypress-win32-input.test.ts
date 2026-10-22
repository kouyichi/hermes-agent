import { describe, expect, it } from 'vitest'

import { INITIAL_STATE, parseMultipleKeypresses } from './parse-keypress.js'

// A Windows console in win32-input-mode (ConPTY `CSI ? 9001 h`) delivers every
// keyboard event as `CSI Vk ; Sc ; Uc ; Kd ; Cs ; Rc _` instead of VT bytes —
// one record for the key-down, one for the key-up. `_` (0x5f) is a valid CSI
// final byte, so the tokenizer already hands each record over whole; the key
// parser had no rule for it, so every record fell through as an empty-name key
// with a printable tail. The InputEvent mapper strips the leading ESC, the
// remainder (`[79;24;79;1;48;1_`) passes the composer's printable gate, and the
// prompt fills with records while Enter / Ctrl+C / Backspace do nothing
// (#143100).
function win32(vk: number, uc: number, kd: 0 | 1, cs: number, rc = 1, sc = 0): string {
  return `\x1b[${vk};${sc};${uc};${kd};${cs};${rc}_`
}

const LSHIFT_NUMLOCK = 0x10 | 0x20
const LCTRL_NUMLOCK = 0x08 | 0x20

describe('win32-input-mode records (DECSET 9001)', () => {
  it('decodes the reported stream: Shift+O types "O", Ctrl+C is a ctrl chord', () => {
    const [keys] = parseMultipleKeypresses(
      INITIAL_STATE,
      win32(79, 79, 1, LSHIFT_NUMLOCK) +
        win32(79, 79, 0, LSHIFT_NUMLOCK) +
        win32(67, 3, 1, LCTRL_NUMLOCK) +
        win32(67, 3, 0, LCTRL_NUMLOCK)
    )

    expect(keys.map(k => [k.name, k.ctrl, k.shift, k.sequence])).toEqual([
      ['o', false, true, 'O'],
      ['c', true, false, '\x03']
    ])
  })

  it('never lets a record reach the composer as text', () => {
    const [keys] = parseMultipleKeypresses(INITIAL_STATE, win32(79, 79, 1, LSHIFT_NUMLOCK))

    // The composer inserts `sequence`; the InputEvent mapper strips a leading
    // ESC, so a raw record left in `sequence` becomes printable garbage.
    expect(keys[0]!.sequence).toBe('O')
    expect(keys[0]!.sequence ?? '').not.toContain('[')
  })

  it('drops key-up records in both orders and keeps the key-down only', () => {
    const [onlyUp] = parseMultipleKeypresses(INITIAL_STATE, win32(67, 3, 0, LCTRL_NUMLOCK))
    const [downThenUp] = parseMultipleKeypresses(INITIAL_STATE, win32(65, 97, 1, 0) + win32(65, 97, 0, 0))

    expect(onlyUp).toHaveLength(0)
    expect(downThenUp.map(k => k.name)).toEqual(['a'])
  })

  it('maps Enter and Backspace to their key names (sequence stays non-printable)', () => {
    const [keys] = parseMultipleKeypresses(INITIAL_STATE, win32(13, 13, 1, 0) + win32(8, 8, 1, 0))

    expect(keys.map(k => k.name)).toEqual(['return', 'backspace'])
  })

  it('maps navigation keys and gives them no insertable sequence', () => {
    const [keys] = parseMultipleKeypresses(INITIAL_STATE, win32(38, 0, 1, 0) + win32(46, 0, 1, 0) + win32(113, 0, 1, 0))

    expect(keys.map(k => k.name)).toEqual(['up', 'delete', 'f2'])
    expect(keys.map(k => k.sequence ?? '')).toEqual(['', '', ''])
  })

  it('keeps Alt as meta and Shift+letter as a shifted lowercase name', () => {
    const [keys] = parseMultipleKeypresses(INITIAL_STATE, win32(70, 102, 1, 0x02) + win32(66, 66, 1, 0x10))

    expect(keys[0]).toMatchObject({ name: 'f', meta: true, ctrl: false, shift: false })
    expect(keys[1]).toMatchObject({ name: 'b', shift: true, sequence: 'B' })
  })

  it('honours the repeat count of a held key', () => {
    const [keys] = parseMultipleKeypresses(INITIAL_STATE, win32(65, 97, 1, 0, 3))

    expect(keys.map(k => k.name)).toEqual(['a', 'a', 'a'])
  })

  it('reassembles a record split across two reads', () => {
    const record = win32(67, 3, 1, LCTRL_NUMLOCK)
    const [first, state] = parseMultipleKeypresses(INITIAL_STATE, record.slice(0, 10))
    const [second] = parseMultipleKeypresses(state, record.slice(10))

    expect(first).toHaveLength(0)
    expect(second.map(k => k.name)).toEqual(['c'])
  })

  it('leaves ordinary CSI keys unchanged', () => {
    const [keys] = parseMultipleKeypresses(INITIAL_STATE, '\x1b[A\x1b[Z\r')

    expect(keys.map(k => k.name)).toEqual(['up', 'tab', 'return'])
    expect(keys[1]).toMatchObject({ shift: true })
  })
})
