"""txtai-kura-sync: keep a txtai-kura search server in step with the documents it indexes.

Reads the sources in sources.toml (git repositories, folders, shelves of books), splits each file into parts
(Markdown: one "## " section each; PDF: one page each, text by pdftotext from poppler; EPUB: one chapter each),
compares each part with the last run (sha256), and sends only what changed: a changed part's chunks are deleted and
sent again, a removed part's chunks are deleted. Each source goes to its namespace; a shelf source maps namespaces to any
number of folders; a shelves source takes every folder directly under its path as one namespace of the same name.
Extracted PDF/EPUB text is cached until the file's size and time change.
Parts are cut into chunks of at most CHUNK characters, each starting with "title › section" so a chunk still tells
what it belongs to. Only one run at a time (lock file).

Usage:
  txtai-kura-sync run [--source NAME] [--kind KIND] [--namespace NAMESPACE] [--due] [--dry-run]
  txtai-kura-sync status
Options before the command: --config sources.toml (default), --data DIR (state and cache).
"""
import argparse
import base64
import fcntl
import fnmatch
import hashlib
import html.parser
import json
import os
import re
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

CHUNK = 1500  # characters; Japanese is about 1-1.5 characters per token
NAMESPACE = re.compile(r"^[a-z][a-z0-9-]*(\.[a-z0-9][a-z0-9-]*)*$")  # the same as txtai-kura' namespace names
MIN_PAGE_CHARS = 50  # a PDF page or EPUB chapter with fewer letters (spaces not counted) is left out: scanned pages
#   without a text layer give only page numbers or nothing, which would fill the index with empty chunks
BATCH = 200  # chunks per request: txtai-kura saves the whole index once per request, so fewer, bigger requests are faster

# Words put into the documents: [text] in sources.toml. Changing them changes every part, so everything is sent again
TEXT = {"intro": "(intro)", "source": "Source: {where}"}

# State and cache (configure() sets them from --data)
STATE_DIR = CACHE_DIR = STATE_FILE = FILES_FILE = RUNS_FILE = LOCK_FILE = Path()


def configure(data: Path) -> None:
    """Where state and cache live. state.json: part -> {sha, namespace, chunks}. files.json: "<source>:<file>" ->
    [size, mtime_ns, namespace] when it was last sent, so a file still the same is skipped without being opened (delete it
    to read everything again). runs.json: source -> start time of its last run without errors (for --due)."""
    global STATE_DIR, CACHE_DIR, STATE_FILE, FILES_FILE, RUNS_FILE, LOCK_FILE
    STATE_DIR, CACHE_DIR = data / "state", data / "cache"
    STATE_FILE, FILES_FILE, RUNS_FILE = STATE_DIR / "state.json", STATE_DIR / "files.json", STATE_DIR / "runs.json"
    LOCK_FILE = STATE_DIR / "lock"


def default_data() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "txtai-kura-sync"


def load_env(path: Path) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    return env


def log(msg: str) -> None:
    print(f"{time.strftime('%F %T')} {msg}", flush=True)


class Rag:
    """The txtai-kura API: /upsert, /delete (by id prefixes), /namespaces."""

    def __init__(self, url: str, api_key: str):
        self.url = url.rstrip("/")
        self.api_key = api_key

    def call(self, method: str, path: str, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"X-API-Key": self.api_key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read() or b"null")

    def upsert(self, namespace: str, docs: list) -> None:
        for i in range(0, len(docs), BATCH):
            self.call("POST", "/upsert", {"namespace": namespace, "documents": docs[i:i + BATCH]})

    def delete(self, namespace: str, prefixes: list) -> int:
        return self.call("POST", "/delete", {"namespace": namespace, "prefixes": prefixes}).get("deleted", 0) if prefixes else 0


