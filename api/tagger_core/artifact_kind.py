"""artifact のバイトが**何なのか**を 1 箇所で判定する。

■ なぜ (2026-08-06)

`tagger_artifact` は (content_sha256, byte_size) しか持っていなかった。
「このバイトが何か」をスキーマが知らないので、**head policy が head 以外の
バイトを指せた**。実際に 2 件そうなっていた:

    anilingus          4,348 B    safetensors の生 probe ({w,b} だけ)
    dark_labia_majora  297,048 B  測定 JSON そのもの ("schema":"vnext-measurement/2")

どちらも policy 行としては完全に正常に見え、fleet の全ノードが同じように
load に失敗し、同じ調査を各ノードで繰り返すことになる。個別に直しても
次に同じことが起きる — **スキーマが弾けないことが本体**。

■ 何をここで判定し、何をしないか

判定は **構造だけ**で行う。torch は import しない。

  * API サーバ (primary) の venv に torch は入っていない。PUT のたびに 347 MB を
    torch.load するのは現実的でもない。
  * 「torch が無ければ OPAQUE」にすると、正常な head bundle が「head ではない」
    と記録され、policy から参照できなくなって fleet の head が一度に落ちる。
    **誤って分類するくらいならしない方がよい。**

構造判定で確実に言えるのは「**head ではない**」の側で、それがまさに今回の
2 件だった (safetensors ヘッダ / JSON 文書)。torch の zip で top-level dict に
`model` を持つものを `content_head_bundle` とする。

**最終的な証拠は「実際に積めたか」で、それは `tagger_node_head` に各ノードが
書く。** ここは「明らかに head でないものを policy から締め出す」ための門で
あって、head であることの証明ではない。2 段構えにしているのは、片方に両方を
やらせると必ずどちらかが嘘になるため。
"""
from __future__ import annotations

import json
import struct
import zipfile
from pathlib import Path

# 閉じた集合。増やすときは DB 側の CHECK も同時に増える (migration 0139)。
CONTENT_HEAD_BUNDLE = "content_head_bundle"
PROBE_SAFETENSORS = "probe_safetensors"
MEASUREMENT_JSON = "measurement_json"
RELEASE_MODEL = "release_model"
OPAQUE = "opaque"

KINDS = (CONTENT_HEAD_BUNDLE, PROBE_SAFETENSORS, MEASUREMENT_JSON,
         RELEASE_MODEL, OPAQUE)


def _is_safetensors(head: bytes) -> bool:
    """先頭 8 byte が LE の header 長で、その先が JSON object。"""
    if len(head) < 9:
        return False
    (n,) = struct.unpack("<Q", head[:8])
    return 0 < n < (100 << 20) and head[8:9] == b"{"


def _torch_archive_keys(p: Path) -> bytes | None:
    """torch の保存形式は zip。top-level dict のキー名は data.pkl に文字列として
    現れるので、**pickle を実行せずに** 覗ける。実行しないのが要点 — 素性の
    分からないバイトを load する門で load してはいけない。"""
    if not zipfile.is_zipfile(p):
        return None
    with zipfile.ZipFile(p) as z:
        names = [n for n in z.namelist() if n.endswith("data.pkl")]
        if not names:
            return None
        with z.open(names[0]) as fh:
            return fh.read(65536)


def classify(path: str | Path) -> str:
    """このファイルが何かを返す。**推測せず、実際に読んで決める。**"""
    p = Path(path)
    with p.open("rb") as fh:
        head = fh.read(4096)

    if _is_safetensors(head):
        return PROBE_SAFETENSORS

    if head.lstrip()[:1] in (b"{", b"["):
        try:
            doc = json.loads(p.read_text())
        except Exception:  # noqa: BLE001
            return OPAQUE
        if isinstance(doc, dict) and str(doc.get("schema", "")).startswith("vnext-measurement"):
            return MEASUREMENT_JSON
        return OPAQUE

    keys = _torch_archive_keys(p)
    if keys is None:
        return OPAQUE
    # content head は {"model": state_dict, "meta": {...}} (content_head_model.load)。
    # release の patched モデルは n_out / patched を持つ別物。
    if b"n_out" in keys:
        return RELEASE_MODEL
    if b"model" in keys:
        return CONTENT_HEAD_BUNDLE
    return OPAQUE
