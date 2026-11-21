// Programmatic WP826 config client — no browser, fully scriptable.
//   login:  POST /cgi-bin/access {access:sha256(user)} -> salt; POST /cgi-bin/dologin {username,password:sha256(pw+salt)} -> {sid}
//   read:   GET  /cgi-bin/config_get?pvalues=<codes w/o P>&sid=...        -> {configs:[{pvalue,value}]}
//   write:  PUT  /cgi-bin/config_update  JSON {alias:{},pvalue:{"<code>":"<val>"}}
//   meta:   GET  /cgi-bin/metaconfig_get  -> [{pvalue,alias}]   (P-code -> alias name)
// Requires Referer/Origin headers or the server 403s.
import https from 'node:https';
import tls from 'node:tls';
import { readFileSync } from 'node:fs';
import crypto from 'node:crypto';

// The handset takes its address from DHCP and will move. Give it a reservation
// on your router and set WP826_HOST=<ip>; the default below is only a placeholder.
const HOST = process.env.WP826_HOST || '192.168.1.84';
const PORT = Number(process.env.WP826_PORT || 443);
const USER = 'admin';
// Read from a file, never an argv or an env value: both land in shell
// history and in `ps` output for every process on the box. Read only when a
// login needs it, so `fingerprint` (which sends no credentials) works without it.
const PASS_FILE = process.env.WP826_PASS_FILE || '/tmp/.wp_pass';
let passCache = null;
const pass = () => (passCache ??= readFileSync(PASS_FILE, 'utf8').trim());
const sha256 = (s) => crypto.createHash('sha256').update(s).digest('hex');

// ── Certificate pinning ─────────────────────────────────────────────────────
// The handset serves its own self-signed certificate and cannot be given a
// CA-signed one, so chain validation can never succeed and every request below
// sets rejectUnauthorized:false. Trust comes instead from pinning the
// certificate's SHA-256. With a pin set, EVERY request runs on a connection
// whose certificate was checked before the request was handed to it
// (pinnedSocket), so neither the login nor anything after it can reach a peer
// presenting an unexpected certificate. verifyPin() also checks once up front,
// for a clear message and exit code before anything else is tried.
// Set WP826_CERT_SHA256=<hex> (get it with: node tools/wp826.mjs fingerprint).
// Unset => unverified, i.e. the previous behaviour, with a warning.
const CERT_PIN = process.env.WP826_CERT_SHA256 || '';
const normPin = (p) => String(p || '').trim().toLowerCase()
  .replace(/^sha256:/, '').replace(/[^0-9a-f]/g, '');

const isIp = (h) => /^[0-9.]+$/.test(h) || h.includes(':');

// The one TLS connection site: resolves {sock, fp} with the socket still open.
function peerConnect(host, port = PORT) {
  return new Promise((res, rej) => {
    // SNI must NOT be set to an IP literal — Node rejects it outright, and the
    // cordless is normally reached by address.
    const opts = { host, port, rejectUnauthorized: false };
    if (!isIp(host)) opts.servername = host;
    const sock = tls.connect(opts, () => {
      const cert = sock.getPeerCertificate();
      const fp = normPin(cert && cert.fingerprint256);
      if (!fp) { sock.destroy(); rej(new Error('no peer certificate')); return; }
      sock.setTimeout(0);
      res({ sock, fp });
    });
    sock.setTimeout(8000, () => sock.destroy(new Error('TLS connect timed out')));
    sock.on('error', rej);
  });
}
const peerFingerprint = (host, port) =>
  peerConnect(host, port).then(({ sock, fp }) => { sock.end(); return fp; });

