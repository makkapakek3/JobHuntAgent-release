const choose = document.querySelector('#choose');
const folderStatus = document.querySelector('#folder-status');

async function showFolder() {
  const folder = await exportDirectory();
  if (!folder) {
    folderStatus.textContent = '尚未选择导出文件夹。';
    return;
  }
  const permission = await folder.queryPermission({ mode: 'readwrite' });
  folderStatus.textContent = permission === 'granted'
    ? `当前导出文件夹：${folder.name}。可以回 BOSS 页面使用扩展。`
    : `「${folder.name}」需要重新授权。请重新选择这个文件夹。`;
}

choose.addEventListener('click', async () => {
  choose.disabled = true;
  try {
    const folder = await window.showDirectoryPicker({ id: 'jobhunt-job-exports', mode: 'readwrite' });
    if (folder.name !== '岗位信息') {
      folderStatus.textContent = '请选择上方路径里的「岗位信息」文件夹。';
      return;
    }
    await exportDirectory(folder);
    await showFolder();
  } catch (error) {
    if (error.name === 'AbortError') {
      await showFolder().catch(() => { folderStatus.textContent = '未更改导出文件夹。'; });
    } else {
      folderStatus.textContent = '无法设置导出文件夹。请在此页面重新选择并允许写入。';
    }
  } finally {
    choose.disabled = false;
  }
});

showFolder().catch(() => { folderStatus.textContent = '无法读取文件夹设置。请重新选择。'; });
