"""Queue semantics of the SQLite store: retries, --limit accounting inputs, and portal 'skipped' villages."""
from bhulekh.store import Store

AM = "Amroha (अमरोहा)"


def _store(tmp_path) -> Store:
    s = Store(str(tmp_path / "t.sqlite"))
    s.upsert_districts([AM])
    s.upsert_tehsils(AM, [AM])
    s.upsert_villages(AM, AM, ["A (अ) - 100001", "B (ब) - 100002", "C (स) - 100003"])
    return s


def test_error_is_retried_until_attempts_exhausted(tmp_path):
    s = _store(tmp_path)
    for _ in range(3):
        s.mark_started("100001")
    s.mark_error("100001", "boom")
    assert [v.code for v in s.next_pending([AM], 10, 3)] == ["100002", "100003"]
    s.reset_errors([AM])
    assert [v.code for v in s.next_pending([AM], 10, 3)] == ["100001", "100002", "100003"]


def test_skipped_village_leaves_queue_and_counts_as_covered(tmp_path):
    s = _store(tmp_path)
    s.mark_started("100002")
    s.mark_skipped("100002", "यह गाँव चकबंदी में है।")
    assert [v.code for v in s.next_pending([AM], 10, 3)] == ["100001", "100003"]
    t = s.totals()
    assert (t["done"], t["errors"], t["skipped"]) == (0, 0, 1)
    cov = {r["district"]: r for r in s.coverage()}[AM]
    assert (cov["skipped"], cov["pending"]) == (1, 2)
    # a skipped village is not resurrected by reset_errors
    s.reset_errors([AM])
    assert [v.code for v in s.next_pending([AM], 10, 3)] == ["100001", "100003"]


def test_done_village_is_never_requeued(tmp_path):
    s = _store(tmp_path)
    s.mark_started("100003")
    s.mark_done("100003")
    assert "100003" not in [v.code for v in s.next_pending([AM], 10, 3)]
