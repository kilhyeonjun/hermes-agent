import fs from 'node:fs'
import path from 'node:path'

import { assertHermesAuthPathAllowed } from './fs-mutations'
import { resolveRequestedPathForIpc } from './hardening'

type GitRepositoryGuardOptions = {
  codexRefreshStateDir?: string
  fs?: typeof fs
  hermesHome: string
  purpose?: string
}

function findGitRoot(start: string, fsImpl: typeof fs = fs): string | null {
  let dir = start

  for (let i = 0; i < 50; i += 1) {
    try {
      if (fsImpl.existsSync(path.join(dir, '.git'))) {
        return dir
      }
    } catch {
      return null
    }

    const parent = path.dirname(dir)

    if (parent === dir) {
      return null
    }

    dir = parent
  }

  return null
}

async function gitRootForIpc(
  startPath: unknown,
  options: { fs?: typeof fs } = {}
): Promise<string | null> {
  const fsImpl = options.fs || fs
  let resolved

  try {
    resolved = resolveRequestedPathForIpc(startPath, { purpose: 'Git root' })
  } catch {
    return null
  }

  try {
    const stat = await fsImpl.promises.stat(resolved)
    const start = stat.isDirectory() ? resolved : path.dirname(resolved)

    return findGitRoot(start, fsImpl)
  } catch {
    return findGitRoot(resolved, fsImpl)
  }
}

async function guardGitRepositoryForIpc(
  startPath: unknown,
  options: GitRepositoryGuardOptions
): Promise<string> {
  const fsImpl = options.fs || fs
  const purpose = String(options.purpose || 'Git repository operation')
  const resolved = resolveRequestedPathForIpc(startPath, { purpose })
  const guardOptions = {
    codexRefreshStateDir: options.codexRefreshStateDir,
    fs: fsImpl,
    hermesHome: options.hermesHome,
    mode: 'tree' as const,
    purpose
  }

  assertHermesAuthPathAllowed(resolved, guardOptions)
  const repositoryRoot = await gitRootForIpc(resolved, { fs: fsImpl })

  if (!repositoryRoot) {
    return resolved
  }
  assertHermesAuthPathAllowed(repositoryRoot, {
    ...guardOptions,
    purpose: `${purpose} repository root`
  })

  return repositoryRoot
}

export { findGitRoot, guardGitRepositoryForIpc, gitRootForIpc }
