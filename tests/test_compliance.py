from compliance import compliance_scan


def test_compliance_clean():
    ok, hits = compliance_scan("Nifty structure shows elevated call OI near 22000.")
    assert ok is True
    assert hits == []


def test_compliance_fails():
    ok, hits = compliance_scan("Buy the dip here.")
    assert ok is False
    assert hits
