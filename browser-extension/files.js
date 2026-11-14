// Keep the user-granted directory handle across browser sessions.
async function exportDirectory(handle) {
  const database = await new Promise((resolve, reject) => {
    const request = indexedDB.open('jobhunt-export', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('settings');
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });

  try {
    return await new Promise((resolve, reject) => {
      const transaction = database.transaction('settings', handle ? 'readwrite' : 'readonly');
      const store = transaction.objectStore('settings');
      const request = handle ? store.put(handle, 'directory') : store.get('directory');
      transaction.oncomplete = () => resolve(request.result);
      transaction.onabort = () => reject(transaction.error);
    });
  } finally {
    database.close();
  }
}

async function saveJobExport(directory, payload, platform = 'boss') {
  return navigator.locks.request('jobhunt-export-filename', async () => {
    const date = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(new Date());
    const prefix = `${platform}-jobs-${date}-`;
    let last = 0;
    for await (const name of directory.keys()) {
      if (!name.startsWith(prefix)) continue;
      const sequence = /^(\d+)\.json$/i.exec(name.slice(prefix.length));
      if (sequence && Number.isSafeInteger(Number(sequence[1]))) last = Math.max(last, Number(sequence[1]));
    }
    if (last === Number.MAX_SAFE_INTEGER) throw new Error('Export sequence exhausted');
    const filename = `${prefix}${String(last + 1).padStart(2, '0')}.json`;
    const contents = JSON.stringify(payload, null, 2) + '\n';
    const file = await directory.getFileHandle(filename, { create: true });
    const writer = await file.createWritable();
    try {
      await writer.write(contents);
      await writer.close();
    } catch (error) {
      await writer.abort().catch(() => {});
      throw error;
    }
    return filename;
  });
}
