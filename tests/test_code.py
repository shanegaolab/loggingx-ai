from loggingx_ai import code
from loggingx_ai.stacktrace import parse
from tests.test_stacktrace import JAVA


def test_locate_prefers_longest_tail_then_shortest_path():
    paths = ["a/src/main/java/com/shop/order/OrderService.java", "legacy/OrderService.java"]
    assert code.locate(paths, "com/shop/order/OrderService.java") == paths[0]
    assert code.locate(paths, "/app/other/OrderService.java") == "legacy/OrderService.java"
    assert code.locate(paths, "missing.py") is None


def test_snippets_mark_frame_line(tmp_path):
    source = tmp_path / "store-7" / "src/main/java/com/shop/order/OrderService.java"
    source.parent.mkdir(parents=True)
    source.write_text("\n".join(f"line {n}" for n in range(1, 101)), encoding="utf-8")
    (tmp_path / "store-7" / "node_modules").mkdir()

    repo = code.store_repo(tmp_path, 7)
    refs = code.snippets(repo, parse(JAVA).app_frames(), max_refs=5, context=5, max_chars=10_000)

    assert len(refs) == 1
    assert (refs[0].start_line, refs[0].focus_line) == (83, 88)
    assert ">   88 | line 88" in code.render(refs)
    assert code.store_repo(tmp_path, 8) is None
