"""Presentation only: evidence gaps are never user approval gates."""
from html import escape


def next_action(reg):
    bucket = (reg or {}).get("bucket")
    if bucket == "配備条件の確認待ち":
        return "運用側の検証待ち", "固定した評価画像での試験は終了。本番に近い画像での精度と各ノードの動作確認が必要です。承認ボタンで省略できる検査ではありません。"
    if bucket == "判定待ち":
        return "システムの判定待ち", "判定結果がまだありません。判定処理の実行は未確認で、本番にも反映されていません。"
    if bucket == "不要":
        return "この候補による変更は不要と判定", "この判定だけを理由に追加の学習や承認をする必要はありません。実際の画像で誤りを見つけた場合は訂正してください。"
    if bucket in ("不採用", "基準未達"):
        return "この候補は採用されていません", "待つだけではこの候補に切り替わりません。利用者は画像の誤タグを訂正でき、運用側は再学習・再評価が必要かを調べます。"
    if bucket == "測定不能":
        return "評価条件の確認が必要", "正解例不足などで比較できていません。画像で確実に分かるタグだけ訂正・確認してください。不足条件の特定は運用側が行います。"
    if bucket in ("配備可", "掃除可"):
        return "候補の評価済み・反映は別途確認", "この判定だけでは使用中とは言えません。運用側が適用範囲と本番への反映を確認します。"
    if bucket == "旧gate誤却下":
        return "運用側の再評価待ち", "旧方式の判定記録です。利用者の承認待ちではありません。"
    return "画像の訂正で協力できます", "間違ったタグを外し、確実に分かる不足タグを追加・確認してください。判別できない画像を無理に確定する必要はありません。"


def overview(registrations, learning, release):
    waiting = sum((r or {}).get("bucket") == "配備条件の確認待ち" for r in registrations)
    rejected = sum((r or {}).get("bucket") in ("不採用", "基準未達", "不要") for r in registrations)
    learning = learning or {}
    label = escape(str(learning.get("label") or "実行状況を確認できません"))
    tag = "（" + escape(str(learning["tag"])) + "）" if learning.get("tag") else ""
    return f"""<section class=panel data-tagger-category=overview aria-label='現在地と次の行動'>
<h1>いまの状況と、あなたにできること</h1>
<p><b>本番への反映:</b> 選択中のreleaseは {escape(str(release)) if release is not None else '未確認'}。候補の学習・評価完了とは別です。各ノードへの反映は「ノード」で確認できます。</p>
<p><b>直近の学習報告:</b> {label}{tag}。この報告だけではtimerの稼働や次の学習開始を保証しません。</p>
<p><b>候補の次工程:</b> 本番条件の検証待ち {waiting} タグ／不採用・基準未達・不要 {rejected} タグ／その他の判定 {len(registrations)-waiting-rejected} タグ（各タグの最新候補。使用中モデルの状態とは別）。検証待ちは運用側の仕事で、あなたの承認待ちではありません。</p>
<p><b>あなたができること:</b> <a href="/tagger-dashboard#tags" target="_top">タグ・学習</a>の「画像でタグを訂正」から、誤タグを外し、不足タグを追加してください。正しいと確認できるタグは固定できます。単体で判別できないキャラクターは無理に確定せず、プール題名はヒントとして扱ってください。</p>
<p>訂正は正解データとして蓄積されますが、直ちにモデルが更新されるわけではありません。<b>訂正 → 学習 → 独立した画像で評価 → 本番条件の検証 → 反映</b>の順に進みます。候補の評価点と、使用中のモデルの精度を分けて見てください。</p>
</section>"""


