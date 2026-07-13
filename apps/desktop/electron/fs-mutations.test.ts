import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test, { type TestContext } from 'node:test'

import {
  assertHermesAuthPathAllowed,
  isHermesAuthPathProtected,
  renamePathWithAuthGuard,
  trashPathWithAuthGuard,
  writeBufferWithAuthGuard,
  writeTextWithAuthGuard
} from './fs-mutations.ts'

function fixture(t: TestContext) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-desktop-auth-guard-'))
  const hermesHome = path.join(root, '.hermes')
  const profileHome = path.join(hermesHome, 'profiles', 'default')
  const codexRefreshStateDir = path.join(hermesHome, 'state', 'codex-refresh')
  fs.mkdirSync(profileHome, { recursive: true })
  fs.mkdirSync(codexRefreshStateDir, { recursive: true })
  fs.writeFileSync(path.join(hermesHome, 'auth.json'), '{"version":1}', 'utf8')
  fs.writeFileSync(path.join(hermesHome, 'auth.lock'), '', 'utf8')
  fs.writeFileSync(path.join(profileHome, 'auth.json'), '{"version":1}', 'utf8')
  fs.writeFileSync(path.join(profileHome, 'auth.lock'), '', 'utf8')
  fs.writeFileSync(path.join(codexRefreshStateDir, 'refresh.hmac.key'), 'private-key', 'utf8')
  fs.writeFileSync(path.join(codexRefreshStateDir, 'grant-example.wal.json'), '{"state":"rotated"}', 'utf8')
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))

  return { codexRefreshStateDir, hermesHome, profileHome, root }
}

function assertProtected(
  targetPath: string,
  hermesHome: string,
  mode: 'entry' | 'tree' = 'entry'
) {
  assert.throws(
    () => assertHermesAuthPathAllowed(targetPath, { hermesHome, mode, purpose: 'Test operation' }),
    (error: any) => {
      assert.equal(error?.code, 'protected-auth-store')

      return true
    }
  )
}

test('entry guard blocks root auth.json and auth.lock', t => {
  const { hermesHome } = fixture(t)

  assertProtected(path.join(hermesHome, 'auth.json'), hermesHome)
  assertProtected(path.join(hermesHome, 'auth.lock'), hermesHome)
})

test('entry guard blocks every named-profile auth.json and auth.lock', t => {
  const { hermesHome, profileHome } = fixture(t)

  assertProtected(path.join(profileHome, 'auth.json'), hermesHome)
  assertProtected(path.join(profileHome, 'auth.lock'), hermesHome)
  assertProtected(path.join(hermesHome, 'profiles', 'future-profile', 'auth.json'), hermesHome)
})

test('entry guard blocks the complete Codex refresh state boundary', t => {
  const { codexRefreshStateDir, hermesHome } = fixture(t)

  assertProtected(codexRefreshStateDir, hermesHome)
  assertProtected(path.join(codexRefreshStateDir, 'refresh.hmac.key'), hermesHome)
  assertProtected(path.join(codexRefreshStateDir, 'grant-example.wal.json'), hermesHome)
  assertProtected(path.join(codexRefreshStateDir, 'grant-future.wal.tmp.1'), hermesHome)
})

test('entry guard blocks an explicit Codex refresh state override', t => {
  const { hermesHome, root } = fixture(t)
  const stateOverride = path.join(root, 'private-codex-refresh')
  fs.mkdirSync(stateOverride)

  assert.throws(
    () =>
      assertHermesAuthPathAllowed(path.join(stateOverride, 'refresh.hmac.key'), {
        codexRefreshStateDir: stateOverride,
        hermesHome,
        mode: 'entry'
      } as any),
    (error: any) => error?.code === 'protected-auth-store'
  )
})

test('entry guard allows unrelated project auth.json and sibling-prefix paths', t => {
  const { hermesHome, root } = fixture(t)
  const projectAuth = path.join(root, 'project', 'auth.json')
  const siblingAuth = path.join(`${hermesHome}-copy`, 'auth.json')
  fs.mkdirSync(path.dirname(projectAuth), { recursive: true })
  fs.mkdirSync(path.dirname(siblingAuth), { recursive: true })

  assert.doesNotThrow(() => assertHermesAuthPathAllowed(projectAuth, { hermesHome, mode: 'entry' }))
  assert.doesNotThrow(() => assertHermesAuthPathAllowed(siblingAuth, { hermesHome, mode: 'entry' }))
})

