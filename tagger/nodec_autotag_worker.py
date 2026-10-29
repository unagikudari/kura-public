#!/usr/bin/env python3
# Description: kura auto_tag キューを WD14 swinv2-v3(patched release)の general タグ + pixai-v0.9 の character タグで消化する常駐ワーカー。GPU自動検出(無ければCPU)、gpu_batch_gate.py配下ならyield協調。claim/complete lease 制。
# Kind: worker
# Effects: mutating
# Requires: torch, timm, torchvision, requests, Pillow (tagger/requirements.txt に準拠)
# Runs-on: nodec (元祖) / nodeb・desk へも同一スクリプトで展開可能 (host名は複製不要、KURA_AUTOTAG_HOST等で上書き可)
# Version: 0.5
"""kura internal tagging キュー(kind=auto_tag)を WD14 一般タグで消化するワーカー。GPU(CUDA/ROCm)があれば自動的に使う、無ければCPU。

v0.2 (2026-07-20): 生の HuggingFace ベースモデルを直接ダウンロードして推論していた
v0.1 (旧 ~/scripts/kura_autotag_worker.py、kuraリポジトリ管理外の孤立スクリプトだった)
の根本的なギャップを修正 — release_client.py 経由で kura の tagger_release
(パッチ適用済みモデル) を取得・検証してから推論するようにした。以前は
tagger_release/tagger_patch レジストリが何を承認していても、このワーカーは一切
関知せず、常に無修正のベースモデルを使い続けていた(2026-06-20〜2026-07-20の約1ヶ月間)。

v0.4 (同日、ユーザー指示「更新をトリガーに取りに行けばいいじゃん」): v0.2/v0.3 の
「キューが空 → exit → systemd 再起動時に再チェック」というポーリング/リスタート方式を
廃止。常駐プロセス内で毎サイクル refresh_model() を呼び、release_client.pull() の
content-addressed skip(release 未変更ならメタデータ確認のみ)を利用して実質無料の
チェックにし、実際に release_id が変わった時だけ in-process でホットスワップする。
反映遅延も(旧: 最大 MAX_ROUNDS 到達 or 空キュー検出まで)アイドル時の不要なモデル
再ロード churn(nodec の OOM 履歴で問題視)も両方解消。systemd の Restart=always は
クラッシュ復旧のセーフティネットとしてのみ残す。

このファイルは kura リポジトリの正式な一部として管理する(以前のように nodec 上の
~/scripts/ に孤立したコピーとしては置かない) — nodec への配置は
scripts/dispatch_kura_deploy.py と同じ git pull 方式のデプロイ経路を通す。

  # 試走(1バッチ・general タグ適用して complete・rating は計算して表示のみ)
  python tagger/nodec_autotag_worker.py --trial 5
  # 常駐(claim=25 ループ、毎サイクル release を再チェック・変化時のみホットスワップ)
  python tagger/nodec_autotag_worker.py

契約: kura api/routers/tagging.py + schemas/tagging.py。
  claim:    POST {API}/api/internal/tagging/claim?kind=auto_tag&limit=N&lease_seconds=S
            → {claimed:[{id,kind,attempts,lease_expires_at,content_url,mime_type}], lease_seconds}
  complete: POST {API}/api/internal/tagging/complete
            body {id, kind:"auto_tag", status:"ok"|"failed", tags:[{name,category}], error}
auth: ~/kura/api/env の KURA_USER/KURA_TOKEN → Authorization: Token base64(user:token)。
rating: kura の rating は post.safety で、この endpoint はタグしか適用できない。RATING_MODE=off が既定
        (primary と符号化合意するまで適用しない)。'meta' で rating:<v> を meta タグとして付与。

release/patch: release_client.py (このディレクトリの同名モジュール、tagger/infer.py の
load_wd14_fixed() と同じ契約) の pull() で kura の tagger_release テーブルが指す
"現在アクティブなリリース" を取得・sha256検証・load+selftest してから使う。取得失敗時は
最後に検証済みの release にフォールバック(zombie 状態で無検証モデルのまま動き続ける
ことは絶対にしない)。一度も検証済みrelease が無ければ起動を拒否する(fail-closed)。
"""
from __future__ import annotations

import argparse
from tagger.credentials import token_headers
import csv
import io
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import requests
from PIL import Image

import release_client as rc

from tagger.api_target import api_origin
API = api_origin(required=False)
ENVF = os.path.expanduser("~/kura/api/env")
GEN_TH = float(os.environ.get("KURA_AUTOTAG_TH", "0.35"))
# wd14 swinv2-v3 の壊れタグ (user 実測 2026-07-15: condom_on_penis 173中4正解 /
# mosaic ほぼ全滅 / bar 新規陽性 30/30 誤検出)。この3タグは tagger/patches/ に
# 承認済みパッチが無いため、release を使っても直らない — 引き続き抑止する
# (既存タグの削除はしない、task 7b6792b5)。canonical 定義は api/tagger_core/config.py に単一化。
from tagger.pixai_chars import RequiredCharacterComponentError, CHARACTER_POLICY, require_character_heads, character_head_source
from tagger.content_fetch import DEFER_SECONDS, ContentUnavailable, is_original_unavailable
from api.tagger_core.config import QUARANTINE_TAGS, WD14_FIXED_THRESH  # noqa: E402
from tagger.head_policy import REPLACE  # noqa: E402
RATING_MODE = os.environ.get("KURA_AUTOTAG_RATING", "off")  # off | meta
SLEEP_POST = float(os.environ.get("KURA_AUTOTAG_SLEEP", "0.3"))  # throttle/ post
RELEASE_CACHE_DIR = Path(
    os.environ.get("KURA_AUTOTAG_RELEASE_CACHE", os.path.expanduser("~/.cache/kura-autotag/release")))
# GPU優先度は低い(user指示 2026-07-20): このワーカーはGPU_BATCH_GATEでラップされる
# 前提(broker scripts/gpu_batch_gate.py — 他ノード同様、admission gate は
# ExecStart側で供給、worker側は KURA_TAGGER_YIELD_FLAG を毎ラウンド確認して協調的に
# yield するだけでよい。ゲートに包まれていない場合は環境変数が空文字列 → 判定は常にFalse
# で従来通り動く(非GPUノード・ゲート未配線ノードでも無害)。
YIELD_FLAG = os.environ.get("KURA_TAGGER_YIELD_FLAG", "")
AUTOTAG_HOST = os.environ.get("KURA_AUTOTAG_HOST") or os.uname().nodename


