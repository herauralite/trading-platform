import test from 'node:test'
import assert from 'node:assert/strict'

import { normalizeTelegramAuthUser, resolveTelegramAuthUser } from './sessionAuth.js'

test('telegram auth user normalization accepts canonical and widget id keys', () => {
  assert.deepEqual(
    normalizeTelegramAuthUser({ telegram_user_id: '123', username: 'alice' }),
    {
      telegram_user_id: '123',
      telegramUserId: '123',
      telegram_username: 'alice',
      username: 'alice',
      first_name: '',
      firstName: '',
      last_name: '',
      lastName: '',
      photo_url: '',
      photoUrl: '',
    },
  )

  assert.deepEqual(
    normalizeTelegramAuthUser({ id: '777', first_name: 'Ana' }),
    {
      id: '777',
      telegram_user_id: '777',
      telegramUserId: '777',
      telegram_username: '',
      username: '',
      first_name: 'Ana',
      firstName: 'Ana',
      last_name: '',
      lastName: '',
      photo_url: '',
      photoUrl: '',
    },
  )
})

test('token-first auth succeeds by recovering user from widget payload when response user is missing', async () => {
  const user = await resolveTelegramAuthUser({
    accessToken: 'token-1',
    responseUser: null,
    widgetUser: { id: '42', username: 'tg_user' },
  })

  assert.equal(user.telegram_user_id, '42')
  assert.equal(user.telegram_username, 'tg_user')
})

test('token-first auth succeeds by recovering user from /auth/me when response and widget users are invalid', async () => {
  let meLookups = 0
  const user = await resolveTelegramAuthUser({
    accessToken: 'token-2',
    responseUser: null,
    widgetUser: { username: 'missing_id' },
    fetchMeUser: async () => {
      meLookups += 1
      return { telegram_user_id: '919', username: 'from_me' }
    },
  })

  assert.equal(meLookups, 1)
  assert.equal(user.telegram_user_id, '919')
  assert.equal(user.username, 'from_me')
})

test('token-first auth fails truthfully when token is missing', async () => {
  await assert.rejects(
    () => resolveTelegramAuthUser({ accessToken: '', widgetUser: { id: '42' } }),
    /missing_session_token/,
  )
})

test('token-first auth fails truthfully when token exists but user cannot be derived from widget or /auth/me', async () => {
  await assert.rejects(
    () =>
      resolveTelegramAuthUser({
        accessToken: 'token-3',
        responseUser: { username: 'still_missing_id' },
        widgetUser: { first_name: 'NoId' },
        fetchMeUser: async () => ({ username: 'still_missing_id' }),
      }),
    /missing_or_invalid_user/,
  )
})