async function verifyPin() {
  const want = normPin(CERT_PIN);
  if (!want) {
    console.warn('WARN: WP826_CERT_SHA256 unset — the handset certificate is NOT verified. ' +
      'Run `node tools/wp826.mjs fingerprint` and set it to pin.');
    return;
  }
  const got = await peerFingerprint(HOST);
  if (got !== want) {
    console.error(`FATAL: WP826 certificate mismatch at ${HOST}\n  expected ${want}\n  got      ${got}\n` +
      'Refusing to send credentials. If the handset was reset or replaced, re-pin with `fingerprint`.');
    process.exit(3);
  }
}
// A TLS connection to the handset whose certificate matches the pin, checked
// before it is returned, or null when no pin is set.
async function pinnedSocket() {
  const want = normPin(CERT_PIN);
  if (!want) return null;
  const { sock, fp } = await peerConnect(HOST);
  if (fp !== want) {
    sock.destroy();
    throw new Error(`WP826 certificate mismatch at ${HOST} (expected ${want}, got ${fp}); nothing was sent`);
  }
  return sock;
}

// One HTTPS request on a pinned connection when a pin is set. No agent at all:
// `agent: false` would mean a NEW agent, which opens its own, unchecked
// connection and never uses createConnection; with no agent, the request
// runs on exactly the socket createConnection returns.
async function send(opts, data) {
  const sock = await pinnedSocket();
  if (sock) { delete opts.agent; opts.createConnection = () => sock; }
  return new Promise((res, rej) => {
    const r = https.request(opts, (resp) => {
      (resp.headers['set-cookie'] || []).forEach((c) => { const kv = c.split(';')[0]; const i = kv.indexOf('='); cookies[kv.slice(0, i)] = kv.slice(i + 1); });
      let b = ''; resp.on('data', (d) => (b += d)); resp.on('end', () => res({ status: resp.statusCode, body: b }));
    });
    r.on('error', rej); if (data != null) r.write(data); r.end();
  });
}
let cookies = {};

function req(method, path, { body, json, sid } = {}) {
  {
    let data = null, ctype = null;
    if (json != null) { data = JSON.stringify(json); ctype = 'application/json'; }
    else if (body != null) { data = new URLSearchParams(body).toString(); ctype = 'application/x-www-form-urlencoded'; }
    let p = '/cgi-bin' + path;
    if (sid) p += (p.includes('?') ? '&' : '?') + 'sid=' + encodeURIComponent(sid);
    const opts = { host: HOST, port: 443, method, path: p, rejectUnauthorized: false, headers: {
      'X-Requested-With': 'XMLHttpRequest', Referer: `https://${HOST}/`, Origin: `https://${HOST}`,
      ...(Object.keys(cookies).length ? { Cookie: Object.entries(cookies).map(([k, v]) => `${k}=${v}`).join('; ') } : {}),
      ...(data != null ? { 'Content-Type': ctype, 'Content-Length': Buffer.byteLength(data) } : {}) } };
    opts.port = PORT;
    return send(opts, data);
  }
}
const J = (s) => { try { return JSON.parse(s); } catch { return null; } };

