import json
import threading
from compression import zstd

import pytest

from runlog.eventlog import EventLogReader, EventLogWriter, LogClosed, LogCorrupt, load_index
from runlog.events import EventRejected, canonical
from runlog.store import LocalObjectStore


def items(n: int, start: int = 0):  # type: ignore[no-untyped-def]
    return [("obs", start + i, {"i": start + i}) for i in range(n)]


def test_append_assigns_gapless_seq_and_server_timestamps(store, run_id, clock) -> None:  # type: ignore[no-untyped-def]
    w = EventLogWriter(store, run_id, clock=clock, segment_size=100)
    a, _ = w.append(items(3))
    b, _ = w.append(items(2, 3))
    assert [e.seq for e in a + b] == [1, 2, 3, 4, 5] and w.next_seq == 6
    assert all(e.ts.endswith("Z") and e.run_id == run_id for e in a + b)
    assert [e.data["i"] for e in EventLogReader(store, run_id).iter_events()] == [0, 1, 2, 3, 4]


def test_segments_seal_every_n_events_with_checksums(store, run_id, clock) -> None:  # type: ignore[no-untyped-def]
    w = EventLogWriter(store, run_id, clock=clock, segment_size=3)
    w.append(items(7))
    keys = sorted(k for k in store.list(f"runs/{run_id}/events"))
    assert [k.rsplit("/", 1)[1] for k in keys] == ["000001.jsonl.zst", "000002.jsonl.zst", "000003.jsonl", "segments.json"]
    idx = load_index(store, run_id)
    assert [(s["first_seq"], s["last_seq"], s["count"]) for s in idx["segments"]] == [(1, 3, 3), (4, 6, 3)]
    assert len(zstd.decompress(store.get(keys[0])).splitlines()) == 3
    assert [e.seq for e in EventLogReader(store, run_id).iter_events()] == list(range(1, 8))


def test_index_offsets_point_at_the_event_lines(store, run_id, clock) -> None:  # type: ignore[no-untyped-def]
    w = EventLogWriter(store, run_id, clock=clock, segment_size=100)
    events, index = w.append(items(5))
    raw = store.get(f"runs/{run_id}/events/000001.jsonl")
    for e, ix in zip(events, index, strict=True):
        line = raw[ix.offset:].split(b"\n", 1)[0].decode()
        assert json.loads(line)["seq"] == e.seq == ix.seq and ix.segment == 1 and ix.type == "obs"


def test_close_seals_the_partial_tail_and_refuses_more(store, run_id, clock) -> None:  # type: ignore[no-untyped-def]
    w = EventLogWriter(store, run_id, clock=clock, segment_size=1000)
    w.append(items(4))
    w.close()
    w.close()
    assert load_index(store, run_id)["closed"] is True and not any(k.endswith(".jsonl") for k in store.list(f"runs/{run_id}/events"))
    with pytest.raises(LogClosed):
        w.append(items(1))
    with pytest.raises(LogClosed):
        EventLogWriter(store, run_id).append(items(1))
    assert len(list(EventLogReader(store, run_id).iter_events())) == 4


def test_invalid_events_are_rejected_before_anything_is_written(store, run_id, clock) -> None:  # type: ignore[no-untyped-def]
    w = EventLogWriter(store, run_id, clock=clock)
    with pytest.raises(EventRejected):
        w.append([("obs", 0, {}), ("bogus", 0, {})])
    assert w.next_seq == 1 and store.list(f"runs/{run_id}/events") == []


def test_reader_pages(store, run_id, clock) -> None:  # type: ignore[no-untyped-def]
    EventLogWriter(store, run_id, clock=clock, segment_size=4).append(items(10))
    r = EventLogReader(store, run_id)
    assert [e.seq for e in r.read_page(0, 3)] == [1, 2, 3]
    assert [e.seq for e in r.read_page(3, 5)] == [4, 5, 6, 7, 8]
    assert [e.seq for e in r.read_page(9, 1000)] == [10] and r.read_page(10) == []
    for bad in ((-1, 5), (0, 0), (0, 1001)):
        with pytest.raises(ValueError):
            r.read_page(*bad)


def test_resume_after_a_restart_continues_the_sequence(store, run_id, clock) -> None:  # type: ignore[no-untyped-def]
    EventLogWriter(store, run_id, clock=clock, segment_size=3).append(items(5))
    w2 = EventLogWriter(store, run_id, clock=clock, segment_size=3)
    assert w2.next_seq == 6
    w2.append(items(3, 5))
    assert [e.seq for e in EventLogReader(store, run_id).iter_events()] == list(range(1, 9))


def test_a_full_tail_left_by_a_crash_is_sealed_on_resume(store, run_id, clock) -> None:  # type: ignore[no-untyped-def]
    big = EventLogWriter(store, run_id, clock=clock, segment_size=1000)
    big.append(items(3))                                                          # 3 events, tail never sealed
    tail = store.get(f"runs/{run_id}/events/000001.jsonl")
    store.put(f"runs/{run_id}/events/000001.jsonl.zst", zstd.compress(tail))      # crash after writing the sealed copy
    w = EventLogWriter(store, run_id, clock=clock, segment_size=3)
    assert w.next_seq == 4 and [s["count"] for s in load_index(store, run_id)["segments"]] == [3]
    assert not store.exists(f"runs/{run_id}/events/000001.jsonl")                 # tail removed
    w.append(items(1, 3))
    assert [e.seq for e in EventLogReader(store, run_id).iter_events()] == [1, 2, 3, 4]


