#!/usr/bin/env bash
# Description: ~/kura を origin/main へ追従させ (main に居るときだけ)、**変わったときだけ**利用者を再起動する
# Kind: tool
# Effects: mutating (git pull + systemctl try-restart)
#
# 2026-08-05 の実測: 自動 pull は 10 分ごとに動いていたが **pull するだけ**で、
# 走っているプロセスは古いコードを抱えたままだった。primary の worker は checkout が
# 最新なのに **15 時間前のプロセス**で稼働しており、その日の修正が 1 つも
# 効いていなかった。「配ったのに反映されない」の正体がこれ。
#
# 取り込みと反映は 1 つの動作でなければならない — 分けると、どちらか片方だけ
# 起きた状態が常態化する。
set -uo pipefail

# **自分の位置から repo を割り出す。** パスをここに書くと、チェックアウト名が
# ノードごとに違う環境 (実測 2026-08-06: primary/nodeb は ~/kura、nodec/desk は
# ~/kura_checkout) で、その 1 行がノードごとに違う状態になる — 今日直した
# 分裂と同じ形。script はリポジトリの中にあるので、自分の位置が答え。
REPO="${KURA_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"

# --- frontend が出荷されていないことを、黙らせない -----------------------------
# 2026-09-06。#95 (タグ一括解除) を merge して本番に載ったつもりでいたが、載っていた
# のは API 半分だけだった。UNITS に kura-web は無く build step も無い。`web/build/` は
# .gitignore 済みなので **pull しても成果物は 1 バイトも変わらない** — timer では永久に
# 直らない。
#
# 見逃した理由がもう 1 つある: `/api/build` は **kura-api の版**を返すので、frontend が
# 何世代古くても "new" と答える。それを根拠に「本番に載っています」と報告した
# (私も、別 lane も)。endpoint が答えられない問いを endpoint に訊いていた。
#
# ここでは deploy しない (10 分ごとの timer で本番 checkout に vite build を走らせるのは
# blast radius が変わる)。**古いことを言う**だけにする。
#
# 判定は version 比較にしない: `web/src/lib/build_info.ts` と `VERSION` は web/ を触らない
# release でも当然ずれるので、それを警告にすると恒常的な偽陽性になる (2026-09-06 07:20 の
# 1.201.35 vs 1.201.36 がまさにその形で、frontend は実質現行だった)。訊くべきは
# 「**web/ が最後に変わったのは、いまの bundle が焼かれた後か**」。
report_stale_frontend() {
  local receipt="$HOME/.local/share/kura-web/active.json"
  if [ -f "$receipt" ]; then
    local active_commit
    active_commit="$(python3 -c 'import json,re,sys; v=json.load(open(sys.argv[1])); c=v.get("commit", ""); assert v.get("format")=="kura-web-release-v1" and re.fullmatch("[0-9a-f]{7,40}",c); print(c)' "$receipt" 2>/dev/null)" || { echo "kura: frontend の配備記録を確認できない"; return 1; }
    if ! git -C "$REPO" diff --quiet "$active_commit" HEAD -- web/; then
      echo "kura: frontend が古い — build 後に web/scripts/web-release.py で切り替えること"
      return 1
    fi
    return 0
  fi
  local bundle="$REPO/web/build/index.js"
  [ -f "$bundle" ] || { echo "kura: web/build/ が無い — frontend は出荷されていない (cd web && npm run build && systemctl --user restart kura-web)"; return 1; }
  local last_web bundle_at
  last_web="$(git -C "$REPO" log -1 --format=%ct -- web/ 2>/dev/null || echo 0)"
  bundle_at="$(stat -c %Y "$bundle" 2>/dev/null || echo 0)"
  [ "$last_web" -gt 0 ] || return 0
  if [ "$bundle_at" -lt "$last_web" ]; then
    echo "kura: **frontend が古い** — web/ の最終変更 $(date -d "@$last_web" '+%m-%d %H:%M') > bundle $(date -d "@$bundle_at" '+%m-%d %H:%M')"
    echo "      /api/build は kura-api の版なので、これを新しさの証拠にしないこと"
    echo "      直し方: cd $REPO/web && npm run build && systemctl --user restart kura-web"
    return 1
  fi
  return 0
}

