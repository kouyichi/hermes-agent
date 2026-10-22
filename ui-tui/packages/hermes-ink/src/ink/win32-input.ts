/**
 * win32-input-mode key records (ConPTY `CSI ? 9001 h`).
 *
 * A Windows console in this mode reports every keyboard event as
 *
 *     CSI Vk ; Sc ; Uc ; Kd ; Cs ; Rc _
 *
 * (`Vk` virtual-key code, `Sc` scan code, `Uc` unicode char or 0, `Kd` 1 =
 * key-down / 0 = key-up, `Cs` control-key state bitmask, `Rc` repeat count).
 * The terminal sends one record per transition, so a single press arrives as a
 * down record followed by an up record.
 *
 * `_` (0x5f) is a valid CSI final byte, so the input tokenizer already emits
 * each record as one sequence token; what was missing is the interpretation.
 * Without it every record parsed as an empty-name key whose `sequence` was the
 * raw record: the InputEvent mapper strips the leading ESC, the printable
 * remainder passed the composer's gate, and the prompt filled with
 * `[79;24;79;1;48;1_` text while Enter / Ctrl+C / Backspace did nothing.
 *
 * Spec: https://github.com/microsoft/terminal/blob/main/doc/specs/%234999%20-%20Improved%20keyboard%20handling%20in%20Conpty.md
 */
import type { ParsedKey } from './parse-keypress.js'

// `CSI Vk ; Sc ; Uc ; Kd ; Cs ; Rc _` — all six params are always present.
// eslint-disable-next-line no-control-regex
const WIN32_INPUT_RE = /^\x1b\[(\d+);(\d+);(\d+);(\d+);(\d+);(\d+)_$/

/** Control-key state bits of a console `KEY_EVENT_RECORD.dwControlKeyState`.
 *  NUMLOCK/CAPSLOCK/ENHANCED carry no modifier meaning for the TUI, so they are
 *  ignored rather than mapped onto a flag. */
const CONTROL_STATE = {
  RIGHT_ALT: 0x0001,
  LEFT_ALT: 0x0002,
  RIGHT_CTRL: 0x0004,
  LEFT_CTRL: 0x0008,
  SHIFT: 0x0010
} as const

/** Virtual-key codes that name a key instead of producing a character. */
const VK_NAMES: Record<number, string> = {
  8: 'backspace',
  9: 'tab',
  13: 'return',
  27: 'escape',
  32: 'space',
  33: 'pageup',
  34: 'pagedown',
  35: 'end',
  36: 'home',
  37: 'left',
  38: 'up',
  39: 'right',
  40: 'down',
  45: 'insert',
  46: 'delete'
}

/** VK_F1..VK_F12 (112..123). */
const VK_F_FIRST = 112
const VK_F_LAST = 123

/** VK_0..VK_9 and VK_A..VK_Z — the keys a console reports with a virtual-key
 *  code, whatever character the chord's `Uc` slot carries. */
const VK_DIGIT_FIRST = 48
const VK_DIGIT_LAST = 57
const VK_LETTER_FIRST = 65
const VK_LETTER_LAST = 90

/** A held key can arrive as one record carrying a repeat count; anything far
 *  beyond a human key-repeat burst is a malformed record, not 4 billion keys. */
const MAX_REPEAT = 32

function win32KeyName(vk: number, uc: number): string | undefined {
  const named = VK_NAMES[vk]

  if (named) {
    return named
  }

  if (vk >= VK_F_FIRST && vk <= VK_F_LAST) {
    return `f${vk - VK_F_FIRST + 1}`
  }

  // Printable characters are named after their lowercase form (the same shape
  // CSI u / modifyOtherKeys produce); Shift is a flag, not part of the name.
  // The virtual-key code decides this, not `Uc`: a ctrl chord reports the
  // control byte there (Ctrl+C → Uc 3), and a dormant AltGr layout can report
  // 0. Only the character itself comes from `Uc`.
  if (vk >= VK_DIGIT_FIRST && vk <= VK_DIGIT_LAST) {
    return String.fromCharCode(vk)
  }

  if (vk >= VK_LETTER_FIRST && vk <= VK_LETTER_LAST) {
    return String.fromCharCode(vk).toLowerCase()
  }

  if (uc >= 0x20 && uc <= 0x7e) {
    return String.fromCharCode(uc).toLowerCase()
  }

  return undefined
}

/**
 * Decode one win32-input-mode record into the keys it represents.
 *
 * Returns `null` when `sequence` is not such a record (the caller then follows
 * its normal path), and an empty array when the record must produce no input —
 * a key-up, or a record with no usable key identity.
 */
export function decodeWin32InputKeys(sequence: string): ParsedKey[] | null {
  const match = WIN32_INPUT_RE.exec(sequence)

  if (!match) {
    return null
  }

  const vk = parseInt(match[1]!, 10)
  const uc = parseInt(match[3]!, 10)
  const isKeyDown = parseInt(match[4]!, 10) !== 0
  const state = parseInt(match[5]!, 10)
  const repeat = parseInt(match[6]!, 10)

  // The key-up half of every press: emitting it would double every keystroke.
  if (!isKeyDown) {
    return []
  }

  const name = win32KeyName(vk, uc)

  if (!name) {
    return []
  }

  // The terminal's own translation of the key: `O` for Shift+O, the control
  // byte for a ctrl chord. Left empty for named keys with no character (arrows,
  // Enter as VK_RETURN with Uc 0 on some builds) — a raw record left here would
  // be stripped of its ESC upstream and inserted as literal `[38;72;0;1;0;1_`.
  const text = uc > 0 ? String.fromCharCode(uc) : undefined

  const key: ParsedKey = {
    kind: 'key',
    name,
    fn: name.startsWith('f'),
    ctrl: (state & (CONTROL_STATE.LEFT_CTRL | CONTROL_STATE.RIGHT_CTRL)) !== 0,
    meta: (state & (CONTROL_STATE.LEFT_ALT | CONTROL_STATE.RIGHT_ALT)) !== 0,
    shift: (state & CONTROL_STATE.SHIFT) !== 0,
    option: false,
    super: false,
    sequence: text,
    raw: sequence,
    isPasted: false
  }

  const repeats = Math.max(1, Math.min(repeat, MAX_REPEAT))

  return Array.from({ length: repeats }, () => key)
}