def _device() -> str:
    """CUDA(NVIDIA)/ROCm(AMD, torch.cuda名前空間で公開)が使えるノードでは
    自動的にGPUへ、無ければCPUへ静かにフォールバック — ノードごとの個別分岐は
    書かない(nodec/nodeb/deskのtorchビルドがGPU対応かどうかだけに依存する)。
    毎 infer() 呼び出しで torch.cuda.is_available() を叩き直すのは無駄なので
    モジュールロード後 refresh_model() の初回呼び出し前に _resolve_device() で
    一度だけ確定する。"""
    import torch
    return "cuda" if torch.cuda.is_available() else "cpu"


DEVICE = "cpu"  # _resolve_device() が起動時に一度だけ上書きする (import torch を遅延させるための暫定値)


def _resolve_device() -> None:
    global DEVICE
    DEVICE = _device()
    print(f"[kura-autotag] device={DEVICE}", flush=True)


def auth_header():
    kv = {}
    for line in open(ENVF):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            kv[k] = v
    return token_headers(kv['KURA_USER'], kv['KURA_TOKEN']), kv


class ModelState:
    """現在ロード中のモデル一式。release_id が None の間は起動直後で
    リフレッシュ未実行、または一度も検証済み release を取得できていない
    (rc.pull() が例外を投げて main() が起動を拒否する)。"""

    def __init__(self):
        self.release_id: int | None = None
        self.model_sha256: str | None = None
        self.model = None
        self.names: list[str] = []
        self.cats: list[int] = []
        self.img_size: int = 448
        self.veto_heads: dict = {}
        # correction-authoritative reap domain: patched rows ∪ veto heads
        # (user 2026-07-22「最終的に一般化」— not just the veto head). Reported
        # per completion so the server can reap a stale corrected tag.
        self.reap_gates: list[str] = []
        # reap 集合のうち **release 由来の部分**。policy 由来の部分は head を
        # 積み直すたびに変わるので、両者を分けて持ち recompute_reap_gates() で
        # 組み直す。ここを分けないと release が変わらない限り reap 集合が
        # 更新されない (2026-08-06 実測の欠陥、下の関数の docstring 参照)。
        self.release_reap: set[str] = set()


def _policy_reap_gates() -> set[str]:
    """policy 由来で「消してよい」head。**実際に load できたものだけ。**

    load に失敗した head を提案すると、補正されていない出力を根拠にサーバが
    既存タグを消す。
    """
    heads = CONTENT_HEADS or {}
    return {
        t for t in heads
        if not t.startswith("__")
        and heads.get(t) is not None
        and (heads.get(f"__policy__{t}") or {}).get("apply_mode") == "replace"
    }


def recompute_reap_gates(state: "ModelState") -> None:
    """reap 集合を組み直す。**release 由来と policy 由来の両方**が入る。

    2026-08-06 実測の欠陥: この計算は `refresh_model()` の release 読み込み
    ブロックの中にしかなく、`refresh_model()` は release が変わっていなければ
    early-return する。つまり **head を hot-swap しても reap 集合は据え置き**
    だった。greyscale が 07:14 に hot-swap で載ったのに `policy_reap` は
    07:10 の値のままで、head は動いているのに「消してよい」がサーバに伝わらない
    状態になっていた。

    reap は「サーバの認可 × worker の提案」の積なので、片側が更新されないと
    掃除は永久に始まらない。head を積み直したら必ずここを通す。
    """
    state.reap_gates = sorted(
        set(state.release_reap) | set(state.veto_heads) | _policy_reap_gates())


def refresh_model(state: "ModelState") -> None:
    """release_client.py 経由で tagger_release の現在アクティブなパッチ適用済み
    モデルを取得・検証する。rc.pull() は release が変わっていなければ軽量な
    メタデータ確認だけで即座に戻る(content-addressed skip) — この関数を
    毎 claim サイクルで呼んでも通常時のコストはほぼゼロ。実際に release_id が
    変わった時だけ torch モデルを再構築する(in-process ホットスワップ、
    systemd 再起動は不要 — 「更新をトリガーに取りに行く」設計)。

    patched 行以外はベースモデルと byte-identical (build_release.py の凍結
    検証で保証) なので、この1モデルだけで general タグ全体を賄える — 生の
    ONNX モデルは不要。"""
    import timm
    import torch

    H, _ = auth_header()
    fetch_meta_fn, fetch_data_fn = rc.make_http_callbacks(API, H)
    verified = rc.pull(fetch_meta_fn, fetch_data_fn, RELEASE_CACHE_DIR, rc.default_loader)

    if verified.release_id == state.release_id and state.model is not None:
        return  # 既にこの release をロード済み — 何もしない

    ckpt = torch.load(str(verified.model_path), map_location="cpu", weights_only=False)
    model = timm.create_model(f"hf-hub:{ckpt['repo']}", pretrained=False, num_classes=ckpt["n_out"])
    model.load_state_dict(ckpt["state_dict"])
    model.eval()
    model = model.to(DEVICE)
    names, cats = [], []
    with open(verified.csv_path) as f:
        for row in csv.DictReader(f):
            names.append(row["name"])
            cats.append(int(row["category"]))
    was_swap = state.release_id is not None
    state.release_id = verified.release_id
    state.model_sha256 = verified.model_sha256
    state.model = model
    state.names = names
    state.cats = cats
    state.img_size = ckpt.get("img_size", 448)
    # Gated-veto heads bundled into the release (build_release.py) — moves
    # each veto tensor to DEVICE so the veto runs on the same GPU/CPU as the
    # backbone. plan:kura-tagger-self-improvement-loop 「打ち消し続ける」.
    vh = ckpt.get("veto_heads") or {}
    state.veto_heads = {
        g: {"w": d["w"].to(DEVICE), "mu": d["mu"].to(DEVICE), "sd": d["sd"].to(DEVICE),
            "b": d["b"], "threshold": d["threshold"]}
        for g, d in vh.items()
    }
    # correction-authoritative reap domain = patched rows ∪ veto heads (general,
    # data-driven from the ckpt — grows as the release adds corrections).
    # **積んだ head の分も提案する。** reap の権限はサーバが持つが、提案は
    # 「私はこのタグについて権威ある出力を出した」という worker 側の申告で、
    # 両方そろって初めて消える。
    #
    # 2026-08-05 実測の切れ目: policy 由来の content head を積んでも、この行が
    # release の ckpt しか見ていなかったので **提案に入らなかった**。サーバが
    # monochrome を認可しても worker が提案しないので `reap_gates` は空のまま
    # = 掃除が永久に始まらない。
    #
    # **実際に load できた head だけ**を入れる。load に失敗した head を提案すると、
    # 補正されていない出力を根拠にサーバが既存タグを消す。
    state.release_reap = set(ckpt.get("patched") or {})
    recompute_reap_gates(state)
    print(f"[kura-autotag] {'HOT-SWAPPED to' if was_swap else 'loaded'} "
          f"release_id={verified.release_id} n_out={ckpt['n_out']} "
          f"patched={sorted(ckpt.get('patched', {}))} veto={sorted(state.veto_heads)}"
          f" reap_gates={state.reap_gates}"
          f"{' (FALLBACK to previous verified release)' if verified.fallback else ''}", flush=True)

    # tagger_node_state へ報告 (fleet drift が SELECT で見えるように — infer.py と同じ契約)
    try:
        report = requests.post(f"{API}/api/tagger/node-state", headers=H,
                      json={"host": AUTOTAG_HOST, "release_id": verified.release_id,
                            "verified": True,
                            "detail": f"n_out={ckpt['n_out']} fallback={verified.fallback}"},
                      timeout=10)
        report.raise_for_status()
    except Exception as e:  # noqa: BLE001
        print(f"[kura-autotag] node-state report failed ({e})", flush=True)


