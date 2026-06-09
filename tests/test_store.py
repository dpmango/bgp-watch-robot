from bgpwatch.store import Store


def test_watch_crud(tmp_path):
    db = Store(str(tmp_path / "t.db"))
    db.add_watch(111, "1.2.3.4", 22, "1.2.3.0/24", 65000)
    db.add_watch(222, "1.2.3.4", 22, "1.2.3.0/24", 65000)  # другой подписчик
    db.add_watch(111, "9.9.9.9", 443, "9.9.9.0/24", 19281)

    assert len(db.list_watches(111)) == 2
    assert db.count_watches(111) == 2
    assert db.count_watches(222) == 1
    assert sorted(db.chats_for("1.2.3.4", 22)) == [111, 222]

    targets = db.distinct_targets()
    keys = {(t["ip"], t["port"]) for t in targets}
    assert keys == {("1.2.3.4", 22), ("9.9.9.9", 443)}

    assert db.remove_watch(111, "9.9.9.9") == 1
    assert len(db.list_watches(111)) == 1
    db.close()


def test_state_change_tracking(tmp_path):
    db = Store(str(tmp_path / "s.db"))
    db.upsert_state("1.2.3.4", 22, "1.2.3.0/24", 1, "open", 326, 326, True, 1000)
    st = db.get_state("1.2.3.4", 22)
    assert st["tcp"] == "open" and st["last_change"] == 1000

    # без изменения last_change держится, last_check обновляется
    db.upsert_state("1.2.3.4", 22, "1.2.3.0/24", 1, "open", 326, 326, False, 2000)
    st = db.get_state("1.2.3.4", 22)
    assert st["last_change"] == 1000 and st["last_check"] == 2000

    # изменение — last_change прыгает
    db.upsert_state("1.2.3.4", 22, "1.2.3.0/24", 1, "closed", 1, 326, True, 3000)
    st = db.get_state("1.2.3.4", 22)
    assert st["tcp"] == "closed" and st["last_change"] == 3000
    db.close()
