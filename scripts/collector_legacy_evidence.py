#!/usr/bin/env python3
"""Normalize a private legacy snapshot offline and verify a lossless round trip."""
import argparse
import json
import os
from pathlib import Path
import tempfile
from api.collector.adapters.legacy_evidence import compact

MAX_INPUT_BYTES = 256 * 1024 * 1024


def prepare(source, target):
    if target.exists():
        raise ValueError('Output exists; choose a new file')
    with source.open('rb') as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError('Snapshot exceeds 256MiB input budget')
    result = compact(json.loads(raw))
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent, delete=False) as out:
            temporary = Path(out.name)
            os.fchmod(out.fileno(), 0o600)
            json.dump(result, out, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
            out.write('\n'); out.flush(); os.fsync(out.fileno())
        os.link(temporary, target)
        fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {'output': str(target), 'source_snapshot_sha256': result['source_snapshot_sha256'],
            'package_sha256': result['package_sha256'], **result['summary'], 'ready_for_import': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = prepare(args.snapshot, args.output)
    except (ValueError, KeyError, TypeError, OSError):
        parser.error('Invalid snapshot, exceeded budget, or output unavailable; no successful package reported')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
