from bgpwatch import monitor
from bgpwatch.bgp import Visibility


def test_classify_bgp():
    assert monitor.classify_bgp(Visibility(326, 326)) == "up"
    assert monitor.classify_bgp(Visibility(1, 326)) == "down"
    assert monitor.classify_bgp(None) == "unknown"


def test_first_run_no_events():
    assert monitor.compute_events(None, "closed", "down") == []


def test_tcp_transition():
    old = {"tcp": "open", "bgp_seeing": 326, "bgp_total": 326}
    events = monitor.compute_events(old, "closed", "up")
    assert events == [{"kind": "tcp", "from": "open", "to": "closed"}]


def test_tcp_no_change():
    old = {"tcp": "open", "bgp_seeing": 326, "bgp_total": 326}
    assert monitor.compute_events(old, "open", "up") == []


def test_bgp_transition():
    old = {"tcp": "open", "bgp_seeing": 326, "bgp_total": 326}
    events = monitor.compute_events(old, "open", "down")
    assert events == [{"kind": "bgp", "from": "up", "to": "down"}]


def test_bgp_unknown_no_event():
    old = {"tcp": "open", "bgp_seeing": 326, "bgp_total": 326}
    assert monitor.compute_events(old, "open", "unknown") == []


def test_old_bgp_unknown_no_event():
    old = {"tcp": "open", "bgp_seeing": None, "bgp_total": None}
    assert monitor.compute_events(old, "open", "down") == []


def test_both_transitions():
    old = {"tcp": "open", "bgp_seeing": 326, "bgp_total": 326}
    events = monitor.compute_events(old, "closed", "down")
    kinds = {e["kind"] for e in events}
    assert kinds == {"tcp", "bgp"}