def git_auth(src: dict, env: dict) -> list:
    """git -c options for a token read from the env var named by token_env: "token" (Forgejo, Gitea), "bearer", or
    "basic" (GitHub: user x-access-token)."""
    token = env.get(src.get("token_env", ""), "") if src.get("token_env") else ""
    if not token:
        return []
    kind = src.get("token_auth", "token")
    value = {"token": f"token {token}", "bearer": f"Bearer {token}",
             "basic": "Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()}[kind]
    return ["-c", f"http.extraHeader=Authorization: {value}"]


def fetch_git(src: dict, env: dict) -> Path:
    """Clone or update the repository (shallow) into cache/git/<name> and return the work tree."""
    dest = CACHE_DIR / "git" / src["name"]
    auth = git_auth(src, env)
    branch = src.get("branch", "main")
    if (dest / ".git").exists():
        subprocess.run(["git", *auth, "-C", str(dest), "fetch", "-q", "--depth", "1", "origin", branch], check=True)
        subprocess.run(["git", "-C", str(dest), "reset", "-q", "--hard", f"origin/{branch}"], check=True)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", *auth, "clone", "-q", "--depth", "1", "--branch", branch, src["url"], str(dest)], check=True)
    return dest


def cached(path: Path, read) -> str | None:
    """read(path), kept in cache/text (keyed by the absolute path) until the file's size or time changes; None when it
    failed (tried again next run)."""
    st = path.stat()
    cache = CACHE_DIR / "text" / (hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:32] + ".json")
    if cache.exists():
        c = json.loads(cache.read_text())
        if c["size"] == st.st_size and c["mtime"] == st.st_mtime:
            return c["text"]
    text = read(path)
    if text is None:  # failed: try again next time
        return None
    pages = text.split("\f")
    short = sum(1 for t in pages if readable(t) is None)
    if short == len(pages):
        log(f"{path}: no text (scanned pages need OCR), skipped")
    elif short:
        log(f"{path}: {short} of {len(pages)} page(s) without text (fewer than {MIN_PAGE_CHARS} letters), skipped")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"size": st.st_size, "mtime": st.st_mtime, "text": text}, ensure_ascii=False))
    return text


_warned_pdftotext = False


def pdf_text(path: Path) -> str | None:
    """The text of a PDF by pdftotext (poppler), pages separated by form feeds. Without pdftotext, None (one warning)."""
    global _warned_pdftotext
    try:
        r = subprocess.run(["pdftotext", "-enc", "UTF-8", str(path), "-"], capture_output=True, text=True, errors="replace")
    except FileNotFoundError:
        if not _warned_pdftotext:
            log("pdftotext (poppler) is not installed: PDFs are skipped")
            _warned_pdftotext = True
        return None
    if r.returncode != 0:
        log(f"{path}: pdftotext failed: {r.stderr.strip()[:200]}")
        return None
    return r.stdout


class _Text(html.parser.HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "blockquote", "pre"}

    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self.skip -= 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def epub_text(path: Path) -> str | None:
    """The text of an EPUB, chapters (the spine's items, in reading order) separated by form feeds."""
    try:
        with zipfile.ZipFile(path) as z:
            container = z.read("META-INF/container.xml").decode()
            opf_path = container.split('full-path="', 1)[1].split('"', 1)[0]
            opf = z.read(opf_path).decode(errors="replace")
            base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
            items = {}
            for tag in opf.split("<item ")[1:]:
                attr = lambda k: tag.split(f'{k}="', 1)[1].split('"', 1)[0] if f'{k}="' in tag else ""
                items[attr("id")] = attr("href")
            spine = [t.split('idref="', 1)[1].split('"', 1)[0] for t in opf.split("<itemref ")[1:] if 'idref="' in t]
            chapters = []
            for idref in spine:
                href = urllib.request.url2pathname(items.get(idref, "")).replace("\\", "/")
                if not href:
                    continue
                t = _Text()
                t.feed(z.read(base + href).decode(errors="replace"))
                text = "\n".join(l.strip() for l in "".join(t.out).splitlines() if l.strip())
                if text:
                    chapters.append(text)
            return "\f".join(chapters)
    except (zipfile.BadZipFile, KeyError, IndexError) as e:
        log(f"{path}: not a readable EPUB: {e!r}")
        return None


def matches(rel: str, patterns: list) -> bool:
    """fnmatch, where a leading "**/" may also match nothing ("**/*.pdf" matches "a.pdf" as well as "x/a.pdf")."""
    return any(fnmatch.fnmatch(rel, p) or (p.startswith("**/") and fnmatch.fnmatch(rel, p[3:])) for p in patterns)


def collect(root: Path, include: list, exclude: list) -> dict:
    """Return {relative path: path} for files matching include and not exclude. The text is read later, one file at
    a time (read_text), so a big library is never held in memory at once."""
    files = {}
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.relative_to(root).parts:
            continue
        rel = path.relative_to(root).as_posix()
        if matches(rel, include) and not matches(rel, exclude):
            files[rel] = path
    return files


