import { appendFileSync, mkdirSync, readFileSync, existsSync } from 'fs';
import path from 'path';
import { createHash } from 'crypto';

const TRUE = new Set(['1', 'true', 'yes', 'on']);

function pseudonym(value) {
  return `participant_${createHash('sha256').update(String(value || 'unknown').trim().toLowerCase()).digest('hex').slice(0, 12)}`;
}

function enabled() {
  return TRUE.has(String(process.env.WHATSAPP_SOURCE_SPOOL_ENABLED || '').toLowerCase());
}

function allowlist() {
  const file = process.env.WHATSAPP_SOURCE_ALLOWLIST_FILE;
  if (!file || !existsSync(file)) return {};
  try {
    const parsed = JSON.parse(readFileSync(file, 'utf8'));
    return parsed.groups || parsed;
  } catch (error) {
    console.warn('[whatsapp-source-spool] invalid allowlist:', error.message);
    return {};
  }
}

function redact(value) {
  let text = String(value || '');
  text = text.replace(/\b(?:Bearer\s+)?(?:sk-[A-Za-z0-9_-]{12,}|eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_.-]+|(?:token|secret|api[_ -]?key|password|passwd)\s*[:=]\s*[^\s,;]+)\b/gi, '[REDACTED_CREDENTIAL]');
  text = text.replace(/\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi, '[REDACTED_EMAIL]');
  text = text.replace(/(?<!\w)\+?\d[\d ().-]{7,}\d(?!\w)/g, '[REDACTED_PHONE]');
  return text.replace(/https?:\/\/[^\s<>]+/gi, (url) => {
    const safe = url.replace(/[?#].*$/, '').replace(/[.,);]>]+$/, '');
    return (/[?#]|token|secret|signature|sig|auth/i.test(url) ? '[REDACTED_URL:' : '[URL:') + safe.replace(/^https?:\/\//, '') + ']';
  });
}

export function createSourceSpool() {
  const root = process.env.WHATSAPP_SOURCE_SPOOL_DIR;
  const groups = allowlist();
  if (!enabled() || !root || Object.keys(groups).length === 0) return null;
  mkdirSync(root, { recursive: true, mode: 0o700 });
  const file = path.join(root, `bridge-${new Date().toISOString().slice(0, 10)}.jsonl`);
  return (event) => {
    if (!event || !event.isGroup || !groups[event.chatId]) return false;
    const minimized = {
      messageId: event.messageId,
      chatId: event.chatId,
      groupAlias: groups[event.chatId],
      senderId: pseudonym(event.senderId),
      senderName: event.senderName ? pseudonym(event.senderName) : undefined,
      body: redact(event.body),
      hasMedia: Boolean(event.hasMedia),
      mediaType: event.hasMedia ? event.mediaType || 'media' : null,
      mime: event.hasMedia ? event.mime || null : null,
      timestamp: event.timestamp,
      isGroup: true,
      source: 'hermes-whatsapp-baileys-bridge',
    };
    appendFileSync(file, JSON.stringify(minimized) + '\n', { mode: 0o600 });
    return true;
  };
}
