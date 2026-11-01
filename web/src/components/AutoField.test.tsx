import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { AutoField } from './AutoField'

function renderNumber(schema: Record<string, unknown>) {
  return renderToStaticMarkup(
    <AutoField
      schemaKey="compression.threshold"
      schema={{ type: 'number', ...schema }}
      value={0.5}
      onChange={() => {}}
    />
  )
}

describe('AutoField numeric bounds', () => {
  it('passes min/max/step from the schema to the number input', () => {
    // #147540: a ratio field without a step attribute steps by the HTML default of 1,
    // so 0.5 became 1.5 in one click and a 0.05 nudge was impossible.
    const html = renderNumber({ step: 0.05, min: 0, max: 1 })

    expect(html).toContain('step="0.05"')
    expect(html).toContain('min="0"')
    expect(html).toContain('max="1"')
  })

  it('leaves a plain count on the HTML default step', () => {
    const html = renderNumber({})

    expect(html).not.toContain('step=')
    expect(html).not.toContain('min=')
  })
})