def read_text(path: Path) -> str | None:
    """The text of a file (PDF and EPUB: extracted, cached); None when it could not be read."""
    low = path.name.lower()
    return (cached(path, pdf_text) if low.endswith(".pdf") else cached(path, epub_text) if low.endswith(".epub")
            else path.read_text(errors="replace"))


def source_line(label: str, rel: str, at: str = "") -> str:
    return TEXT["source"].format(where=" ".join(x for x in (where(label, rel), at) if x))


def split_markdown(text: str, label: str, rel: str) -> list:
    """Split a Markdown file into its "## " sections: [(section name, text)].
    Each part starts with the file's title and where it comes from, so a part still tells what it belongs to.
    The text before the first section is TEXT["intro"]. "## " inside code fences does not split.
    A part is named after its heading, so adding a section does not rename (and re-send) the others."""
    lines = text.splitlines()
    title = next((l[2:].strip() for l in lines if l.startswith("# ")), rel)
    intro = TEXT["intro"]
    parts, name, buf, fence = [], intro, [], False
    for line in lines:
        if line.lstrip().startswith("```"):
            fence = not fence
        if not fence and line.startswith("## "):
            parts.append((name, buf))
            name, buf = line[3:].strip(), []
        buf.append(line)
    parts.append((name, buf))
    out, used = [], {}
    for name, buf in parts:
        body = "\n".join(buf).strip()
        if not body or (name == intro and body.lstrip("# ").strip() == title):
            continue
        used[name] = used.get(name, 0) + 1
        if used[name] > 1:
            name = f"{name} ({used[name]})"
        src = source_line(label, rel)
        out.append((name, f"# {title}\n\n{src}\n\n{body}\n" if name != intro else f"{src}\n\n{body}\n"))
    return out


def chunks(text: str) -> list:
    """Cut a part into chunks of at most CHUNK characters at paragraph breaks; each starts with "title › section"."""
    lines = text.splitlines()
    title = lines[0].lstrip("# ").strip() if lines and lines[0].startswith("# ") else ""
    section = next((l[3:].strip() for l in lines if l.startswith("## ")), "")
    head = " › ".join(x for x in (title, section) if x)
    out, buf = [], ""
    for para in text.split("\n\n"):
        if buf and len(buf) + len(para) > CHUNK:
            out.append(buf)
            buf = ""
        buf = f"{buf}\n\n{para}" if buf else para
        while len(buf) > CHUNK:  # one very long paragraph (a big table)
            out.append(buf[:CHUNK])
            buf = buf[CHUNK:]
    if buf.strip():
        out.append(buf)
    return [c if i == 0 or not head else f"{head}\n{c}" for i, c in enumerate(out)]


def where(label: str, rel: str) -> str:
    """Where a file comes from: "<label>/<path>", or the path alone for a shelf source (its paths are absolute)."""
    return f"{label}/{rel}" if label else rel


def label_of(src: dict) -> str:
    return src.get("label", "" if src["type"] in ("shelf", "shelves") else src["name"])


def readable(page: str) -> str | None:
    """A page with its line ends trimmed, or None when it has fewer than MIN_PAGE_CHARS letters (spaces not counted)."""
    page = "\n".join(l.rstrip() for l in page.splitlines()).strip()
    return page if len("".join(page.split())) >= MIN_PAGE_CHARS else None


def split_pages(text: str, label: str, rel: str, unit: str = "p") -> list:
    """A PDF's pages or an EPUB's chapters (form feeds): [("<unit>.<n>", text)], each with the file name, page or
    chapter and where it comes from. Pages with too little text (readable) are left out."""
    title = Path(rel).stem
    out = []
    for n, page in enumerate(text.split("\f"), 1):
        page = readable(page)
        if page:
            out.append((f"{unit}.{n}", f"# {title}\n\n## {unit}.{n}\n\n{source_line(label, rel, f'{unit}.{n}')}\n\n{page}\n"))
    return out


def parts_of(src: dict, files: dict) -> dict:
    """{part key: text}: Markdown split by section ("<path>#<section>"), PDF by page ("<path>#p.<n>"), EPUB by
    chapter ("<path>#ch.<n>"), other files whole ("<path>")."""
    out = {}
    for rel, text in files.items():
        low = rel.lower()
        if low.endswith(".pdf") or low.endswith(".epub"):
            unit = "p" if low.endswith(".pdf") else "ch"
            for name, part in split_pages(text, label_of(src), rel, unit):
                out[f"{rel}#{name}"] = part
        elif low.endswith(".md"):
            for name, part in split_markdown(text, label_of(src), rel):
                out[f"{rel}#{name}"] = part
        else:
            out[rel] = text
    return out