def task(row, learning=None, progress=None):
    """Evidence-derived next step, not a command or a claim of execution.

    Dedicated job observations and automatic reconciler decisions have distinct
    scopes. Stale decisions are never used to request new labels or promise work.
    """
    reg = row.get("reg") or {}
    decision = row.get("disp") or {}
    progress = progress or {}
    result = dict(group="unknown", owner="システム担当", action="学習・判定の記録を取得する",
                  reason="次工程を確定できる候補・配備・実行の記録が不足しています",
                  done="対象タグの記録と取得日時が表示される", user_action=False,
                  observed_at=progress.get("generated_at"),
                  candidate_at=reg.get("at"), executing=False)
    def update(**values):
        return dict(result, **values)
    if row.get("quar"):
        return update(group="held", action="保留対象のモデルを再評価し、修正版を検証する",
                      reason="APIの機械出力受付でこのタグの付与を停止。人の編集は可能。既存の誤タグは残ります。解除には品質検証が必要です",
                      done="同じ評価標本で修正版が品質基準を満たし、配備と監視が完了する")
    if learning and learning.get("tag")==row.get("tag") and learning.get("fresh") and learning.get("state") in ("preparing","measuring"):
        return update(group="measuring", owner="学習プロセス", action="学習・評価の完了報告を待つ",
                      reason=learning.get("label") or "学習・評価の実行報告あり", observed_at=learning.get("observed_at"),
                      done="成功または失敗の終了報告が記録される", executing=True)
    if learning and learning.get("tag") == row.get("tag") and learning.get("lease_expires_at"):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        expires = datetime.fromisoformat(learning["lease_expires_at"]).astimezone(ZoneInfo("Asia/Tokyo"))
        return update(group="candidate", action="学習ジョブの回収状況を確認する",
                      reason=f"学習ロックの期限 {expires:%m/%d %H:%M} JST。ワーカーの稼働は未確認",
                      done="終了報告または次の実行状態が記録される")
    if progress.get("current") and decision.get("disposition") in ("dispatch", "in_flight"):
        return update(group="candidate", action="依頼済みジョブの開始・終了記録を確認する",
                      reason="学習依頼は記録済み。プロセスが稼働中かは未確認",
                      done="開始報告または失敗理由が記録される")
    bucket=reg.get("bucket")
    if progress.get("current"):
        reason=decision.get("reason")
        if reason=="need_positives" and type(decision.get("need_positives")) is int and decision["need_positives"]>0:
            return update(group="labels", owner="利用者（判別できる画像のみ）",action=f"自動再学習の開始条件: 正例あと{decision['need_positives']}件（協力は任意）",
                          reason="最新の自動判定で学習用の正例が不足",done="必要数を保存し、次の自動判定で不足が解消する",user_action=True)
        if reason=="downstream_saturated":
            return update(group="blocked", owner="学習システム", action="実行中ジョブの終了と待ち行列の解消を確認する",
                          reason=f"待ち行列が上限（{decision.get('backlog','?')}/{decision.get('backlog_max','?')}）",
                          done="待ち行列が上限を下回り、次の自動判定で依頼される")
        if reason in ("no_growth","insufficient_labels"):
            needed=[]
            for key,label in (("need_pos","正しい例"),("need_neg","誤付与の訂正")):
                n=decision.get(key)
                if type(n) is int and n>0:needed.append(f"{label} {n}件")
            if needed:
                return update(group="labels", owner="利用者（判別できる画像のみ）", action="自動再学習の開始条件: "+" または ".join(needed)+"（協力は任意）",
                              reason="最新の自動判定が求める前回学習以降の増分。累積訂正数とは別です。増分が揃っても実行・採用は保証しません",
                              done="必要数を保存し、次の自動判定で不足が解消する", user_action=True)
        if decision:
            from api.services.tagger_progress import describe_decision
            return update(group="blocked", action="記録された停止条件を解消し、次の自動判定を確認する",
                          reason=describe_decision(decision),done="停止理由が解消し、次の判定または実行報告が記録される")
    if progress.get("paused"):
        return update(group="blocked", action="自動処理の停止理由を解消してから再判定する",
                      reason="自動処理停止: "+str(progress.get("halt_reason") or "理由未記録"),
                      done="停止条件を解消し、新しい自動判定を記録する")
    if bucket=="配備条件の確認待ち":
        return update(group="validation", action="適用対象から評価標本を固定し、候補の適合率を測定する",
                      reason=reg.get("reason") or "本番条件の精度証拠が未登録",
                      done="適用対象と一致する標本で品質基準を満たし、ノード動作検証を通る")
    if bucket in ("不採用","基準未達","不要"):
        return update(group="rejected", owner="利用者（誤りを見つけた場合）", action="実際の画像の誤タグ・不足タグを訂正する",
                      reason=reg.get("reason") or "最新候補は採用されていません。使用中モデルとは別の判定です",
                      done="訂正を保存。次の学習は新しいデータと実行条件が揃ってから", user_action=True)
    if bucket in ("配備可","掃除可"):
        return update(group="candidate", action="配備対象・ノード検証・公開条件を照合する",
                      reason="候補の評価は完了。配備の設定は別欄、実際の実行成功はノード欄で確認します",
                      done="公開記録と対象ノードの実行記録が同じ版を示す")
    if bucket=="判定待ち":
        return update(group="candidate", action="候補判定の処理結果と失敗理由を取得する",
                      reason="登録済み・判定待ち。判定処理の実行は未確認",
                      done="候補に採用・不採用または不足条件が記録される")
    if bucket=="測定不能":
        why=reg.get("measurement_reason")
        # Older reports retained only the reason text; do not invent counts.
        if why=="insufficient_human_split_support" or (why is None and "insufficient_human_split_support" in (reg.get("reason") or "")):
            support=reg.get("support") or {}
            recorded=[]
            for key,label in (("positive","正しいと確認した例"),("negative","誤付与を訂正した例")):
                item=support.get(key) if isinstance(support,dict) else None
                if not isinstance(item,dict):continue
                actual,required=item.get("observed"),item.get("required")
                if type(actual) is int and type(required) is int and actual>=0 and required>0:
                    recorded.append(f"{label} {actual}/{required}件")
            return update(group="labels", owner="学習システム",
                          action="訂正後のデータで不足条件を再判定する",
                          reason="判定時、学習用と独立評価用に分ける人手の正解データが不足。"+("／".join(recorded) if recorded else "当時の必要数は未記録。"),
                          done="最新の判定で必要数と独立した画像群を確認する。現在も不足かは未確認。利用者は実際の誤りだけ訂正でき、数合わせの訂正は不要です")
        if why=="whole_group_split_unavailable" or (why is None and "whole_group_split_unavailable" in (reg.get("reason") or "")):
            return update(group="labels", action="類似画像を分離した学習・評価標本を組み直す",
                          reason="画像数があっても、同じ画像群を学習と評価に分離できません",
                          done="画像群を混ぜずに必要な正例・負例を確保する。基準は緩めない")
        return update(group="labels", action="測定を止めた条件を調べ、再実行条件を記録する",
                      reason=reg.get("reason") or "評価不能の具体的な不足条件が未記録",
                      done="不足条件と次の処理が記録される。学習完了・本番反映とは扱わない")
    if row.get("cand"):
        return update(group="candidate", action="候補の評価記録を確認する",reason="候補が登録済み。実行は未確認",done="候補に評価結果と次工程が記録される")
    if row.get("live") or row.get("veto") or row.get("policy") is not None:
        return update(group="serving", owner="利用者（誤りを見つけた場合）",action="このタグの画像を確認し、誤りがあれば訂正する",
                      reason="配備設定あり。実際の実行成功や現在の精度を保証する表示ではありません",
                      done="訂正を保存。モデルの評価・学習は別工程", user_action=True)
    reason={"missing":"自動判定の報告が未取得","stale":"自動判定の報告が古い","invalid":"自動判定の報告を読めない"}.get(progress.get("observation_state"))
    if progress.get("paused"):
        reason="自動処理停止: "+str(progress.get("halt_reason") or "理由未記録")
    if bucket=="測定不能":reason=reg.get("reason") or "評価不能の具体的な不足条件が未記録"
    return update(reason=reason or result["reason"])


def task_html(value):
    def e(v):return escape(str(v), quote=True)
    def stamp(v):
        if v is None:return "未記録"
        if isinstance(v,(int,float)):
            from datetime import datetime,timezone
            return datetime.fromtimestamp(v,timezone.utc).isoformat()
        return str(v)
    guidance='画像で確実な誤りだけ訂正してください。' if value['user_action'] else 'あなたの承認・追加操作は不要です。'
    execution='実行報告あり' if value['executing'] else '実行は未確認'
    return (f'<div class="next-task"><b>{e(value["action"])}</b>'
            f'<small>担当: {e(value["owner"])} · {execution}</small>'
            f'<details><summary>理由・実行報告・完了条件</summary><p>{guidance}</p>'
            f'<p>{e(value["reason"])}</p><p>完了条件: {e(value["done"])}</p>'
            f'<p>実行・自動判定の報告: {e(stamp(value["observed_at"]))}<br>'
            f'候補の判定日時: {e(stamp(value["candidate_at"]))}</p></details></div>')
