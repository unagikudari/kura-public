/** Browser enumeration only; no upload/DB knowledge. Files remain lazy handles.
 * readEntries must be drained: Chromium returns directory entries in batches.
 * https://developer.mozilla.org/en-US/docs/Web/API/FileSystemDirectoryReader/readEntries
 */
export type ImportFile = { file: File; relativePath?: string };
export const MAX_IMPORT_FILES = 10_000;
const formats = new Set(['image/jpeg', 'image/png', 'image/webp', 'image/gif', 'video/webm']);
const natural = new Intl.Collator('en', { numeric: true, sensitivity: 'variant' });

export function planFiles(files: ImportFile[]) {
  if (files.length > MAX_IMPORT_FILES) throw new Error('1回の取り込みは10,000ファイルまでです。フォルダを分けて追加してください。');
  const paths = new Set<string>();
  for (const item of files) {
    if (item.relativePath) {
      if (paths.has(item.relativePath)) throw new Error(`同じ相対パスが重複しています：${item.relativePath}`);
      paths.add(item.relativePath);
    }
  }
  const supported = files.filter(({ file }) => formats.has(file.type) || /\.(jpe?g|png|webp|gif|webm)$/i.test(file.name));
  supported.sort((a, b) => {
    const left = a.relativePath ?? a.file.name, right = b.relativePath ?? b.file.name;
    return natural.compare(left, right) || (left < right ? -1 : left > right ? 1 : 0);
  });
  return { files: supported, skipped: files.length - supported.length };
}

export function selectedFiles(files: FileList | File[]): ImportFile[] {
  return Array.from(files, file => ({ file, relativePath: file.webkitRelativePath || undefined }));
}

export async function droppedFiles(transfer: DataTransfer, progress: (count: number) => void): Promise<ImportFile[]> {
  // Capture entries and files synchronously while the drop event permits access.
  const sources = Array.from(transfer.items ?? []).filter(item => item.kind === 'file')
    .map(item => ({ entry: item.webkitGetAsEntry?.(), file: item.getAsFile() }));
  const fallback = selectedFiles(transfer.files);
  const found: ImportFile[] = [];
  let visited = 0;
  function add(file: File, path?: string) {
    found.push({ file, relativePath: path });
    if (found.length > MAX_IMPORT_FILES) throw new Error('1回の取り込みは10,000ファイルまでです。フォルダを分けて追加してください。');
    progress(found.length);
  }
  async function walk(entry: FileSystemEntry, parent = '', depth = 0): Promise<void> {
    if (depth > 64 || ++visited > 20_000) throw new Error('フォルダの階層または件数が多すぎます。フォルダを分けて追加してください。');
    const path = parent ? `${parent}/${entry.name}` : entry.name;
    if (entry.isFile) {
      const file = await new Promise<File>((resolve, reject) => (entry as FileSystemFileEntry).file(resolve, reject));
      add(file, parent ? path : undefined);
    } else if (entry.isDirectory) {
      const reader = (entry as FileSystemDirectoryEntry).createReader();
      for (;;) {
        const batch = await new Promise<FileSystemEntry[]>((resolve, reject) => reader.readEntries(resolve, reject));
        if (!batch.length) break;
        for (const child of batch) await walk(child, path, depth + 1);
      }
    } else {
      throw new Error('読み取れない項目があります。フォルダ選択から追加し直してください。');
    }
  }
  try {
    if (!sources.length) {
      for (const item of fallback) add(item.file, item.relativePath);
    } else {
      for (const source of sources) {
        if (source.entry) await walk(source.entry);
        else if (source.file) add(source.file);
        else throw new Error('このブラウザではフォルダを読み取れません。「フォルダを選択」を使ってください。');
      }
    }
  } catch (e) {
    if (e instanceof DOMException) throw new Error('フォルダを最後まで読み取れませんでした。アクセス権を確認して選択し直してください。');
    throw e;
  }
  return found;
}
