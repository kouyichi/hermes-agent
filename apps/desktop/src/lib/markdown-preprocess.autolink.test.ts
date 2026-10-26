import { tailBoundedRemend } from '@assistant-ui/react-streamdown'
import remarkGfm from 'remark-gfm'
import remarkParse from 'remark-parse'
import { unified } from 'unified'
import { describe, expect, it } from 'vitest'

import { preprocessMarkdown } from './markdown-preprocess'

/**
 * The bare-URL autolinker wraps prose URLs in `<…>`. It must leave URLs that
 * markdown syntax already owns alone: wrapping the label of `[url](url)`
 * swallowed `](url)` into the href (#49822, #85234). Each case asserts the
 * hrefs the renderer's parser actually produces from the preprocessed text.
 */

type MdNode = { children?: MdNode[]; identifier?: string; type: string; url?: string }

// Every href the parser resolves, reference links included, in document order.
function hrefs(markdown: string, prepare = preprocessMarkdown): string[] {
  const tree = unified().use(remarkParse).use(remarkGfm).parse(prepare(markdown)) as MdNode
  const definitions = new Map<string, string>()
  const found: (() => string)[] = []

  const walk = (node: MdNode) => {
    if (node.type === 'definition') {
      definitions.set(node.identifier ?? '', node.url ?? '')
    } else if (node.type === 'link' || node.type === 'image') {
      found.push(() => node.url ?? '')
    } else if (node.type === 'linkReference' || node.type === 'imageReference') {
      found.push(() => definitions.get(node.identifier ?? '') ?? '')
    }

    node.children?.forEach(walk)
  }

  walk(tree)

  return found.map(resolve => resolve())
}

describe('preprocessMarkdown / raw-URL autolinking inside markdown links', () => {
  it.each([
    'https://example.com',
    'http://example.com',
    'https://example.com:8080',
    'https://user@example.com',
    'https://example.com/page',
    'https://example.com?q=test',
    'https://example.com#test',
    'https://www.metodic.io/articles/whats-moving-in-facilitation-july-2026'
  ])('keeps [url](url) intact for %s', url => {
    const input = `[${url}](${url})`

    expect(preprocessMarkdown(input)).toBe(input)
    expect(hrefs(input)).toEqual([url])
  })

  it.each([
    ['[see https://a.example/x for more](https://b.example/y)', ['https://b.example/y']],
    ['[http://[::1]:8080/status](https://example.com/status)', ['https://example.com/status']],
    ['[docs [https://example.com/path]](https://example.com/path)', ['https://example.com/path']],
    ['[a\\]https://example.com/x](https://example.com/y)', ['https://example.com/y']],
    ['[[inner](https://i.example) outer](https://o.example)', ['https://i.example', 'https://o.example']],
    ['![https://example.com/a.png](https://example.com/a.png)', ['https://example.com/a.png']],
    ['[https://x.example](https://x.example "https://x.example title")', ['https://x.example']],
    ['[Foo](https://en.wikipedia.org/wiki/Foo_(bar))', ['https://en.wikipedia.org/wiki/Foo_(bar)']],
    ['<https://example.com/a>', ['https://example.com/a']]
  ])('keeps link labels and targets intact: %s', (input, expected) => {
    expect(preprocessMarkdown(input)).toBe(input)
    expect(hrefs(input)).toEqual(expected)
  })

  it('keeps full and collapsed reference links intact', () => {
    const full = '[https://example.com/a][ref]\n\n[ref]: https://example.com/b'
    const collapsed = '[https://example.com/a][]\n\n[https://example.com/a]: https://example.com/b'

    expect(preprocessMarkdown(full)).toBe(full)
    expect(hrefs(full)).toEqual(['https://example.com/b'])
    expect(preprocessMarkdown(collapsed)).toBe(collapsed)
    expect(hrefs(collapsed)).toEqual(['https://example.com/b'])
  })

  it('never wraps URLs in inline code or code fences', () => {
    const input =
      'Try `[https://x.example](https://x.example)` or\n\n```md\n[https://x.example](https://x.example)\n```'

    expect(preprocessMarkdown(input)).toBe(input)
  })

  it('still autolinks bare URLs sharing a line with a markdown link', () => {
    const input = 'Docs https://a.example/x and [https://b.example/y](https://b.example/y).'

    expect(hrefs(input)).toEqual(['https://a.example/x', 'https://b.example/y'])
  })

  it('never leaks link syntax into an href while the link streams in', () => {
    const link = '[https://example.com/a](https://example.com/a)'
    const streamed = (text: string) => tailBoundedRemend(preprocessMarkdown(text))

    for (let end = 1; end <= link.length; end += 1) {
      for (const url of hrefs(`See ${link.slice(0, end)}`, streamed)) {
        expect(url).not.toMatch(/[\]()]|%5D/)
      }
    }
  })
})

