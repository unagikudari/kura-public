"""Verify a collector exchange ZIP without extraction or database writes."""
import argparse
import json
from pathlib import Path
from api.collector.export import verify_archive as verify

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    print(json.dumps(verify(parser.parse_args().archive)))