def shelf_files(src: dict, include: list, exclude: list):
    """A shelf source: ({path: file}, path -> namespace, errors). [source.map] maps a namespace (academic.math) to one folder
    or a list of them; with sub, the namespace is "<key>.<sub>". Paths are the files' absolute paths without the leading
    "/", so files of different folders never collide. A file in two mapped folders goes to the first."""
    files, namespace_of, bad = {}, {}, 0
    for key, folders in src["map"].items():
        namespace = f"{key}.{src['sub']}" if src.get("sub") else key
        for folder in [folders] if isinstance(folders, str) else folders:
            root = Path(folder)
            if not root.is_dir():
                bad += 1
                log(f"{src['name']}: {root} ({namespace}) is not a directory")
                continue
            for rel, path in collect(root, include, exclude).items():
                k = (root / rel).as_posix().lstrip("/")
                if k in namespace_of:
                    if namespace_of[k] != namespace:
                        log(f"{src['name']}: {k} is mapped to {namespace_of[k]} and {namespace}; kept in {namespace_of[k]}")
                    continue
                files[k], namespace_of[k] = path, namespace
    return files, namespace_of.get, bad


def load_sources(config: Path) -> dict:
    """The config file, then <name>.local.toml next to it (not in git, like .env: places that should not be on
    record). A local source replaces the shared one of the same name; new names are added; other local tables
    ([rag], [text], [kinds.*]) replace the shared one's keys."""
    cfg = tomllib.loads(config.read_text())
    local = config.with_name(config.stem + ".local.toml")
    if local.exists():
        extra = tomllib.loads(local.read_text())
        names = {s["name"] for s in extra.get("source", [])}
        cfg["source"] = [s for s in cfg.get("source", []) if s["name"] not in names] + extra.get("source", [])
        for table in ("rag", "text"):
            cfg[table] = cfg.get(table, {}) | extra.get(table, {})
        cfg["kinds"] = cfg.get("kinds", {}) | extra.get("kinds", {})
    TEXT.update(cfg.get("text", {}))
    return cfg


def load_state() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    tmp.replace(path)


def interval(text: str) -> int:
    """"30m" / "1h" / "1d" -> seconds."""
    n, unit = int(text[:-1]), text[-1]
    return n * {"m": 60, "h": 3600, "d": 86400}[unit]


def cron_field(field: str, value: int, low: int, high: int) -> bool:
    """One field of a cron expression: *, 5, 1-5, 1,3,5, */2, 1-10/3."""
    for part in field.split(","):
        rng, _, step = part.partition("/")
        if rng == "*":
            a, b = low, high
        elif "-" in rng:
            a, b = (int(x) for x in rng.split("-"))
        else:
            a = b = int(rng)
        if a <= value <= b and (value - a) % int(step or 1) == 0:
            return True
    return False


def cron_hour(expr: str, t: time.struct_time) -> bool:
    """Whether the hour of t matches the cron expression "min hour day month weekday". The minute is not used:
    `run --due` is meant to run once an hour. Weekday 0 or 7 = Sunday. Day and weekday both restricted: either may match."""
    _, hour, day, month, wday = expr.split()
    wd = (t.tm_wday + 1) % 7  # struct_time: Monday 0; cron: Sunday 0
    days = cron_field(day, t.tm_mday, 1, 31)
    wdays = cron_field(wday, wd, 0, 7) or (wd == 0 and cron_field(wday, 7, 0, 7))
    dayok = (days or wdays) if day != "*" and wday != "*" else (days and wdays)
    return cron_field(hour, t.tm_hour, 0, 23) and cron_field(month, t.tm_mon, 1, 12) and dayok


