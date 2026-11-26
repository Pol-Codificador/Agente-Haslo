import assert from 'node:assert/strict';
import test from 'node:test';

import { formatAiReply } from './index.js';

test('adapta Markdown a un mensaje presentable de WhatsApp', () => {
  const input = [
    '### 🌐 **2026 Lab Redes B**',
    '* **⚠️ Vencida recientemente:** Subir Laboratorio',
    '- **📢 Anuncio:** Recordatorio',
    '',
    '[Abrir Classroom](https://classroom.google.com/)',
  ].join('\n');
  assert.equal(
    formatAiReply(input),
    '>_ *🌐 2026 Lab Redes B*\n• *⚠️ Vencida recientemente:* Subir Laboratorio\n• *📢 Anuncio:* Recordatorio\n\nAbrir Classroom: https://classroom.google.com/',
  );
});

test('conserva intactos los fragmentos de código', () => {
  assert.equal(
    formatAiReply('## Ejemplo\n`const title = "##";`\n**Listo**'),
    '>_ *Ejemplo*\n`const title = "##";`\n*Listo*',
  );
});
