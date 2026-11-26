/* Puente de mensajería hecho para Vortex AI Desktop. */
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import axios from 'axios';
import dotenv from 'dotenv';
import NodeCache from 'node-cache';
import pino from 'pino';
import qrcode from 'qrcode-terminal';
import makeWASocket, {
  DisconnectReason,
  downloadMediaMessage,
  makeCacheableSignalKeyStore,
  useMultiFileAuthState,
} from '@whiskeysockets/baileys';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const isMainEntry =
  Boolean(process.argv[1]) &&
  path.resolve(process.argv[1]) === path.resolve(__filename);
const ROOT_DIR = path.resolve(__dirname, '..');
const TEMP_DIR = path.join(ROOT_DIR, 'temp');
const AUTH_DIR = path.join(__dirname, 'auth_info_baileys');
const PROCESSED_FILE = path.join(__dirname, 'processed_messages.json');
const CONTACTS_FILE = path.join(__dirname, 'contacts_cache.json');
const MAX_PROCESSED_IDS = 500;
const STARTUP_GRACE_MS = 8000;
const BACKEND_TIMEOUT_MS = 660000;

let connectionOpenedAt = 0;
let syncReadyTimer = null;

dotenv.config({ path: path.join(ROOT_DIR, '.env') });
fs.mkdirSync(TEMP_DIR, { recursive: true });

const AGENT_HOST = (process.env.AGENT_HOST || '127.0.0.1').trim();
const AGENT_PORT = (process.env.AGENT_PORT || '8000').trim();
const BACKEND_URL = `http://${AGENT_HOST}:${AGENT_PORT}`;
const ALLOWED_PHONE = (process.env.ALLOWED_PHONE_NUMBER || '').replace(/\D/g, '');
const BOT_PREFIX = (process.env.BOT_PREFIX || '').trim();
const AI_REPLY_PREFIX = '>_ ';
const WHATSAPP_NOTIFY_PORT = Number((process.env.WHATSAPP_NOTIFY_PORT || '8001').trim()) || 8001;

let activeSock = null;
let manualLogout = false;