test('entry guard follows the host filesystem case-sensitivity contract', t => {
  const { hermesHome } = fixture(t)
  const caseVariant = path.join(hermesHome, 'profiles', 'future-profile', 'AUTH.JSON')

  if (process.platform === 'win32' || process.platform === 'darwin') {
    assertProtected(caseVariant, hermesHome)
  } else {
    assert.doesNotThrow(() => assertHermesAuthPathAllowed(caseVariant, { hermesHome, mode: 'entry' }))
  }
})

test('entry guard follows a final symlink to a canonical auth store', t => {
  const { hermesHome, root } = fixture(t)
  const alias = path.join(root, 'innocent.json')

  try {
    fs.symlinkSync(path.join(hermesHome, 'auth.json'), alias, 'file')
  } catch (error: any) {
    if (error?.code === 'EPERM' || error?.code === 'EACCES') {
      t.skip(`symlink creation is not permitted on this platform (${error.code})`)

      return
    }
    throw error
  }

  assertProtected(alias, hermesHome)
})

test('entry guard resolves an existing symlinked parent for a missing destination', t => {
  const { hermesHome, root } = fixture(t)
  const freshProfile = path.join(hermesHome, 'profiles', 'fresh')
  const aliasDir = path.join(root, 'safe-directory-name')
  fs.mkdirSync(freshProfile)

  try {
    fs.symlinkSync(freshProfile, aliasDir, process.platform === 'win32' ? 'junction' : 'dir')
  } catch (error: any) {
    if (error?.code === 'EPERM' || error?.code === 'EACCES') {
      t.skip(`symlink creation is not permitted on this platform (${error.code})`)

      return
    }
    throw error
  }

  assertProtected(path.join(aliasDir, 'auth.json'), hermesHome)
})

test('entry guard detects a hardlink alias of an existing auth store', t => {
  const { hermesHome, root } = fixture(t)
  const alias = path.join(root, 'innocent-hardlink.json')

  try {
    fs.linkSync(path.join(hermesHome, 'auth.json'), alias)
  } catch (error: any) {
    if (['EPERM', 'EACCES', 'EXDEV'].includes(error?.code)) {
      t.skip(`hardlink creation is not permitted on this platform (${error.code})`)

      return
    }
    throw error
  }

  assertProtected(alias, hermesHome)
})

test('entry guard detects a hardlink alias of Codex refresh state', t => {
  const { codexRefreshStateDir, hermesHome, root } = fixture(t)
  const alias = path.join(root, 'innocent-state-hardlink')

  try {
    fs.linkSync(path.join(codexRefreshStateDir, 'refresh.hmac.key'), alias)
  } catch (error: any) {
    if (['EPERM', 'EACCES', 'EXDEV'].includes(error?.code)) {
      t.skip(`hardlink creation is not permitted on this platform (${error.code})`)

      return
    }
    throw error
  }

  assertProtected(alias, hermesHome)
})

test('tree guard blocks Hermes home, profiles root, and a profile container', t => {
  const { hermesHome, profileHome } = fixture(t)

  assertProtected(hermesHome, hermesHome, 'tree')
  assertProtected(path.join(hermesHome, 'profiles'), hermesHome, 'tree')
  assertProtected(profileHome, hermesHome, 'tree')
})

test('tree guard blocks the Codex refresh state directory', t => {
  const { codexRefreshStateDir, hermesHome } = fixture(t)

  assertProtected(codexRefreshStateDir, hermesHome, 'tree')
})

test('tree guard blocks an ancestor that contains Hermes home', t => {
  const { hermesHome, root } = fixture(t)

  assertProtected(root, hermesHome, 'tree')
})

test('tree guard follows a directory symlink to a protected profile container', t => {
  const { hermesHome, profileHome, root } = fixture(t)
  const alias = path.join(root, 'profile-alias')

  try {
    fs.symlinkSync(profileHome, alias, process.platform === 'win32' ? 'junction' : 'dir')
  } catch (error: any) {
    if (error?.code === 'EPERM' || error?.code === 'EACCES') {
      t.skip(`symlink creation is not permitted on this platform (${error.code})`)

      return
    }
    throw error
  }

  assertProtected(alias, hermesHome, 'tree')
})