export async function login() {
  const a = await req('POST', '/access', { body: { access: sha256(USER) } });
  const salt = J(a.body)?.body;
  if (!salt) throw new Error('access failed: ' + a.status + ' ' + a.body.slice(0, 120));
  const d = await req('POST', '/dologin', { body: { username: USER, password: sha256(pass() + salt) } });
  const dj = J(d.body);
  if (dj?.response !== 'success') throw new Error('dologin failed: ' + d.status + ' ' + d.body.slice(0, 160));
  return dj.body.sid;
}
export async function meta(sid) {
  const r = await req('GET', '/metaconfig_get', { sid });
  const arr = J(r.body) || [];
  const map = {}; arr.forEach((e) => (map['P' + e.pvalue] = e.alias));
  return map; // {P330: "alias.name", ...}
}
export async function get(sid, codes) {
  const pv = codes.map((c) => String(c).replace(/^P/, '')).join(',');
  const r = await req('GET', '/config_get?pvalues=' + encodeURIComponent(pv) + '&update_session=false', { sid });
  const jr = J(r.body); const out = {};
  (jr?.configs || []).forEach((c) => (out['P' + c.pvalue] = c.value));
  return { status: r.status, values: out, raw: r.body };
}
export async function set(sid, kv) { // kv: {P330:"val", ...}
  const pvalue = {}; for (const [k, v] of Object.entries(kv)) pvalue[String(k).replace(/^P/, '')] = String(v);
  const r = await req('PUT', '/config_update', { sid, json: { alias: {}, pvalue } });
  const jr = J(r.body);
  return { status: r.status, ok: jr?.response === 'success' && jr?.body?.status === 'right', body: r.body };
}
// Reboot: POST /api-sys_operation body request=REBOOT -> {response:"success",body:"savereboot"}.
// (NOT /api-reboot — that 404s on this firmware.)
export async function reboot(sid) { return req('POST', '/api-sys_operation', { sid, body: { request: 'REBOOT' } }); }
// Upload a custom ringtone (WAV, 16 kHz mono 16-bit PCM works). ★ tags MUST be empty — a
// non-empty tags value 400s. Returns the new ringtone {id, fileName, type:"user"}.
export async function uploadRing(sid, filePath) {
  const buf = readFileSync(filePath); const name = filePath.split('/').pop();
  const B = '----wpb' + buf.length + name.length;
  const head = Buffer.from(`--${B}\r\nContent-Disposition: form-data; name="file"; filename="${name}"\r\nContent-Type: audio/x-wav\r\n\r\n`);
  const body = Buffer.concat([head, buf, Buffer.from(`\r\n--${B}--\r\n`)]);
  let p = '/cgi-bin/ringtone?tags=&sid=' + encodeURIComponent(sid);
  const r = await send({ host: HOST, port: PORT, method: 'POST', path: p, rejectUnauthorized: false, headers: {
    'X-Requested-With': 'XMLHttpRequest', Referer: `https://${HOST}/`, Origin: `https://${HOST}`,
    Cookie: Object.entries(cookies).map(([k, v]) => `${k}=${v}`).join('; '),
    'Content-Type': `multipart/form-data; boundary=${B}`, 'Content-Length': body.length } }, body);
  return { status: r.status, ring: (J(r.body)?.ringtones || [])[0], body: r.body };
}
export async function listRings(sid, type = 0) { const r = await req('GET', '/ringtone?type=' + type, { sid }); return J(r.body)?.ringtones || []; }

// --- Virtual keys / Quick Access (MPK) ---------------------------------------
// Layout: GET /api-mpk_download -> {results:[{VKID,DisplayIndex,Priority,Locked,
//   TypeMode,Description,ValueName,Account,AutoType}]} (76 keys across groups:
//   Priority 1xxx=MPK/Programmable Keys VKID 1-10; 2xxx=Quick Access grid VKID 11-50).
// Save: POST /api-save_mpk (JSON array; merges by VKID). TypeMode enum:
//   -1 none · 2 SPEED_DIAL (ValueName=number, Account=SIP acct) · 6 SPEED_DIAL_ACTIVE_ACCOUNT
//   · 3 BLF · 7 DIAL_DTMF · 8 VOICE_MAIL(no value) · 9 CALL_RETURN · 10 TRANSFER
//   · 11 CALL_PARK · 39 QUICK_JUMP app (ValueName=app id e.g. contacts.fav).
export async function mpkList(sid) { const r = await req('GET', '/api-mpk_download', { sid }); return J(r.body)?.results || []; }
export async function mpkSave(sid, keys) {
  const norm = keys.map((e) => ({ ...e, VKID: +e.VKID, Priority: +e.Priority, Locked: +e.Locked || 0, TypeMode: +e.TypeMode, Account: +e.Account }));
  const r = await req('POST', '/api-save_mpk', { sid, json: norm });
  return { status: r.status, ok: J(r.body)?.response === 'success', body: r.body };
}
// Set VKID -> Speed Dial <number> (label optional). Keeps the key's grid position.
export async function speedDial(sid, vkid, number, label = '', account = 0) {
  const keys = await mpkList(sid);
  const k = keys.find((x) => +x.VKID === +vkid);
  if (!k) return { ok: false, body: `VKID ${vkid} not found` };
  return mpkSave(sid, [{ ...k, TypeMode: 2, ValueName: String(number), Account: account, Description: label }]);
}

