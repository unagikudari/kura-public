"""Portable, hash-bound human training data; no credentials needed by trainer."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
from . import dedicated_protocol as protocol


def eligible_ids(doc, *, include_production=False):
    protocol.validate_snapshot(doc)
    ids = set(doc["split"]["train"] + doc["split"]["heldout"])
    if include_production:
        production, _ = protocol.independent_production_frame(doc)
        ids.update(production)
    return sorted(ids)


class FrozenImages:
    def __init__(self, root, doc):
        self.root = Path(root)
        self.doc = protocol.read_verified(self.root / "dataset.json", "dedicated-dataset/1")
        self.includes_production = self.doc.get("includes_production", False)
        if type(self.includes_production) is not bool:
            raise protocol.ProtocolError("invalid production dataset flag")
        ids = set(eligible_ids(doc, include_production=self.includes_production))
        expected = {str(r["id"]): r["blob_sha256"] for r in doc["media"] if r["id"] in ids}
        if self.doc["snapshot_sha256"] != doc["sha256"] or self.doc["images"] != expected:
            raise protocol.ProtocolError("dataset snapshot/input binding mismatch")
        self.hashes = expected

    def __call__(self, ids):
        return read_frozen_images(self.root, self.hashes, ids)


def read_frozen_images(root, hashes, ids):
    rows = []
    for pid in ids:
        if type(pid) is not int or str(pid) not in hashes:
            raise protocol.ProtocolError("image outside frozen dataset")
        p = root / f"{pid}.bin"
        if p.is_symlink():
            raise protocol.ProtocolError("dataset symlink")
        data = p.read_bytes()
        if hashlib.sha256(data).hexdigest() != hashes[str(pid)]:
            raise protocol.ProtocolError("dataset bytes changed")
        rows.append((pid, data))
    return rows


def export(snapshot, output, *, max_bytes=16 * 1024**3, include_production=False, resume=False):
    doc = protocol.read_verified(snapshot, protocol.SCHEMA)
    ids = eligible_ids(doc, include_production=include_production)
    by_id = {r["id"]: r["blob_sha256"] for r in doc["media"]}
    expected = {post_id: by_id[post_id] for post_id in ids}
    return export_frozen_media(output, expected, {
        "schema": "dedicated-dataset/1", "snapshot_sha256": doc["sha256"],
        "includes_production": include_production,
    }, max_bytes=max_bytes, resume=resume)


def export_frozen_media(output, expected, binding, *, max_bytes=16 * 1024**3, resume=False,
                        require_current=None):
    from . import vnext_arms as arms, display_bytes
    ids = sorted(expected)
    root = Path(output)
    if root.is_symlink() or (root.exists() and (not resume or (root / "dataset.json").exists())):
        raise protocol.ProtocolError("dataset output already exists; preserve evidence")
    root.mkdir(parents=True, mode=0o700, exist_ok=resume)
    conn = arms.connect_primary()
    conn.autocommit = False
    conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
    total = 0
    try:
        if require_current is not None:
            require_current(conn, ids)
        for start in range(0, len(ids), 32):
            chunk = ids[start:start+32]
            existing = []
            missing = []
            for pid in chunk:
                path = root / f"{pid}.bin"
                if path.is_symlink():
                    raise protocol.ProtocolError("dataset symlink")
                if resume and path.exists():
                    existing.append((pid, path.read_bytes()))
                else:
                    missing.append(pid)
            rows = display_bytes.load(lambda sql, args: arms.q(conn, sql, args), missing, workers=2) if missing else []
            if {i for i, _ in rows} != set(missing):
                raise protocol.ProtocolError("frozen input unavailable")
            for pid, data in existing + rows:
                if hashlib.sha256(data).hexdigest() != expected[pid]:
                    raise protocol.ProtocolError("frozen input changed")
                total += len(data)
                if total > max_bytes:
                    raise protocol.ProtocolError("dataset byte budget exceeded")
            for pid, data in rows:
                protocol.write_bytes_once(root / f"{pid}.bin", data)
            print(json.dumps({"state":"exporting", "images":min(start+32,len(ids)), "bytes":total}), flush=True)
    finally:
        conn.rollback(); conn.close()
    return protocol.write_once(root / "dataset.json", {
        **binding, "images": {str(i): expected[i] for i in ids}, "bytes": total,
    })


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("action",choices=["export","measure"])
    p.add_argument("--snapshot",required=True)
    p.add_argument("--media-dir",required=True)
    p.add_argument("--out")
    a=p.parse_args()
    if a.action=="export":
        result=export(a.snapshot,a.media_dir)
    else:
        if not a.out:p.error("measure requires --out")
        doc=protocol.read_verified(a.snapshot,protocol.SCHEMA)
        result=protocol.measure(doc["tag"],a.snapshot,a.out,media_dir=a.media_dir)
    print(json.dumps({"outcome":"succeeded","state":a.action,"reason":"evidence_saved","next":"qualification", "sha256":result["sha256"]}))

if __name__=="__main__":main()
