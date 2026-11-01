"""Knowledge graph extraction with a stand-in model and server."""
import json
import unittest

from txtai_kura_sync import extract


class FakeLlm:
    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def chat(self, system, user):
        self.asked.append(user)
        a = self.answers.get(user, '{"entities": [], "relations": []}')
        if isinstance(a, Exception):
            raise a
        return a


class FakeRag:
    """/kg/todo and /kg/upsert with done, as txtai-kura does them."""

    def __init__(self, chunks):
        self.chunks, self.done, self.upserts = chunks, set(), []

    def call(self, method, path, body=None):
        if path == "/kg/todo":
            left = [c for c in self.chunks if c["id"] not in self.done]
            return {"chunks": left[: body["limit"]], "remaining": len(left)}
        if path == "/kg/upsert":
            self.upserts.append(body)
            self.done.update(body["done"])
            return {}
        raise AssertionError(path)


ANSWER = json.dumps({"entities": [{"name": "Kubernetes", "type": "Software", "description": "コンテナを動かす"},
                                  {"name": " Pod ", "type": "concept"}, {"name": "pod", "type": "concept"}, {"name": ""}],
                     "relations": [{"source": "kubernetes", "target": "Pod", "type": "Runs"},
                                   {"source": "Pod", "target": "Nowhere", "type": "x"}]}, ensure_ascii=False)


class ParseTest(unittest.TestCase):
    def test_clean_and_link(self):
        r = extract.parse("```json\n" + ANSWER + "\n```", "d:a.md#S#0")
        self.assertEqual([e["name"] for e in r["entities"]], ["Kubernetes", "Pod"])  # duplicates and empty names go
        self.assertEqual(r["entities"][0]["type"], "software")
        self.assertEqual(r["relations"], [{"source": "Kubernetes", "target": "Pod", "type": "runs", "description": "",
                                           "chunk": "d:a.md#S#0"}])  # unknown target dropped, names matched by case
        self.assertEqual(r["mentions"], [{"entity": "Kubernetes", "chunk": "d:a.md#S#0"},
                                         {"entity": "Pod", "chunk": "d:a.md#S#0"}])

    def test_not_json(self):
        with self.assertRaises(ValueError):
            extract.parse("Sure! Here are the entities", "x")


class RetryTest(unittest.TestCase):
    def test_refused_then_answers(self):
        import urllib.error
        from unittest import mock

        class Flaky(FakeLlm):
            def chat(self, system, user):
                self.asked.append(user)
                if len(self.asked) < 3:
                    raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
                return ANSWER

        llm = Flaky({})
        with mock.patch.object(extract.time, "sleep") as sleep:
            result, _ = extract.extract_one(llm, {"id": "x", "text": "t"})
        self.assertEqual((len(llm.asked), sleep.call_count, len(result["entities"])), (3, 2, 2))

    def test_gives_up_after_three(self):
        import urllib.error
        from unittest import mock
        llm = FakeLlm({"t": urllib.error.URLError("down")})
        with mock.patch.object(extract.time, "sleep"), self.assertRaises(urllib.error.URLError):
            extract.extract_one(llm, {"id": "x", "text": "t"})
        self.assertEqual(len(llm.asked), 3)


class RunTest(unittest.TestCase):
    def chunks(self, n):
        return [{"id": f"d:a.md#S#{i}", "text": f"text {i}", "source": "d"} for i in range(n)]

    def test_extracts_and_marks_done(self):
        rag = FakeRag(self.chunks(5))
        llm = FakeLlm({"text 0": ANSWER, "text 3": ValueError("bad answer")})
        self.assertEqual(extract.run(rag, llm, "it", parallel=2), 1)  # one chunk failed
        self.assertEqual(rag.done, {"d:a.md#S#0", "d:a.md#S#1", "d:a.md#S#2", "d:a.md#S#4"})
        first = next(u for u in rag.upserts if u["done"] == ["d:a.md#S#0"])
        self.assertEqual(len(first["relations"]), 1)
        self.assertEqual(llm.asked.count("text 3"), 1)  # a failed chunk is not asked again in the same run

    def test_limit_and_resume(self):
        rag = FakeRag(self.chunks(5))
        self.assertEqual(extract.run(rag, FakeLlm({}), "it", parallel=2, limit=3), 0)
        self.assertEqual(len(rag.done), 3)
        extract.run(rag, FakeLlm({}), "it", parallel=2)
        self.assertEqual(len(rag.done), 5)

    def test_dry_run_sends_nothing(self):
        rag = FakeRag(self.chunks(4))
        llm = FakeLlm({"text 0": ANSWER})
        self.assertEqual(extract.run(rag, llm, "it", parallel=2, limit=3, dry_run=True), 0)
        self.assertEqual((rag.upserts, len(llm.asked)), ([], 3))


if __name__ == "__main__":
    unittest.main()