def test_a_sealed_copy_that_disagrees_with_its_tail_is_corruption(store, run_id, clock) -> None:  # type: ignore[no-untyped-def]
    big = EventLogWriter(store, run_id, clock=clock, segment_size=1000)
    big.append(items(3))
    store.put(f"runs/{run_id}/events/000001.jsonl.zst", zstd.compress(b"something else\n"))
    with pytest.raises(LogCorrupt, match="differs"):
        EventLogWriter(store, run_id, clock=clock, segment_size=3)


# ---- tamper and corruption detection -------------------------------------------------------------
def build(store, run_id, clock, n=7):  # type: ignore[no-untyped-def]
    w = EventLogWriter(store, run_id, clock=clock, segment_size=3)
    w.append(items(n))
    return w


def test_modified_sealed_segment_fails_its_checksum(store, run_id, clock) -> None:  # type: ignore[no-untyped-def]
    build(store, run_id, clock)
    key = f"runs/{run_id}/events/000001.jsonl.zst"
    forged = zstd.compress(zstd.decompress(store.get(key)).replace(b'"i":0', b'"i":9'))
    store.put(key, forged, overwrite=True)
    with pytest.raises(LogCorrupt, match="checksum"):
        list(EventLogReader(store, run_id).iter_events())


def test_missing_and_reordered_segments_are_detected(store, run_id, clock) -> None:  # type: ignore[no-untyped-def]
    build(store, run_id, clock)
    idx_key = f"runs/{run_id}/events/segments.json"
    idx = json.loads(store.get(idx_key))
    idx["segments"].reverse()
    store.put(idx_key, canonical(idx).encode(), overwrite=True)
    with pytest.raises(LogCorrupt):
        list(EventLogReader(store, run_id).iter_events())


def test_dropped_tail_line_makes_a_gap(store, run_id, clock) -> None:  # type: ignore[no-untyped-def]
    build(store, run_id, clock, n=8)                       # tail holds seq 7,8
    tail = f"runs/{run_id}/events/000003.jsonl"
    lines = store.get(tail).splitlines(keepends=True)
    store.put(tail, lines[1], overwrite=True)               # leaves only seq 8 after seq 6
    with pytest.raises(LogCorrupt, match="gap"):
        list(EventLogReader(store, run_id).iter_events())


def test_another_runs_events_are_refused(store, run_id, clock) -> None:  # type: ignore[no-untyped-def]
    import uuid
    other = str(uuid.uuid4())
    EventLogWriter(store, other, clock=clock).append(items(2))
    EventLogWriter(store, run_id, clock=clock).append(items(2))
    store.put(f"runs/{run_id}/events/000001.jsonl", store.get(f"runs/{other}/events/000001.jsonl"), overwrite=True)
    with pytest.raises(LogCorrupt):
        list(EventLogReader(store, run_id).iter_events())


def test_garbage_in_the_index_or_tail_is_corruption_not_a_crash(store, run_id, clock) -> None:  # type: ignore[no-untyped-def]
    store.put(f"runs/{run_id}/events/segments.json", b"not json")
    with pytest.raises(LogCorrupt):
        load_index(store, run_id)
    store.put(f"runs/{run_id}/events/segments.json", b'{"run_id":"x","segments":[]}', overwrite=True)
    with pytest.raises(LogCorrupt):
        load_index(store, run_id)


def test_decompression_bombs_are_refused(store, run_id, clock) -> None:  # type: ignore[no-untyped-def]
    import hashlib

    import runlog.eventlog as el
    build(store, run_id, clock, n=3)
    key = f"runs/{run_id}/events/000001.jsonl.zst"
    bomb = zstd.compress(b"A" * (el.MAX_SEGMENT_BYTES + 1024))
    store.put(key, bomb, overwrite=True)
    idx_key = f"runs/{run_id}/events/segments.json"
    idx = json.loads(store.get(idx_key))
    idx["segments"][0]["sha256"] = hashlib.sha256(bomb).hexdigest()     # even with a matching checksum
    store.put(idx_key, canonical(idx).encode(), overwrite=True)
    with pytest.raises(LogCorrupt):
        list(EventLogReader(store, run_id).iter_events())


def test_bad_run_id_is_refused(store) -> None:  # type: ignore[no-untyped-def]
    for bad in ("", "../x", "not-a-uuid", "A" * 36):
        with pytest.raises(ValueError):
            EventLogWriter(store, bad)


def test_concurrent_appends_keep_the_sequence_gapless(tmp_path, run_id) -> None:  # type: ignore[no-untyped-def]
    s = LocalObjectStore(tmp_path / "o")
    w = EventLogWriter(s, run_id, segment_size=50)

    def worker(n: int) -> None:
        for i in range(40):
            w.append([("obs", i, {"w": n})])

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    seqs = [e.seq for e in EventLogReader(s, run_id).iter_events()]
    assert seqs == list(range(1, 241))
