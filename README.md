# PDF Translate

Batch-translate English PDFs (mostly papers) into Chinese PDFs, keeping
formulas, figures and layout. Ships with its own Python runtime, so nothing
needs installing.

The engine is [PDFMathTranslate-next](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next)
(`babeldoc` + `pdf2zh_next`) inside `venv/`.

## Using it

Drag PDFs (or a folder of them) onto `翻译.bat`. Each file is translated in
turn and written beside the original as `<name>_中文.pdf`. Re-translating a file
overwrites that file in place; `_v2`, `_v3` only appear when the old translation
is locked by another program.

Command line:

```
.\翻译.bat a.pdf b.pdf papers\      translate these
.\翻译.bat --plan a.pdf             list what would be translated
.\翻译.bat config                    show engine and concurrency
.\翻译.bat logs                      open the log folder
.\翻译.bat clean [--force]           show or remove temporary files
```

Options: `--qps N`, `--no-adapt`, `--no-color`.

## Where the model comes from

`config.env` has a `MODEL_SOURCE` setting with three values:

| value | meaning |
|---|---|
| `public` | the shared free endpoint (SiliconFlow Free). No Magpie, no API key, no model to pick. |
| `pinned` | chosen in Magpie. The model Magpie's Agents page writes into `config.env`. |
| `magpie` | follow whatever model Magpie currently gives its agents. |

### Choosing the model in Magpie (recommended)

`magpie-plugin/` registers this translator as an agent of
[Magpie](https://usemagpie.com), the local LLM gateway. Install it once:

```
magpie plugin add "D:\Tools\PDF翻译工具-便携版\magpie-plugin"
```

Magpie's Agents page then has a **PDF Translate** row. Pick a model there and
Magpie writes `MODEL`, `API_KEY` and `BASE_URL` into `config.env`, with a
gateway key of its own for this agent. That key is what makes the translator's
usage and quota visible separately in Magpie.

The panel's **在 Magpie 里选模型…** button and the `A` option in the
command-line picker both open Magpie for this.

`pinned` and `magpie` differ in what they follow: `pinned` keeps the model
chosen for the PDF Translate row alone, while `magpie` follows the global
current model, so switching models in another agent moves this one too. Both
go through Magpie's gateway.

`magpie` falls back to the `MODEL` line when the gateway is offline or the
name is not in its model list, so a name Magpie does not serve is never sent.

## Configuration

`config.env` is not tracked (it holds a real key). Start from
`config.env.example`:

```
cp config.env.example config.env
```

Keys: `ENGINE`, `MODEL_SOURCE`, `API_KEY`, `MODEL`, `BASE_URL`, `QPS`.

`QPS` is requests per second. The free endpoint needs 5-10; a paid key of your
own takes 32-64. The translator also adapts on its own: it starts low, backs
off on 429 and climbs again once the rate holds.

## Layout

```
pdfq.py           the batch translator (command line)
pdf_panel.pyw     the window: engine state and the drop target
select_model.py   interactive model/engine picker
model_source.py   where the model comes from (public / pinned / magpie)
cleanup.py        temporary-file cleanup, also run before each batch
pdfq.bat          launcher (double-click, drag-and-drop)
翻译.bat           same launcher under a Chinese name
选择模型.bat       opens select_model.py
magpie-plugin/    the Magpie agent plugin
python/           bundled Python runtime
venv/             the translation engine
models/           offline seed: layout model, fonts, cmap
```

## Notes

- One file at a time: a single translation peaks around 2 GB of memory.
- Each file gets up to 3 attempts, waiting 20 s times the attempt number.
- A file is abandoned after 90 minutes and the batch moves on.
- Logs: `%LOCALAPPDATA%\PDFTranslateQueue\logs`, one per file.
- There is no persistent queue. The files given in one run are the whole job.
