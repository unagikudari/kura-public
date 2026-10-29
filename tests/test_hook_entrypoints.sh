#!/usr/bin/env bash
# kura#120 — tracked hook entrypoint が「どの checkout でも git に実行される」ことを測る。
#
# ここで見るのは **dispatch** であって検査の中身ではない。実装は合成 stub に差し替える
# ので torch も DB も venv も要らず、CI でもそのまま走る。中身の回帰は各実装側が持つ。
#
#   bash tests/test_hook_entrypoints.sh
set -uo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
tmp=$(mktemp -d "${TMPDIR:-/tmp}/kura-hookentry.XXXXXX")
trap 'rm -rf "$tmp"' EXIT

pass=0 fail=0
ok()   { pass=$((pass+1)); echo "  ok   — $1"; }
bad()  { fail=$((fail+1)); echo "  FAIL — $1"; }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected [$3], got [$2])"; fi; }

git_q() { git -c user.email=t@example.invalid -c user.name=t "$@"; }

# 被験体は「この repo の tracked entrypoint + installer」だけ。実装は stub。
seed_repo() {
    local dst=$1 marker=$2
    mkdir -p "$dst/hooks" "$dst/scripts/hooks"
    # -P: symlink を辿らない。辿ると「絶対 symlink へ戻した退行」を通常ファイルとして
    # 写し取ってしまい、この test が防ぐはずの形を自分で消してしまう。
    cp -P "$repo/hooks/pre-commit" "$repo/hooks/pre-push" "$dst/hooks/"
    cp "$repo/scripts/install_hooks.sh" "$dst/scripts/"
    # 合成 stub: 自分がどの worktree の実装として呼ばれたかを名乗り、契約どおり通す。
    for h in pre-commit pre-push; do
        cat > "$dst/scripts/hooks/$h" <<STUB
#!/bin/sh
echo "STUB $h marker=$marker root=\$(git rev-parse --show-toplevel)" >&2
[ "$h" = "pre-push" ] && { refs=\$(cat); echo "STUB pre-push stdin=[\$refs]" >&2; }
exit 0
STUB
        chmod +x "$dst/scripts/hooks/$h"
    done
    echo "0.0.1" > "$dst/VERSION"
    ( cd "$dst" && git init -q . && git_q add -A && git_q commit -qm "seed" )
}

echo "== 1. 元の checkout が消えた clone でも entrypoint が走る =="
seed_repo "$tmp/origin" origin
git clone -q "$tmp/origin" "$tmp/clone"
rm -rf "$tmp/origin"                       # 元 checkout を消す — 絶対参照なら必ず壊れる
( cd "$tmp/clone" && bash scripts/install_hooks.sh >/dev/null )
out=$( cd "$tmp/clone" && echo x > a.txt && git_q add a.txt && git_q commit -m t 2>&1 )
case "$out" in *"STUB pre-commit marker=origin"*) ok "clone で pre-commit が dispatch された";;
                                              *) bad "clone で pre-commit が走っていない: $out";; esac
case "$out" in *"root=$tmp/clone"*) ok "実装は clone 自身を root として解決した";;
                                 *) bad "root が clone でない: $out";; esac

echo "== 2. installer は tracked file を書き換えない =="
# kura#120 の引き金は「core.hooksPath=hooks が **既に** 設定された checkout で installer
# を走らせる」こと。旧版はそこで --git-path hooks が tracked ディレクトリを返すため、
# tracked な hooks/pre-commit を絶対 symlink へ書き換えていた。2 回目の実行で再現する。
( cd "$tmp/clone" && bash scripts/install_hooks.sh >/dev/null )
dirty=$( cd "$tmp/clone" && git status --porcelain -- hooks scripts )
check "hooksPath 設定済みで再実行しても hooks/ scripts/ は無変更" "$dirty" ""
for h in pre-commit pre-push; do
    stray=$( cd "$tmp/clone" && test -e ".git/hooks/$h" -o -L ".git/hooks/$h" && echo present || echo absent )
    check "installer は .git/hooks/$h を作らない" "$stray" "absent"
done
link=$( cd "$tmp/clone" && test -L hooks/pre-commit && echo symlink || echo regular )
check "tracked hooks/pre-commit は通常ファイルのまま" "$link" "regular"
check "core.hooksPath は相対 1 パス" "$(cd "$tmp/clone" && git config core.hooksPath)" "hooks"

echo "== 3. linked worktree は自分の worktree の実装へ解決する =="
( cd "$tmp/clone" && git_q worktree add -q -b feature "$tmp/wt" >/dev/null 2>&1 )
# worktree 側だけ marker を変える。共有 config のまま、別の実装が走るはず。
sed -i 's/marker=origin/marker=worktree/' "$tmp/wt/scripts/hooks/pre-commit"
out=$( cd "$tmp/wt" && echo y > b.txt && git_q add b.txt && git_q commit -m t 2>&1 )
case "$out" in *"STUB pre-commit marker=worktree"*) ok "worktree 自身の実装が走った";;
                                                 *) bad "worktree の実装が走っていない: $out";; esac
case "$out" in *"root=$tmp/wt"*) ok "root は linked worktree";;
                              *) bad "root が worktree でない: $out";; esac
# installer を worktree で走らせても tracked file を書き換えない (kura#120 の退行)
( cd "$tmp/wt" && bash scripts/install_hooks.sh >/dev/null )
dirty=$( cd "$tmp/clone" && git status --porcelain -- hooks )
check "worktree で installer を走らせても canonical の hooks/ は無変更" "$dirty" ""

echo "== 4. pre-push が dispatch され、ref を stdin で受け取る =="
git init -q --bare "$tmp/remote.git"
( cd "$tmp/clone" && git_q remote add tmpremote "$tmp/remote.git" )
branch=$( cd "$tmp/clone" && git rev-parse --abbrev-ref HEAD )
out=$( cd "$tmp/clone" && git_q push tmpremote "$branch" 2>&1 )
case "$out" in *"STUB pre-push"*) ok "pre-push が dispatch された";;
                               *) bad "pre-push が走っていない: $out";; esac
case "$out" in *"STUB pre-push stdin=[refs/heads/$branch"*) ok "push される ref が stdin で届いた";;
                                                         *) bad "stdin に ref が届いていない: $out";; esac

echo "== 5. 実装を解決できないときは通さない (fail-closed) =="
rm "$tmp/clone/scripts/hooks/pre-commit"
out=$( cd "$tmp/clone" && echo z > c.txt && git_q add c.txt && git_q commit -m t 2>&1 ); rc=$?
check "実装が無い commit は失敗する" "$rc" "1"
case "$out" in *"[hooks] BLOCK"*) ok "止めた理由を名乗る";;
                               *) bad "BLOCK メッセージが無い: $out";; esac

echo
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
