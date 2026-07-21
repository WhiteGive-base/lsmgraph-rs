# P20 formal runner contract

P20 is intentionally a two-phase protocol.  Correctness is established first
with current-schema, digest-emitting outputs for every canonical stage.  Feed
those outputs to `build_correctness_pass.py`; its `PASS` is an immutable input
to later measured runs.  `run_single_profile.py` therefore rejects
`mode=correctness` and never certifies the run it is about to measure.  Legacy
W6 gates that lack per-stage current digests and the four input hashes are not
silently upgraded.

Each invocation of `run_single_profile.py` executes one mode, stage, workload,
and independent repeat.  It requires a fresh stage-store clone whose complete
file inventory and per-file SHA-256 match a frozen pristine-store manifest.
Formal performance modes additionally require CPU pinning and a
`p20-clean-window-sentinel` JSON artifact bound to the binary, dataset, sample
plan, truth, pristine-store tree hash, scale, and host.  The sentinel must have
at least three observations with QPS CV at most 3% and P99 CV at most 5%.
It expires one hour after `completed_at_utc`, so an old quiet period cannot be
reused after host load has changed.

`summary.tsv` remains entry-level provenance.  It carries the complete latency
histogram for every benchmark entry, the measured-round `query_cpu_total_ns`,
the five mutually exclusive query phases, current-run truth-digest status, and
P31 process-tree resources.  These CPU totals are deliberately distinct:
`query_cpu_total_ns` is the measured query loop; `process_cpu_total_ns` is the
whole P31 command process tree.

For Figure 2, use `aggregate_figure2.py` with all summaries from the canonical
stage matrix and repeats 1--3.  The tool merges entry histograms/counters into
one run-stage observation, pairs an uninstrumented formal `latency` run with
the matching diagnostic `cpu-phase` run, and only then computes across-run
95% CIs.  It refuses missing pairs, missing repeats, noncanonical stages, or
entry-level pseudo-replication.  A paired CPU-phase run remains
`performance_eligible=false` for latency claims, but it must carry the same
fresh clean-window sentinel as its latency half so its CPU/op values are
eligible for the diagnostic panel.  A6 has no CPU-phase half because its automatic
lifecycle CPU is reported by the closed-loop resource experiment.

A3 and A4 replay the identical training trace from the same fresh-clone/cache
prestate.  A3 disables feedback and applies no feedback compaction; A4 enables
feedback and applies its one selected compaction.  The admissible claim is the
overall effect of **feedback-driven adaptation, including its triggered
compaction**.  It is not a pure-feedback-priority or isolated compaction-
algorithm comparison.
