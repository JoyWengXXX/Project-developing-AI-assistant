import assert from 'node:assert/strict'
import { test } from 'node:test'
import { waitForVisiblePoll } from '../src/visiblePolling.ts'

class Visibility extends EventTarget {
  hidden = false

  setHidden(hidden) {
    this.hidden = hidden
    this.dispatchEvent(new Event('visibilitychange'))
  }
}

test('polling pauses while hidden and resumes immediately in foreground', async () => {
  const visibility = new Visibility()
  globalThis.document = visibility
  visibility.setHidden(true)
  let finished = false
  const waiting = waitForVisiblePoll(0, new AbortController().signal, () => {}).then(() => { finished = true })
  await new Promise((resolve) => setTimeout(resolve, 20))
  assert.equal(finished, false)
  visibility.setHidden(false)
  await waiting
  assert.equal(finished, true)
})

test('wake while hidden does not poll until foreground', async () => {
  const visibility = new Visibility()
  globalThis.document = visibility
  let wake = () => {}
  let finished = false
  const waiting = waitForVisiblePoll(10_000, new AbortController().signal, (callback) => { wake = callback })
    .then(() => { finished = true })
  visibility.setHidden(true)
  wake()
  await new Promise((resolve) => setTimeout(resolve, 20))
  assert.equal(finished, false)
  visibility.setHidden(false)
  await waiting
  assert.equal(finished, true)
})

test('abort ends a hidden wait', async () => {
  const visibility = new Visibility()
  globalThis.document = visibility
  visibility.setHidden(true)
  const controller = new AbortController()
  const waiting = waitForVisiblePoll(10_000, controller.signal, () => {})
  controller.abort()
  await assert.rejects(waiting, { name: 'AbortError' })
})