def infer(model, names, cats, size, img, veto_heads=None, *, characters=(), patched_characters=()):
    """release_client.py が配布する merged checkpoint 専用の前処理: Resize+ToTensor、
    正規化なし(意図的 — tagger/infer.py の _run_wd14_fixed と同一。全パッチ行は
    このパイプラインで学習されており、timm の「正しい」変換に変えると全パッチが
    無効になる)。

    veto_heads がある場合、backbone の pooled 特徴(pre_logits)を同じ 1 回の
    forward から取り、gated-veto を適用する(その gate タグが general に居るとき
    のみ発火、probe < threshold で削除)。tagger/infer.py::tag_image と同一ロジック
    (tagger/veto_apply.run_veto 共有)で、fleet 全 worker が同じ打ち消しをする。"""
    import torch
    from torchvision import transforms

    x = transforms.Compose([
        transforms.Resize((size, size)),
        transforms.ToTensor(),
    ])(img.convert("RGB")).unsqueeze(0)
    pooled = None
    try:
        x = x.to(DEVICE)
        with torch.no_grad():
            if veto_heads and hasattr(model, "forward_features") and hasattr(model, "forward_head"):
                feats = model.forward_features(x)
                pooled = model.forward_head(feats, pre_logits=True)[0]
                probs = torch.sigmoid(model.forward_head(feats)[0])
            else:
                probs = torch.sigmoid(model(x)[0])
        # NumPy does not represent bfloat16. Convert that supported torch
        # output to float32 before transfer; conversion/device failures are
        # still model failures, never evidence that this image is invalid.
        if probs.dtype == torch.bfloat16:
            probs = probs.float()
        probs_np = probs.cpu().numpy()
    except Exception as exc:
        if patched_characters or set(veto_heads or {}) & set(characters):
            raise RequiredCharacterComponentError(f"character correction model unavailable: {exc}") from exc
        raise
    if patched_characters or set(veto_heads or {}) & set(characters):
        if probs_np.shape != (len(names),) or len(cats) != len(names):
            raise RequiredCharacterComponentError("character correction output shape does not match release vocabulary")
    # cat 0 = general, cat 4 = character (skip), cat 9 = rating
    general = [names[i] for i in range(len(names)) if cats[i] == 0 and probs_np[i] >= GEN_TH]
    # Merge the base character proposal BEFORE authoritative corrections/veto.
    # A negative character patch must not be resurrected by a later PixAI append.
    general = list(dict.fromkeys([*general, *characters]))
    for name in patched_characters:
        if name not in names:
            raise RequiredCharacterComponentError(f"character patch {name} has no release output row")
        idx = names.index(name)
        if not 0 <= probs_np[idx] <= 1:
            raise RequiredCharacterComponentError(f"invalid character patch probability for {name}")
        general = [tag for tag in general if tag != name]
        # Same calibrated logit-patch operating point as the full pipeline.
        if probs_np[idx] >= WD14_FIXED_THRESH:
            general.append(name)
    if veto_heads and pooled is not None and general:
        from .veto_apply import run_veto
        gen_set = set(general)
        for gate_tag, vh in veto_heads.items():
            if gate_tag not in gen_set:
                continue  # gate closed: this worker didn't produce it
            try:
                score = run_veto(pooled, vh)
                if (gate_tag in characters or gate_tag in patched_characters) and not 0 <= score <= 1:
                    raise ValueError("invalid character veto probability")
                if score < vh["threshold"]:
                    gen_set.discard(gate_tag)  # veto: cancel the false positive
            except Exception as e:  # noqa: BLE001
                print(f"[kura-autotag] veto {gate_tag} failed: {e}", flush=True)
                if gate_tag in characters or gate_tag in patched_characters:
                    raise RequiredCharacterComponentError(f"character veto {gate_tag} failed: {e}") from e
        general = [t for t in general if t in gen_set]
    ratings = {names[i]: float(probs_np[i]) for i in range(len(names)) if cats[i] == 9}
    top_rating = max(ratings, key=ratings.get) if ratings else None
    return general, top_rating, ratings


# ── content head (grey_owl) ─────────────────────────────────────────────
# このワーカーは 2026-08-04 まで WD14 専用で、content head を一切 load して
# いなかった。実測された帰結: release 9 の audit 334,572 件のうち 99.4% を
# このワーカー (nodec/nodeb/desk) が処理しており、**v2 head でタグ付けされた
# post は primary の 584 件だけ**だった。head を更新しても、コーパスのほぼ全体は
# その head を一度も通らない。
#
# plan:tagger-release-is-the-whole-ensemble rev16 が「release はアンサンブル
# 全体である」と定めた以上、release が content head を component として宣言
# するなら、その release を処理するワーカーは head を適用できなければならない。
#
# 適用規則は tagger/infer.py::tag_image と **完全に同一** にする (fleet 全
# ワーカーが同じタグ付けをすることが前提):
#   * ADD-ONLY — 削除はしない (削除安全性を校正するデータが無い)
#   * "penis" ゲート — 教師が penis 文脈から取られているので、無関係な画像への
#     無駄な推論を避ける
#   * 閾値 0.5 / 既に付いているタグは上書きしない
# 起動時に一度だけ load する (main() が埋める)。provenance と適用の両方が
# これを見るので、片方だけ head を積んだ状態が作れないようにモジュール級に置く。
CONTENT_HEADS: dict | None = None
_POLICY_FP: str | None = None
# smegma-foreskin-phimosis モデル (多出力)。release がある限り、このワーカーで
# 供給するのは **foreskin だけ** — smegma/precum/phimosis は release の patched 行
# が所有する (infer.py::tag_image と同じ判断: 同じタグに補正器を 2 つ走らせると
# どちらが決めたのか分からなくなる)。foreskin は wd14-fixed に行が無い唯一の
# 担当タグなので、これが無いとこのワーカーは foreskin を一度も付けられない。
SKIN_MODEL = None
SKIN_MODEL_SHA: str | None = None
# smegma-foreskin-phimosis は **ContentHeadModel ではない** — backbone + 単一
# Linear ではなく skin/smegma/precum の 3 出力を持つ別アーキテクチャ
# (tagger/smegma_model.py::TaggerModel)。content head として load しようとすると
# state_dict が合わずに落ちる (2026-08-04 nodec で実測)。専用に扱う (SKIN_MODEL)。
# **タグ名の表をここに置かない** (user 2026-08-05:「マジックナンバーや特殊変数で
# 管理するな。スキーマを整えろ」)。旧 `_HEAD_SPECS` は 3 タグを手で列挙して
# いたので、昇格しても書き足すまで適用されなかった — monochrome / greyscale が
# patch まで到達したのに永久に効かなかったのがそれ。
#
# 適用対象・閾値・当て方・gate は `GET /api/tagger/head-policies` が返す。
# bytes は `GET /api/tagger/artifact/{sha}` から取り、**受け取ってから hash し
# 直す** (名前は約束にならない — release_client.pull と同じ規律)。