test('tree guard allows unrelated project trees and non-auth profile children', t => {
  const { hermesHome, profileHome, root } = fixture(t)

  assert.doesNotThrow(() =>
    assertHermesAuthPathAllowed(path.join(root, 'project'), { hermesHome, mode: 'tree' })
  )
  assert.doesNotThrow(() =>
    assertHermesAuthPathAllowed(path.join(profileHome, 'sessions'), { hermesHome, mode: 'tree' })
  )
})

test('boolean classifier uses the same narrow canonical boundary', t => {
  const { hermesHome, root } = fixture(t)

  assert.equal(isHermesAuthPathProtected(path.join(hermesHome, 'auth.json'), { hermesHome }), true)
  assert.equal(isHermesAuthPathProtected(path.join(root, 'project', 'auth.json'), { hermesHome }), false)
})

test('write wrapper blocks canonical auth before underlying I/O', async t => {
  const { hermesHome } = fixture(t)
  let calls = 0

  await assert.rejects(
    writeTextWithAuthGuard(path.join(hermesHome, 'auth.json'), 'replacement', {
      hermesHome,
      writeFile: async () => {
        calls += 1
      }
    } as any),
    (error: any) => error?.code === 'protected-auth-store'
  )
  assert.equal(calls, 0)
})

test('write wrapper allows an unrelated project auth.json', async t => {
  const { hermesHome, root } = fixture(t)
  const projectAuth = path.join(root, 'project', 'auth.json')
  let calls = 0

  await writeTextWithAuthGuard(projectAuth, 'project auth', {
    hermesHome,
    writeFile: async () => {
      calls += 1
    }
  } as any)
  assert.equal(calls, 1)
})

test('binary write wrapper blocks Codex refresh state before underlying I/O', async t => {
  const { codexRefreshStateDir, hermesHome } = fixture(t)
  let calls = 0

  await assert.rejects(
    writeBufferWithAuthGuard(path.join(codexRefreshStateDir, 'refresh.hmac.key'), Buffer.from('replacement'), {
      hermesHome,
      writeFile: async () => {
        calls += 1
      }
    } as any),
    (error: any) => error?.code === 'protected-auth-store'
  )
  assert.equal(calls, 0)
})

test('rename wrapper checks protected source and destination before I/O', async t => {
  const { hermesHome, root } = fixture(t)
  let calls = 0
  const rename = async () => {
    calls += 1
  }

  await assert.rejects(
    renamePathWithAuthGuard(path.join(hermesHome, 'auth.json'), path.join(root, 'moved.json'), {
      hermesHome,
      rename
    } as any),
    (error: any) => error?.code === 'protected-auth-store'
  )
  await assert.rejects(
    renamePathWithAuthGuard(path.join(root, 'safe.json'), path.join(hermesHome, 'auth.json'), {
      hermesHome,
      rename
    } as any),
    (error: any) => error?.code === 'protected-auth-store'
  )
  assert.equal(calls, 0)
})

test('rename wrapper blocks protected containers and allows project paths', async t => {
  const { hermesHome, profileHome, root } = fixture(t)
  let calls = 0
  const rename = async () => {
    calls += 1
  }

  await assert.rejects(
    renamePathWithAuthGuard(profileHome, path.join(hermesHome, 'profiles', 'renamed'), {
      hermesHome,
      rename
    } as any),
    (error: any) => error?.code === 'protected-auth-store'
  )
  await renamePathWithAuthGuard(path.join(root, 'project', 'auth.json'), path.join(root, 'project', 'login.json'), {
    hermesHome,
    rename
  } as any)
  assert.equal(calls, 1)
})

test('trash wrapper blocks protected trees before shell and allows project trees', async t => {
  const { hermesHome, root } = fixture(t)
  let calls = 0
  const trashItem = async () => {
    calls += 1
  }

  await assert.rejects(
    trashPathWithAuthGuard(hermesHome, { hermesHome, trashItem }),
    (error: any) => error?.code === 'protected-auth-store'
  )
  assert.equal(calls, 0)
  await trashPathWithAuthGuard(path.join(root, 'project'), { hermesHome, trashItem })
  assert.equal(calls, 1)
})