def due(cfg: dict, now: float | None = None) -> list[str]:
    """Names of the sources whose time has come. Schedule: the source's own, else its kind's ([kinds.<kind>]).
    cron: "min hour day month weekday" for calendar times (minute not used); every: "1h", "48h", "1d" for intervals;
    at: the hour ("03") an every one may run. Meant to be called once an hour; a source without any schedule is
    never run by --due."""
    now = now or time.time()
    runs = json.loads(RUNS_FILE.read_text()) if RUNS_FILE.exists() else {}
    t = time.localtime(now)
    names = []
    for src in cfg.get("source", []):
        own = {k: src[k] for k in ("cron", "every", "at") if k in src}
        sched = own or cfg.get("kinds", {}).get(src.get("kind", ""), {})  # the source's own schedule replaces the kind's
        last = runs.get(src["name"], 0)
        if "cron" in sched:
            # once per matching hour
            if cron_hour(sched["cron"], t) and now - last >= 3000:
                names.append(src["name"])
        elif "every" in sched:
            if "at" in sched and t.tm_hour != int(sched["at"]):
                continue
            # 10 minutes of slack: an hourly source run at 10:15:40 is due again at 11:15:05
            if now - last >= interval(sched["every"]) - 600:
                names.append(src["name"])
    return names


def mark_runs(names: list[str], started: float) -> None:
    """Record when the run started, not when it ended: a run from 05:15 that ends at 06:06 must not count as the 06
    o'clock run of a cron source ("0 5-7 * * *"), or the 06:15 slot is skipped."""
    runs = json.loads(RUNS_FILE.read_text()) if RUNS_FILE.exists() else {}
    runs.update({n: started for n in names})
    save_json(RUNS_FILE, runs)


def one_format(files: dict, prefer: list) -> dict:
    """Of files with the same folder and name but different extensions in prefer (book.epub and book.pdf), keep only
    the one whose extension comes first; files of other extensions are kept as they are."""
    best = {}
    for rel in files:
        stem, _, ext = rel.rpartition(".")
        ext = ext.lower()
        if stem and ext in prefer and (stem not in best or prefer.index(ext) < prefer.index(best[stem].rpartition(".")[2].lower())):
            best[stem] = rel
    keep = set(best.values())
    left = [rel for rel in files if rel.rpartition(".")[2].lower() in prefer and rel not in keep]
    if left:
        log(f"{len(left)} file(s) also in a preferred format, skipped (e.g. {left[0]})")
    return {rel: f for rel, f in files.items() if rel not in left}


def in_namespace(namespace: str, under: str | None) -> bool:
    """Whether namespace is under or below it (language.pt.book is in language and in language.pt), as txtai-kura searches."""
    return not under or namespace == under or namespace.startswith(under + ".")


def shelves_map(src: dict) -> dict:
    """A shelves source: every folder directly under path whose name is a namespace name is one namespace of that name
    (with prefix: "<prefix>.<name>"). only: keep only folders with this word between dots in the name."""
    root = Path(src["path"])
    names = sorted(d.name for d in root.iterdir() if d.is_dir() and NAMESPACE.match(d.name)) if root.is_dir() else []
    if src.get("only"):
        skipped = [n for n in names if f".{src['only']}." not in f".{n}."]
        if skipped:
            log(f"{src['name']}: folders without .{src['only']}. in the name, skipped: {', '.join(skipped)}")
        names = [n for n in names if n not in skipped]
    pre = f"{src['prefix']}." if src.get("prefix") else ""
    return {pre + n: str(root / n) for n in names}