describe('preprocessMarkdown / bare loopback URLs in prose', () => {
  // A loopback URL the model writes into a sentence is user-facing content —
  // "the dev server is at http://localhost:3000" must render the address,
  // not leave a hole (#121683). The only legitimate suppression is the fenced
  // preview hand-off (a whole fenced block whose body is just the URL),
  // owned by LOCAL_PREVIEW_ONLY_RE in normalizeFenceBlocks.
  it.each([
    ['Bare autolink: http://localhost:3000', 'http://localhost:3000'],
    ['With a path: http://localhost:3000/dashboard', 'http://localhost:3000/dashboard'],
    ['Loopback IP: http://127.0.0.1:8080', 'http://127.0.0.1:8080'],
    ['Wildcard bind: http://0.0.0.0:8000', 'http://0.0.0.0:8000'],
    ['IPv6 loopback: http://[::1]:8080/status', 'http://[::1]:8080/status'],
    ['Trailing slash: http://localhost:3000/ - panel', 'http://localhost:3000/'],
    [
      'Server at http://localhost:3000 and docs at https://example.com',
      ['http://localhost:3000', 'https://example.com']
    ]
  ])('preserves bare loopback URLs in prose: %s', (input, expected) => {
    expect(hrefs(input)).toEqual([expected].flat())
  })

  it('keeps a markdown-labeled loopback link intact', () => {
    const input = '[my dev server](http://localhost:3000)'

    expect(preprocessMarkdown(input)).toBe(input)
    expect(hrefs(input)).toEqual(['http://localhost:3000'])
  })

  it('still drops a fenced block whose whole body is a loopback URL (preview hand-off)', () => {
    const input = 'Before\n\n```\nhttp://localhost:3000\n```\n\nAfter'

    expect(preprocessMarkdown(input)).toBe('Before\n\n\nAfter')
    expect(hrefs(input)).toEqual([])
  })
})

describe('preprocessMarkdown / bare-URL trailing punctuation', () => {
  it.each([
    ['Visit https://example.com/a.', 'https://example.com/a'],
    ['Visit https://example.com/a, then', 'https://example.com/a'],
    ['(see https://example.com/a)', 'https://example.com/a'],
    ['(see https://example.com/a).', 'https://example.com/a'],
    ['(see https://example.com/a.)', 'https://example.com/a'],
    ['[https://example.com/a]', 'https://example.com/a'],
    ['See https://en.wikipedia.org/wiki/Foo_(bar).', 'https://en.wikipedia.org/wiki/Foo_(bar)'],
    ['(https://en.wikipedia.org/wiki/Foo_(bar))', 'https://en.wikipedia.org/wiki/Foo_(bar)'],
    ['Host http://[2001:db8::1]:8080/status.', 'http://[2001:db8::1]:8080/status']
  ])('%s links %s', (input, expected) => {
    expect(hrefs(input)).toEqual([expected])
  })
})

describe('preprocessMarkdown / bare-URL boundary at CJK punctuation', () => {
  // Chinese and Japanese prose abut a link with no space, so the sentence
  // delimiter is a full-width mark: `…/v2.1.0（private，done）`, `…/docs。`.
  // The ASCII-only boundary class ran on to the next ASCII whitespace and
  // swallowed that tail into the href — the link 404s (the tail is
  // percent-encoded into it) and the rest of the sentence renders as link
  // text (#144736).
  it.each([
    ['看 https://example.com/release/v2.1.0（private，建库成功）', 'https://example.com/release/v2.1.0'],
    ['地址 https://example.com/docs。', 'https://example.com/docs'],
    ['引用 https://example.com/a」', 'https://example.com/a'],
    ['枚举 https://example.com/a、后面', 'https://example.com/a']
  ])('%s links %s', (input, expected) => {
    expect(hrefs(input)).toEqual([expected])
  })

  it('keeps the punctuation and the following prose outside the anchor', () => {
    expect(preprocessMarkdown('地址 https://example.com/docs。')).toBe('地址 <https://example.com/docs>。')
    expect(preprocessMarkdown('看 https://example.com/v2.1.0（private）')).toBe(
      '看 <https://example.com/v2.1.0>（private）'
    )
  })

  it('splits two URLs separated only by a full-width mark', () => {
    expect(hrefs('结果 https://example.com/a。然后 https://example.com/b 也看下')).toEqual([
      'https://example.com/a',
      'https://example.com/b'
    ])
  })

  // The stop set is punctuation only. Ideographs and half-width katakana are
  // legal IRI path/query content and already worked; narrowing the class must
  // not truncate them.
  it.each([
    ['ideographs in the path', '看 https://example.com/路径/含中文'],
    ['ideographs in the query', '看 https://example.com/a?q=中文'],
    ['half-width katakana', '見 https://example.com/ｶﾀｶﾅ']
  ])('keeps %s inside the URL', (_label, input) => {
    expect(hrefs(input)).toEqual([input.slice(input.indexOf('https://'))])
  })

  // A markdown link owns its own destination, so a full-width mark inside it is
  // the author's choice, not a sentence boundary.
  it('leaves a markdown link destination that contains full-width punctuation whole', () => {
    const input = '[发布记录](https://example.com/路径（v2.1.0）)'

    expect(preprocessMarkdown(input)).toBe(input)
    expect(hrefs(input)).toEqual(['https://example.com/路径（v2.1.0）'])
  })
})