import { pathToFileURL } from 'node:url';
const isMain = import.meta.url === pathToFileURL(process.argv[1] || '').href;
const cmd = process.argv[2];
if (isMain) (async () => {
  // `fingerprint` needs no credentials — it is how you OBTAIN the pin.
  if (cmd === 'fingerprint') {
    const fp = await peerFingerprint(HOST);
    console.log(`WP826 ${HOST} certificate SHA-256:\n  ${fp}\n\n` +
      'Pin it so the admin password is never sent to an impostor:\n' +
      `  export WP826_CERT_SHA256=${fp}\n` +
      '  add-on option: cordless_cert_sha256');
    return;
  }
  // Verify the certificate BEFORE login() sends the password.
  await verifyPin();
  const sid = await login();
  if (cmd === 'login') { console.log('LOGIN OK sid=' + sid.slice(0, 10) + '…'); return; }
  if (cmd === 'meta') {
    const m = await meta(sid); const kw = (process.argv[3] || '').toLowerCase();
    const entries = Object.entries(m).filter(([, a]) => !kw || (a || '').toLowerCase().includes(kw));
    console.log(`metaconfig: ${Object.keys(m).length} codes total, ${entries.length} matching "${kw}"`);
    entries.slice(0, 120).forEach(([p, a]) => console.log(`  ${p} = ${a}`));
    return;
  }
  if (cmd === 'get') { const r = await get(sid, (process.argv[3] || '').split(',').filter(Boolean)); console.log('[' + r.status + ']', JSON.stringify(r.values, null, 2)); return; }
  if (cmd === 'set') { // set P332=60 P330=1 ...
    const kv = {}; process.argv.slice(3).forEach((a) => { const i = a.indexOf('='); if (i > 0) kv[a.slice(0, i)] = a.slice(i + 1); });
    const before = (await get(sid, Object.keys(kv))).values;
    const r = await set(sid, kv);
    const after = (await get(sid, Object.keys(kv))).values;
    console.log(r.ok ? 'SET OK' : 'SET FAILED [' + r.status + '] ' + r.body.slice(0, 120));
    for (const k of Object.keys(kv)) console.log(`  ${k}: ${JSON.stringify(before[k])} -> ${JSON.stringify(after[k])} (wanted ${JSON.stringify(kv[k])})`);
    return;
  }
  if (cmd === 'reboot') { const r = await reboot(sid); console.log('reboot ->', r.status, r.body.slice(0, 80)); return; }
  if (cmd === 'rings') { console.log(JSON.stringify(await listRings(sid, Number(process.argv[3] || 0)), null, 2)); return; }
  if (cmd === 'ring-upload') { const r = await uploadRing(sid, process.argv[3]); console.log(r.ring ? 'UPLOADED ' + JSON.stringify(r.ring) : 'FAIL [' + r.status + '] ' + r.body.slice(0, 120)); return; }
  if (cmd === 'mpk') { const ks = await mpkList(sid); console.log(JSON.stringify(ks.filter((k) => +k.TypeMode !== -1), null, 2)); return; }
  if (cmd === 'speeddial') { // speeddial <VKID> <number> [label]
    const r = await speedDial(sid, process.argv[3], process.argv[4], process.argv[5] || '');
    console.log(r.ok ? `SET VKID ${process.argv[3]} -> Speed Dial ${process.argv[4]}` : 'FAIL ' + (r.body || '').slice(0, 120)); return;
  }
  console.log('usage: wp826.mjs <login|get P1,P2|set P1=v P2=v|rings [type]|ring-upload f.wav|mpk|speeddial VKID num [label]|reboot>');
})().catch((e) => { console.log('ERR:', e.message); process.exit(1); });
