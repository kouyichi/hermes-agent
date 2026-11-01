import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ConfigFieldSchema } from '@/types/hermes'

import { ConfigField } from './config-field'

afterEach(() => {
  cleanup()
})

function numberInput(schema: ConfigFieldSchema) {
  const { container } = render(
    <ConfigField onChange={vi.fn()} schema={schema} schemaKey="compression.threshold" value={0.5} />
  )

  const input = container.querySelector('input')

  expect(input).not.toBeNull()

  return input as HTMLInputElement
}

describe('ConfigField numeric bounds', () => {
  it('passes min/max/step from the schema to the number input', () => {
    // #147540: a ratio field without a step attribute steps by the HTML default of 1,
    // so 0.5 became 1.5 in one click and a 0.05 nudge was impossible.
    const input = numberInput({ type: 'number', step: 0.05, min: 0, max: 1 })

    expect(input.getAttribute('step')).toBe('0.05')
    expect(input.getAttribute('min')).toBe('0')
    expect(input.getAttribute('max')).toBe('1')
  })

  it('leaves a plain count on the HTML default step', () => {
    const input = numberInput({ type: 'number' })

    expect(input.hasAttribute('step')).toBe(false)
    expect(input.hasAttribute('min')).toBe(false)
    expect(input.hasAttribute('max')).toBe(false)
  })
})
