/* Proyecto independiente. No pegar en el Apps Script de inventario o aprendizaje. */
function instalarMemoriaPaty() {
  const p = PropertiesService.getScriptProperties();
  if (!p.getProperty('PATY_FOLDER_ID'))
    p.setProperty('PATY_FOLDER_ID', DriveApp.createFolder('Paty - memoria privada').getId());
  if (!p.getProperty('PATY_TOKEN'))
    p.setProperty('PATY_TOKEN', Utilities.getUuid() + Utilities.getUuid());
  // Consultar el token en Configuración del proyecto > Propiedades del script.
}

function doPost(e) {
  let result;
  try {
    if (!e.postData || e.postData.contents.length > 2000000) throw Error('request');
    const q = JSON.parse(e.postData.contents);
    const p = PropertiesService.getScriptProperties();
    const token = p.getProperty('PATY_TOKEN');
    if (!token || q.token !== token) throw Error('auth');
    // Lectura agregada fuera del bloqueo: no retrasa las conversaciones.
    if (q.action === 'offer_stats') {
      result = estadisticasOfertas(DriveApp.getFolderById(p.getProperty('PATY_FOLDER_ID')));
    } else {
    const lock = LockService.getScriptLock();
    if (!lock.tryLock(10000)) throw Error('busy');
    try { result = operarMemoria(q, DriveApp.getFolderById(p.getProperty('PATY_FOLDER_ID'))); }
    finally { lock.releaseLock(); }
    }
  } catch (_) { result = {ok: false}; }
  return ContentService.createTextOutput(JSON.stringify(result)).setMimeType(ContentService.MimeType.JSON);
}

function estadisticasOfertas(folder) {
  const stats = {A: {offers: 0, notified: 0}, B: {offers: 0, notified: 0}};
  const files = folder.getFiles();
  const seen = {};
  const now = Date.now();
  let count = 0;
  while (files.hasNext()) {
    const file = files.next();
    const name = file.getName();
    if (!/^paty-[a-f0-9]{64}\.json$/.test(name)) continue;
    if (++count > 500 || seen[name]) throw Error('stats_limit_or_duplicate');
    seen[name] = true;
    const record = JSON.parse(file.getBlob().getDataAsString());
    if (record.schema !== 1) throw Error('stats_corrupt');
    const state = record.state;
    if (!state || state.rol !== 'cliente') continue;
    const variant = state.advisor_offer_variant;
    const date = Date.parse(state.advisor_offer_at || '');
    if (!Object.prototype.hasOwnProperty.call(stats, variant)
        || !Number.isFinite(date) || now - date < 86400000
        || now - date > 30 * 86400000) continue;
    stats[variant].offers++;
    const lead = state.lead || {};
    const phone = String(lead.whatsapp || '').replace(/\D/g, '');
    if (state.lead_confirmado === true && state.notificacion_enviada === true
        && state.agente_asignado && typeof lead.nombre === 'string'
        && lead.nombre.trim().length >= 2 && phone.length >= 10 && phone.length <= 15
        && lead.whatsapp_confirmado === true)
      stats[variant].notified++;
  }
  return {ok: true, stats: stats};
}

function operarMemoria(q, folder) {
  if (q.action === 'ping') { folder.getName(); return {ok: true}; }
  if (!/^[a-f0-9]{64}$/.test(q.sender || '')) throw Error('sender');
  if (!['acquire', 'commit', 'release', 'reset'].includes(q.action)) throw Error('action');
  if (q.action !== 'reset' && !/^[a-f0-9]{32}$/.test(q.owner || '')) throw Error('owner');
  const files = folder.getFilesByName('paty-' + q.sender + '.json');
  const file = files.hasNext() ? files.next() : null;
  if (files.hasNext()) throw Error('duplicate_files');
  let r = file ? JSON.parse(file.getBlob().getDataAsString()) :
    {schema: 1, state: null, expires: 0, lease: null, messages: {}, committed: null};
  if (r.schema !== 1 || !r.messages || typeof r.messages !== 'object' || Array.isArray(r.messages)
      || (r.state !== null && (typeof r.state !== 'object' || Array.isArray(r.state)))
      || !Number.isFinite(r.expires)) throw Error('corrupt');
  const now = Date.now();
  const active = r.lease && r.lease.until > now;
  Object.keys(r.messages).forEach(k => { if (r.messages[k] <= now) delete r.messages[k]; });
  function save() {
    const text = JSON.stringify(r);
    if (text.length > 1800000) throw Error('size');
    if (file) file.setContent(text);
    else folder.createFile('paty-' + q.sender + '.json', text, MimeType.PLAIN_TEXT);
  }
  function seconds(value, max) {
    if (!Number.isFinite(value) || value < 1 || value > max) throw Error('ttl');
    return value * 1000;
  }
  if (q.action === 'acquire') {
    if (q.message && !/^[a-f0-9]{64}$/.test(q.message)) throw Error('message');
    if (q.message && r.messages[q.message] > now) return {ok: true, duplicate: true};
    if (active && r.lease.owner !== q.owner) throw Error('busy');
    if (active) return {ok: true, state: r.state};
    if (r.expires <= now) r.state = null;
    r.lease = {owner: q.owner, until: now + seconds(q.lease, 360), message: q.message};
    save(); return {ok: true, state: r.state};
  }
  if (q.action === 'commit') {
    if (r.committed === q.owner) return {ok: true};
    if (!active || r.lease.owner !== q.owner || r.lease.message !== q.message) throw Error('stale');
    if (!q.state || typeof q.state !== 'object' || Array.isArray(q.state)) throw Error('state');
    r.state = q.state; r.expires = now + seconds(q.ttl, 31536000);
    if (q.message) r.messages[q.message] = now + seconds(q.duplicate_ttl, 86400);
    r.committed = q.owner; r.lease = null; save(); return {ok: true};
  }
  if (q.action === 'release') {
    if (r.lease && r.lease.owner === q.owner) { r.lease = null; save(); }
    return {ok: true};
  }
  if (active) throw Error('busy');
  r.state = null; r.expires = 0; r.committed = null; r.lease = null;
  if (file) save();
  return {ok: true};
}
