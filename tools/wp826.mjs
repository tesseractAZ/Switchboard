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
  console.log('usage: wp826.mjs <login|get P1,P2|set P1=v P2=v|reboot>');
})().catch((e) => { console.log('ERR:', e.message); process.exit(1); });
