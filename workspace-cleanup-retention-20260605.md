# Workspace Cleanup And Retention

Date: 2026-06-05

## Purpose

Reduce workspace noise before opening a new window, without deleting evidence.

## Cleanup Decision

Safe to delete now:

```text
target-codex*
```

Reason:

- these are Cargo build target directories created for separate Codex runs;
- they are generated artifacts;
- they are not source code;
- they are not experiment stores;
- they are not paper evidence;
- deleting them only means future Rust builds may take longer.

Do not delete now:

```text
src/
tests/
examples/
paper/
remote-logs/
logs/
figures/
docs/
data/
deps/
target/
target-check/
remote-docs/
*.tgz
*.zip
*.tar.gz
```

Reason:

- source and tests are the implementation surface;
- `remote-logs/` and archives may contain evidence summaries;
- `paper/` is not the current focus, but it is the existing source package;
- `target/` and `target-check/` are older standard build outputs and can be reviewed later;
- archives should be retained until final evidence mapping is complete.

## Build Artifact Inventory

The following local directories were identified as disposable build artifacts:

```text
target-codex
target-codex-check
target-codex-check2
target-codex-check-highthread
target-codex-p11
target-codex-p31feedback
target-codex-p620-local
target-codex-p620-local-check
target-codex-p630
target-codex-p638-local
target-codex-p644-local
target-codex-p648-local
target-codex-p649-local
target-codex-p650-local
target-codex-p651-local
target-codex-p652-local
target-codex-p654
target-codex-p655
target-codex-p656
target-codex-p657-local-check
target-codex-p658-local-check
target-codex-p659-local-check
target-codex-sample-plan-check
target-codex-semantic-check
target-codex-semantic-check2
```

Approximate total size:

```text
about 1.9 GB
```

## Retention Rule For Future Cleanup

Before deleting anything else, classify it as one of:

| Class | Delete? | Examples |
|---|---|---|
| source | no | `src/`, `tests/`, `Cargo.toml` |
| evidence | no | `remote-logs/`, named result `.md`, archives |
| paper package | no for now | `paper/`, `figures/` |
| generated build artifact | yes | `target-codex*` |
| unknown | no | anything not classified |

## Cleanup Result

Completed:

```text
deleted target-codex* directories: 25
remaining target-codex* directories: 0
```

Linux cleanup also completed:

```text
deleted Linux target-codex* directories: 115
remaining Linux target-codex* directories: 0
```

Local cleanup also completed:

```text
deleted local target* directories after elevated cleanup: 2
remaining local target* directories: 0
```

Left in place for later review:

```text
__pycache__/
```

These are also generated-looking directories, but they were not part of the narrow
`target-codex*` cleanup requested here. Review them separately if more local disk
cleanup is needed.

On Linux, standard `target/` was also deleted after the `target-codex*` cleanup. It
was a Cargo build cache, not evidence. The next readiness gate will rebuild it.

Linux final cleanup result:

```text
deleted Linux target-codex* directories: 115
deleted Linux target/ directory: 1
remaining Linux target* directories: 0
/data available after cleanup: about 517G
```

Root md consolidation result:

```text
Linux root md remaining: 5
Linux stage archive md: 401
Linux evidence summary md: 19
Local root md remaining: 5
Local stage archive md: 400
Local evidence summary md: 24
```

The Linux workspace is the primary source of truth.

Second root cleanup result:

```text
run-*.sh moved under scripts/experiments/{active,optional,legacy}/
render-*.py and summarize-*.py moved to scripts/analysis/current/
maintenance helpers moved under scripts/maintenance/{current,dangerous-archive}/
root .bib and .tex candidates moved to paper/archive/root-candidates-20260605/
tmp-sync-files and tmp-sync-files-p15 deleted on Linux
__pycache__ deleted
root scratch files first archived under docs/archive/root-scratch-20260605/
```

Docs cleanup result:

```text
docs/current/ now holds active operational notes
old root docs moved into docs/evidence-summaries-20260605/
docs/archive/root-scratch-20260605/ deleted
docs/archive/stage-md-20260605/ retained as low-priority history
```
