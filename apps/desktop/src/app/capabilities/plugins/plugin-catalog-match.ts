import type { CatalogEntry } from '../catalog/catalog-data'

/** Installed package as the Plugins page knows it: the pseudo-entry the catalog
 *  browser renders for it, plus the provenance the installer recorded. */
export interface InstalledCatalogCandidate {
  entry: CatalogEntry
  /** Catalog name from the install sidecar (`agent.catalog_name`, else the desktop
   *  package marker). Absent for a package installed from a repository or by hand. */
  catalogName?: string
  /** Where the package came from: the sidecar's repo, else the git remote. */
  repo: string
  /** Catalog subdirectory the entry installs from, when it is part of a monorepo. */
  subdir?: string
}

/** Git-ish URL schemes a package can be installed from. */
const IDENTITY_PROTOCOLS = new Set(['git:', 'git+https:', 'git+ssh:', 'http:', 'https:', 'ssh:'])

const stripSlashes = (value: string) => value.replace(/^\/+|\/+$/g, '')

/**
 * Stable source identity of a repository: `host/path[#subdir]`.
 *
 * The same package reaches the app under many spellings — `https://github.com/o/r`,
 * `https://github.com/o/r.git`, `git@github.com:o/r.git`, `git+https://…` — so the
 * catalog and an installed package can only be compared after normalization. The
 * subdirectory stays part of the identity because catalog entries can share a
 * monorepo, and `owner/repo` shorthand resolves to GitHub. Anything that is not a
 * git-ish remote (a local path, a `file://` URL, a branch URL) has no identity.
 */
export function repositoryIdentity(value: string, subdir = ''): null | string {
  let repo = value.trim()

  if (!repo) {
    return null
  }

  const shorthand = /^([^/:?#\s]+)\/([^/:?#\s]+)$/.exec(repo)

  if (shorthand) {
    repo = `https://github.com/${repo}`
  } else if (!repo.includes('://')) {
    // scp-style remote (`git@host:owner/repo.git`). A user part or a dotted host
    // is what makes it a remote rather than a `kind:name` identifier, and a
    // one-segment path (a bare folder name) is never owner/repo.
    const scp = /^(?:([^@/:\s]+)@)?([^/:\s]+):(\S+)$/.exec(repo)

    if (scp && !scp[3].startsWith('/') && scp[3].includes('/') && (scp[1] || scp[2].includes('.'))) {
      repo = `ssh://${scp[1] ? `${scp[1]}@` : ''}${scp[2]}/${scp[3]}`
    }
  }

  // URL.parse, not `new URL` in try/catch: every hand-copied folder and every
  // skill row without a repo lands here, and a thrown TypeError costs more than
  // the null this needs to return anyway (same reason as catalog-data.ts).
  const url = URL.parse(repo)

  if (!url || !IDENTITY_PROTOCOLS.has(url.protocol)) {
    return null
  }

  const host = url.host.toLowerCase()
  const path = stripSlashes(url.pathname).replace(/\.git$/i, '')
  const markerSubdir = stripSlashes(url.hash.slice(1))
  const catalogSubdir = stripSlashes(subdir.trim())

  // Two different subdirectories of one repository are different packages.
  if (!host || !path || (markerSubdir && catalogSubdir && markerSubdir !== catalogSubdir)) {
    return null
  }

  // GitHub owner/repo paths are case-insensitive; other hosts may not be.
  const repository = `${host}/${host === 'github.com' ? path.toLowerCase() : path}`
  const packageSubdir = catalogSubdir || markerSubdir

  return packageSubdir ? `${repository}#${packageSubdir}` : repository
}

/** How a catalog entry maps back to an installed package. */
export interface CatalogMatch {
  match: (entry: CatalogEntry) => CatalogEntry | undefined
}

/**
 * Associate catalog entries with installed packages.
 *
 * Association is display-only: an explicit `catalog_name` always wins, and only
 * when it is absent does the repository identity stand in. Provenance is never
 * written back — `plugin-update.ts` still reads `catalog_name` to decide git vs
 * catalog semantics, so pinning it here would change update behaviour.
 *
 * A repository is ambiguous when several installed packages or several catalog
 * entries claim it; nothing is matched then, because guessing would show one
 * package's state on another's card.
 */
export function catalogAssociation(
  installed: readonly InstalledCatalogCandidate[],
  catalog: readonly CatalogEntry[]
): CatalogMatch {
  const byName = new Map<string, CatalogEntry>()
  const byRepository = new Map<string, CatalogEntry | null>()

  for (const candidate of installed) {
    if (candidate.catalogName) {
      byName.set(candidate.catalogName, candidate.entry)
    }

    const identity = repositoryIdentity(candidate.repo, candidate.subdir)

    if (identity) {
      byRepository.set(identity, byRepository.has(identity) ? null : candidate.entry)
    }
  }

  const sharedInCatalog = new Map<string, number>()

  for (const entry of catalog) {
    const identity = repositoryIdentity(entry.repo, entry.subdir)

    if (identity) {
      sharedInCatalog.set(identity, (sharedInCatalog.get(identity) ?? 0) + 1)
    }
  }

  return {
    match: entry => {
      const named = byName.get(entry.name)

      if (named) {
        return named
      }

      const identity = repositoryIdentity(entry.repo, entry.subdir)

      return identity && sharedInCatalog.get(identity) === 1 ? (byRepository.get(identity) ?? undefined) : undefined
    }
  }
}
