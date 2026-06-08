/**
 * x.ai (xAI) audio client for jainechat — TTS + STT.
 *
 * Auth: reuses the grok CLI's OAuth session from ~/.grok/auth.json
 * (the JWT carries scope `api:access`, which api.x.ai honours for /v1/tts
 * and /v1/stt — verified by round-trip). Falls back to XAI_API_KEY env.
 * Refreshes the OIDC token via the refresh_token when near expiry / on 401.
 */
import { readFileSync } from 'fs'
import { homedir } from 'os'
import { join } from 'path'

const AUTH_FILE = join(homedir(), '.grok', 'auth.json')
const API = 'https://api.x.ai/v1'
const TOKEN_URL = 'https://auth.x.ai/token'

type Session = {
  key: string
  refresh_token?: string
  expires_at?: string
  oidc_client_id?: string
}

let cached: { token: string; exp: number } | null = null

function readSession(): Session | null {
  try {
    const raw = JSON.parse(readFileSync(AUTH_FILE, 'utf8')) as Record<string, Session>
    return Object.values(raw)[0] ?? null
  } catch {
    return null
  }
}

async function refresh(s: Session): Promise<string | null> {
  if (!s.refresh_token || !s.oidc_client_id) return null
  try {
    const res = await fetch(TOKEN_URL, {
      method: 'POST',
      headers: { 'content-type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        grant_type: 'refresh_token',
        refresh_token: s.refresh_token,
        client_id: s.oidc_client_id,
      }),
    })
    if (!res.ok) return null
    const j = (await res.json()) as { access_token?: string; expires_in?: number }
    if (!j.access_token) return null
    cached = { token: j.access_token, exp: Date.now() + (j.expires_in ?? 3600) * 1000 }
    return j.access_token
  } catch {
    return null
  }
}

export async function getToken(force = false): Promise<string> {
  if (process.env.XAI_API_KEY) return process.env.XAI_API_KEY
  if (!force && cached && cached.exp - Date.now() > 60_000) return cached.token
  const s = readSession()
  if (!s) throw new Error('no x.ai token: set XAI_API_KEY or run `grok` to log in')
  const exp = s.expires_at ? Date.parse(s.expires_at) : 0
  if (force || (exp && exp - Date.now() < 5 * 60_000)) {
    const t = await refresh(s)
    if (t) return t
    if (force) return s.key // refresh failed → last-resort stale key
  }
  cached = { token: s.key, exp: exp || Date.now() + 5 * 60_000 }
  return s.key
}

async function authed(path: string, init: RequestInit, retried = false): Promise<Response> {
  const token = await getToken(retried)
  const res = await fetch(`${API}${path}`, {
    ...init,
    headers: { ...(init.headers ?? {}), authorization: `Bearer ${token}` },
  })
  if (res.status === 401 && !retried) return authed(path, init, true)
  return res
}

/** Synthesize speech. Returns MP3 bytes. */
export async function tts(
  text: string,
  opts?: { voice?: string; language?: string },
): Promise<ArrayBuffer> {
  const res = await authed('/tts', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      text: text.slice(0, 15000),
      voice_id: opts?.voice ?? 'eve',
      language: opts?.language ?? 'auto',
      output_format: { codec: 'mp3', sample_rate: 24000, bit_rate: 128000 },
    }),
  })
  if (!res.ok) throw new Error(`tts ${res.status}: ${await res.text()}`)
  return res.arrayBuffer()
}

/** Transcribe audio. Returns the recognized text (language auto-detected). */
export async function stt(
  audio: ArrayBuffer,
  filename: string,
  opts?: { language?: string },
): Promise<string> {
  const form = new FormData()
  if (opts?.language) {
    form.set('language', opts.language)
    form.set('format', 'true') // text normalization (requires language)
  }
  form.set('file', new Blob([audio]), filename) // file must be last per x.ai docs
  const res = await authed('/stt', { method: 'POST', body: form })
  if (!res.ok) throw new Error(`stt ${res.status}: ${await res.text()}`)
  const j = (await res.json()) as { text?: string }
  return (j.text ?? '').trim()
}
