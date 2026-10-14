import io
import json

from llmgw.events import JsonLinesSink, MemorySink, NullSink


def test_json_lines_sink_writes_one_parseable_object_per_event():
    stream = io.StringIO()
    sink = JsonLinesSink(stream)
    sink.emit("llm.completed", 12.3456789, request_id="r1", tokens=250)
    sink.emit("llm.rejected", 13.0, request_id="r2", reason="queue_full")

    first, second = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert first == {"event": "llm.completed", "ts": 12.345679, "request_id": "r1", "tokens": 250}
    assert second["event"] == "llm.rejected"
    assert second["reason"] == "queue_full"


def test_json_lines_sink_stringifies_values_json_cannot_encode():
    stream = io.StringIO()
    JsonLinesSink(stream).emit("e", 0.0, thing=object)
    assert json.loads(stream.getvalue())["thing"].startswith("<class")


def test_memory_sink_keeps_events_and_filters_by_name():
    sink = MemorySink()
    sink.emit("a", 1.0, x=1)
    sink.emit("b", 2.0)
    sink.emit("a", 3.0, x=2)
    assert [e.fields["x"] for e in sink.named("a")] == [1, 2]
    assert [e.at for e in sink.events] == [1.0, 2.0, 3.0]


def test_null_sink_discards_events():
    NullSink().emit("anything", 0.0, x=1)
