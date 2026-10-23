import { describe, expect, it } from 'vitest'

import { type CatalogEntry, parseCatalog } from '../catalog/catalog-data'

import { catalogAssociation, type InstalledCatalogCandidate, repositoryIdentity } from './plugin-catalog-match'

const catalogEntry = (name: string, repo: string, subdir = ''): CatalogEntry =>
  parseCatalog('plugins', [{ name, repo, subdir }])[0]

/** What the tab builds for an installed package: a pseudo-entry per package. */
const installed = (key: string, repo: string, catalogName?: string): InstalledCatalogCandidate => {
  const [entry] = parseCatalog('plugins', [{ name: key, repo }])

  return { catalogName, entry: { ...entry, id: `installed:${key}` }, repo }
}

describe('repositoryIdentity', () => {
  it('folds every spelling of one repository into a single identity', () => {
    const spellings = [
      'https://github.com/example/weather-plugin',
      'https://github.com/example/weather-plugin.git',
      'https://github.com/example/weather-plugin/',
      'https://github.com/Example/Weather-Plugin',
      'git@github.com:example/weather-plugin.git',
      'ssh://git@github.com/example/weather-plugin',
      'git://github.com/example/weather-plugin',
      'git+https://github.com/example/weather-plugin',
      'example/weather-plugin'
    ]

    for (const spelling of spellings) {
      expect(repositoryIdentity(spelling), spelling).toBe('github.com/example/weather-plugin')
    }
  })

  it('ignores an install ref, which is not part of the source identity', () => {
    expect(repositoryIdentity('https://github.com/example/weather-plugin.git?ref=v1.2.0')).toBe(
      'github.com/example/weather-plugin'
    )
  })

  it('keeps the path case of hosts that may be case-sensitive', () => {
    expect(repositoryIdentity('https://gitlab.com/Group/Repo')).toBe('gitlab.com/Group/Repo')
  })

  it('keeps a monorepo subdirectory in the identity, from the URL or from the catalog field', () => {
    const fromMarker = repositoryIdentity('https://github.com/example/mono#plugins/weather')
    const fromField = repositoryIdentity('https://github.com/example/mono', 'plugins/weather')

    expect(fromMarker).toBe('github.com/example/mono#plugins/weather')
    expect(fromField).toBe(fromMarker)
    expect(repositoryIdentity('https://github.com/example/mono', 'plugins/climate')).not.toBe(fromMarker)
  })

  it('refuses two different subdirectories of one repository', () => {
    expect(repositoryIdentity('https://github.com/example/mono#plugins/weather', 'plugins/climate')).toBeNull()
  })

  it('has no identity for anything that is not a git-ish remote', () => {
    for (const value of ['', '   ', '/home/me/weather-plugin', 'file:///tmp/weather-plugin', 'desktop:weather']) {
      expect(repositoryIdentity(value), value).toBeNull()
    }
  })
})

describe('catalogAssociation', () => {
  it('matches a git-installed package by repository when no catalog name was recorded', () => {
    const weather = catalogEntry('weather-plugin', 'https://github.com/example/weather-plugin')
    const candidate = installed('weather', 'git@github.com:example/weather-plugin.git')
    const match = catalogAssociation([candidate], [weather])

    expect(match.match(weather)?.id).toBe('installed:weather')
  })

  it('prefers the catalog name the installer recorded over the repository', () => {
    const weather = catalogEntry('weather-plugin', 'https://github.com/example/weather-plugin')
    const named = installed('renamed-fork', 'https://github.com/elsewhere/fork', 'weather-plugin')
    const byRepo = installed('weather', 'https://github.com/example/weather-plugin')
    const match = catalogAssociation([named, byRepo], [weather])

    expect(match.match(weather)?.id).toBe('installed:renamed-fork')
  })

  it('refuses to guess when two installed packages claim one repository', () => {
    const weather = catalogEntry('weather-plugin', 'https://github.com/example/weather-plugin')

    const match = catalogAssociation(
      [
        installed('weather-a', 'https://github.com/example/weather-plugin'),
        installed('weather-b', 'git@github.com:example/weather-plugin.git')
      ],
      [weather]
    )

    expect(match.match(weather)).toBeUndefined()
  })

  it('refuses to guess when two catalog entries claim one repository identity', () => {
    const weather = catalogEntry('weather-plugin', 'https://github.com/example/weather-plugin')
    const alias = { ...catalogEntry('weather-plugin-alias', 'https://github.com/example/weather-plugin.git') }

    const match = catalogAssociation(
      [installed('weather', 'https://github.com/example/weather-plugin')],
      [weather, alias]
    )

    expect(match.match(weather)).toBeUndefined()
    expect(match.match(alias)).toBeUndefined()
  })

  it('separates entries that share a monorepo but install from different subdirectories', () => {
    const weather = catalogEntry('weather-plugin', 'https://github.com/example/mono', 'plugins/weather')
    const climate = catalogEntry('climate-plugin', 'https://github.com/example/mono', 'plugins/climate')

    const match = catalogAssociation(
      [installed('weather', 'https://github.com/example/mono.git#plugins/weather')],
      [weather, climate]
    )

    expect(match.match(weather)?.id).toBe('installed:weather')
    expect(match.match(climate)).toBeUndefined()
  })

  it('leaves the installed provenance untouched', () => {
    const weather = catalogEntry('weather-plugin', 'https://github.com/example/weather-plugin')
    const candidate = installed('weather', 'https://github.com/example/weather-plugin')
    const match = catalogAssociation([candidate], [weather])

    expect(match.match(weather)?.id).toBe('installed:weather')
    expect(candidate.catalogName).toBeUndefined()
    expect(candidate.entry.id).toBe('installed:weather')
  })
})
