// Programmatic WP826 config client — no browser, fully scriptable.
//   login:  POST /cgi-bin/access {access:sha256(user)} -> salt; POST /cgi-bin/dologin {username,password:sha256(pw+salt)} -> {sid}
//   read:   GET  /cgi-bin/config_get?pvalues=<codes w/o P>&sid=...        -> {configs:[{pvalue,value}]}
//   write:  PUT  /cgi-bin/config_update  JSON {alias:{},pvalue:{"<code>":"<val>"}}
//   meta:   GET  /cgi-bin/metaconfig_get  -> [{pvalue,alias}]   (P-code -> alias name)
// Requires Referer/Origin headers or the server 403s.
import https from 'node:https';
import { readFileSync } from 'node:fs';
import crypto from 'node:crypto';

const HOST = '192.168.6.71';
const USER = 'admin';
const PASS = readFileSync('/tmp/.wp_pass', 'utf8').trim();
const sha256 = (s) => crypto.createHash('sha256').update(s).digest('hex');
let cookies = {};

function req(method, path, { body, json, sid } = {}) {
  return new Promise((res, rej) => {
    let data = null, ctype = null;
    if (json != null) { data = JSON.stringify(json); ctype = 'application/json'; }
    else if (body != null) { data = new URLSearchParams(body).toString(); ctype = 'application/x-www-form-urlencoded'; }
    let p = '/cgi-bin' + path;
    if (sid) p += (p.includes('?') ? '&' : '?') + 'sid=' + encodeURIComponent(sid);
    const opts = { host: HOST, port: 443, method, path: p, rejectUnauthorized: false, headers: {
      'X-Requested-With': 'XMLHttpRequest', Referer: 'https://192.168.6.71/', Origin: 'https://192.168.6.71',
      ...(Object.keys(cookies).length ? { Cookie: Object.entries(cookies).map(([k, v]) => `${k}=${v}`).join('; ') } : {}),
      ...(data != null ? { 'Content-Type': ctype, 'Content-Length': Buffer.byteLength(data) } : {}) } };
    const r = https.request(opts, (resp) => {
      (resp.headers['set-cookie'] || []).forEach((c) => { const kv = c.split(';')[0]; const i = kv.indexOf('='); cookies[kv.slice(0, i)] = kv.slice(i + 1); });
      let b = ''; resp.on('data', (d) => (b += d)); resp.on('end', () => res({ status: resp.statusCode, body: b }));
    });
    r.on('error', rej); if (data != null) r.write(data); r.end();
  });
}
const J = (s) => { try { return JSON.parse(s); } catch { return null; } };

export async function login() {
  const a = await req('POST', '/access', { body: { access: sha256(USER) } });
  const salt = J(a.body)?.body;
  if (!salt) throw new Error('access failed: ' + a.status + ' ' + a.body.slice(0, 120));
  const d = await req('POST', '/dologin', { body: { username: USER, password: sha256(PASS + salt) } });
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
export async function reboot(sid) { return req('POST', '/api-reboot', { sid, body: {} }); }
// Upload a custom ringtone (WAV, 16 kHz mono 16-bit PCM works). ★ tags MUST be empty — a
// non-empty tags value 400s. Returns the new ringtone {id, fileName, type:"user"}.
export async function uploadRing(sid, filePath) {
  const buf = readFileSync(filePath); const name = filePath.split('/').pop();
  const B = '----wpb' + buf.length + name.length;
  const head = Buffer.from(`--${B}\r\nContent-Disposition: form-data; name="file"; filename="${name}"\r\nContent-Type: audio/x-wav\r\n\r\n`);
  const body = Buffer.concat([head, buf, Buffer.from(`\r\n--${B}--\r\n`)]);
  let p = '/cgi-bin/ringtone?tags=&sid=' + encodeURIComponent(sid);
  const r = await new Promise((res, rej) => {
    const rq = https.request({ host: HOST, port: 443, method: 'POST', path: p, rejectUnauthorized: false, headers: {
      'X-Requested-With': 'XMLHttpRequest', Referer: 'https://192.168.6.71/', Origin: 'https://192.168.6.71',
      Cookie: Object.entries(cookies).map(([k, v]) => `${k}=${v}`).join('; '),
      'Content-Type': `multipart/form-data; boundary=${B}`, 'Content-Length': body.length } }, (resp) => {
      let b = ''; resp.on('data', (d) => (b += d)); resp.on('end', () => res({ status: resp.statusCode, body: b }));
    }); rq.on('error', rej); rq.write(body); rq.end();
  });
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