function formatAiReply(text) {
  const clean = String(text || '').trim();
  if (!clean) return '';
  const protectedCode = [];
  const codeSafeText = clean.replace(/```[\s\S]*?```|`[^`\n]*`/g, (code) => {
    const marker = `\u0000CODE_${protectedCode.length}\u0000`;
    protectedCode.push(code);
    return marker;
  });
  const whatsappText = codeSafeText
    .replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, '*$1*')
    .replace(/^\s{0,3}#{1,6}\s+(.+)$/gm, (_, title) => `*${title.replace(/\*/g, '').trim()}*`)
    .replace(/^\s*[-+*]\s+/gm, '• ')
    .replace(/^\s*>\s?/gm, '▌ ')
    .replace(/\[([^\]]+)]\((https?:\/\/[^\s)]+)\)/g, '$1: $2')
    .replace(/^\s*_{3,}\s*$|^\s*-{3,}\s*$|^\s*\*{3,}\s*$/gm, '────────')
    .replace(/\n{3,}/g, '\n\n')
    .replace(/\u0000CODE_(\d+)\u0000/g, (_, index) => protectedCode[Number(index)] || '');
  return whatsappText.startsWith(AI_REPLY_PREFIX)
    ? whatsappText
    : `${AI_REPLY_PREFIX}${whatsappText}`;
}

const socketFactory =
  typeof makeWASocket === 'function'
    ? makeWASocket
    : makeWASocket?.default || makeWASocket?.makeWASocket;

const NOISY_SIGNAL_PATTERNS = [
  'Bad MAC',
  'Failed to decrypt message with any known session',
  'SessionError',
  'Closing open session',
  'Closing session: SessionEntry',
  'MessageCounterError',
  'Key used already or never filled',
  'Decrypted message with closed session',
  'Removing old closed session',
];

function isNoisySignalLog(args) {
  const joined = args
    .map((item) => (typeof item === 'string' ? item : String(item?.message || item || '')))
    .join(' ');
  return NOISY_SIGNAL_PATTERNS.some((pattern) => joined.includes(pattern));
}

function installLogFilters() {
  for (const method of ['log', 'warn', 'error']) {
    const original = console[method].bind(console);
    console[method] = (...args) => {
      if (isNoisySignalLog(args)) return;
      original(...args);
    };
  }

  for (const stream of [process.stdout, process.stderr]) {
    const originalWrite = stream.write.bind(stream);
    stream.write = (chunk, encoding, callback) => {
      const text = typeof chunk === 'string' ? chunk : chunk?.toString?.('utf8') || '';
      if (NOISY_SIGNAL_PATTERNS.some((pattern) => text.includes(pattern))) {
        if (typeof encoding === 'function') encoding();
        else if (typeof callback === 'function') callback();
        return true;
      }
      return originalWrite(chunk, encoding, callback);
    };
  }
}

installLogFilters();

const logger = pino({ level: 'silent' });

// Control anti-bucle y persistencia de mensajes ya procesados
const sentByBotIds = new Set();
const recentBotTexts = new Map();
const inFlightMessageIds = new Set();
const sentMessageStore = new NodeCache({ stdTTL: 86400, checkperiod: 600 });
const msgRetryCounterCache = new NodeCache();

function loadProcessedMessageIds() {
  try {
    if (!fs.existsSync(PROCESSED_FILE)) return [];
    const parsed = JSON.parse(fs.readFileSync(PROCESSED_FILE, 'utf8'));
    if (Array.isArray(parsed)) {
      return parsed.filter((id) => typeof id === 'string').slice(-MAX_PROCESSED_IDS);
    }
  } catch {
    // Ignorar archivo corrupto y regenerar limpio
  }
  return [];
}

const processedMessageList = loadProcessedMessageIds();
const processedMessageIds = new Set(processedMessageList);

function markMessageProcessed(msgId) {
  if (!msgId || processedMessageIds.has(msgId)) return;
  processedMessageIds.add(msgId);
  processedMessageList.push(msgId);
  while (processedMessageList.length > MAX_PROCESSED_IDS) {
    const removed = processedMessageList.shift();
    if (removed) processedMessageIds.delete(removed);
  }
  try {
    fs.writeFileSync(PROCESSED_FILE, JSON.stringify(processedMessageList, null, 2), 'utf8');
  } catch {
    // No bloquear ejecución si falla escritura en disco
  }
}

function _getTimestampSeconds(msg) {
  const ts = msg?.messageTimestamp;
  if (ts == null) return 0;
  return typeof ts === 'object' && ts !== null
    ? ts.low || Number(ts) || 0
    : Number(ts) || 0;
}

// Índice persistente de contactos y chats conocidos
const contactsIndex = new Map();
let contactsSaveTimer = null;

function loadContactsCache() {
  try {
    if (!fs.existsSync(CONTACTS_FILE)) return;
    const parsed = JSON.parse(fs.readFileSync(CONTACTS_FILE, 'utf8'));
    if (Array.isArray(parsed)) {
      for (const item of parsed) {
        if (item?.jid && typeof item.jid === 'string') {
          contactsIndex.set(item.jid, item);
        }
      }
    }
  } catch {
    // Ignorar caché corrupto y regenerar
  }
}

function flushContactsCache() {
  try {
    const arr = Array.from(contactsIndex.values());
    fs.writeFileSync(CONTACTS_FILE, JSON.stringify(arr, null, 2), 'utf8');
  } catch {
    // No bloquear ejecución si falla escritura en disco
  }
}

function scheduleSaveContactsCache() {
  if (!isMainEntry) return;
  if (contactsSaveTimer) return;
  contactsSaveTimer = setTimeout(() => {
    contactsSaveTimer = null;
    flushContactsCache();
  }, 500);
}

function normalizePeruvianPhone(digits) {
  const clean = String(digits || '').replace(/\D/g, '');
  if (!clean) return '';
  return clean.length === 9 && clean.startsWith('9') ? `51${clean}` : clean;
}

function normalizeContactJid(raw) {
  const candidates = [raw?.jid, raw?.id, raw?.lid].map((v) => String(v || '').trim()).filter(Boolean);
  const rawPhone = normalizePeruvianPhone(raw?.phoneNumber || raw?.phone || '');
  const primary = candidates[0] || '';
  if (
    primary === 'status@broadcast' ||
    primary.endsWith('@g.us') ||
    primary.endsWith('@newsletter') ||
    primary.endsWith('@broadcast')
  ) {
    return { jid: '', phone: '', lid: '' };
  }

  let resolvedLid = '';
  for (const cand of candidates) {
    if (cand.endsWith('@lid')) {
      resolvedLid = cand;
      break;
    }
  }

  if (rawPhone.length >= 7) {
    return { jid: `${rawPhone}@s.whatsapp.net`, phone: rawPhone, lid: resolvedLid };
  }

  for (const cand of candidates) {
    if (cand.includes('@s.whatsapp.net')) {
      const user = normalizePeruvianPhone(extractJidUser(cand));
      if (user) {
        return { jid: `${user}@s.whatsapp.net`, phone: rawPhone || user, lid: resolvedLid };
      }
    }
  }

  if (resolvedLid) {
    return { jid: resolvedLid, phone: '', lid: resolvedLid };
  }

  return { jid: '', phone: '', lid: '' };
}

function upsertContactEntry(raw) {
  if (!raw || typeof raw !== 'object') return false;
  const { jid, phone, lid } = normalizeContactJid(raw);
  if (!jid) return false;

  const existingFromLid = lid && lid !== jid ? contactsIndex.get(lid) : null;
  const prev = contactsIndex.get(jid) ||
    existingFromLid || {
      jid,
      phone,
      name: '',
      notify: '',
      verifiedName: '',
      updatedAt: 0,
    };

  if (existingFromLid && lid !== jid) {
    contactsIndex.delete(lid);
  }

  const incomingName = typeof raw.name === 'string' ? raw.name.trim() : '';
  const incomingSubject = typeof raw.subject === 'string' ? raw.subject.trim() : '';
  const incomingNotify =
    typeof raw.notify === 'string'
      ? raw.notify.trim()
      : typeof raw.pushName === 'string'
        ? raw.pushName.trim()
        : '';
  const incomingVerified = typeof raw.verifiedName === 'string' ? raw.verifiedName.trim() : '';

  const next = {
    jid,
    phone: phone || prev.phone,
    name: incomingName || incomingSubject || prev.name || '',
    notify: incomingNotify || prev.notify || '',
    verifiedName: incomingVerified || prev.verifiedName || '',
    vip: Boolean(raw.vip || prev.vip),
    updatedAt: Date.now(),
  };

  const changed =
    next.name !== prev.name ||
    next.notify !== prev.notify ||
    next.verifiedName !== prev.verifiedName ||
    next.vip !== Boolean(prev.vip) ||
    next.jid !== prev.jid ||
    !contactsIndex.has(jid);

  if (changed) {
    contactsIndex.set(jid, next);
    scheduleSaveContactsCache();
  }
  return changed;
}

function normalizeTextForMatch(str) {
  let cleaned = String(str || '')
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s]/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  for (const prefix of ['mi ', 'el ', 'la ', 'contacto ', 'a ']) {
    if (cleaned.startsWith(prefix)) {
      cleaned = cleaned.slice(prefix.length).trim();
    }
  }
  return cleaned;
}

function resolveContactTarget(query) {
  const rawQuery = String(query || '').trim();
  if (!rawQuery) return { ok: false, suggestions: [] };

  const digitsOnly = rawQuery.replace(/\D/g, '');
  const looksLikePhone =
    digitsOnly.length >= 8 &&
    (/^[\d\s+\-()]+$/.test(rawQuery) || digitsOnly.length >= 9);
  if (looksLikePhone) {
    const normalizedPhone = normalizePeruvianPhone(digitsOnly);
    return {
      ok: true,
      jid: `${normalizedPhone}@s.whatsapp.net`,
      phone: normalizedPhone,
      matchedName: normalizedPhone,
      matchType: 'phone',
    };
  }

  const normQuery = normalizeTextForMatch(rawQuery);
  if (!normQuery) return { ok: false, suggestions: [] };

  const queryTokens = normQuery.split(/\s+/).filter((t) => t.length >= 2);
  const escapedQuery = normQuery.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const wordRegex = new RegExp(`(?:^|\\s)${escapedQuery}(?:\\s|$)`);

  let bestMatch = null;
  for (const entry of contactsIndex.values()) {
    const entryPhone = normalizePeruvianPhone(
      entry.phone ||
        (String(entry.jid || '').includes('@s.whatsapp.net') ? extractJidUser(entry.jid) : '')
    );
    const preferredJid =
      entryPhone.length >= 7 ? `${entryPhone}@s.whatsapp.net` : String(entry.jid || '');

    const candidates = [
      { field: entry.name, weight: 30, kind: 'name' },
      { field: entry.verifiedName, weight: 20, kind: 'verifiedName' },
      { field: entry.notify, weight: 10, kind: 'notify' },
      { field: entryPhone || entry.phone, weight: 5, kind: 'phone' },
    ];
    for (const c of candidates) {
      if (!c.field) continue;
      const normField = normalizeTextForMatch(c.field);
      if (!normField) continue;

      const fieldTokens = normField.split(/\s+/).filter(Boolean);
      let matchedTokens = 0;
      for (const qt of queryTokens) {
        if (
          fieldTokens.some(
            (ft) =>
              ft === qt ||
              (qt.length >= 3 && (ft.includes(qt) || (ft.length >= 3 && qt.includes(ft))))
          )
        ) {
          matchedTokens += 1;
        }
      }

      let score = 0;
      if (normField === normQuery) score = 400 + c.weight;
      else if (wordRegex.test(normField)) score = 300 + c.weight;
      else if (queryTokens.length > 0 && matchedTokens === queryTokens.length)
        score = 260 + c.weight;
      else if (
        normField.includes(normQuery) ||
        (normQuery.length >= 3 && normQuery.includes(normField))
      )
        score = 180 + c.weight;
      else if (matchedTokens > 0) score = 120 + matchedTokens * 40 + c.weight;

      if (score > 0) {
        const lenDiff = Math.abs(normField.length - normQuery.length);
        const sJidBonus = preferredJid.endsWith('@s.whatsapp.net') ? 1 : 0;
        const rank = score * 1000 - lenDiff + sJidBonus;
        if (!bestMatch || rank > bestMatch.rank) {
          bestMatch = {
            rank,
            jid: preferredJid,
            phone: entryPhone,
            matchedName: entry.name || entry.verifiedName || entry.notify || entryPhone || entry.phone,
            matchType: c.kind,
          };
        }
      }
    }
  }

  if (!bestMatch) {
    const suggestions = [];
    for (const entry of contactsIndex.values()) {
      const label = entry.name || entry.verifiedName || entry.notify || entry.phone;
      if (label && !suggestions.includes(label)) {
        suggestions.push(label);
      }
      if (suggestions.length >= 10) break;
    }
    return { ok: false, suggestions };
  }
  return {
    ok: true,
    jid: bestMatch.jid,
    phone: bestMatch.phone,
    matchedName: bestMatch.matchedName,
    matchType: bestMatch.matchType,
  };
}

loadContactsCache();

function trackSentMessage(sentMsg, textFallback = '') {
  const msgId = sentMsg?.key?.id;
  if (msgId) {
    if (sentMsg?.message) {
      sentMessageStore.set(msgId, sentMsg.message);
    }
    sentByBotIds.add(msgId);
    markMessageProcessed(msgId);
    if (sentByBotIds.size > 500) {
      const oldest = sentByBotIds.values().next().value;
      sentByBotIds.delete(oldest);
    }
  }
  if (textFallback) {
    recentBotTexts.set(textFallback, Date.now());
    for (const [txt, ts] of recentBotTexts.entries()) {
      if (Date.now() - ts > 10000) recentBotTexts.delete(txt);
    }
  }
}

function extractJidUser(jid) {
  if (!jid || typeof jid !== 'string') return '';
  return jid.split('@')[0].split(':')[0];
}

function extractTextFromMessage(messageObj) {
  if (!messageObj) return '';
  if (typeof messageObj.conversation === 'string') {
    return messageObj.conversation;
  }
  if (messageObj.extendedTextMessage?.text) {
    return messageObj.extendedTextMessage.text;
  }
  if (messageObj.ephemeralMessage?.message) {
    return extractTextFromMessage(messageObj.ephemeralMessage.message);
  }
  if (messageObj.viewOnceMessage?.message) {
    return extractTextFromMessage(messageObj.viewOnceMessage.message);
  }
  return '';
}

function extractAudioMessage(messageObj) {
  if (!messageObj) return null;
  if (messageObj.audioMessage) return messageObj.audioMessage;
  if (messageObj.ephemeralMessage?.message?.audioMessage) {
    return messageObj.ephemeralMessage.message.audioMessage;
  }
  return null;
}

function isAuthorizedSelfOrWhitelist(sock, msg) {
  const remoteJid = msg?.key?.remoteJid || '';
  if (!remoteJid) return false;

  // Ignorar estados, grupos y canales
  if (
    remoteJid === 'status@broadcast' ||
    remoteJid.endsWith('@g.us') ||
    remoteJid.endsWith('@newsletter')
  ) {
    return false;
  }

  const remoteUser = extractJidUser(remoteJid);
  const myNumber = extractJidUser(sock.user?.id || '');
  const myLid = extractJidUser(sock.user?.lid || '');
  const isFromMe = Boolean(msg.key.fromMe);

  const isSelfChatTarget =
    (myNumber && remoteUser === myNumber) ||
    (myLid && remoteUser === myLid) ||
    (ALLOWED_PHONE && remoteUser === ALLOWED_PHONE);

  // Caso 1: Me escribo a mí mismo en el chat personal ("Tú")
  if (isFromMe) {
    return isSelfChatTarget;
  }

  // Caso 2: Mensaje entrante directo desde el número autorizado en .env (si es distinto al vinculado)
  if (ALLOWED_PHONE && remoteUser === ALLOWED_PHONE) {
    return true;
  }

  return false;
}

async function callBackend(endpoint, payload, timeoutMs = BACKEND_TIMEOUT_MS) {
  const response = await axios.post(`${BACKEND_URL}${endpoint}`, payload, {
    headers: { 'Content-Type': 'application/json' },
    timeout: timeoutMs,
    validateStatus: () => true,
    maxContentLength: Infinity,
    maxBodyLength: Infinity,
  });

  const rawData = response.data;
  let parsed = null;
  let rawText = '';

  if (rawData && typeof rawData === 'object') {
    parsed = rawData;
  } else if (typeof rawData === 'string') {
    rawText = rawData;
    try {
      parsed = rawText ? JSON.parse(rawText) : null;
    } catch (_err) {
      parsed = null;
    }
  } else if (rawData != null) {
    rawText = String(rawData);
  }

  if (response.status < 200 || response.status >= 300) {
    const detail =
      (parsed && (parsed.reply || parsed.detail || parsed.error)) ||
      rawText ||
      `HTTP ${response.status}`;
    throw new Error(`HTTP ${response.status}: ${detail}`);
  }

  if (parsed && typeof parsed === 'object') {
    return parsed;
  }
  return {
    reply: rawText || '⚠️ Respuesta no-JSON recibida del backend.',
    image_path: null,
    images: [],
    files: [],
  };
}

function reportBridgeState(status, detail = '', qrText = '') {
  callBackend(
    '/api/whatsapp/state',
    { status, detail, qr_text: qrText, account_phone: extractJidUser(activeSock?.user?.id || ''), account_name: activeSock?.user?.name || '' },
    2500
  ).catch(() => {
    // No bloquear el puente si FastAPI todavía está arrancando
  });
}

function getMimeTypeForFile(filePath) {
  const ext = path.extname(filePath).toLowerCase();
  const mimeMap = {
    '.pdf': 'application/pdf',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.doc': 'application/msword',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.xls': 'application/vnd.ms-excel',
    '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    '.zip': 'application/zip',
    '.rar': 'application/vnd.rar',
    '.txt': 'text/plain',
    '.md': 'text/markdown',
    '.csv': 'text/csv',
    '.json': 'application/json',
    '.py': 'text/x-python',
    '.js': 'text/javascript',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
  };
  return mimeMap[ext] || 'application/octet-stream';
}

async function reactToMessage(sock, msg, emoji) {
  const remoteJid = msg?.key?.remoteJid;
  if (!remoteJid || !msg?.key?.id) return;
  try {
    await sock.sendMessage(remoteJid, {
      react: {
        text: emoji,
        key: msg.key,
      },
    });
  } catch {
    // Ignorar errores no críticos al enviar reacciones
  }
}

async function sendAgentResponse(sock, remoteJid, quotedMsg, data) {
  const images = Array.isArray(data.images)
    ? data.images.filter((p) => p && fs.existsSync(p))
    : data.image_path && fs.existsSync(data.image_path)
      ? [data.image_path]
      : [];
  const files = Array.isArray(data.files)
    ? data.files.filter((p) => p && fs.existsSync(p))
    : [];
  const replyText = formatAiReply(data.reply);
  let textDelivered = false;

  if (images.length > 0) {
    for (let i = 0; i < images.length; i += 1) {
      const imgPath = images[i];
      const caption = i === 0 && replyText ? replyText : undefined;
      const sentImg = await sock.sendMessage(
        remoteJid,
        {
          image: fs.readFileSync(imgPath),
          caption,
        },
        { quoted: quotedMsg }
      );
      trackSentMessage(sentImg, caption || '');
      if (caption) textDelivered = true;
    }
  }

  if (!textDelivered && replyText && (files.length === 0 || replyText.length > 900)) {
    const sentTxt = await sock.sendMessage(
      remoteJid,
      { text: replyText },
      { quoted: quotedMsg }
    );
    trackSentMessage(sentTxt, replyText);
    textDelivered = true;
  }

  for (let i = 0; i < files.length; i += 1) {
    const filePath = files[i];
    const fileName = path.basename(filePath);
    const mimetype = getMimeTypeForFile(filePath);
    const caption = !textDelivered && i === 0 && replyText ? replyText : undefined;

    const sentDoc = await sock.sendMessage(
      remoteJid,
      {
        document: fs.readFileSync(filePath),
        fileName,
        mimetype,
        caption,
      },
      { quoted: quotedMsg }
    );
    trackSentMessage(sentDoc, caption || '');
    if (caption) textDelivered = true;
  }

  if (!textDelivered && replyText) {
    const sentTxt = await sock.sendMessage(
      remoteJid,
      { text: replyText },
      { quoted: quotedMsg }
    );
    trackSentMessage(sentTxt, replyText);
  }
}

async function handleIncomingMessage(sock, msg) {
  if (!msg?.message || !msg?.key) return;

  const msgId = msg.key.id;
  if (
    msgId &&
    (sentByBotIds.has(msgId) ||
      processedMessageIds.has(msgId) ||
      inFlightMessageIds.has(msgId))
  ) {
    return;
  }

  if (!isAuthorizedSelfOrWhitelist(sock, msg)) {
    return;
  }

  if (msgId) inFlightMessageIds.add(msgId);
  try {
    await _dispatchAuthorizedMessage(sock, msg, msgId);
  } finally {
    if (msgId) inFlightMessageIds.delete(msgId);
  }
}

async function _dispatchAuthorizedMessage(sock, msg, msgId) {
  const remoteJid = msg.key.remoteJid;
  const senderKey = extractJidUser(remoteJid) || 'default';

  // 1. Verificar si es nota de voz (Audio)
  const audioMsg = extractAudioMessage(msg.message);
  if (audioMsg) {
    markMessageProcessed(msgId);
    try {
      await sock.readMessages([msg.key]);
    } catch {
      // Ignorar errores no críticos al marcar leído
    }
    console.log(`[WhatsApp] Nota de voz autorizada recibida de ${remoteJid}`);
    console.log('[GUI_EVENT] ORDER|[Nota de voz]');
    await reactToMessage(sock, msg, '⏳');
    await sock.sendPresenceUpdate('composing', remoteJid);

    const tempAudioPath = path.join(TEMP_DIR, `audio_${Date.now()}.ogg`);
    try {
      const buffer = await downloadMediaMessage(
        msg,
        'buffer',
        {},
        { logger, reuploadRequest: sock.updateMediaMessage }
      );
      fs.writeFileSync(tempAudioPath, buffer);

      const data = await callBackend('/audio', {
        audio_path: tempAudioPath,
        sender: senderKey,
      });
      await sendAgentResponse(sock, remoteJid, msg, data);
      await reactToMessage(sock, msg, '✅');
    } catch (err) {
      console.error('[Error Audio]', err);
      const sentErr = await sock.sendMessage(
        remoteJid,
        { text: formatAiReply(`Error procesando tu nota de voz: ${err.message}`) },
        { quoted: msg }
      );
      trackSentMessage(sentErr);
      await reactToMessage(sock, msg, '❌');
    } finally {
      await sock.sendPresenceUpdate('paused', remoteJid);
      if (fs.existsSync(tempAudioPath)) {
        try {
          fs.unlinkSync(tempAudioPath);
        } catch {
          // El backend también limpia el archivo temporal
        }
      }
    }
    return;
  }

  // 2. Verificar si es mensaje de texto
  let text = extractTextFromMessage(msg.message).trim();
  if (!text) return;

  // Segunda barrera anti-bucle si Baileys entrega el eco antes del ACK
  if (msg.key.fromMe && recentBotTexts.has(text)) {
    const ts = recentBotTexts.get(text);
    if (Date.now() - ts < 5000) {
      return;
    }
  }

  if (BOT_PREFIX) {
    if (!text.startsWith(BOT_PREFIX)) {
      return;
    }
    text = text.slice(BOT_PREFIX.length).trim();
    if (!text) return;
  }

  markMessageProcessed(msgId);
  try {
    await sock.readMessages([msg.key]);
  } catch {
    // Ignorar errores no críticos al marcar leído
  }
  const preview = text.length > 90 ? `${text.slice(0, 87)}...` : text;
  console.log(`[WhatsApp] Mensaje autorizado: "${text}"`);
  console.log(`[GUI_EVENT] ORDER|${preview.replace(/[\r\n]+/g, ' ')}`);
  await reactToMessage(sock, msg, '⏳');
  await sock.sendPresenceUpdate('composing', remoteJid);

  try {
    const endpoint = text.toLowerCase() === '/reset' ? '/reset' : '/chat';
    const payload =
      endpoint === '/reset'
        ? { sender: senderKey }
        : { message: text, sender: senderKey };

    const data = await callBackend(endpoint, payload);
    if (endpoint === '/reset') {
      const sentReset = await sock.sendMessage(
        remoteJid,
        { text: formatAiReply('Memoria de conversación reiniciada.') },
        { quoted: msg }
      );
      trackSentMessage(sentReset, formatAiReply('Memoria de conversación reiniciada.'));
    } else {
      await sendAgentResponse(sock, remoteJid, msg, data);
    }
    await reactToMessage(sock, msg, '✅');
  } catch (err) {
    console.error('[Error Chat]', err);
    const errText = formatAiReply(
      `No pude comunicarme con el backend Python (${BACKEND_URL}): ${err.message}`
    );
    const sentErr = await sock.sendMessage(
      remoteJid,
      { text: errText },
      { quoted: msg }
    );
    trackSentMessage(sentErr, errText);
    await reactToMessage(sock, msg, '❌');
  } finally {
    await sock.sendPresenceUpdate('paused', remoteJid);
  }
}

async function startWhatsAppBridge() {
  if (manualLogout) return;
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  // Usa la versión incluida en Baileys: generar el QR no depende de GitHub.
  reportBridgeState('connecting', 'Conectando con WhatsApp para obtener el QR…');
  const sock = socketFactory({
    connectTimeoutMs: 20000,
    auth: {
      creds: state.creds,
      keys: makeCacheableSignalKeyStore(state.keys, logger),
    },
    msgRetryCounterCache,
    getMessage: async (key) => {
      if (!key?.id) return undefined;
      const stored = sentMessageStore.get(key.id);
      return stored || undefined;
    },
    logger,
    printQRInTerminal: false,
    syncFullHistory: false,
    browser: ['Vortex-AI-Desktop', 'Windows', '1.0.0'],
    markOnlineOnConnect: false,
  });

  activeSock = sock;
  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      console.log('\n==============================================================');
      console.log('Escanea este código QR en WhatsApp -> Dispositivos vinculados:');
      console.log('==============================================================\n');
      console.log('[GUI_EVENT] QR_REQUIRED');
      console.log('[GUI_QR_START]');
      qrcode.generate(qr, { small: true });
      console.log('[GUI_QR_END]');
      reportBridgeState('qr', 'Escanea el código QR para vincular WhatsApp', qr);
    }

    if (connection === 'open') {
      connectionOpenedAt = Date.now();
      if (syncReadyTimer) {
        clearTimeout(syncReadyTimer);
        syncReadyTimer = null;
      }
      const myUser = extractJidUser(sock.user?.id || '');
      console.log('\n[WhatsApp] Conectado exitosamente.');
      console.log(`[WhatsApp] Cuenta vinculada: ${myUser}`);
      console.log(
        `[WhatsApp] Whitelist (.env): ${ALLOWED_PHONE || '(solo self-chat activo)'}`
      );
      console.log(`[WhatsApp] Backend Python: ${BACKEND_URL}`);
      console.log('[WhatsApp] Escudo de arranque activo (8s) descartando historial viejo...\n');
      console.log('[GUI_EVENT] WA_SYNCING|8');
      reportBridgeState('syncing', `Cuenta ${myUser || 'vinculada'} sincronizando (8s)`);
      syncReadyTimer = setTimeout(() => {
        console.log(`[GUI_EVENT] WA_READY|${myUser}`);
        reportBridgeState('connected', myUser ? `Conectado (${myUser})` : 'Conectado');
      }, STARTUP_GRACE_MS);
    }

    if (connection === 'close') {
      if (syncReadyTimer) {
        clearTimeout(syncReadyTimer);
        syncReadyTimer = null;
      }
      const statusCode = lastDisconnect?.error?.output?.statusCode;
      console.log(`[WhatsApp] Conexión cerrada (código ${statusCode ?? 'desconocido'}).`);
      reportBridgeState('disconnected', statusCode === 408 ? 'WhatsApp no respondió. Comprueba la conexión o prueba otra red y vuelve a generar el QR.' : `Conexión cerrada (código ${statusCode ?? 'desconocido'}).`);

      if (statusCode === 440) {
        console.log(
          '[WhatsApp] Sesión reemplazada (código 440). Cerrando proceso limpiamente para evitar guerra de sesiones.'
        );
        console.log('[GUI_EVENT] WA_CLOSED|440|false');
        process.exit(0);
      }

      if (manualLogout) return;
      const shouldReconnect = statusCode !== DisconnectReason.loggedOut;
      console.log(`[GUI_EVENT] WA_CLOSED|${statusCode ?? 'desconocido'}|${shouldReconnect}`);
      if (shouldReconnect) {
        setTimeout(() => {
          startWhatsAppBridge().catch((err) =>
            console.error('[WhatsApp] Error al reconectar:', err)
          );
        }, 3000);
      } else {
        console.log(
          '[WhatsApp] Sesión cerrada desde el teléfono. Borra whatsapp_bridge/auth_info_baileys y vuelve a escanear el QR.'
        );
      }
    }
  });

  sock.ev.on('messaging-history.set', ({ chats, contacts, messages }) => {
    for (const contact of contacts || []) {
      upsertContactEntry(contact);
    }
    for (const chat of chats || []) {
      upsertContactEntry(chat);
    }
    for (const histMsg of messages || []) {
      const rJid = histMsg?.key?.remoteJid || '';
      const rAlt = histMsg?.key?.remoteJidAlt || '';
      if (rJid || rAlt) {
        upsertContactEntry({
          id: rJid,
          jid: rAlt.endsWith('@s.whatsapp.net') ? rAlt : undefined,
          notify: !histMsg?.key?.fromMe ? histMsg?.pushName || '' : '',
        });
      }
    }
  });

  sock.ev.on('chats.phoneNumberShare', ({ lid, jid }) => {
    if (lid || jid) {
      upsertContactEntry({ lid, jid, id: jid || lid });
    }
  });

  sock.ev.on('lid-mapping.update', (mapping) => {
    const items = Array.isArray(mapping) ? mapping : [mapping];
    for (const item of items) {
      const lid = String(item?.lid || '').trim();
      const rawPn = String(item?.pn || item?.jid || '').trim();
      const pnDigits = normalizePeruvianPhone(rawPn);
      const pnJid = rawPn.includes('@')
        ? rawPn
        : pnDigits
          ? `${pnDigits}@s.whatsapp.net`
          : '';
      if (lid || pnJid) {
        upsertContactEntry({
          lid,
          phoneNumber: pnJid || pnDigits,
          jid: pnJid || lid,
          id: pnJid || lid,
        });
      }
    }
  });

  sock.ev.on('contacts.set', ({ contacts }) => {
    for (const contact of contacts || []) {
      upsertContactEntry(contact);
    }
  });

  sock.ev.on('contacts.upsert', (contacts) => {
    for (const contact of contacts || []) {
      upsertContactEntry(contact);
    }
  });

  sock.ev.on('contacts.update', (updates) => {
    for (const update of updates || []) {
      upsertContactEntry(update);
    }
  });

  sock.ev.on('chats.upsert', (chats) => {
    for (const chat of chats || []) {
      upsertContactEntry(chat);
    }
  });

  sock.ev.on('chats.update', (updates) => {
    for (const update of updates || []) {
      upsertContactEntry(update);
    }
  });

  sock.ev.on('messages.upsert', (upsert) => {
    for (const msg of upsert.messages || []) {
      const rJid = msg?.key?.remoteJid || '';
      const rAlt = msg?.key?.remoteJidAlt || '';
      if (rJid || rAlt) {
        upsertContactEntry({
          id: rJid,
          jid: rAlt.endsWith('@s.whatsapp.net') ? rAlt : undefined,
          notify: !msg?.key?.fromMe ? msg?.pushName || '' : '',
        });
      }
    }

    if (upsert.type !== 'notify') return;

    for (const msg of upsert.messages || []) {
      const msgId = msg?.key?.id;
      if (
        msgId &&
        (sentByBotIds.has(msgId) ||
          processedMessageIds.has(msgId) ||
          inFlightMessageIds.has(msgId))
      ) {
        continue;
      }

      const nowMs = Date.now();
      const openSec = Math.floor(connectionOpenedAt / 1000);
      const msgTsSec = _getTimestampSeconds(msg);
      const inGracePeriod = !connectionOpenedAt || nowMs - connectionOpenedAt < STARTUP_GRACE_MS;

      if (inGracePeriod || !msgTsSec || msgTsSec < openSec) {
        if (msgId) markMessageProcessed(msgId);
        if (msg?.key?.remoteJid && msgId) {
          sock.readMessages([msg.key]).catch(() => {
            // Ignorar errores al marcar leído durante sincronización inicial
          });
        }
        continue;
      }

      // Despacho concurrente sin bloquear órdenes entrantes adicionales
      handleIncomingMessage(sock, msg).catch((err) => {
        console.error('[Error Concurrent Message]', err);
      });
    }
  });
}

function resolveNotifyTargetJid(requestedPhone = '') {
  const digits = String(requestedPhone || ALLOWED_PHONE || '').replace(/\D/g, '');
  if (digits) return `${digits}@s.whatsapp.net`;
  const selfUser = extractJidUser(activeSock?.user?.id || '');
  return selfUser ? `${selfUser}@s.whatsapp.net` : '';
}

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    let raw = '';
    req.on('data', (chunk) => {
      raw += chunk;
      if (raw.length > 1024 * 1024) {
        reject(new Error('Payload demasiado grande'));
        req.destroy();
      }
    });
    req.on('end', () => {
      try {
        resolve(raw ? JSON.parse(raw) : {});
      } catch (err) {
        reject(err);
      }
    });
    req.on('error', reject);
  });
}

function sendJson(res, statusCode, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(statusCode, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
  });
  res.end(body);
}

async function ensureSignalSessionAndCanonicalJid(sock, rawTargetJid, rawPhone = '') {
  if (!sock) return rawTargetJid;
  let targetJid = String(rawTargetJid || '').trim();
  if (!targetJid || targetJid.endsWith('@g.us') || targetJid.endsWith('@broadcast')) {
    return targetJid;
  }

  let digits = normalizePeruvianPhone(
    rawPhone || (targetJid.endsWith('@s.whatsapp.net') ? extractJidUser(targetJid) : '')
  );

  if (
    !digits &&
    targetJid.endsWith('@lid') &&
    typeof sock.signalRepository?.lidMapping?.getPNForLID === 'function'
  ) {
    try {
      const resolvedPn = await sock.signalRepository.lidMapping.getPNForLID(targetJid);
      const pnDigits = normalizePeruvianPhone(extractJidUser(resolvedPn || ''));
      if (pnDigits.length >= 7) {
        digits = pnDigits;
        const pnJid = `${pnDigits}@s.whatsapp.net`;
        upsertContactEntry({
          lid: targetJid,
          phoneNumber: pnDigits,
          jid: pnJid,
          id: pnJid,
        });
        targetJid = pnJid;
      }
    } catch {
      // Continuar con @lid si aún no existe mapeo LID -> PN local
    }
  }

  if (digits.length >= 7) {
    targetJid = `${digits}@s.whatsapp.net`;
    if (typeof sock.onWhatsApp === 'function') {
      try {
        const waResults = await sock.onWhatsApp(digits);
        const waInfo = Array.isArray(waResults) ? waResults[0] : null;
        if (waInfo?.exists && waInfo?.jid) {
          targetJid = waInfo.jid;
        }
      } catch {
        // Mantener JID normalizado si onWhatsApp falla transitoriamente
      }
    }
  }

  if (typeof sock.assertSessions === 'function') {
    await sock.assertSessions([targetJid], false).catch(() => {});
  }
  if (typeof sock.presenceSubscribe === 'function') {
    await sock.presenceSubscribe(targetJid).catch(() => {});
  }

  return targetJid;
}

async function deliverPayloadToJid(targetJid, messageText, imagePaths = [], filePaths = [], rawPhone = '') {
  const effectiveJid = await ensureSignalSessionAndCanonicalJid(activeSock, targetJid, rawPhone);
  const validImages = Array.from(
    new Set(imagePaths.map((p) => String(p || '').trim()).filter((p) => p && fs.existsSync(p)))
  );
  const validFiles = Array.from(
    new Set(filePaths.map((p) => String(p || '').trim()).filter((p) => p && fs.existsSync(p)))
  );
  const textMsg = String(messageText || '').trim() || 'Notificación proactiva del sistema.';
  let textDelivered = false;

  for (let i = 0; i < validImages.length; i += 1) {
    const imgPath = validImages[i];
    const caption = i === 0 && textMsg ? textMsg : undefined;
    const sentImg = await activeSock.sendMessage(effectiveJid, {
      image: fs.readFileSync(imgPath),
      caption,
    });
    trackSentMessage(sentImg, caption || '');
    if (caption) textDelivered = true;
  }

  if (!textDelivered && textMsg && (validFiles.length === 0 || textMsg.length > 900)) {
    const sentTxt = await activeSock.sendMessage(effectiveJid, { text: textMsg });
    trackSentMessage(sentTxt, textMsg);
    textDelivered = true;
  }

  for (let i = 0; i < validFiles.length; i += 1) {
    const filePath = validFiles[i];
    const caption = !textDelivered && i === 0 && textMsg ? textMsg : undefined;
    const sentDoc = await activeSock.sendMessage(effectiveJid, {
      document: fs.readFileSync(filePath),
      fileName: path.basename(filePath),
      mimetype: getMimeTypeForFile(filePath),
      caption,
    });
    trackSentMessage(sentDoc, caption || '');
    if (caption) textDelivered = true;
  }

  if (!textDelivered && textMsg) {
    const sentTxt = await activeSock.sendMessage(effectiveJid, { text: textMsg });
    trackSentMessage(sentTxt, textMsg);
  }

  return effectiveJid;
}

function startNotifyServer() {
  const server = http.createServer(async (req, res) => {
    const parsedUrl = new URL(req.url || '/', 'http://127.0.0.1');
    const reqPath = parsedUrl.pathname || '/';

    if (req.method === 'POST' && reqPath === '/session/logout') {
      if (req.headers.origin) {
        sendJson(res, 403, {ok: false});
        return;
      }
      manualLogout = true;
      if (syncReadyTimer) {
        clearTimeout(syncReadyTimer);
        syncReadyTimer = null;
      }
      try {
        if (activeSock?.user?.id) {
          await activeSock.logout();
          sendJson(res, 200, {ok: true, revoked: true});
        } else {
          sendJson(res, 200, {ok: true, revoked: false});
        }
      } catch {
        sendJson(res, 200, {ok: true, revoked: false});
      }
      return;
    }

    if (req.method === 'GET' && reqPath === '/health') {
      sendJson(res, 200, {
        ok: true,
        connected: Boolean(activeSock?.user?.id),
        contacts_count: contactsIndex.size,
      });
      return;
    }

    if (req.method === 'GET' && reqPath === '/contacts') {
      const list = Array.from(contactsIndex.values()).sort((a, b) => {
        if (Boolean(b.vip) !== Boolean(a.vip)) return b.vip ? 1 : -1;
        return (a.name || a.notify || a.phone).localeCompare(b.name || b.notify || b.phone);
      });
      sendJson(res, 200, {
        ok: true,
        count: list.length,
        contacts: list,
      });
      return;
    }

    if (req.method === 'POST' && reqPath === '/contacts') {
      try {
        const body = await readJsonBody(req);
        const cleanName = String(body.name || '').trim();
        let digits = String(body.phone || '').replace(/\D/g, '');
        if (digits.length === 9 && digits.startsWith('9')) {
          digits = `51${digits}`;
        }
        if (!cleanName || digits.length < 7) {
          sendJson(res, 400, {
            ok: false,
            error: 'Debes indicar "name" y un "phone" válido (mínimo 7 dígitos).',
          });
          return;
        }
        const jid = `${digits}@s.whatsapp.net`;
        const entry = {
          jid,
          phone: digits,
          name: cleanName,
          notify: cleanName,
          verifiedName: '',
          vip: true,
          updatedAt: Date.now(),
        };
        contactsIndex.set(jid, entry);
        flushContactsCache();
        console.log(`[WhatsApp Contacts] Contacto VIP guardado: ${cleanName} (${jid})`);
        sendJson(res, 200, {
          ok: true,
          contact: entry,
          count: contactsIndex.size,
        });
      } catch (err) {
        sendJson(res, 500, { ok: false, error: err?.message || String(err) });
      }
      return;
    }

    if (req.method === 'DELETE' && reqPath === '/contacts') {
      try {
        let jidParam = String(parsedUrl.searchParams.get('jid') || '').trim();
        let phoneParam = String(parsedUrl.searchParams.get('phone') || '').replace(/\D/g, '');
        if (!jidParam && !phoneParam) {
          const body = await readJsonBody(req).catch(() => ({}));
          jidParam = String(body.jid || '').trim();
          phoneParam = String(body.phone || '').replace(/\D/g, '');
        }
        if (!jidParam && phoneParam) {
          if (phoneParam.length === 9 && phoneParam.startsWith('9')) {
            phoneParam = `51${phoneParam}`;
          }
          jidParam = `${phoneParam}@s.whatsapp.net`;
        }
        if (!jidParam) {
          sendJson(res, 400, { ok: false, error: 'Indica jid o phone a eliminar.' });
          return;
        }
        const deleted = contactsIndex.delete(jidParam);
        flushContactsCache();
        sendJson(res, 200, {
          ok: true,
          deleted,
          jid: jidParam,
          count: contactsIndex.size,
        });
      } catch (err) {
        sendJson(res, 500, { ok: false, error: err?.message || String(err) });
      }
      return;
    }

    if (req.method === 'POST' && reqPath === '/notify') {
      try {
        const body = await readJsonBody(req);
        if (!activeSock?.user?.id) {
          sendJson(res, 503, { ok: false, error: 'WhatsApp aún no está conectado.' });
          return;
        }

        const targetJid = resolveNotifyTargetJid(body.phone);
        if (!targetJid) {
          sendJson(res, 400, { ok: false, error: 'No se pudo determinar el JID destino.' });
          return;
        }

        const images = [
          ...(Array.isArray(body.images) ? body.images : []),
          ...(body.image_path ? [body.image_path] : []),
        ];
        const files = [
          ...(Array.isArray(body.files) ? body.files : []),
          ...(body.file_path ? [body.file_path] : []),
        ];

        await deliverPayloadToJid(targetJid, body.message, images, files);
        console.log(`[WhatsApp Notify] Alerta proactiva enviada a ${targetJid}`);
        sendJson(res, 200, { ok: true, target: targetJid });
      } catch (err) {
        sendJson(res, 500, { ok: false, error: err?.message || String(err) });
      }
      return;
    }

    if (req.method === 'POST' && reqPath === '/send-contact') {
      try {
        const body = await readJsonBody(req);
        if (!activeSock?.user?.id) {
          sendJson(res, 503, { ok: false, error: 'WhatsApp aún no está conectado.' });
          return;
        }

        const contactQuery = String(body.contact || '').trim();
        const messageText = String(body.message || '').trim();
        if (!contactQuery || !messageText) {
          sendJson(res, 400, {
            ok: false,
            error: 'Debes indicar "contact" y "message" para enviar WhatsApp a un contacto.',
          });
          return;
        }

        const resolved = resolveContactTarget(contactQuery);
        if (!resolved.ok || !resolved.jid) {
          sendJson(res, 404, {
            ok: false,
            error: `No se encontró ningún contacto coincidente con '${contactQuery}'.`,
            known_contacts: contactsIndex.size,
            suggestions: Array.isArray(resolved.suggestions) ? resolved.suggestions : [],
          });
          return;
        }

        const images = [
          ...(Array.isArray(body.images) ? body.images : []),
          ...(body.image_path ? [body.image_path] : []),
        ];
        const files = [
          ...(Array.isArray(body.files) ? body.files : []),
          ...(body.file_path ? [body.file_path] : []),
        ];

        const deliveredJid = await deliverPayloadToJid(
          resolved.jid,
          messageText,
          images,
          files,
          resolved.phone || ''
        );
        console.log(
          `[WhatsApp Contact] Mensaje enviado a ${resolved.matchedName} (${deliveredJid})`
        );
        sendJson(res, 200, {
          ok: true,
          jid: deliveredJid,
          matched_name: resolved.matchedName,
          match_type: resolved.matchType,
        });
      } catch (err) {
        sendJson(res, 500, { ok: false, error: err?.message || String(err) });
      }
      return;
    }

    sendJson(res, 404, { ok: false, error: 'Ruta no encontrada' });
  });

  server.on('error', (err) => {
    console.error(`[WhatsApp Notify] Error en puerto ${WHATSAPP_NOTIFY_PORT}:`, err?.message || err);
  });

  server.listen(WHATSAPP_NOTIFY_PORT, '127.0.0.1', () => {
    console.log(`[WhatsApp Notify] Servidor local activo en http://127.0.0.1:${WHATSAPP_NOTIFY_PORT}/notify`);
  });
}

if (isMainEntry) {
  console.log('Iniciando puente de WhatsApp (Vortex AI Desktop)...');
  startNotifyServer();
  startWhatsAppBridge().catch((err) => {
    console.error('Error fatal en el puente de WhatsApp:', err);
    process.exit(1);
  });
}

export {
  contactsIndex,
  formatAiReply,
  normalizePeruvianPhone,
  normalizeTextForMatch,
  resolveContactTarget,
  upsertContactEntry,
};
