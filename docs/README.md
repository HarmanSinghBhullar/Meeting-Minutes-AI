# Documentation

The [top-level README](../README.md) explains *why* this system is shaped the way
it is, and is the right place to start. These pages explain *what it does* and
*where things are* — reference material, kept close to the code it describes.

| Page | What it covers |
|---|---|
| [Architecture](architecture.md) | The processes, how a meeting flows through them, and the boundaries that matter |
| [Data model](data-model.md) | Every table, column, enum, and cascade |
| [API reference](api-reference.md) | Every endpoint: paths, bodies, responses, error codes |
| [Pipeline](pipeline.md) | The worker: queue, stages, alignment, minutes, grounding |
| [Extension](extension.md) | MV3 layout, message protocol, capture, adapters, UI |
| [Configuration](configuration.md) | Every environment variable and what it does |
| [Operations](operations.md) | Install, run, and the failure modes that cost people an hour |
| [Development](development.md) | Tests, migrations, scripts, evaluation, code style |

## Reading order

If you are new to the codebase, read [Architecture](architecture.md) first — it
is the map the other pages hang off. From there:

- **Working on the backend?** [Data model](data-model.md) → [Pipeline](pipeline.md).
  The segment table is the spine of the whole system; almost every feature is a
  read of it.
- **Working on the extension?** [Extension](extension.md) →
  [API reference](api-reference.md) for the endpoints it calls.
- **Just trying to get it running?** [Operations](operations.md) →
  [Configuration](configuration.md).

## Conventions in these docs

Names of real symbols (`run_transcribe`, `MIN_RUN_MS`, `Speaker.source`) are
written exactly as they appear in the code, so they are greppable. Where a
constant has a value, the value is given — but the code is the authority, and if
the two disagree the code is right and the doc is stale. Please fix it.
