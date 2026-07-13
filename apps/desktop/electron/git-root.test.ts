import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { pathToFileURL } from 'node:url'

import { guardGitRepositoryForIpc, gitRootForIpc } from './git-root.ts'

function mkTmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-git-root-'))
}

test('gitRootForIpc returns null for invalid and device paths', async () => {
  assert.equal(await gitRootForIpc(''), null)
  assert.equal(await gitRootForIpc('   '), null)
  assert.equal(await gitRootForIpc(null), null)
  assert.equal(await gitRootForIpc('\\\\?\\C:\\secret'), null)
  assert.equal(await gitRootForIpc('file:///%E0%A4%A'), null)
})

test('gitRootForIpc resolves directories files missing descendants and file URLs', async t => {
  const root = mkTmpDir()
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))

  const gitDir = path.join(root, '.git')
  const srcDir = path.join(root, 'src')
  const filePath = path.join(srcDir, 'index.ts')
  fs.mkdirSync(gitDir)
  fs.mkdirSync(srcDir)
  fs.writeFileSync(filePath, 'export {}\n', 'utf8')

  assert.equal(await gitRootForIpc(root), root)
  assert.equal(await gitRootForIpc(srcDir), root)
  assert.equal(await gitRootForIpc(filePath), root)
  assert.equal(await gitRootForIpc(pathToFileURL(filePath).toString()), root)
  assert.equal(await gitRootForIpc(path.join(srcDir, 'missing.ts')), root)
})

test('guardGitRepositoryForIpc rejects a protected parent repository discovered from a child cwd', async t => {
  const root = mkTmpDir()
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))

  const hermesHome = path.join(root, '.hermes')
  const childCwd = path.join(hermesHome, 'projects', 'safe-looking-child')
  fs.mkdirSync(path.join(hermesHome, '.git'), { recursive: true })
  fs.mkdirSync(childCwd, { recursive: true })

  await assert.rejects(
    guardGitRepositoryForIpc(childCwd, {
      hermesHome,
      purpose: 'Review commit'
    }),
    (error: any) => error?.code === 'protected-auth-store'
  )
})