def run(cfg: dict, env: dict, only: str | None, dry: bool, kind: str | None = None, names: list | None = None,
        under: str | None = None) -> int:
    """Send what changed. under: only the files going to that namespace or below it; what is not found any more is then
    deleted only there, so the source's other namespaces are left alone. A source's max_minutes stops it after that long;
    what was sent is kept (state is saved every batch), the rest goes in on the next run, and nothing is deleted as
    gone until a run sees every file."""
    rag = Rag(cfg["rag"]["url"], env.get(cfg["rag"].get("api_key_env", "RAG_API_KEY"), ""))
    state = load_state()
    file_stamps = json.loads(FILES_FILE.read_text()) if FILES_FILE.exists() else {}
    changed = errors = 0
    for src in cfg.get("source", []):
        if only and src["name"] != only:
            continue
        if names is not None and src["name"] not in names:
            continue
        if kind and src.get("kind") != kind:
            continue
        include, exclude = src.get("include", ["**/*.md"]), src.get("exclude", [])
        if src["type"] in ("shelf", "shelves"):
            if src["type"] == "shelves":
                src = {**src, "sub": "", "map": shelves_map(src)}
            files, namespace_of, bad = shelf_files(src, include, exclude)
            errors += bad
        elif src["type"] in ("git", "dir"):
            if not src.get("namespace"):
                errors += 1
                log(f"{src['name']}: no namespace, skipped")
                continue
            if src["type"] == "git":
                roots = [fetch_git(src, env)]
            else:
                roots = [Path(p) for p in ([src["path"]] if isinstance(src["path"], str) else src["path"])]
                missing = [r for r in roots if not r.is_dir()]
                folders = [r.name for r in roots]
                if missing or len(set(folders)) < len(folders):
                    errors += 1
                    log(f"{src['name']}: " + (f"{missing[0]} is not a directory" if missing else
                                              f"two paths end in the same folder name ({', '.join(folders)})"))
                    continue
            namespace_of = lambda rel, a=src["namespace"]: a
            if len(roots) > 1:  # several paths: ids start with the folder name
                files = {f"{r.name}/{rel}": f for r in roots for rel, f in collect(r, include, exclude).items()}
            else:
                files = collect(roots[0], include, exclude)
        else:
            log(f"{src['name']}: unknown type {src['type']}, skipped")
            continue
        unmapped = [rel for rel in files if not namespace_of(rel)]
        if unmapped:
            log(f"{src['name']}: {len(unmapped)} file(s) outside every namespace folder, skipped (e.g. {unmapped[0]})")
        files = {rel: f for rel, f in files.items() if namespace_of(rel) and in_namespace(namespace_of(rel), under)}
        if src.get("prefer"):  # the same book in several formats: read only the first one listed (e.g. epub before pdf)
            files = one_format(files, [e.lower().lstrip(".") for e in src["prefer"]])
        prefix = f"{src['name']}:"
        seen = set()
        pending = []  # parts to store: (key, sha, namespace, old namespace or None, docs)
        before = errors
        stopped = False  # max_minutes reached: the rest is left for the next run
        deadline = time.time() + float(src["max_minutes"]) * 60 if src.get("max_minutes") else None
        stamps = {}  # file key -> [size, mtime_ns, namespace], kept in FILES_FILE once the source ran without errors
        parts_by_file = {}  # the file's relative path -> its part keys in state
        for k in state:
            if k.startswith(prefix):
                parts_by_file.setdefault(k[len(prefix):].split("#")[0], []).append(k)

        def flush():
            """Delete the old chunks of the pending parts and store the new ones, a few requests per namespace."""
            nonlocal errors
            if not pending:
                return
            try:
                for a in {p[3] for p in pending if p[3]}:
                    rag.delete(a, [p[0] + "#" for p in pending if p[3] == a])
                for a in {p[2] for p in pending}:
                    rag.upsert(a, [d for p in pending if p[2] == a for d in p[4]])
                for key, sha, a, _, docs in pending:
                    state[key] = {"sha": sha, "namespace": a, "chunks": len(docs)}
                save_json(STATE_FILE, state)
            except urllib.error.HTTPError as e:
                errors += 1
                log(f"  failed ({len(pending)} part(s)): HTTP {e.code} {e.read().decode(errors='replace')[:200]}")
            pending.clear()

        for rel in sorted(files):
            if deadline and time.time() > deadline:
                stopped = True
                log(f"{src['name']}: {src['max_minutes']} minutes up; the rest goes in on the next run")
                break
            st, namespace = files[rel].stat(), namespace_of(rel)
            stamp = [st.st_size, st.st_mtime_ns, namespace]
            if file_stamps.get(prefix + rel) == stamp:  # unchanged since it was last sent: not even opened
                seen.update(parts_by_file.get(rel, []))
                stamps[prefix + rel] = stamp
                continue
            text = read_text(files[rel])
            if text is None:  # could not be read: keep what was sent before, try again next run
                errors += 1
                seen.update(parts_by_file.get(rel, []))
                continue
            stamps[prefix + rel] = stamp
            for part, ptext in sorted(parts_of(src, {rel: text}).items()):
                key = prefix + part
                seen.add(key)
                sha = hashlib.sha256(ptext.encode()).hexdigest()
                old = state.get(key)
                if old and old["sha"] == sha and old.get("namespace") == namespace:
                    continue
                log(f"{'update' if old else 'add'} {key}")
                changed += 1
                if dry:
                    continue
                docs = [{"id": f"{key}#{i}", "text": c, "source": where(label_of(src), rel)}
                        for i, c in enumerate(chunks(ptext))]
                pending.append((key, sha, namespace, old.get("namespace", namespace) if old else None, docs))
                if sum(len(p[4]) for p in pending) >= BATCH:
                    flush()
        flush()
        # Files not reached before the time limit are not gone: delete only after a run that saw every file
        gone = [] if stopped else [k for k in state if k.startswith(prefix) and k not in seen and in_namespace(state[k]["namespace"], under)]
        for key in gone:
            log(f"delete {key}")
        changed += len(gone)
        if gone and not dry:
            try:
                for a in {state[k]["namespace"] for k in gone}:
                    rag.delete(a, [k + "#" for k in gone if state[k]["namespace"] == a])
                for k in gone:
                    del state[k]
                save_json(STATE_FILE, state)
            except urllib.error.HTTPError as e:
                errors += 1
                log(f"  failed: HTTP {e.code} {e.read().decode(errors='replace')[:200]}")
        if not dry and errors == before:  # with errors, every changed file is read again next run
            if not stopped:  # every file was seen: forget the ones that are gone
                for k in [k for k in file_stamps if k.startswith(prefix) and in_namespace(file_stamps[k][2], under)]:
                    del file_stamps[k]
            file_stamps.update(stamps)
            save_json(FILES_FILE, file_stamps)
    log(f"done: {changed} change(s), {errors} error(s){' (dry run)' if dry else ''}")
    return 1 if errors else 0


