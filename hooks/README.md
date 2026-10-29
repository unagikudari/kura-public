# Repo hooks

このディレクトリは git が実行する hook を **追跡** している。clone / worktree ごとに
一度だけ:

```bash
bash scripts/install_hooks.sh
```

これは `core.hooksPath=hooks` を設定するだけで、tracked file は書き換えない。値が相対
パスなので、git は commit / push しようとしている **その worktree** の `hooks/` を見る。

## 構成 — entrypoint と実装を分ける

`hooks/pre-commit` と `hooks/pre-push` は数行の POSIX shell で、`git rev-parse
--show-toplevel` で今の worktree を解決し、同じ worktree の実装へ `exec` する:

| git が実行する | 実装 |
| --- | --- |
| `hooks/pre-commit` | `scripts/hooks/pre-commit` |
| `hooks/pre-push` | `scripts/hooks/pre-push` |

絶対パスを書かないので、元の checkout が存在しないホストの clone でも、linked worktree
でも同じように動く。root を解決できない / 実装が実行可能な形で無いときは **止める** —
検査不能を合格にはしない。

以前 `hooks/pre-commit` は一つの開発 checkout への絶対 symlink で、`hooks/pre-push` は
そもそも存在しなかった。**git は存在しない hook を黙って無視する**ので、他ホストでは
警告も無くゲートが外れ、`scripts/hooks/pre-push` は tracked なのに一度も呼ばれなかった
(kura#120)。正しいスクリプトがあっても、git が実行しなければゲートは無い。

## pre-commit

CLAUDE.md は各 commit に VERSION 更新と CHANGELOG を要求する。現在の機械検査は、
VERSION が staged されていない deployable 変更には警告し、staged VERSION の逆行・
据え置きと新 VERSION に対応する CHANGELOG 見出しの欠落を拒否する。

ほかに secret 検査、endpoints snapshot の drift、replica DDL 宣言、vendored refguard の
一致も見る。詳細は `scripts/hooks/pre-commit` を参照。

## pre-push

学習の意味に触るファイル (`tagger/heads/train_heads.py`、`tagger/heads/planner.py`、
`tests/test_heads_planner.py`) が push に含まれるときだけ torch ゲートを走らせる。
それ以外の push は素通しする。詳細は `scripts/hooks/pre-push` を参照。

緊急時は `git commit --no-verify` / `git push --no-verify` でスキップ可能（次の bump で
理由を CHANGELOG に記録）。

`tests/test_hook_entrypoints.sh` は一時 clone と linked worktree で実行経路を検証し、GitHub Actions でも実行する。
