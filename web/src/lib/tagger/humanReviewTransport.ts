// The production /m proxy supplies /api upstream. Never add /api twice.
export function reviewRequestPath(root: string, ref: string, suffix = ''): string {
  if (root !== '/m' || !/^[a-f0-9]{64}$/.test(ref) || !/^(?:\/posts\/[1-9][0-9]*(?:\/(?:group-)?judgments)?)?$/.test(suffix)) throw new Error('確認対象のURLが不正です。');
  return `${root}/tagger-review/${ref}${suffix}`;
}
export function reviewMediaPath(root: string, content: string, postId: number): string {
  if (root !== '/m' || !Number.isSafeInteger(postId) || postId <= 0 || content !== `/api/post/${postId}/content`) throw new Error('表示画像の参照が不正です。');
  return `${root}/post/${postId}/content`;
}
