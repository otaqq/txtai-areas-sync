"""Knowledge graph extraction: ask an LLM for the entities and relations in each chunk that txtai-kura has not
extracted yet (/kg/todo), and send them back (/kg/upsert with done).

Resumable: a chunk counts as done on the server once its result is sent, so a run stopped by --minutes, --limit or
an error goes on where it left off. A chunk sent again by `run` (changed text) is to do again, and its new extraction
replaces the old one. Any OpenAI-compatible chat API works (OpenAI, Ollama's /v1, a LiteLLM proxy); with a proxy in
front of several machines, --parallel requests are spread over them.
"""
import concurrent.futures
import json
import re
import time
import urllib.error
import urllib.request

PROMPT = """You extract a knowledge graph from one passage of a document. Answer with JSON only, in this form:
{"entities": [{"name": "...", "type": "...", "description": "..."}],
 "relations": [{"source": "...", "target": "...", "type": "...", "description": "..."}]}
Rules:
- entities: the specific things the passage is about: software, tools, concepts, people, organizations, places,
  machines, works. Leave out generic words (system, data, method, example).
- name: the usual name of the thing as the passage writes it, in the passage's language; always the same name for the
  same thing; no articles.
- type: one lower-case English word: software, concept, person, organization, place, machine, document, event, other.
- description: one short sentence about the entity, from the passage, in the passage's language.
- relations: only between names in your entities list, only what the passage states or clearly implies; type: a short
  lower-case English verb phrase (uses, runs on, part of, depends on, written by, ...).
- At most 15 entities and 20 relations. If the passage names nothing specific: {"entities": [], "relations": []}."""

TYPES = {"software", "concept", "person", "organization", "place", "machine", "document", "event", "other"}
MAX_TEXT = 6000  # characters of a chunk sent to the model
RETRIES = (15, 60)  # seconds to wait before the 2nd and 3rd try when the model's machine refuses or fails (restarting)


def log(msg: str) -> None:
    print(f"{time.strftime('%F %T')} {msg}", flush=True)


class Llm:
    """An OpenAI-compatible chat completions API."""

    def __init__(self, api_base: str, model: str, api_key: str = "", timeout: int = 900):
        self.url = api_base.rstrip("/") + "/chat/completions"
        self.model, self.api_key, self.timeout = model, api_key, timeout

    def chat(self, system: str, user: str) -> str:
        body = {"model": self.model, "temperature": 0, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"] or ""


def parse(answer: str, chunk_id: str) -> dict:
    """The model's answer as /kg/upsert lists for one chunk. Raises ValueError when it is not the JSON asked for."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", answer.strip())
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    entities, names = [], {}
    for e in data.get("entities") or []:
        if not isinstance(e, dict) or not str(e.get("name") or "").strip():
            continue
        name = " ".join(str(e["name"]).split())[:200]
        if name.lower() in names:
            continue
        kind = str(e.get("type") or "other").strip().lower()
        names[name.lower()] = name
        entities.append({"name": name, "type": kind if kind in TYPES else "other",
                         "description": " ".join(str(e.get("description") or "").split())[:500]})
    relations = []
    for r in data.get("relations") or []:
        if not isinstance(r, dict):
            continue
        s, t = names.get(" ".join(str(r.get("source") or "").split()).lower()), names.get(" ".join(str(r.get("target") or "").split()).lower())
        if s and t and s != t:
            relations.append({"source": s, "target": t, "type": " ".join(str(r.get("type") or "related to").lower().split())[:80],
                              "description": " ".join(str(r.get("description") or "").split())[:500], "chunk": chunk_id})
    return {"entities": entities, "relations": relations,
            "mentions": [{"entity": e["name"], "chunk": chunk_id} for e in entities]}


def extract_one(llm: Llm, chunk: dict) -> tuple[dict, float]:
    """One chunk. A refused connection, a timeout or a 5xx is tried again (RETRIES); a 4xx or a bad answer is not."""
    for wait in (*RETRIES, None):
        t = time.time()
        try:
            answer = llm.chat(PROMPT, chunk["text"][:MAX_TEXT])
            return parse(answer, chunk["id"]), time.time() - t
        except urllib.error.HTTPError as e:
            if e.code < 500 or wait is None:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if wait is None:
                raise
        log(f"{chunk['id']}: model unreachable, trying again in {wait} s")
        time.sleep(wait)


def run(rag, llm: Llm, namespace: str, parallel: int = 1, minutes: float | None = None, limit: int | None = None,
        dry_run: bool = False) -> int:
    """Extracts until nothing is left, the time is up or limit chunks are done. Returns 0, or 1 when chunks failed."""
    deadline = time.time() + minutes * 60 if minutes else None
    failed: set[str] = set()
    done = entities = relations = 0
    seconds = []
    started = time.time()
    while (limit is None or done < limit) and (deadline is None or time.time() < deadline):
        if dry_run:  # nothing is marked done, so the same chunks would come back: one batch only
            want = limit or parallel * 2
        else:
            want = parallel * 2 if limit is None else min(parallel * 2, limit - done)
        todo = rag.call("POST", "/kg/todo", {"namespace": namespace, "limit": want + len(failed)})
        chunks = [c for c in todo["chunks"] if c["id"] not in failed][:want]
        if not chunks:
            break
        with concurrent.futures.ThreadPoolExecutor(parallel) as pool:
            futures = {pool.submit(extract_one, llm, c): c for c in chunks}
            for f in concurrent.futures.as_completed(futures):
                c = futures[f]
                try:
                    result, took = f.result()
                except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:
                    failed.add(c["id"])
                    log(f"{c['id']}: failed ({e!r:.200})")
                    continue
                seconds.append(took)
                done += 1
                entities += len(result["entities"])
                relations += len(result["relations"])
                if dry_run:
                    print(json.dumps({"chunk": c["id"], "seconds": round(took, 1), **result}, ensure_ascii=False), flush=True)
                else:
                    rag.call("POST", "/kg/upsert", {"namespace": namespace, **result, "done": [c["id"]]})
                    log(f"{c['id']}: {len(result['entities'])} entities, {len(result['relations'])} relations, {took:.1f} s")
        if dry_run:
            break
    left = rag.call("POST", "/kg/todo", {"namespace": namespace, "limit": 1})["remaining"]
    avg = sum(seconds) / len(seconds) if seconds else 0
    log(f"done: {done} chunks, {entities} entities, {relations} relations, {len(failed)} failed, "
        f"{avg:.1f} s per chunk, {time.time() - started:.0f} s in all, {left} left{' (dry run)' if dry_run else ''}")
    return 1 if failed else 0
