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
    const lock = LockService.getScriptLock();
    if (!lock.tryLock(10000)) throw Error('busy');
    try { result = operarMemoria(q, DriveApp.getFolderById(p.getProperty('PATY_FOLDER_ID'))); }
    finally { lock.releaseLock(); }
  } catch (_) { result = {ok: false}; }
  return ContentService.createTextOutput(JSON.stringify(result)).setMimeType(ContentService.MimeType.JSON);
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
