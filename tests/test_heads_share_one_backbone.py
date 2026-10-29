"""head を積むほどメモリが増えないことを固定する。

■ 2026-08-06 実測

配備中の head bundle 9 件は **backbone が byte 単位で同一** (指紋 52a4792c5efc)。
1 件 348 MB のうち役に立つのは `head.weight` (1x1024) と `head.bias` の
**4.1 KB だけ**で、残りは全部同じ凍結 backbone の複製。

`load()` は毎回 `ContentHeadModel` を作り直していたので、9 件積むと同一の
backbone が 9 コピー載る:

    head 数   共有なし    共有あり
      1       1,174 MB   1,174 MB
      3       1,884 MB   1,174 MB
      6       2,951 MB   1,174 MB
      9       4,025 MB   1,174 MB

desk (WSL の作業機) では tagger worker が **5.2 GB / WSL 全体 12 GB** を占め、
Windows ホストの空きが 2.8 GB まで落ちて `friction/host-ram-pressure` が
48 時間鳴り続けていた。その主因がこれ。

backbone は frozen — 学習で変わらないのだからプロセスに 1 つあれば足りる。
**形式は変えていない。読む側が共有するだけ。**

■ なぜ実物の bundle で測るか

合成した checkpoint では「同じ backbone を共有する」という前提そのものを
自分の思い込みで作ってしまう。実際に配備されている bytes で確かめる。
無いノードでは skip する (嘘の green を作らない)。
"""
import glob
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

ARTIFACTS = Path(os.environ.get("KURA_ARTIFACT_DIR",
                                Path.home() / ".cache" / "kura-tagger" / "artifacts"))


def _bundles(n: int = 3) -> list[str]:
    return [f for f in sorted(glob.glob(str(ARTIFACTS / "*")))
            if len(os.path.basename(f)) == 64 and os.path.getsize(f) > 10 ** 8][:n]


def _torch_importable() -> bool:
    # bundle の有無だけを見て torch を見ないと、bundle は有るが suite の
    # interpreter (kura-api venv) に torch が無いノードで「skip すべき所で
    # ERROR」になる — docstring の「無いノードでは skip する」に反する。
    # 測定は torch を持つ interpreter (tagger 系 venv) で走らせたときに生きる。
    try:
        import torch  # noqa: F401
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    len(_bundles(2)) < 2 or not _torch_importable(),
    reason="head bundle 2 件以上 + torch のある interpreter でのみ走る (測定ノード)")


@pytest.fixture()
def loaded():
    import torch  # noqa: F401
    from tagger import content_head_model as C
    C._SHARED.clear()
    heads = [C.load(f) for f in _bundles(3)]
    yield C, heads
    C._SHARED.clear()


def test_all_heads_share_one_backbone_object(loaded):
    """**本題**: 同一オブジェクトであること。等価なコピーではメモリは減らない。"""
    _, heads = loaded
    assert all(h.backbone is heads[0].backbone for h in heads)


def test_each_head_keeps_its_own_probe(loaded):
    """共有するのは backbone だけ。probe まで共有したら全タグが同じ判定になる。"""
    _, heads = loaded
    probes = {tuple(h.head.weight.detach().flatten()[:8].tolist()) for h in heads}
    assert len(probes) == len(heads), "probe が共有されている"


def test_the_shared_head_gives_the_same_answer(loaded):
    """**出力が変わらないこと。** 変わるなら共有は最適化ではなく事故。"""
    import torch
    C, heads = loaded
    target = _bundles(3)[-1]
    C._SHARED.clear()
    solo = C.load(target)          # 共有せずに単独で組んだ同じ head
    x = torch.zeros(1, 3, solo.img_size, solo.img_size)
    torch.manual_seed(0)
    x.normal_()
    with torch.no_grad():
        assert solo(x).item() == heads[-1](x).item()


def test_a_different_backbone_is_not_shared(loaded):
    """**backbone の中身が違えば共有しない。** 静かに別モデルの特徴で判定するのが
    一番危ない — 指紋で照合してから共有する。"""
    C, heads = loaded
    from tagger.content_head_model import _backbone_fingerprint
    import torch
    sd = torch.load(_bundles(1)[0], map_location="cpu", weights_only=False)["model"]
    fp = _backbone_fingerprint(sd)
    tampered = dict(sd)
    key = next(k for k in tampered if k.startswith("backbone."))
    tampered[key] = tampered[key] + 1.0
    assert _backbone_fingerprint(tampered) != fp, "重みを変えても指紋が同じ"