test('realpath EACCES and ELOOP failures are fail-closed', t => {
  const { hermesHome, root } = fixture(t)

  for (const code of ['EACCES', 'ELOOP']) {
    const fsImpl = {
      ...fs,
      realpathSync: Object.assign(
        () => {
          const error: any = new Error(code)
          error.code = code
          throw error
        },
        { native: undefined }
      )
    } as unknown as typeof fs

    assert.throws(
      () => isHermesAuthPathProtected(path.join(root, 'project', 'auth.json'), { hermesHome, fs: fsImpl }),
      (error: any) => error?.code === 'auth-guard-io'
    )
  }
})

test('desktop main wires every renderer file surface through the auth guard', () => {
  const main = fs.readFileSync(new URL('./main.ts', import.meta.url), 'utf8')
  const readDirStart = main.indexOf('async function readDirForDesktopIpc')
  const nextHelperStart = main.indexOf('function hermesManagedNodePathEntries')
  const externalStart = main.indexOf('function openExternalUrl')
  const previewBrowserStart = main.indexOf('async function openPreviewInBrowser')
  const previewBrowserEnd = main.indexOf('function ensureWslWindowsFonts')
  const saveImageStart = main.indexOf('async function saveImageFromUrl')
  const composerImageStart = main.indexOf('async function writeComposerImage')
  const renameStart = main.indexOf("ipcMain.handle('hermes:fs:rename'")
  const writeStart = main.indexOf("ipcMain.handle('hermes:fs:writeText'")
  const trashStart = main.indexOf("ipcMain.handle('hermes:fs:trash'")
  const gitStart = main.indexOf("ipcMain.handle('hermes:git:worktreeList'")
  const terminalStart = main.indexOf("ipcMain.handle('hermes:terminal:start'")
  const renameHandler = main.slice(renameStart, writeStart)
  const writeHandler = main.slice(writeStart, trashStart)
  const trashHandler = main.slice(trashStart, gitStart)
  const gitHandlers = main.slice(gitStart, terminalStart)
  const saveImageHandler = main.slice(saveImageStart, composerImageStart)
  const readDirHelper = main.slice(readDirStart, nextHelperStart)
  const externalHelper = main.slice(externalStart, previewBrowserStart)
  const previewBrowserHelper = main.slice(previewBrowserStart, previewBrowserEnd)

  assert.match(main, /codexRefreshStateDir:\s*CODEX_REFRESH_STATE_DIR/)
  assert.match(main, /const HERMES_AUTH_ROOT =/)
  assert.match(main, /hermesHome:\s*HERMES_AUTH_ROOT/)
  assert.doesNotMatch(main, /hermesHome:\s*HERMES_HOME/)
  assert.equal((main.match(/resolveReadableFileForIpc\(/g) || []).length, 1)
  assert.equal((main.match(/resolveDesktopReadableFileForIpc\(/g) || []).length, 7)
  assert.match(main, /hermes:fs:readDir'[\s\S]{0,120}readDirForDesktopIpc/)
  assert.match(readDirHelper, /assertHermesAuthPathAllowed\(dirPath/)
  assert.ok(readDirHelper.indexOf('assertHermesAuthPathAllowed') < readDirHelper.indexOf('readDirForIpc'))
  assert.match(externalHelper, /assertHermesAuthPathAllowed\(localPath/)
  assert.match(previewBrowserHelper, /assertHermesAuthPathAllowed\(localPath/)
  assert.match(renameHandler, /renamePathWithAuthGuard\(src, dst/)
  assert.doesNotMatch(renameHandler, /fs\.promises\.rename\(/)
  assert.match(writeHandler, /writeTextWithAuthGuard\(resolved, text/)
  assert.doesNotMatch(writeHandler, /fs\.promises\.writeFile\(/)
  assert.match(trashHandler, /trashPathWithAuthGuard\(target/)
  assert.doesNotMatch(trashHandler, /shell\.trashItem\(target\)/)
  assert.match(saveImageHandler, /writeBufferWithAuthGuard\(result\.filePath, buffer/)
  assert.doesNotMatch(saveImageHandler, /fs\.promises\.writeFile\(/)
  for (const operation of [
    'switchBranch',
    'reviewDiff',
    'fileDiffVsHead',
    'reviewRevert',
    'reviewCommit',
    'reviewCommitContext'
  ]) {
    assert.doesNotMatch(gitHandlers, new RegExp(`${operation}\\(repoPath`))
  }
  assert.match(gitHandlers, /guardGitRepoPath\(repoPath/)
  assert.match(gitHandlers, /guardGitFileTarget\(repoPath, filePath/)
})
