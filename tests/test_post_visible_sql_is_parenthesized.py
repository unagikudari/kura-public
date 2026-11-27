from api.services.pool_visibility import post_visible_sql


def test_post_visible_sql_is_one_parenthesized_term():
    """Spliced after AND, the OR inside must not escape the caller's filter."""
    sql = post_visible_sql("p.id")
    assert sql.startswith("(") and sql.endswith(")")
    depth = 0
    for i, ch in enumerate(sql):
        depth += ch == "("
        depth -= ch == ")"
        if depth == 0:
            assert i == len(sql) - 1, "the OR clause must stay inside the outer parentheses"