# 再起動対象。ここに無いものは触らない (oneshot の timer はプロセスを持たない
# ので毎回新しいコードで走る = 再起動不要)。
UNITS=(
  "kura-tagger@full.service"
  "kura-tagger@wd14.service"
  "kura-api.service"
)
# An extension's long-running units come from its extension.json "units" (unit ->
# the paths whose change restarts it), as compose recorded them: a --loop process
# never runs a merged change until it restarts (2026-09-27, a hook added to an
# import worker was live in the checkout and dead in the worker).

# --- unit code paths (tests/test_restart_only_when_unit_code_changed.py loads this block) ---
# 各 unit の「コードの出どころ」。**そのコードが変わったときだけ再起動する。**
# 2026-09-27 実測: docs だけの commit (1.215.3) で primary の import worker unit が
# 再起動され、30 分かかる取り込みが途中で殺された (lease は reaper が
# 回収するが、取り込みはやり直し)。import worker が読むのは scripts/ だけ
# (worker 本体と、その import 先・subprocess 先はすべて scripts/ の
# .py の sibling)。scripts/ の .sh (この script 自身を含む) は worker のコードではない。
# ここに無い unit は repo 全体 = 従来どおりどの commit でも古くなる扱い。
unit_paths() {
  case "$1" in
    # UI-only releases do not change the API/tagger's running code. Keep the broad
    # dependency boundary for backend/helper/extension changes, excluding only
    # frontend sources, release labels, tests and this pull controller itself.
    kura-api.service|kura-tagger@*.service) echo ". :(exclude)web :(exclude)VERSION :(exclude)CHANGELOG.md :(exclude)tests :(exclude)scripts/kura_checkout_pull.sh :(exclude)scripts/check_private_ingress.py" ;;
    *) local p; p="$(extension_units | awk -F'\t' -v u="$1" '$1 == u {print $2}')"; echo "${p:-.}" ;;
  esac
}
# unit<TAB>paths for every unit the composed extensions declare.
extension_units() {
  python3 -c 'import json, sys
try:
    state = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    state = {}
for ext in state.get("extensions", []):
    for unit, paths in (ext.get("units") or {}).items():
        print(f"{unit}\t{paths}")' "$REPO/.kura-extensions.json"
}
# その unit のコードを最後に変えた commit の時刻 (無ければ 0)。
# pathspec は read で配列にする: 未クォートの $(...) だと bash が scripts/*.py を
# cwd に対して glob 展開してしまい、削除された file の変更を見落とす。
code_at() {
  local paths
  read -ra paths <<< "$(unit_paths "$1")"
  git -C "$REPO" log -1 --format=%ct HEAD -- "${paths[@]}" 2>/dev/null | grep . || echo 0
}
# before..after でその unit のコードが変わったか。
code_changed() {
  local paths
  read -ra paths <<< "$(unit_paths "$3")"
  ! git -C "$REPO" diff --quiet "$1" "$2" -- "${paths[@]}" 2>/dev/null
}
# --- end unit code paths ---
while IFS=$'\t' read -r unit _; do [ -n "$unit" ] && UNITS+=("$unit"); done < <(extension_units)

before="$(git -C "$REPO" rev-parse HEAD 2>/dev/null)" || exit 0

