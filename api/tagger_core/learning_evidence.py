"""Dashboard presentation of measured candidates, never current model accuracy."""
import hashlib
import json


def serving_summary(report):
    try:
        body={k:v for k,v in report.items() if k!='sha256'}
        digest=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        if report.get('sha256')!=digest or report['schema'] != 'dedicated-serving-qualification/1':
            raise ValueError()
        def point(value):
            counts=[value[k] for k in ('tp','fp','tn','fn')]
            if any(type(n) is not int or n < 0 for n in counts):
                raise ValueError()
            tp,fp,tn,fn=counts
            def rate(n,d):return f'{100*n/d:.1f}%' if d else '未測定'
            return f'適合率 {rate(tp,tp+fp)} / 再現率 {rate(tp,tp+fn)}（{sum(counts)}件、誤付与 {fp}、見逃し {fn}）'
        text='未配備候補の固定評価: '+point(report['heldout'])
        baseline=report.get('baseline_diagnostic')
        if baseline is not None:
            text+=' / 比較元: '+point(baseline)+'（学習データとの重複は未確認）'
        else:text+=' / 比較元モデルなし'
        if report.get('parity',{}).get('passed') is not True:
            text+=' / 配備形式との再現性検査に失敗'
        text+=' / 本番全体の精度ではありません'
        return text
    except (KeyError,ValueError,TypeError):
        return '候補の評価証跡を読めません（改善・配備済みとは判定しません）'
