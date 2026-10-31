"""Machine-readable provenance for automated browser checks, never device attestation."""
import datetime
import json
import os
from pathlib import Path
import subprocess


def write_evidence(*, root, cases, errors, browser_version, case_filter):
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=root, text=True).strip()

    failed = bool(errors) or not cases or any(not case.get('pass_', False) for case in cases.values())
    report = {
        'schema': 'kura.browser-verification/v1',
        'recorded_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'source': {'commit': git('rev-parse', 'HEAD'), 'dirty': bool(git('status', '--porcelain'))},
        'automated': {
            'status': 'failed' if failed else 'passed',
            'suite': 'manga-regressions',
            'selection': case_filter.split(',') if case_filter else 'all',
            'browser': {'engine': os.environ.get('KURA_UI_BROWSER_ENGINE','chromium'), 'channel': (os.environ.get('KURA_UI_BROWSER_CHANNEL') or 'playwright-'+os.environ.get('KURA_UI_BROWSER_ENGINE','chromium')), 'version': browser_version},
            'input_generation': ['Playwright mouse.wheel/mouse/keyboard', 'DOM events and synthetic image responses', 'screenshot pixel sampling'] + (['CDP synthetic touch'] if os.environ.get('KURA_UI_BROWSER_ENGINE','chromium')=='chromium' else []),
            'fixture': {'data': 'synthetic GET-only API', 'image': 'PNG 2400x3600' if os.environ.get('KURA_MANGA_RASTER') else 'SVG 600x900', 'viewport': [1300, 800], 'touch_emulation': True},
            'cases': cases,
            'errors': errors,
        },
        'physical_device': {'status': 'not_performed', 'reason': 'Generated browser input does not attest physical mouse, trackpad or touch behavior.'},
        'user_confirmation': {'status': 'not_collected', 'reason': 'An automated run cannot produce a user report for this candidate.'},
    }
    path = Path(os.environ.get('KURA_BROWSER_EVIDENCE', '/tmp/kura-manga-regressions-results.json'))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'evidence': str(path), 'status': report['automated']['status'], 'cases': len(cases), 'physical_device': 'not_performed'}))
    return 1 if failed else 0