def status(cfg: dict, env: dict) -> int:
    rag = Rag(cfg["rag"]["url"], env.get(cfg["rag"].get("api_key_env", "RAG_API_KEY"), ""))
    state = load_state()
    print(f"tracked parts: {len(state)}, chunks: {sum(v.get('chunks', 0) for v in state.values())}")
    print("namespaces (chunks): " + ", ".join(f"{a} {n}" for a, n in rag.call("GET", "/namespaces").items()))
    return 0


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(prog="txtai-kura-sync", description=__doc__.splitlines()[0])
    ap.add_argument("--config", type=Path, default=Path("sources.toml"),
                    help="sources file (default: ./sources.toml); <name>.local.toml next to it is read too")
    ap.add_argument("--data", type=Path, default=None,
                    help="state and cache (default: $XDG_STATE_HOME/txtai-kura-sync or ~/.local/state/txtai-kura-sync)")
    ap.add_argument("--env-file", type=Path, default=None, help="KEY=value lines (default: .env next to the config)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="send what changed")
    r.add_argument("--source", help="only this source")
    r.add_argument("--kind", help="only sources of this kind")
    r.add_argument("--namespace", help="only files going to this namespace or below it, e.g. language or language.pt")
    r.add_argument("--dry-run", action="store_true", help="show what would be sent or deleted, change nothing")
    r.add_argument("--due", action="store_true", help="only the sources whose time has come (cron / every / at)")
    sub.add_parser("status", help="parts and chunks tracked here, chunks per namespace on the server")
    a = ap.parse_args(argv)
    configure(a.data or default_data())
    cfg = load_sources(a.config)
    env_file = a.env_file or a.config.with_name(".env")
    env = {**load_env(env_file), **os.environ}
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if a.cmd == "status":
        # Read-only: never wait for a running sync
        try:
            return status(cfg, env)
        except urllib.error.URLError as e:
            log(f"error: {e}")
            return 1
    with open(LOCK_FILE, "w") as lock:
        # Wait (up to 30 minutes) for a run that is already going, instead of failing
        deadline = time.time() + 1800
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() > deadline:
                    log("another run has been going for 30 minutes; exit")
                    return 1
                time.sleep(10)
        started = time.time()
        try:
            if a.due:
                names = due(cfg, started)
                log(f"due: {', '.join(names) or 'nothing'}")
                if not names:
                    return 0
                rc = run(cfg, env, None, a.dry_run, names=names)
                if rc == 0 and not a.dry_run:
                    mark_runs(names, started)  # with errors, they are due again next hour
                return rc
            rc = run(cfg, env, a.source, a.dry_run, a.kind, under=a.namespace)
            ran = [s["name"] for s in cfg.get("source", [])
                   if (not a.source or s["name"] == a.source) and (not a.kind or s.get("kind") == a.kind)]
            if rc == 0 and not a.dry_run and not a.namespace:  # a full or a kind run also counts for --due (not part of one)
                mark_runs(ran, started)
            return rc
        except (urllib.error.URLError, subprocess.CalledProcessError) as e:
            log(f"error: {e}")
            return 1


if __name__ == "__main__":
    sys.exit(main())
