#!/usr/bin/env bash
# kura git hooks installer — clone ごとに 1 回実行する。
#   bash scripts/install_hooks.sh
#
# やることは **設定 1 行だけ**: core.hooksPath を tracked な hooks/ に向ける。hook の
# 中身は tracked なので配る必要が無く、この installer は tracked file を一切書き換えない。
#
# ■ 以前の版が壊していたもの (kura#120)
#
# 旧版は `git rev-parse --git-path hooks` を install 先として使っていた。ところが git は
# このパスに **core.hooksPath を反映する** ので、設定済みの checkout では `hooks` —
# つまり tracked ディレクトリ自身 — が返る。そこへ `ln -sf` すると、installer は
# .git/hooks を用意する代わりに **tracked な hooks/pre-commit を、走らせた worktree の
# 絶対パスへ向かう symlink に書き換えていた**。他の clone からは宙ぶらりんになる。
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

for hook in pre-commit pre-push; do
  entry="hooks/$hook"
  impl="scripts/hooks/$hook"
  [ -f "$entry" ] || { echo "missing tracked entrypoint: $entry" >&2; exit 1; }
  [ -f "$impl" ]  || { echo "missing implementation: $impl" >&2; exit 1; }
  # 実行ビットは tracked mode (100755) が運ぶ。ここで chmod すると tracked file の
  # 変更になるので、立っていなければ直さずに報告して止める。
  [ -x "$entry" ] || { echo "not executable: $entry (git update-index --chmod=+x $entry)" >&2; exit 1; }
  [ -x "$impl" ]  || { echo "not executable: $impl (git update-index --chmod=+x $impl)" >&2; exit 1; }
done

git config core.hooksPath hooks

# 設定は clone 単位 (linked worktree 間で共有される) だが、値が相対パスなので git は
# **いま commit しようとしている worktree** の hooks/ を見る。実際にそう解決されるかを
# 名乗りではなく git 自身に確かめる。
resolved=$(git rev-parse --git-path hooks)
echo "configured: core.hooksPath=hooks"
echo "git はこの worktree で $repo_root/$resolved を見ます"
for hook in pre-commit pre-push; do
  echo "  $resolved/$hook -> scripts/hooks/$hook (worktree ごとに解決)"
done

# 旧 installer が .git/hooks へ残した symlink は core.hooksPath の下では実行されない。
# 黙っていると「入れ直した」と思ったまま古い方を読み続けるので、あるなら言う。
git_dir=$(git rev-parse --git-dir)
for hook in pre-commit pre-push; do
  stale="$git_dir/hooks/$hook"
  if [ -e "$stale" ] || [ -L "$stale" ]; then
    echo "note: $stale は残っていますが core.hooksPath の下では実行されません (削除は任意)。"
  fi
done

echo "done. bypass は git commit --no-verify / git push --no-verify (明示時のみ)。"
