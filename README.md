# txtai-kura-sync

*kura* (蔵) is Japanese for a storehouse: a place that keeps every kind of thing, not only books.

It talks to txtai-kura v0.2.0 or later.

Keep a [txtai-kura](https://github.com/otaqq/txtai-kura) search server in step with the documents it indexes:
git repositories, folders (an Obsidian vault, for example) and shelves of books.

txtai-kura only stores and searches; splitting files and keeping them in step with their source is up to its
client. This is that client.

- **Sources**: a git repository, one or more folders, or shelves (folders mapped to namespaces). Each source sends its
  files to a namespace of txtai-kura.
- **Formats**: Markdown (one part per `## ` section), PDF (one part per page, text by `pdftotext`), EPUB (one part
  per chapter), and plain text. When a book is there as both EPUB and PDF, `prefer` reads only one of them.
- **Incremental**: every part is compared with the last run (sha256). Only changed parts are sent again, removed
  parts are deleted, and a file whose size and time did not change is not even opened. Extracted PDF and EPUB text
  is cached.
- **Schedules**: `cron` or `every` per source or per kind, for `run --due` called once an hour. `max_minutes`
  stops a big source after that long; the rest goes in on the next run.
- No dependencies outside the Python standard library (3.11+). `pdftotext` (poppler) is needed only for PDFs, and
  `git` only for git sources.

## Knowledge graph extraction

`extract` asks an LLM for the entities and relations in each chunk the server has not extracted yet
(txtai-kura's `/kg/todo`), and sends them back (`/kg/upsert`). It works on the server's chunks, so it needs no
sources, only `[rag]`; any OpenAI-compatible chat API works (OpenAI, Ollama's `/v1`, a LiteLLM proxy).

```sh
txtai-kura-sync extract --namespace notes --api-base http://localhost:11434/v1 --model gemma3:12b --limit 10 --dry-run
txtai-kura-sync extract --namespace notes --api-base http://localhost:4000/v1 --model extract --parallel 3 --minutes 50
```

```toml
[extract]                          # defaults for the options above
api_base = "http://localhost:4000/v1"
model = "extract"
api_key_env = "LLM_API_KEY"        # if the API needs a key
parallel = 3
minutes = 50
```

- Resumable: a chunk counts as done once its result is sent, so a run stopped by `--minutes`, `--limit` or an error
  goes on where it left off. A chunk changed by `run` is extracted again, replacing what was learned from it.
- `--parallel` requests at a time; behind a proxy over several machines they are spread over them.
- A refused connection, a timeout or a server error is tried again after 15 and 60 seconds (a machine restarting);
  a chunk that still fails is left for the next run.
- `--dry-run` prints what the model finds as JSON lines and sends nothing.

Speed is mostly the model's machine. On 10 chunks of technical notes (180-1500 characters), Gemma 3 12B took
23 s per chunk on an RTX 3060 and about 60-70 s on Apple silicon laptops and minis; models that do not fit in the GPU's
memory were several times slower.

## Install

```sh
pip install git+https://github.com/otaqq/txtai-kura-sync
# for PDFs: apt install poppler-utils / brew install poppler
```

## Use

```sh
txtai-kura-sync --config sources.toml run --dry-run   # what would be sent or deleted
txtai-kura-sync --config sources.toml run             # send what changed
txtai-kura-sync --config sources.toml run --due       # only the sources whose time has come
txtai-kura-sync --config sources.toml status          # parts tracked here, chunks per namespace on the server
```

`run` also takes `--source NAME`, `--kind KIND` and `--namespace NAMESPACE` (only files going to that namespace or below it).
Only one run goes at a time: a second one waits up to 30 minutes for the first.

State and cache go to `--data` (default `$XDG_STATE_HOME/txtai-kura-sync`, else `~/.local/state/txtai-kura-sync`):
`state/state.json` (every part sent: sha, namespace, number of chunks), `state/files.json` (size and time of every file
sent), `state/runs.json` (when each source last ran, for `--due`), `cache/` (git clones, extracted text).
Deleting `files.json` makes the next run open every file again; deleting `state.json` makes it send everything again.

## Configuration

`sources.toml` (see [sources.toml.example](sources.toml.example)). A `sources.local.toml` next to it, if there is
one, is read too: its sources replace those of the same name, so places that should not be in git can live there.
Secrets come from environment variables, or from a `.env` file next to the config (`--env-file` to point elsewhere).

```toml
[rag]
url = "http://127.0.0.1:9630"
api_key_env = "RAG_API_KEY"        # the env var that holds the txtai-kura API key

[kinds.repos]
cron = "0 3 * * *"                 # every day at 3

[[source]]
name = "handbook"                  # ids start with "handbook:"
kind = "repos"
type = "git"
url = "https://git.example.com/team/handbook.git"
branch = "main"
token_env = "GIT_TOKEN"            # optional; token_auth = "token" (Forgejo, Gitea), "bearer" or "basic" (GitHub)
namespace = "docs.handbook"

[[source]]
name = "notes"
type = "dir"
path = "/srv/vault"                # or a list of folders, then ids start with each folder's name
every = "1h"
namespace = "notes"

[[source]]
name = "books"
type = "shelf"
cron = "0 5-7 * * *"
max_minutes = 50
include = ["**/*.pdf", "**/*.epub", "**/*.md"]
prefer = ["epub", "pdf"]
sub = "book"                       # the namespace is "<key>.<sub>": math.book, cs.ml.book
[source.map]
"math" = ["/srv/books/math"]
"cs.ml" = ["/srv/books/ml", "/srv/books/ml-en"]
```

| Key | Sources | Meaning |
|---|---|---|
| `type` | all | `git`, `dir`, `shelf` or `shelves` |
| `namespace` | git, dir | the txtai-kura namespace the files go to |
| `include`, `exclude` | all | globs relative to the folder (default `["**/*.md"]`); `**/` also matches nothing |
| `prefer` | all | file extensions in order: of `x.epub` and `x.pdf`, read only the first one listed |
| `map`, `sub` | shelf | namespace (or namespace prefix with `sub`) -> one folder or a list |
| `path`, `prefix`, `only` | shelves | every folder under `path` whose name is a namespace name is one namespace of that name; `prefix` puts `<prefix>.` in front, `only` keeps folders with that word between dots |
| `kind`, `cron`, `every`, `at` | all | schedule for `--due` (a source's own replaces its kind's); `cron` is matched by the hour |
| `max_minutes` | all | stop the source after that long; the rest goes in on the next run |
| `label` | all | how the source is named in the "Source" line of every part (default: its name; none for shelves) |

The words put into the documents can be changed in `[text]` (changing them changes every part, so everything is
sent again):

```toml
[text]
intro = "(intro)"                  # the name of the text before the first "## " section
source = "Source: {where}"         # the line under each title
```

## Ids

Chunk ids are `<source>:<path>#<part>#<n>`: `handbook:guide/setup.md#Install#0`, `books:srv/books/math/x.pdf#p.12#0`.
txtai-kura deletes by id prefix, so a part is replaced as a whole (`…#Install#`), and so is a file.

## Running it every hour

A cron line or a systemd timer that calls `txtai-kura-sync --config /path/to/sources.toml run --due` once an hour
(the minute does not matter; `cron` schedules are matched by the hour).

## License

MIT