# **main に居ないなら追従しない。** この unit の Description は「origin/main へ
# 追従させ」だが、`git pull` は追従先を **その時 checkout されている branch** から
# 取る。両者が食い違った日に何が起きるかは 2026-09-05 に実測された:
# review lane が read のつもりで `gh pr checkout 85` を打ち (read-only な fetch は
# `||` の後ろに置かれ、primary が成功したので走らなかった)、~/kura が PR branch に
# 残った。10 分後の tick がその branch を FF して kura-api を再起動し、**未 merge の
# PR が約 3 分間 production になった** (/api/build が 1.201.17 / perf/bulk-bands-
# viewport を返した)。
#
# 一時的な checkout は起きる。起きたときに **本番へ配ってしまう** のがこの script の
# 側の欠陥で、branch を暗黙に受け取っていたのが原因。追従先を明示し、居場所が違えば
# 追従も再起動もしない。
#
# 黙って何もしないのではなく **非ゼロで落ちる**: service checkout が branch に
# 停まっているのは異常で、直るまで `systemctl --user --failed` に出ているべき。
# 今回の事故で切り替えを見えなくしたのは沈黙 (`>/dev/null 2>&1`) だったので、
# ここで沈黙を選ぶのは同じ誤りを反対側から犯すことになる。
PIN_BRANCH="${KURA_PULL_BRANCH:-main}"
head_branch="$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
if [ "$head_branch" != "$PIN_BRANCH" ]; then
  echo "追従しない: $REPO は '$head_branch' に居る (期待: '$PIN_BRANCH')。" >&2
  echo "  この checkout は production が読む木なので、branch のまま FF すると" >&2
  echo "  未 merge のコードが次の tick で本番になる (2026-09-05 に発生)。" >&2
  echo "  戻し方: git -C $REPO checkout $PIN_BRANCH" >&2
  echo "  PR を読むだけなら checkout せずに:" >&2
  echo "    git -C $REPO fetch -q origin \"+refs/pull/<N>/head:refs/remotes/origin/pr<N>\" && git -C $REPO show origin/pr<N>:<path>" >&2
  echo "  build して動かすなら worktree で: git -C $REPO worktree add ~/wt-pr<N> origin/pr<N>" >&2
  exit 1
fi

# 追従先を **明示する**。bare な `git pull` は tracking branch を読むので、
# それが付け替えられた木では main 以外へ FF しうる (上と同じ穴の別の入口)。
git -C "$REPO" pull --ff-only --quiet origin "$PIN_BRANCH" || {
  echo "pull に失敗 (ローカル変更/分岐の疑い) — 再起動しない" >&2
  exit 1
}
after="$(git -C "$REPO" rev-parse HEAD 2>/dev/null)"

# **branch 名では足りない。commit で比べる。**
#
# 上の PIN_BRANCH guard は「別 branch に居る」を止めるが、**main のまま origin より
# 先行している形**を素通りさせる: 未 push の merge、直接 commit、rebase 途中、
# `git branch -f main <sha>` による force-move。2026-09-05 の reflog では ~/kura の
# HEAD が 1 日に 43 回動いており、branch 切替 (4 回) より**こちらのほうが桁で多い**。
# 実例として、同日 `main` 上で `git merge --no-ff origin/fix/...` してから push まで
# 4m16s 空けた lane が居る — timer 周期 10 分に対して十分な当たり判定で、しかも
# 「pytest が赤なら巻き戻す」前提の窓なので、**赤いコードが本番に載りうる**。
#
# origin/main と一致していることだけが「これは誰かがレビューして push したものだ」の
# 証拠になる。ここを通さないと下の unit 同期と stale-process 再起動の両方が走り、
# どちらも HEAD が何であるかを見ない。
# `--verify` is load-bearing: a bare `git rev-parse origin/main` does NOT fail on a
# missing ref, it echoes the string back. Without it `target` becomes the literal
# "origin/main", which is never equal to a sha, so the tree gets reported as
# "ahead by ? commits" with `origin/main=origin/m` in the message — the misleading
# diagnosis this block exists to prevent, produced by the block itself.
target="$(git -C "$REPO" rev-parse --verify --quiet "origin/$PIN_BRANCH^{commit}" 2>/dev/null || true)"
if [ -z "$target" ]; then
  # 「解決できない」を「先行している」と同じ声で言わない。前者は fetch が届いて
  # いない/ref が無いという **観測の失敗**で、後者は push し忘れという木の状態。
  # 出す指示が正反対 (前者は経路を直せ、後者は push しろ) なので、混ぜると
  # operator を誤った方へ送る — 今日 reader-absent/reader-missing で直したのと同じ形。
  echo "追従しない: origin/$PIN_BRANCH を解決できない ($REPO)。" >&2
  echo "  fetch が届いていないか、その ref が無い。**先行しているのではない** —" >&2
  echo "  この木が本番に出してよい状態かを判断できないので、何もしない。" >&2
  echo "  確認: git -C $REPO fetch origin $PIN_BRANCH && git -C $REPO rev-parse origin/$PIN_BRANCH" >&2
  exit 1
