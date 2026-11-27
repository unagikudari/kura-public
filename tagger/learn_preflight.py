"""Read-only dependency and exact-event check in the actual measurement Python."""
import argparse
import importlib
import importlib.metadata
import json

DEPENDENCIES = {'numpy':'numpy', 'Pillow':'PIL.Image', 'onnxruntime':'onnxruntime',
                'huggingface-hub':'huggingface_hub', 'av':'av', 'requests':'requests',
                'timm':'timm', 'torchvision':'torchvision', 'psycopg2-binary':'psycopg2',
                'torch':'torch'}


def probe(tag, event):
    versions = {}
    for distribution, module in DEPENDENCIES.items():
        importlib.import_module(module)
        versions[distribution] = importlib.metadata.version(distribution)
    # Verify the same reader and actual DB connection settings used by the child.
    from tagger.vnext_runner import kura_read_primary, current_stage
    if kura_read_primary('SHOW transaction_read_only') != [('on',)]:
        raise RuntimeError('child DB connection is not read-only')
    stage, current_event = current_stage(tag)
    if stage != 'training' or current_event != event:
        raise RuntimeError('requested event is not the current training event')
    return {'ok': True, 'tag': tag, 'event': event, 'stage': stage,
            'transaction_read_only': True, 'versions': versions}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--event', type=int, required=True)
    args = parser.parse_args()
    try:
        result = probe(args.tag, args.event)
    except Exception as exc:
        # Do not log driver exception text/DSNs or env credentials.
        print(json.dumps({'ok': False, 'error_type': type(exc).__name__}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
