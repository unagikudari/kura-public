from api.tagger_core.dashboard_actions import next_action, overview


def test_verification_wait_is_not_owner_approval():
    title, action = next_action({"bucket": "配備条件の確認待ち"})
    assert title == "運用側の検証待ち"
    assert "省略できる検査ではありません" in action


def test_validated_candidate_does_not_claim_live():
    assert "使用中とは言えません" in next_action({"bucket": "配備可"})[1]


def test_unknown_does_not_invent_work_in_progress():
    html = overview([], None, None)
    assert "実行状況を確認できません" in html
    assert "未確認" in html


def test_counts_and_escape():
    html = overview([{"bucket":"配備条件の確認待ち"},{"bucket":"不採用"}],
                    {"tag":"<script>","label":"<img>"}, 15)
    assert "検証待ち 1 タグ" in html
    assert "不要 1 タグ" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<img>" not in html


def test_rejection_does_not_suggest_waiting_will_publish():
    assert "待つだけでは" in next_action({"bucket":"不採用"})[1]


def test_no_forced_labels_for_ambiguous_images():
    assert "無理に確定" in next_action({})[1]


def test_unnecessary_does_not_promise_training():
    assert "追加の学習や承認をする必要はありません" in next_action({"bucket":"不要"})[1]


def test_partial_input_is_unknown_and_other_states_are_counted():
    text = overview([None, {"bucket":"判定待ち"}], {"label":None}, None)
    assert "実行状況を確認できません" in text
    assert "その他の判定 2 タグ" in text
