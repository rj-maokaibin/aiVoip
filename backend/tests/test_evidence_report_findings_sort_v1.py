from app.reports.finding_composer import sort_findings

def test_findings_sort_high_before_medium_before_info():
    rows=[{"severity":"INFO","evidence_level":"L3","finding_signature":"i","time_range":{}},{"severity":"HIGH","evidence_level":"L3","finding_signature":"h","time_range":{}},{"severity":"MEDIUM","evidence_level":"L3","finding_signature":"m","time_range":{}}]
    assert [x["severity"] for x in sort_findings(rows)]==["HIGH","MEDIUM","INFO"]
