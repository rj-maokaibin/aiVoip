from app.reports.evidence_brief import canonical_hash

def test_canonical_hash_is_order_independent_for_objects():
    assert canonical_hash({"b":2,"a":1})==canonical_hash({"a":1,"b":2})
