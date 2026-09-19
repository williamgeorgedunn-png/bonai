# Documentation

Practical how-to guides for the features added in this tree. The design
specs under `design/` are for implementers; start here if you want to *use*
the features.

## How to use

- [Pipeline mode on two GPUs](using-pipeline-and-dual-gpu.md) — run a planning
  architect on one GPU and a small editing worker on another.
- [Tracing code](using-tracing.md) — find where a symbol is defined, called,
  read and written, without adding whole files to the chat.

## Design specs

- [Pipeline mode](design/dual-gpu-pipeline-mode.md)
- [Explain checkpoints](design/explain-checkpoints.md) (not implemented yet)