fi
if [ "$after" != "$target" ]; then
  ahead="$(git -C "$REPO" rev-list --count "origin/$PIN_BRANCH..HEAD" 2>/dev/null || echo '?')"
  echo "追従しない: $REPO の $PIN_BRANCH が origin/$PIN_BRANCH より先行している ($ahead commit)。" >&2
  echo "  HEAD=${after:0:8} origin/$PIN_BRANCH=${target:0:8}" >&2
  echo "  未 push の commit/merge は本番に出さない。push するか、戻すこと。" >&2
  exit 1
fi

# **自分の unit も追従させる。毎回、commit が動いていなくても。**
#
# 2026-08-06 まで、この節は「新しい commit が来た」の **後ろ** にあった。
# つまり既に HEAD のノードは unit を一度も見直さない — 手で書き換えられた
# unit が永久に残る。実測: trainer も desk も自前の timer を持っていて、
# repo 側と Description も OnBootSec も違っていた。下のコメントは
# 「差があれば入れ替える」と言っているのに、実際は commit が動いたときしか
# 見ていなかった。**宣言した規律を一部にしか配線していない形。**
# cmp 2 回なので毎 tick 走らせて困らない。
#
# unit をノードごとに手で書くと、その 1 行が
# ノードごとに違う状態が常態化する (2026-08-05 に worker のコードベースが
# 4 系統に分裂していたのと同じ形)。リポジトリ側が正で、差があれば入れ替える。
# これがあるので「機構を入れる手作業」は各ノード 1 回で済み、以後の変更は
# git 経由で届く。
# **service だけでなく timer も同期する。** 2026-08-06 まで同期していたのは
# .service だけで、**発火周期は各ノードが手で決めていた** — desk は
# OnBootSec=2min/OnUnitActiveSec=10min を自分で書いて入れた。上のコメントが
# 戒めている「ノードごとに違う状態が常態化する」が、schedule 側にそのまま
# 残っていた。周期もリポジトリが正。
reload_unit() {
  systemctl --user is-active --quiet "$1" || return 0
  if [ "$1" = "kura-api.service" ]; then
    python3 "$REPO/scripts/kura_api_reload.py"
  else
    systemctl --user try-restart "$1" 2>/dev/null
  fi
}

CHANGED=0
for unit in kura-checkout-pull.service kura-checkout-pull.timer; do
  SRC_UNIT="$REPO/systemd/user/$unit"
  DST_UNIT="$HOME/.config/systemd/user/$unit"
  if [ -f "$SRC_UNIT" ] && ! cmp -s "$SRC_UNIT" "$DST_UNIT"; then
    mkdir -p "$(dirname "$DST_UNIT")"
    cp "$SRC_UNIT" "$DST_UNIT"
    CHANGED=1
    echo "  $unit を更新"
  fi
done
if [ "$CHANGED" = 1 ]; then
  systemctl --user daemon-reload
fi
# timer が有効でなければ有効にする。**入れただけでは動かない** —
# 「機構を入れる手作業は各ノード 1 回」を成り立たせるにはここまで要る。
if ! systemctl --user is-enabled --quiet kura-checkout-pull.timer 2>/dev/null; then
  systemctl --user enable --now kura-checkout-pull.timer 2>/dev/null \
    && echo "  kura-checkout-pull.timer を有効化"
