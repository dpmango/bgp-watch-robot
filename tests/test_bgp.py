from bgpwatch import bgp


def test_parse_network_info_ok():
    payload = {"data": {"prefix": "185.157.212.0/24", "asns": ["216071"]}}
    prefix, asn = bgp.parse_network_info(payload)
    assert prefix == "185.157.212.0/24"
    assert asn == 216071


def test_parse_network_info_empty():
    prefix, asn = bgp.parse_network_info({"data": {}})
    assert prefix is None and asn is None


def test_parse_routing_status_up():
    payload = {"data": {"visibility": {"v4": {"ris_peers_seeing": 326, "total_ris_peers": 326}}}}
    vis = bgp.parse_routing_status(payload)
    assert vis.seeing == 326 and vis.total == 326
    assert vis.ratio == 1.0 and vis.pct == 100


def test_parse_routing_status_down():
    payload = {"data": {"visibility": {"v4": {"ris_peers_seeing": 1, "total_ris_peers": 326}}}}
    vis = bgp.parse_routing_status(payload)
    assert vis.seeing == 1 and vis.pct == 0


def test_parse_routing_status_missing():
    assert bgp.parse_routing_status({"data": {}}) is None


def test_fallback_prefix_v4():
    assert bgp.fallback_prefix("1.1.1.1") == "1.1.1.0/24"


def test_fallback_prefix_v6():
    assert bgp.fallback_prefix("2001:db8::1").endswith("/64")
