import fs from 'node:fs'
import path from 'node:path'

type GuardMode = 'entry' | 'tree'

type GuardOptions = {
  codexRefreshStateDir?: string
  hermesHome: string
  mode?: GuardMode
  purpose?: string
  fs?: typeof fs
}

type MutationOptions = GuardOptions & {
  rename?: typeof fs.promises.rename
  trashItem?: (targetPath: string) => Promise<void>
  writeFile?: typeof fs.promises.writeFile
}

const AUTH_STORE_NAMES = new Set(['auth.json', 'auth.lock'])
const CASE_INSENSITIVE_PATHS = process.platform === 'win32' || process.platform === 'darwin'

function authGuardError(code: string, message: string): Error & { code: string } {
  const error = new Error(message) as Error & { code: string }
  error.code = code

  return error
}

function comparisonKey(filePath: string): string {
  const resolved = path.resolve(filePath)

  return CASE_INSENSITIVE_PATHS ? resolved.toLowerCase() : resolved
}

function samePath(left: string, right: string): boolean {
  return comparisonKey(left) === comparisonKey(right)
}

function isSameOrAncestor(candidate: string, target: string): boolean {
  if (samePath(candidate, target)) {
    return true
  }

  const relative = path.relative(path.resolve(candidate), path.resolve(target))

  return Boolean(relative) && relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative)
}

function relativeSegments(candidate: string, hermesHome: string): string[] | null {
  const relative = path.relative(path.resolve(hermesHome), path.resolve(candidate))

  if (!relative) {
    return []
  }
  if (relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    return null
  }

  const segments = relative.split(path.sep).filter(Boolean)

  return CASE_INSENSITIVE_PATHS ? segments.map(segment => segment.toLowerCase()) : segments
}

function isExactCanonicalAuthPath(candidate: string, hermesHome: string): boolean {
  const segments = relativeSegments(candidate, hermesHome)

  if (!segments) {
    return false
  }
  if (segments.length === 1) {
    return AUTH_STORE_NAMES.has(segments[0])
  }

  return segments.length === 3 && segments[0] === 'profiles' && AUTH_STORE_NAMES.has(segments[2])
}

function isProtectedStatePath(candidate: string, stateDirs: string[]): boolean {
  return stateDirs.some(stateDir => isSameOrAncestor(stateDir, candidate))
}

function isProtectedTreePath(candidate: string, hermesHome: string, stateDirs: string[]): boolean {
  if (
    isExactCanonicalAuthPath(candidate, hermesHome) ||
    isSameOrAncestor(candidate, hermesHome) ||
    isProtectedStatePath(candidate, stateDirs) ||
    stateDirs.some(stateDir => isSameOrAncestor(candidate, stateDir))
  ) {
    return true
  }

  const segments = relativeSegments(candidate, hermesHome)

  return Boolean(
    segments &&
      ((segments.length === 1 && segments[0] === 'profiles') ||
        (segments.length === 2 && segments[0] === 'profiles'))
  )
}

function realpathWithMissingTail(fsImpl: typeof fs, targetPath: string): string {
  let cursor = path.resolve(targetPath)
  const missingTail: string[] = []

  while (true) {
    try {
      const resolved = fsImpl.realpathSync(cursor, { encoding: 'utf8' })

      return path.resolve(resolved, ...missingTail)
    } catch (error: any) {
      if (error?.code !== 'ENOENT' && error?.code !== 'ENOTDIR') {
        throw authGuardError(
          'auth-guard-io',
          `Hermes auth path check failed closed: ${error instanceof Error ? error.message : String(error)}`
        )
      }
      const parent = path.dirname(cursor)

      if (parent === cursor) {
        return path.resolve(targetPath)
      }
      missingTail.unshift(path.basename(cursor))
      cursor = parent
    }
  }
}

function uniquePaths(paths: string[]): string[] {
  const seen = new Set<string>()
  const result: string[] = []

  for (const item of paths) {
    const key = comparisonKey(item)

    if (!seen.has(key)) {
      seen.add(key)
      result.push(path.resolve(item))
    }
  }

  return result
}

function pathForms(fsImpl: typeof fs, targetPath: string): string[] {
  return uniquePaths([path.resolve(targetPath), realpathWithMissingTail(fsImpl, targetPath)])
}

function statIfPresent(fsImpl: typeof fs, targetPath: string): fs.Stats | null {
  try {
    return fsImpl.statSync(targetPath)
  } catch (error: any) {
    if (error?.code === 'ENOENT' || error?.code === 'ENOTDIR') {
      return null
    }
    throw authGuardError(
      'auth-guard-io',
      `Hermes auth path check failed closed: ${error instanceof Error ? error.message : String(error)}`
    )
  }
}

