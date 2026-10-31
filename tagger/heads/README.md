# tagger heads — production cycle

`tagger-head-update.service`（6 時間周期）が回す本番経路。**コードはここ、成果物はここに置かない。**

## 境界

| | 置き場所 | 理由 |
|---|---|---|
| コード | この repo | レビュー・rollback・clean checkout 再現ができる |
| 成果物 (`heads/*.safetensors`, `*.log`, `applied_log.jsonl`, `verified_log.jsonl`, `generation`) | `$TAGGER_HEADS_DATA`（既定 `~/tagger-heads`） | 17 GB。repo に入れるものではない |
| 秘密 (`KTOK`) | **環境変数のみ** | パスで到達できる秘密は commit できる秘密 |

## 環境変数

| 変数 | 既定 | 用途 |
|---|---|---|
| `TAGGER_HEADS_DATA` | `~/tagger-heads` | 成果物ルート |
| `KTOK` | （なし・必須） | kura API トークン。未設定なら **fail closed** |
| `BROKER_ENV_FILE` | `~/deploy-checkout/.env` | broker signal 用キーの取得元（コピーせず参照） |
| `EMBED_BS` / `EMBED_BS_FLOOR` | 64 / 1 | 埋め込み batch（OOM 時に自動で半減） |
| `APPLY_SRC` / `APPLY_BS` | — | `auto_apply.py` 側 |

## 実行

```
KTOK=<token> bash tagger/heads/update_cycle.sh
```

`update_cycle.sh` は `$TAGGER_HEADS_DATA` へ `cd` し、スクリプトは自分の隣（この repo）から
呼びます。つまり **cwd = 成果物、コード = repo** です。

## rollback

世代は `$TAGGER_HEADS_DATA/generation` と `heads/` にあります。前世代へ戻すには:

```
ls $TAGGER_HEADS_DATA/heads.gen*        # 世代別スナップショット
cp -a $TAGGER_HEADS_DATA/heads.gen<N>/. $TAGGER_HEADS_DATA/heads/
echo <N> > $TAGGER_HEADS_DATA/generation
```

コード側の rollback は git（この repo）。**成果物とコードの世代は独立に戻せる** — 片方だけ
壊れたときに、もう片方を巻き添えにしないため。

## 移設 (2026-09-04) で変えたこと

パス 5 箇所と秘密 3 箇所のパラメータ化のみ。**既定値は移設前のリテラルと同一**で、挙動は
変えていません（差分は意図箇所のみ、11 ファイル中 6 つは byte 一致）。

移設していないもの: `~/tagger-heads` の 300 本超の一回限り分析スクリプト（`analyze_*` /
`build_*` / `check_*`）。どの unit からも呼ばれておらず、本番経路ではありません。それらを
どうするかは別の判断です。

## ⚠ この checkout は本番サイクルの実行元 — ブランチを切らないこと

`tagger-head-update.service` はここのファイルを直接実行します（Phase 0 のカットオーバー）。
つまり **`~/kura` で feature ブランチを出すと、本番サイクルがそのブランチのコードを走らせます。**

2026-09-04 に実際に起きました。Phase 1 を `~/kura` で開発した結果、`fleet-invariant/
kura-head-agrees-across-nodes` が発火して初めて気づきました。私は「main は無傷なので
次サイクルは Phase 0 のコード」と報告していましたが、**確かめたのは main のファイルで
あって、ディスク上の作業ツリーではありませんでした。**

**開発は worktree で行うこと**:

```
git -C ~/kura worktree add ~/wt-kura-<slug> -b feat/<slug>
```

`~/kura` は **main 固定**。稼働中の service が読む checkout なので、作業は worktree で行います。

なお `.git/hooks` は worktree 間で共有されるので、versioned hook が一部ブランチにしか
無いと別ブランチ側で symlink が宙ぶらりんになり、**git はそれを黙って無視します**。
pre-commit がその不在を報告します (1.187.1)。

## 除外一覧の同期（v1.229.2以降）

`fetch_excluded.py` はSSH先・DB・ユーザー・パスワードを固定しない。
次の両方をサービスの秘密設定から渡す（コマンド引数やGitには置かない）。

| 変数 | 用途 |
|---|---|
| `KURA_EXCLUDE_SOURCE_DSN` | 正本のpool / pool_post / post_tag / tag_nameをSELECTできるPostgreSQL接続 |
| `KURA_EXCLUDE_TARGET_DSN` | ローカル学習用キャッシュのPostgreSQL接続 |

標準のlibpq URI/DSNを使用する。TLSが必要な接続はDSNで `sslmode=verify-full` 等を指定する。
ソース接続はread-only / repeatable-readで実行し、ライブラリを変更しない。
targetは学習用scratch領域であり、正本のアーカイブDBや複製対象の表へ書く設定にしない。
共有環境ではsource用read-only権限とtarget用cache権限を分ける。
環境変数未設定時には接続せず失敗する。暗黙のlocalhostや私設ホストへのfallbackはない。

一時表へ分割取得してから、削除と追加を単一transactionで確定する。
取得・検証・更新が失敗した場合、既存cacheを保持する。
正しく取得できた0件は正常な空snapshotとして扱う。
同時更新はadvisory lockで直列化し、変更のないIDは書き直さない。
同期に失敗したcycleは、以前のcacheを使って学習を継続せず `exclusions` 段階で停止する。

旧版の実行ノードを更新するときは、接続設定と到達性・権限を先に確認する。
この変更だけを理由に実行中の学習を中断したり、旧版ノードへ本体の全migrationを適用しない。
