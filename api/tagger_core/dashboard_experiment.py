"""Experiment progress beside, not instead of, the production lifecycle."""
from html import escape
from datetime import datetime, timezone
from api.tagger_core.experiment_status import PHASES


def render(value):
    if value is None:
        return '<div id=experiment-observation data-tagger-category=overview></div>'
    tag, host = (escape(value[k], quote=True) for k in ('tag', 'host'))
    fresh = value['fresh']
    terminal = value['phase'] in ('completed', 'stopped')
    label = (('実験完了の記録・品質未承認' if value['phase']=='completed' else '停止の記録・原因確認が必要') + ('（直近の報告）' if fresh else '（過去の報告・現在の稼働を示しません）')) if terminal else (PHASES[value['phase']] if fresh else '観測が古いため、稼働状況は未確認')
    unit = {'extracting': '枚', 'training': 'epoch', 'evaluating': '工程',
            'qualifying': '工程', 'completed': '工程', 'stopped': '工程'}[value['phase']]
    count = f"{value['done']:,} / {value['total']:,} {unit}" if value["total"] else "工程数は未記録"
    try:
        observed = datetime.fromtimestamp(value['observed_at'], timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        observed = ''
    experiment = escape(value['experiment'], quote=True)
    progress = (f'<progress value="{value["done"]}" max="{value["total"]}" aria-label="現在の工程 {count}"></progress>'
                if fresh and value['total'] and value['phase'] in ('extracting', 'training', 'evaluating', 'qualifying') else '')
    return f'''<div id=experiment-observation class=learning-strip data-tagger-category=overview aria-label="未公開の改善実験">
<b>{tag} の改善実験（未公開）</b><span data-experiment-freshness>{escape(label)} · {host}</span>
<span>実験ID: <code>{experiment}</code></span>
<span>観測: <time datetime="{observed}">{observed or '観測時刻が不正です'}</time></span>
{progress}<span>{count}（最終報告の工程）</span><small>進捗が最後に増えた時刻: 未記録。観測時刻は稼働確認の時刻です。</small>
<small>本番未反映。下の候補一覧の評価値とは別の実験です。実験の最終報告工程であり、精度や全工程の完了率ではありません。稼働の確認と処理の前進は別です。</small>
<details><summary>判別が難しい画像は？</summary><p>無理に確定する必要はありません。明らかな誤タグは外し、判断できない画像はそのまま保留してください。タグ削除は「無い」という教師情報になるため、単なる判別不能とは区別します。</p></details></div>'''