fi
# --- タガーワーカーの配備を宣言 (tagger/deploy/nodes.json) に揃える ---------------
# 2026-10-25 まで、各ノードの kura-tagger@.service は手で写した複製で、誰も揃えて
# いなかった。毎 tick 宣言から生成して比べ、違えば書き直す (変わったときだけワーカーを
# 再起動する)。宣言に無いノードでは何もしない。失敗しても pull は止めない。
# apply が再起動した unit は、下のコード変更による再起動から外す (1 tick で 1 unit 1 回)。
DEPLOY_RESTARTED="$(mktemp)"
trap 'rm -f "$DEPLOY_RESTARTED"' EXIT
python3 "$REPO/scripts/tagger_deploy.py" apply --restarted-file "$DEPLOY_RESTARTED" \
  || echo "kura: tagger の配備を宣言に揃えられなかった (次の tick でやり直す。差分は scripts/tagger_deploy.py check)" >&2
restarted_by_deploy() { grep -qxF "$1" "$DEPLOY_RESTARTED" 2>/dev/null; }
# frontend を出していないノード (wd14 の worker だけのノード) では、frontend の古さを言わない。
serves_frontend() {
  [ -f "$HOME/.local/share/kura-web/active.json" ] \
    || systemctl --user cat kura-web.service >/dev/null 2>&1
}
if [ "$before" = "$after" ]; then
  # **commit が変わっていなくても、動いているプロセスが古ければ再起動する。**
  #
  # 2026-08-06 に unit の同期は早期 exit の外へ出したが、**worker の再起動は
  # 中に残っていた**。その結果「checkout は新しいがプロセスは古い」状態が
  # 自動では直らず、3 回踏んでいる:
  #
  #     08-06 nodeb      pull 前に起動 → 旧コードのまま tag 付けを継続
  #     08-08 trainer  WORKER_GENERATION=5 を持ちながら 4 を報告
  #     08-08 primary       同上 (publish の前提確認で発覚)
  #
  # 判定は **プロセスの起動時刻 < その unit のコードを最後に変えた commit の時刻**
  # (unit_paths。既定は repo 全体 = HEAD の commit 時刻)。commit が変わった
  # ときは下の通常経路が再起動するので、ここが発火するのは**取りこぼした
  # ときだけ**。発火後は起動時刻が HEAD より新しくなるので繰り返さない。
  stale=0
  for u in "${UNITS[@]}"; do
    systemctl --user is-active --quiet "$u" || continue
    started="$(systemctl --user show "$u" --property=ExecMainStartTimestamp --value 2>/dev/null)"
    [ -n "$started" ] || continue
    started_at="$(date -d "$started" +%s 2>/dev/null || echo 0)"
    [ "$started_at" -gt 0 ] || continue
    if [ "$u" = "kura-api.service" ]; then
      worker_at="$(python3 "$REPO/scripts/kura_api_reload.py" --started-at 2>/dev/null || echo 0)"
      [ "$worker_at" -gt 0 ] && started_at="$worker_at"
    fi
    unit_at="$(code_at "$u")"
    restarted_by_deploy "$u" && continue
    if [ "$started_at" -lt "$unit_at" ]; then
      echo "kura: $u は自分のコードの最新 commit より古いプロセスで動いている (再起動)"
      reload_unit "$u" && stale=1
    fi
  done
  if serves_frontend; then report_stale_frontend || true; fi   # deploy はしないが、古ければ黙らない
  exit 0                      # 変わっていない = ここまで (通常は無音)
fi


echo "kura: ${before:0:8} -> ${after:0:8}"

for u in "${UNITS[@]}"; do
  if restarted_by_deploy "$u"; then
    echo "  $u: 配備の更新で再起動済み — 2 回目はしない"
    continue
  fi
  # その unit のコードが動いていなければ再起動しない (unit_paths 参照)。
  if ! code_changed "$before" "$after" "$u"; then
    echo "  $u: コード不変 — 再起動しない"
    continue
  fi
  # try-restart: **動いていないものは起動しない**。このノードで使っていない
  # プロファイルを勝手に立ち上げないため。
  if reload_unit "$u"; then
    systemctl --user is-active --quiet "$u" && echo "  restarted $u"
  fi
done

if serves_frontend; then report_stale_frontend || true; fi
exit 0
