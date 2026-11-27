"""Immutable v1 Unicode contract for parser and migration replay. Add a v2 to change it."""
from bisect import bisect_right
import hashlib
import json
from pathlib import Path

EXPECTED_SHA256 = "9a15ab2e05f50f79353cd698962b673047b9628d4ab8b7614addcb6678a42be8"
_raw=(Path(__file__).resolve().parents[1]/"contracts/review_tag_codepoints_v1.json").read_bytes()
if hashlib.sha256(_raw).hexdigest()!=EXPECTED_SHA256:
    raise RuntimeError("review_tag_contract_changed")
_document=json.loads(_raw)
RANGES=tuple(tuple(pair) for pair in _document["ranges"])
_STARTS=tuple(a for a,b in RANGES)

def valid(value):
    if not isinstance(value,str) or not 0<len(value)<=200:
        return False
    for char in value:
        point=ord(char)
        index=bisect_right(_STARTS,point)-1
        if index<0 or point>=RANGES[index][1]:
            return False
    return True

def sql_function():
    literal="{"+",".join("["+str(a)+","+str(b)+")" for a,b in RANGES)+"}"
    return """
    CREATE FUNCTION public.tagger_review_tag_valid(value text) RETURNS boolean
    LANGUAGE sql IMMUTABLE PARALLEL SAFE SET search_path=pg_catalog AS $$
      SELECT value IS NOT NULL AND char_length(value) BETWEEN 1 AND 200 AND NOT EXISTS(
        SELECT FROM generate_series(1,char_length(value)) n
        WHERE NOT ('%s'::int4multirange @> ascii(substr(value,n,1))))
    $$""" % literal