function canonicalAuthCandidates(
  fsImpl: typeof fs,
  homeForms: string[],
  stateDirs: string[]
): string[] {
  const candidates: string[] = []

  for (const home of homeForms) {
    for (const name of AUTH_STORE_NAMES) {
      candidates.push(path.join(home, name))
    }
    const profilesRoot = path.join(home, 'profiles')
    let profileNames: string[] = []

    try {
      profileNames = fsImpl.readdirSync(profilesRoot)
    } catch (error: any) {
      if (error?.code !== 'ENOENT' && error?.code !== 'ENOTDIR') {
        throw authGuardError(
          'auth-guard-io',
          `Hermes auth path check failed closed: ${error instanceof Error ? error.message : String(error)}`
        )
      }
    }
    for (const profileName of profileNames) {
      for (const name of AUTH_STORE_NAMES) {
        candidates.push(path.join(profilesRoot, profileName, name))
      }
    }
  }

  for (const stateDir of stateDirs) {
    let stateNames: string[] = []

    try {
      stateNames = fsImpl.readdirSync(stateDir)
    } catch (error: any) {
      if (error?.code !== 'ENOENT' && error?.code !== 'ENOTDIR') {
        throw authGuardError(
          'auth-guard-io',
          `Hermes auth path check failed closed: ${error instanceof Error ? error.message : String(error)}`
        )
      }
    }
    for (const stateName of stateNames) {
      candidates.push(path.join(stateDir, stateName))
    }
  }

  return uniquePaths(candidates)
}

function isHardlinkAlias(
  fsImpl: typeof fs,
  candidate: string,
  homeForms: string[],
  stateDirs: string[]
): boolean {
  const candidateStat = statIfPresent(fsImpl, candidate)

  if (!candidateStat?.isFile() || Number(candidateStat.nlink) < 2) {
    return false
  }

  for (const canonicalPath of canonicalAuthCandidates(fsImpl, homeForms, stateDirs)) {
    const canonicalStat = statIfPresent(fsImpl, canonicalPath)

    if (
      canonicalStat?.isFile() &&
      canonicalStat.dev === candidateStat.dev &&
      canonicalStat.ino === candidateStat.ino
    ) {
      return true
    }
  }

  return false
}

function validateGuardInputs(targetPath: string, hermesHome: string): void {
  if (typeof targetPath !== 'string' || !targetPath.trim() || targetPath.includes('\0')) {
    throw authGuardError('invalid-path', 'Hermes auth path check failed: target path is invalid.')
  }
  if (typeof hermesHome !== 'string' || !hermesHome.trim() || hermesHome.includes('\0')) {
    throw authGuardError('invalid-path', 'Hermes auth path check failed: HERMES_HOME is invalid.')
  }
}

function isHermesAuthPathProtected(targetPath: string, options: GuardOptions): boolean {
  const fsImpl = options.fs || fs
  const mode = options.mode || 'entry'
  validateGuardInputs(targetPath, options.hermesHome)
  const candidateForms = pathForms(fsImpl, targetPath)
  const homeForms = pathForms(fsImpl, options.hermesHome)
  const stateDirs = options.codexRefreshStateDir
    ? pathForms(fsImpl, options.codexRefreshStateDir)
    : uniquePaths(homeForms.map(home => path.join(home, 'state', 'codex-refresh')))

  for (const candidate of candidateForms) {
    for (const home of homeForms) {
      if (
        (mode === 'tree' && isProtectedTreePath(candidate, home, stateDirs)) ||
        (mode === 'entry' &&
          (isExactCanonicalAuthPath(candidate, home) || isProtectedStatePath(candidate, stateDirs)))
      ) {
        return true
      }
    }
  }

  return isHardlinkAlias(fsImpl, path.resolve(targetPath), homeForms, stateDirs)
}

function assertHermesAuthPathAllowed(targetPath: string, options: GuardOptions): void {
  if (!isHermesAuthPathProtected(targetPath, options)) {
    return
  }
  const purpose = String(options.purpose || 'File operation')

  throw authGuardError(
    'protected-auth-store',
    `${purpose} blocked: Hermes auth stores may only be changed through the canonical auth transaction.`
  )
}

async function writeTextWithAuthGuard(targetPath: string, content: string, options: MutationOptions): Promise<void> {
  assertHermesAuthPathAllowed(targetPath, { ...options, mode: 'entry' })
  const writeFile = options.writeFile || fs.promises.writeFile
  await writeFile(targetPath, content, 'utf8')
}

async function writeBufferWithAuthGuard(
  targetPath: string,
  content: Uint8Array,
  options: MutationOptions
): Promise<void> {
  assertHermesAuthPathAllowed(targetPath, { ...options, mode: 'entry' })
  const writeFile = options.writeFile || fs.promises.writeFile
  await writeFile(targetPath, content)
}

async function renamePathWithAuthGuard(sourcePath: string, destinationPath: string, options: MutationOptions): Promise<void> {
  assertHermesAuthPathAllowed(sourcePath, { ...options, mode: 'tree' })
  assertHermesAuthPathAllowed(destinationPath, { ...options, mode: 'tree' })
  const rename = options.rename || fs.promises.rename
  await rename(sourcePath, destinationPath)
}

async function trashPathWithAuthGuard(targetPath: string, options: MutationOptions): Promise<void> {
  assertHermesAuthPathAllowed(targetPath, { ...options, mode: 'tree' })
  const trashItem = options.trashItem

  if (!trashItem) {
    throw authGuardError('invalid-operation', 'Trash operation is unavailable.')
  }
  await trashItem(targetPath)
}

export {
  assertHermesAuthPathAllowed,
  isHermesAuthPathProtected,
  renamePathWithAuthGuard,
  trashPathWithAuthGuard,
  writeBufferWithAuthGuard,
  writeTextWithAuthGuard
}
