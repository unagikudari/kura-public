import { MAX_IMPORT_FILES, type ImportFile } from './collector/importFiles.ts';
const natural = new Intl.Collator('en', { numeric: true, sensitivity: 'variant' });
/** Same image/video allowance as ordinary drops, including files with no MIME. */
export function planDropImport(files: ImportFile[]) {
  if (files.length > MAX_IMPORT_FILES) throw new Error('一度に取り込めるのは10,000ファイルまでです。');
  const supported = files.filter(({ file }) => file.type.startsWith('image/') || file.type.startsWith('video/')
    || /\.(jpe?g|png|webp|gif|avif|bmp|tiff?|webm|mp4|m4v|mov)$/i.test(file.name));
  supported.sort((a,b) => natural.compare(a.relativePath ?? a.file.name, b.relativePath ?? b.file.name));
  return { files: supported, skipped: files.length-supported.length };
}
export function duplicateUpload(error: unknown) {
  const e = error as { status?: number; message?: string; error?: string };
  // /posts has exactly one duplicate refusal: the slug post_already_exists (1.263.14).
  // The bare sentence is what an API before it answers. Other conflicts must
  // remain failures, rather than silently claiming the content already exists.
  return e?.status === 409
    && (e.error === 'post_already_exists' || e.message === 'すでに登録済みです (checksum重複)');
}
/** Window event DropImport sends after a batch added posts; the list reloads in place. */
export const POSTS_ADDED_EVENT = 'kura:posts-added';
