"""README の install は api/requirements.lock を入れ、release gate の依存監査も
同じ lock を見る。lock が requirements.txt の範囲から外れると、監査した版と
宣言した範囲が食い違ったまま install される。"""
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

API = Path(__file__).resolve().parent.parent / "api"


def _lines(name: str):
    for line in (API / name).read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip().removesuffix("\\").strip()
        if line and not line.startswith("--hash="):
            yield line


def test_every_declared_requirement_is_pinned_inside_its_range():
    pins = {canonicalize_name(n): v for n, v in (l.split("==") for l in _lines("requirements.lock"))}
    for line in _lines("requirements.txt"):
        req = Requirement(line)
        pinned = pins.get(canonicalize_name(req.name))
        assert pinned is not None, f"{req.name} が lock に無い"
        assert req.specifier.contains(pinned, prereleases=True), f"{req.name}=={pinned} は {line} の外"


def test_every_pin_carries_hashes():
    """pip は取ってきたファイルをこの hash と照合する。index への TLS で偽の証明書を
    掴まされても、別のファイルは入らない。"""
    text = (API / "requirements.lock").read_text(encoding="utf-8")
    blocks = [b for b in text.replace("\\\n", " ").splitlines() if b.split("#")[0].strip()]
    assert blocks and all("--hash=sha256:" in b for b in blocks)


def test_the_readme_installs_the_lock():
    readme = (API.parent / "README.md").read_text(encoding="utf-8")
    assert "pip install -r api/requirements.lock" in readme
    assert "pip install -r api/requirements.txt" not in readme
