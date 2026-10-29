"""Library-label policy v1: normalized names and reproducible comparison evidence.

These labels record organizational intent on an asset, not inferred scene facts.
Semantic observations/resolution remain a separate bounded context.
"""
import unicodedata

POLICY_VERSION = "collector-labels-v1"
MAX_TARGETS = 100
MAX_CHANGES = 16
MAX_LABELS = 128


def normalize_label(value: str) -> tuple[str, str]:
    normalized = unicodedata.normalize("NFKC", value)
    if any(c in normalized for c in (",", "、", "\n", "\r", "\x00")):
        raise ValueError("タグ内には区切りのカンマや改行を含められません。別々のタグとして指定してください。")
    name = " ".join(normalized.split())
    if not name or len(name) > 100 or any(unicodedata.category(c).startswith("C") for c in name):
        raise ValueError("タグは1～100文字で入力してください。制御文字は使えません。")
    key = name.casefold()
    if len(key) > 100:
        raise ValueError("正規化後のタグは100文字以下にしてください。")
    return name, key


def changes(add: list[str], remove: list[str]) -> list[tuple[str, str, bool]]:
    if len(add) > MAX_CHANGES or len(remove) > MAX_CHANGES:
        raise ValueError("一度に変更できるタグは16個までです。")
    positive = {normalize_label(value)[1]: normalize_label(value)[0] for value in add}
    negative = {normalize_label(value)[1]: normalize_label(value)[0] for value in remove}
    if positive.keys() & negative.keys():
        raise ValueError("同じタグを付ける操作と外す操作は同時に指定できません。")
    result = [(name, key, True) for key, name in positive.items()] + [(name, key, False) for key, name in negative.items()]
    if not 1 <= len(result) <= MAX_CHANGES:
        raise ValueError("一度に変更できるタグは1～16個です。")
    return sorted(result, key=lambda item: item[1])


def fold_observations(events):
    """Reference rebuild: latest (observed_at,id) comparison wins per asset/tag.

    The write service serializes asset edits and makes timestamps nondecreasing.
    Both positive and negative winners survive, so prior-state comparison and
    provenance can be rebuilt without treating absent labels as missing evidence.
    """
    winners = {}
    for event in events:
        key = event["asset_id"], event["tag_id"]
        current = winners.get(key)
        if current is None or (event["observed_at"], event["id"]) > (current["observed_at"], current["id"]):
            winners[key] = event
    return winners