def _artifact_cache_dir():
    from pathlib import Path as _P
    d = _P(os.environ.get("KURA_ARTIFACT_DIR")
           or (_P.home() / ".cache" / "kura-tagger" / "artifacts"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fetch_artifact(sha: str, *, api_url=None, headers=None):
    """sha の bytes をローカルに用意して path を返す。既にあれば取りに行かない。

    **受け取った bytes を hash し直して一致しなければ捨てる。** 一致しない
    ものを載せると、以後この head の判定は誰にも説明できない値になる。
    """
    import hashlib
    dest = _artifact_cache_dir() / sha
    if dest.is_file() and hashlib.sha256(dest.read_bytes()).hexdigest() == sha:
        return dest
    H = headers if headers is not None else auth_header()[0]
    r = requests.get(f"{api_url or API}/api/tagger/artifact/{sha}", headers=H, timeout=300)
    if r.status_code != 200:
        print(f"[kura-autotag] artifact {sha[:12]} を取得できない HTTP {r.status_code}",
              flush=True)
        return None
    actual = hashlib.sha256(r.content).hexdigest()
    if actual != sha:
        print(f"[kura-autotag] artifact {sha[:12]} の hash 不一致 (実際 {actual[:12]}) "
              f"— 破棄", flush=True)
        return None
    tmp = dest.with_name(f".partial-{sha}")
    tmp.write_bytes(r.content)
    os.replace(tmp, dest)
    return dest


def load_head_policies(*, api_url=None, headers=None) -> list[dict]:
    """Fetch the declared ensemble. Transport failure is not an empty policy."""
    try:
        H = headers if headers is not None else auth_header()[0]
        response = requests.get(f"{api_url or API}/api/tagger/head-policies", headers=H, timeout=30)
        response.raise_for_status()
        policies = response.json()
        if not isinstance(policies, list):
            raise ValueError("head policies must be a list")
        return policies
    except Exception as exc:
        raise RequiredCharacterComponentError(f"head policy unavailable; retry before claiming: {exc}") from exc


def policy_fingerprint(policies: list[dict]) -> str:
    """配備中のアンサンブルを一意に表す文字列。

    ここが変わらなければ積み直す必要は無い。release_client.pull が
    content-addressed skip で「変わっていなければほぼ無料」なのと同じ契約。
    """
    return ";".join(
        f"{p.get('tag')}={p.get('content_sha256')}@{p.get('threshold')}"
        f":{p.get('apply_mode')}:{','.join(p.get('gate_tags') or [])}:{p.get('category', 'general')}"
        for p in sorted(policies, key=lambda x: x.get("tag") or ""))


def refresh_content_heads() -> bool:
    """policy が変わっていれば積み直す。**再起動を要らなくする。**

    2026-08-05 まで `CONTENT_HEADS` は起動時に 1 度だけ読んでいた。つまり
    **新しいタグが昇格しても、誰かが worker を再起動するまで適用されない**。
    release は `refresh_model()` が毎サイクル見てホットスワップしているのに、
    head だけ手動だった。同じ契約に揃える。

    戻り値は「積み直したか」。変わっていなければ HTTP 1 回だけで返る。
    """
    global CONTENT_HEADS, _POLICY_FP
    policies = load_head_policies()
    fp = policy_fingerprint(policies)
    if fp == _POLICY_FP and CONTENT_HEADS is not None:
        return False
    CONTENT_HEADS = load_content_heads(policies)
    _POLICY_FP = fp
    report_node_heads(policies)
    print(f"[kura-autotag] content-heads {'HOT-SWAPPED' if _POLICY_FP else 'loaded'} "
          f"({len(policies)} policy)", flush=True)
    return True


def report_node_heads(policies: list[dict]) -> None:
    """このノードが **どの head を実際に積めたか** をスキーマに書く。

    2026-08-06 まで、これは完了ごとに `veto_gates` 配列で運ばれていた。
    ノードが head を積めたかはノード固有の事実なのに置き場所が無かったため。
    結果「どのノードが greyscale を積んだか」を調べるのに 4 台の journal を
    grep することになった。`tagger_node_state` が release 粒度で持っている
    ものの head 粒度版に揃える。

    fail-soft: ここが落ちてもタグ付けは続ける (観測のための報告であって、
    タグ付けの前提条件ではない)。
    """
    heads = CONTENT_HEADS or {}
    body = {"host": AUTOTAG_HOST, "heads": [
        {"tag": p["tag"], "content_sha256": p["content_sha256"],
         "loaded": heads.get(p["tag"]) is not None,
         "detail": (heads.get(f"__err__{p['tag']}")
                    or f"content_head_bundle loaded (policy v{p.get('patch_version')})")}
        for p in policies if p.get("tag") and p.get("content_sha256")]}
    try:
        H, _ = auth_header()
        r = requests.post(f"{API}/api/tagger/node-heads", headers=H, json=body, timeout=30)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        print(f"[kura-autotag] node-heads の報告に失敗 (タグ付けは継続): {e}", flush=True)


def load_content_heads(policies: list[dict] | None = None, *, artifact_fetcher=None) -> dict:
    """policy が指す head を load する。**タグごとの分岐は無い。**

    欠けている head は「無かったこと」にせず記録する — 何を積んで走ったかが
    provenance に出ないと、後から「この post に head が当たったか」を判定できない。
    """
    if policies is None:
        policies = load_head_policies()
    heads: dict[str, object] = {}
    loaded, missing = [], []
    for pol in policies:
        tag, sha = pol.get("tag"), pol.get("content_sha256")
        if not tag or not sha:
            continue
        heads[f"__policy__{tag}"] = {
            "threshold": pol.get("threshold"),
            "apply_mode": pol.get("apply_mode"),
            "gate_tags": pol.get("gate_tags") or [],
            "operating_point": pol.get("operating_point"),
            "category": pol.get("category", "general"),
        }
        try:
            path = (artifact_fetcher or _fetch_artifact)(sha)
        except Exception as exc:
            if pol.get("category") == "character":
                raise RequiredCharacterComponentError(f"character artifact {tag} unavailable: {exc}") from exc
            raise
        if path is None:
            heads[tag] = None
            missing.append(tag)
            continue
        try:
            from .content_head_model import load as _load
            # backbone と同じ DEVICE に載せる。content_head_model.load() は
            # map_location="cpu" 固定で、ここで移さないと head だけ CPU に
            # 残る — infer() の wd14 backbone は GPU、head は CPU という
            # 混在になり、GPU が遊んでいるのに CPU が張り付く (2026-08-29
            # nodeb 実測: GPU busy 0%・CPU 200%・0.7 post/min、head 11 本の
            # forward が CPU 22.6s に対し GPU 1.6s = 14.2x)。veto_heads が
            # refresh_model() で .to(DEVICE) されているのと同じ扱いに揃える。
            heads[tag] = _load(path).to(DEVICE)
            heads[f"__sha__{tag}"] = sha[:12]
            loaded.append(tag)
        except Exception as e:  # noqa: BLE001
            print(f"[kura-autotag] content-head {tag} の load に失敗: {e}", flush=True)
            heads[tag] = None
            # 失敗理由を捨てない — スキーマに報告するので、どのノードで何が
            # 積めなかったかを journal を grep せずに引けるようにする。
            heads[f"__err__{tag}"] = str(e)
            missing.append(tag)
    print(f"[kura-autotag] content-heads loaded={loaded} missing={missing} "
          f"(policy {len(policies)} 件)", flush=True)
    return heads


def load_skin_model():
    """smegma-foreskin-phimosis モデルを load する。ContentHeadModel とは別経路。"""
    from pathlib import Path as _P
    models_dir = _P(os.environ.get("KURA_MODELS_DIR")
                    or (_P.home() / ".cache" / "kura-tagger" / "models"))
    ckpt = models_dir / "smegma-foreskin-phimosis" / "tagger-smegma-foreskin-phimosis-v1-best.pt"
    if not ckpt.is_file():
        print(f"[kura-autotag] skin model 未配置 ({ckpt}) — foreskin を供給できない", flush=True)
        return None, None
    try:
        from . import smegma_model
        # content head と同じく DEVICE へ。load() は CPU 固定で読むので、
        # ここで移さないと apply_skin_model() の forward が CPU に落ちる。
        m = smegma_model.load(ckpt).to(DEVICE)
        sha = _sha256_short(ckpt)
        print(f"[kura-autotag] skin model loaded ({sha}) — foreskin を供給する", flush=True)
        return m, sha
    except Exception as e:  # noqa: BLE001
        print(f"[kura-autotag] skin model の load に失敗: {e}", flush=True)
        return None, None


def skin_verdict(general: list[str], skin_class: str | None) -> list[str]:
    """skin class からタグ集合を決める純関数 (torch 不要なので単体で検証できる)。

    infer.py::tag_image と同一: release がある限り供給するのは **foreskin だけ**。
    smegma / precum / phimosis は release の patched 行が所有する — 同じタグに
    補正器を 2 つ走らせると、どちらが決めたのか分からなくなる。
    """
    if skin_class == "foreskin" and "foreskin" not in general:
        return list(general) + ["foreskin"]
    return general


def apply_skin_model(general: list[str], img, model) -> list[str]:
    """infer.py::tag_image と同一: penis ゲート、release 有り時は foreskin のみ additive。"""
    if model is None or "penis" not in general or "foreskin" in general:
        return general
    try:
        import torch
        from torchvision import transforms

        from .smegma_model import SKIN_CLASSES
        x = transforms.Compose([
            transforms.Resize((384, 384)),
            transforms.ToTensor(),
            transforms.Normalize(mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225)),
        ])(img.convert("RGB")).unsqueeze(0).to(DEVICE)
        with torch.no_grad():
            skin_logits, _, _ = model(x)
        return skin_verdict(general, SKIN_CLASSES[int(skin_logits.argmax(1)[0])])
    except Exception as e:  # noqa: BLE001
        print(f"[kura-autotag] skin model の推論に失敗: {e}", flush=True)
    return general


def _sha256_short(path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def _content_head_prob(img, model) -> float:
    """tagger/infer.py::_run_content_head と同一の前処理。"""
    import torch
    from torchvision import transforms

    from .content_head_model import IMG_SIZE

    size = getattr(model, "img_size", IMG_SIZE)
    mean = list(getattr(model, "mean", (0.485, 0.456, 0.406)))
    std = list(getattr(model, "std", (0.229, 0.224, 0.225)))
    x = transforms.Compose([
        transforms.Resize((size, size)),
        transforms.ToTensor(),
        transforms.Normalize(mean=mean, std=std),
    ])(img.convert("RGB")).unsqueeze(0).to(DEVICE)
    with torch.no_grad():
        return float(torch.sigmoid(model(x))[0].item())


def apply_content_heads(general: list[str], img, heads: dict | None, *, scores=None) -> list[str]:
    """policy に書かれたとおりに head を当てる。

    **閾値も gate もここに書かない** — `__policy__<tag>` に載っている値を使う。
    旧版は `"penis" not in general` という**タグ名で書かれた前提**と
    `GREYOWL_ADD_THRESH = 0.5` という全 head 共通の既定閾値を持っていた。
    前者は monochrome のような無関係なタグの head を永久に発火させないし、
    後者は測定が選んだ閾値と見分けがつかないまま配備に混ざる。

    gate は `gate_tags` が全部そろっているときだけ発火する (domain gate =
    「この視覚領域でだけ校正されている」の意味なので、一部一致は校正の外)。
    """
    if not heads:
        return general
    require_character_heads(heads)
    out = list(general)
    present = set(out)
    for tag in sorted(t for t in heads if not t.startswith("__")):
        m = heads.get(tag)
        if m is None:
            continue
        pol = heads.get(f"__policy__{tag}") or {}
        # **既に付いているタグを素通ししない。** `replace` の head は「付いて
        # いるが違う」を言えなければ意味が無い。旧版は `tag in present` で
        # 早期に抜けており、**base の wd14 が出し続ける限り出力に残り、
        # サーバの reap は「出力にある」ので消さなかった** (2026-08-06 実測:
        # 再タグした post の 8 割が monochrome を保持していた)。
        if tag in present and pol.get("apply_mode") != REPLACE:
            continue
        thr = pol.get("threshold")
        if not isinstance(thr, (int, float)):
            # **既定値で埋めない。** 閾値の無い head は当てない。
            continue
        gates = pol.get("gate_tags") or []
        if gates and not all(g in present for g in gates):
            continue
        try:
            probability = _content_head_prob(img, m)
            if pol.get("category") == "character" and not 0 <= probability <= 1:
                raise ValueError("invalid character probability")
            fires = probability >= float(thr)
        except Exception as e:  # noqa: BLE001
            print(f"[kura-autotag] content-head {tag} の推論に失敗: {e}", flush=True)
            if pol.get("category") == "character":
                raise RequiredCharacterComponentError(f"character head {tag} inference failed: {e}") from e
            continue
        if fires:
            if scores is not None:
                scores[tag] = probability
            if tag not in present:
                out.append(tag)
                present.add(tag)
        elif tag in present and pol.get("apply_mode") == REPLACE:
            # **出力から外す。** 消すのはサーバ (reap) の仕事で、worker は
            # 「自分の出力に無い」と言うだけ。権限 (policy の apply_mode) と
            # 提案 (veto_gates) と**この取り下げ**の 3 つが揃って初めて消える。
            out = [t for t in out if t != tag]
            present.discard(tag)
            if scores is not None:
                scores.pop(tag, None)
    return out


# ── pixai character component (2026-09-04) ──────────────────────────────
# このワーカーは character を一切付けていなかった (`infer()` の cat==4 skip)。
# 実測された帰結: 直近 30 日でキャラタグを付与した host は primary (full pipeline)
# の 9,118 件**のみ** — スループットの 98.5% を担うこのワーカー群を通った post
# にはキャラが付かない。pixai-tagger-v0.9 は release アンサンブルの**宣言済み
# component** (tagger_release_component role='pixai') なので、これは content
# head のとき (2026-08-04、「99.4% が head を一度も通らない」) と同じ
# 「宣言されたアンサンブルと配備の乖離」だった。
#
# 適用は tagger/pixai_chars.py (infer.py と共有の唯一の実装、閾値込み)。
# **CPUExecutionProvider 固定**なのでこのワーカーの torch/GPU 状態 (nodeb の
# ROCm 等) には触れない。fail-soft: load できなければキャラ無しで走り続け、
# サイクル毎に再試行する — ただし沈黙はしない (毎回 stderr に出る)。
PIXAI_CHARS: dict | None = None


def refresh_pixai_chars() -> None:
    global PIXAI_CHARS
    if PIXAI_CHARS is not None:
        return
    try:
        from . import pixai_chars
        PIXAI_CHARS = pixai_chars.load()
        print(f"[kura-autotag] pixai chars loaded ({len(PIXAI_CHARS['chars'])} 語彙, "
              f"sha={PIXAI_CHARS['sha12']}, CPU)", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[kura-autotag] pixai chars UNAVAILABLE — claim を停止 "
              f"(次サイクル再試行): {type(e).__name__}: {str(e)[:160]}",
              file=sys.stderr, flush=True)


def apply_pixai_chars(img) -> list[str]:
    """Return genuine predictions, including zero hits; never hide component failure."""
    global PIXAI_CHARS
    if PIXAI_CHARS is None:
        raise RequiredCharacterComponentError("PixAI character model is not loaded; retry loading before claiming")
    try:
        from . import pixai_chars
        return [n for n, _ in pixai_chars.char_tags(PIXAI_CHARS, img)]
    except Exception as exc:  # reload a broken session before another claim
        PIXAI_CHARS = None
        raise RequiredCharacterComponentError(f"PixAI character inference failed: {exc}") from exc


def ensemble_source_id(release_id, heads: dict | None) -> str:
    """provenance に「実際に積んで走ったアンサンブル」を刻む。

    これまでは `nodec-autotag-r{release_id}` という **固定文字列**で、release_id
    以外に何も語っていなかった。head を積んだか否かが記録に残らないので、
    「この post に head が当たったか」を後から判定できなかった。

    pixai (character) も同じ理由で刻む。さらに actor_id が変わることで、
    pixai を積めなかった個体の self-retraction (reap_scope='own') が pixai
    個体の付けたキャラタグを**別 actor の claim として触れない**側に倒れる。
    """
    parts = [f"nodec-autotag-r{release_id}" if release_id is not None else "nodec-autotag"]
    parts.append(CHARACTER_POLICY)
    if policy_source := character_head_source(heads):
        parts.append(policy_source)
    parts.append(f"skin={SKIN_MODEL_SHA}" if SKIN_MODEL_SHA else "skin=absent")
    parts.append(f"pixai={PIXAI_CHARS['sha12']}" if PIXAI_CHARS else "pixai=absent")
    for tag in sorted(t for t in (heads or {}) if not t.startswith("__")):
        sha = (heads or {}).get(f"__sha__{tag}")
        parts.append(f"{tag}={sha}" if sha else f"{tag}=absent")
    return ";".join(parts)


def build_tags(general, top_rating, chars=()):
    tags = [{"name": n, "category": "general"} for n in general if n not in QUARANTINE_TAGS]
    tags += [{"name": n, "category": "character"} for n in chars if n not in QUARANTINE_TAGS]
    if RATING_MODE == "meta" and top_rating:
        tags.append({"name": f"rating:{top_rating}", "category": "meta"})
    return tags


def fetch_image(H, content_url):
    url = content_url if content_url.startswith("http") else API + content_url
    try:
        r = requests.get(url, headers=H, timeout=60, allow_redirects=True)
    except (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ContentDecodingError) as exc:
        # A connection that drops before Content-Length is reached surfaces here as
        # ChunkedEncodingError (codex review of 1.269.0), not as a short body.
        raise ContentUnavailable(f"{type(exc).__name__}: {exc}") from exc
    if is_original_unavailable(r.status_code, r.content):
        raise ContentUnavailable("HTTP 503 original_unavailable")
    r.raise_for_status()
    expected = r.headers.get("Content-Length")
    if expected is not None and expected.isdigit() and len(r.content) < int(expected):
        raise ContentUnavailable(f"short body: {len(r.content)} of {expected} bytes")
    img = Image.open(io.BytesIO(r.content))
    img.load()  # Decode failures belong to the image, not the required model.
    return img


# Claim guard identity (plan:kura-tagger-lifecycle-redesign Step 11): this
# worker is the WD14-only pipeline, which is what tagger_expected_worker.
# profile='wd14' means for nodec/nodeb/desk. A worker reporting the wrong
# profile for its host is refused the claim (409) -- see
# api/services/tagging.py::enforce_claim_guard.
WORKER_PROFILE = "wd14"
# 2026-08-04: 2 -> 3。**publish より先に上げて配り切る**のが唯一の安全な順序。
# tagger_governed_publish_release は active generation を必ず +1 するので、
# worker が 2 のまま publish すると active=3 > worker=2 で全ノードが 409 を返し
# fleet が止まる (2026-08-02 に実際に 14.5 時間・503,486 件を滞留させた)。
# worker >= floor なら通るので、worker=3 / active=2 の窓は安全。
WORKER_GENERATION = 5  # 2026-08-08: 4 -> 5, phimosis v2 retirement (chance AUC, serving P≈0.199) を載せる release の先行 bump — publish は active generation を必ず +1 するので、先に配り切るのが唯一の安全な順序 (2026-08-02 の 14.5h 停止の教訓)


def _provider_label() -> str:
    """ONNX-style provider name for the currently resolved torch device, so
    tagger_worker_heartbeat/tagger_fleet stay consistent with infer.py's
    onnxruntime-based workers even though this one uses torch. ROCm builds
    of torch expose GPUs under the same `cuda` namespace as NVIDIA, so
    DEVICE=='cuda' alone can't distinguish them -- torch.version.hip is set
    only on ROCm builds."""
    import torch
    if DEVICE != "cuda":
        return "CPUExecutionProvider"
    return "ROCMExecutionProvider" if getattr(torch.version, "hip", None) else "CUDAExecutionProvider"


def claim(H, limit, lease, state: "ModelState"):
    if PIXAI_CHARS is None:
        raise RequiredCharacterComponentError("PixAI character model unavailable; no posts claimed")
    require_character_heads(CONTENT_HEADS)
    params = {
        "kind": "auto_tag", "limit": limit, "lease_seconds": lease, "host": AUTOTAG_HOST,
        "worker_id": f"{AUTOTAG_HOST}-autotag", "profile": WORKER_PROFILE,
        "generation": WORKER_GENERATION, "provider": _provider_label(),
    }
    if state.release_id is not None:
        params["release_id"] = state.release_id
    if state.model_sha256:
        params["model_sha256"] = state.model_sha256
    r = requests.post(f"{API}/api/internal/tagging/claim", headers=H, params=params, timeout=60)
    r.raise_for_status()
    return r.json().get("claimed", [])


#: The 404 the complete route answers when the post was deleted after its claim
#: (api/routers/tagging.py). The item is dropped, not reported failed.
POST_GONE = "tagging_post_gone"


class PostGone(Exception):
    """The claimed post no longer exists; nothing to complete."""


def complete(H, post_id, status, tags=None, error=None, release_id=None, veto_gates=None):
    body = {"id": post_id, "kind": "auto_tag", "status": status,
            "tags": tags or [], "error": error,
            # veto-authority reap (plan:kura-tagger-self-improvement-loop, user
            # 2026-07-22): the gate tags this worker's veto applied + its code
            # generation (non-repudiation). Lets the server DELETE a gate tag
            # the veto suppressed, not just skip re-adding it. Empty on
            # gen<2/no-veto workers → server no-ops the veto reap.
            "veto_gates": veto_gates or [],
            "generation": WORKER_GENERATION,
            # host+release_id (0072): host is auto-detected (os.uname().nodename,
            # overridable via KURA_AUTOTAG_HOST) -- this script now runs
            # identically on nodec/nodeb/desk (2026-07-20, GPU fleet expansion),
            # so a hardcoded "nodec" would mislabel every non-nodec node's
            # completions. `source`/actor_id is DELIBERATELY left host-agnostic
            # ("nodec-autotag-rN") -- every physical node running this script
            # drains the SAME auto_tag queue with the SAME model, so treating
            # them as one logical actor for claim/self-retraction purposes
            # (post_tag_actor_claim, 0073) is correct, not a bug: `host`
            # already gives the accurate per-completion physical breakdown
            # (see the tagger-dashboard 稼働量 panel, which shows both).
            "host": AUTOTAG_HOST, "release_id": release_id,
            "source": ensemble_source_id(release_id, CONTENT_HEADS),
            # rev7 self-retraction (plan kura-tagger-audit-and-remediation,
            # fact 00702a48): nodec is the first production reap_scope='own'
            # trial (the shadow period the plan requires before any
            # default-on decision). nodec is a single coherent actor -- one
            # completion per post always carries its FULL current WD14
            # proposal -- so self-retraction here can only ever remove a
            # tag nodec itself previously proposed and no longer does; it is
            # structurally unable to touch another actor's (e.g. a
            # trainer head's) tags. This is the actual "sweep stale WD14
            # tags" behavior originally asked for at the start of this
            # session.
            "reap_scope": "own"}
    r = requests.post(f"{API}/api/internal/tagging/complete", headers=H, json=body, timeout=60)
    if r.status_code == 404 and _error_slug(r) == POST_GONE:
        raise PostGone(post_id)
    r.raise_for_status()
    return r.json()


def _error_slug(r):
    try:
        detail = r.json().get("detail")
    except ValueError:
        return None
    return detail.get("error") if isinstance(detail, dict) else None


def process_item(H, model, names, cats, size, item, release_id=None, verbose=False,
                 veto_heads=None, reap_gates=None, content_heads=None, patched_tags=()):
    pid = item["id"]
    try:
        img = fetch_image(H, item.get("content_url") or f"/api/post/{pid}/content")
        chars = apply_pixai_chars(img)
        character_names = set(PIXAI_CHARS["chars"].values()) | {
            name for name, category in zip(names, cats) if category == 4}
        character_names.update(tag for tag in (content_heads or {})
                               if (content_heads.get(f"__policy__{tag}") or {}).get("category") == "character")
        candidates, top_rating, ratings = infer(
            model, names, cats, size, img, veto_heads=veto_heads, characters=chars,
            patched_characters=set(patched_tags) & character_names)
        candidates = apply_content_heads(candidates, img, content_heads)
        candidates = apply_skin_model(candidates, img, SKIN_MODEL)
        tags = build_tags([tag for tag in candidates if tag not in character_names], top_rating,
                          [tag for tag in candidates if tag in character_names])
        # reap_gates = correction-authoritative domain (patched ∪ veto), NOT just
        # veto heads — server reaps a stale corrected tag absent from this output.
        res = complete(H, pid, "ok", tags=tags, release_id=release_id,
                       veto_gates=reap_gates or [])
        if verbose:
            print(f"  post {pid}: applied={res.get('tags_applied')} rating={top_rating} "
                  f"tags[:8]={candidates[:8]}")
        return True, len(tags), top_rating
    except RequiredCharacterComponentError as exc:
        # No completion => no successful reap, no failed-attempt charge. The
        # existing lease expiry returns this post and the rest of the batch.
        raise RequiredCharacterComponentError(f"post {pid}: {exc}") from exc
    except ContentUnavailable:
        # Infrastructure, not this post (tagger/content_fetch.py): same as above.
        raise
    except PostGone:
        # Deleted after the claim: a failed completion would meet the same 404.
        if verbose:
            print(f"  post {pid}: GONE (deleted after claim) — dropped")
        return False, 0, None
    except Exception as e:  # noqa: BLE001
        try:
            complete(H, pid, "failed", error=str(e)[:300], release_id=release_id)
        except Exception:  # noqa: BLE001
            pass
        if verbose:
            print(f"  post {pid}: FAILED {str(e)[:120]}")
        return False, 0, None


def process_batch(H, state, limit, lease, *, verbose=False):
    """A required-component failure aborts the batch without consuming image retries."""
    items = claim(H, limit, lease, state)
    ok = 0
    for item in items:
        succeeded, _, _ = process_item(
            H, state.model, state.names, state.cats, state.img_size, item,
            release_id=state.release_id, verbose=verbose,
            veto_heads=state.veto_heads, reap_gates=state.reap_gates,
            content_heads=CONTENT_HEADS, patched_tags=state.release_reap)
        ok += succeeded
        time.sleep(SLEEP_POST)
    return ok, len(items)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trial", type=int, default=0, help="1バッチだけ N 件 claim して処理し終了(検証用)")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--lease", type=int, default=600)
    a = ap.parse_args()
    if not API:
        print("ERROR: set KURA_URL or KURA_API before starting the worker", file=sys.stderr)
        return 2
    H, _ = auth_header()
    _resolve_device()
    # content head は毎サイクル policy を見て積み直す (下の loop も同じ)。
    # 起動時に 1 度だけだと、新しいタグが昇格しても再起動まで適用されない。
    refresh_content_heads()
    refresh_pixai_chars()
    global SKIN_MODEL, SKIN_MODEL_SHA, _POLICY_FP
    SKIN_MODEL, SKIN_MODEL_SHA = load_skin_model()
    print(f"[kura-autotag] API={API} th={GEN_TH} rating_mode={RATING_MODE} "
          f"{'TRIAL' if a.trial else 'RESIDENT'}", flush=True)
    state = ModelState()
    refresh_model(state)
    recompute_reap_gates(state)

    if a.trial:
        try:
            ok, count = process_batch(H, state, a.trial, a.lease, verbose=True)
        except (RequiredCharacterComponentError, ContentUnavailable) as exc:
            print(f"[trial deferred] {exc}; incomplete leases expire within {a.lease}s; no completion sent", flush=True)
            return 1
        print(f"[trial done] ok={ok}/{count}", flush=True)
        return 0

    # v0.4 (2026-07-20, ユーザー指示「更新をトリガーに取りに行けばいい」):
    # v0.2/v0.3 は「キューが空になったら exit → systemd 再起動で release
    # 再チェック」というポーリング/リスタート方式だった — 反映が最大数分
    # 遅れる上、アイドル中も定期的にモデル再ロードの churn が発生していた
    # (nodec の OOM 履歴を踏まえると望ましくない)。
    #
    # v0.4 はリスタート機構を完全に廃止し、常駐プロセス内で毎サイクル
    # refresh_model() を呼ぶだけにした。release_client.pull() は release が
    # 変わっていなければメタデータ確認のみ(content-addressed skip)で
    # ほぼ無料 — 実際に release_id が変わった時だけ torch モデルを
    # 再構築してホットスワップする。systemd の Restart=always は純粋な
    # クラッシュ復旧用のセーフティネットとして残るが、release 反映の
    # トリガーとしては使わない。
    done = 0
    t0 = time.time()
    while True:
        # GPU優先度は低い(user指示 2026-07-20): gpu_batch_gate.py に ExecStart
        # をラップされているノード(GPU使用時のみ意味を持つ)では、他の作業が
        # GPUを使い始めると環境変数 KURA_TAGGER_YIELD_FLAG が指すファイルが
        # touch される — ラウンド先頭で確認し、あれば即座に exit する
        # (tagger/loop.py と同じ協調的 yield 契約)。systemd の Restart=always
        # がゲート再admit後に再起動する。ゲートに包まれていないノード(CPU
        # のみ、または未配線)では環境変数が空文字列 → 判定は常に False で
        # 従来通り動く。
        if YIELD_FLAG and os.path.exists(YIELD_FLAG):
            print(f"[kura-autotag] yield flag present ({YIELD_FLAG}) — graceful exit", flush=True)
            return 0
        # **head を先に積み直してから** release を見る — reap_gates は
        # refresh_model が CONTENT_HEADS から組み立てるので、順序が逆だと
        # 新しい head が 1 サイクル遅れて提案される。
        try:
            refresh_content_heads()
            refresh_pixai_chars()
            refresh_model(state)
            # **毎サイクル必ず組み直す。** 「head が変わったときだけ」にすると、
            # release 側が early-return する経路で更新が落ちる (それがこの欠陥の
            # 本体だった)。集合演算だけなので毎回やって困らない。
            recompute_reap_gates(state)
            ok, count = process_batch(H, state, a.limit, a.lease)
        except ContentUnavailable as exc:
            print(f"[kura-autotag] content unavailable: {exc}; batch stopped, no completion "
                  f"sent; incomplete leases expire within {a.lease}s; retry in {DEFER_SECONDS}s",
                  flush=True)
            time.sleep(DEFER_SECONDS)
            continue
        except RequiredCharacterComponentError as exc:
            print(f"[kura-autotag] character component unavailable: {exc}; "
                  f"incomplete leases expire within {a.lease}s; no completion sent; "
                  "retry model load in 30s", flush=True)
            state.model = None
            _POLICY_FP = None
            time.sleep(30)
            continue
        if not count:
            time.sleep(SLEEP_POST if SLEEP_POST >= 5 else 5)
            continue
        done += ok
        rate = done / max(1, (time.time() - t0) / 60)
        print(f"[kura-autotag] done={done} rate={rate:.1f}/min", flush=True)


if __name__ == "__main__":
    sys.exit(main())
