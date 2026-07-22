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
Every measured mode, including the diagnostic `cpu-phase`, requires a formal
P02B `p02b-sf10-sentinel-result-v1` release for consumer `P20`.  P20 invokes
the canonical P02B validator rather than interpreting a second sentinel
schema.  The validator binds the adjacent `PASS` marker and `provenance.json`,
the clean Git HEAD and benchmark binary, a common P31 host fingerprint, and a
maximum age of six hours.  P02B is explicitly a same-host, host-global release
gate: its SF10 workload/store and 48-thread protocol are not required to equal
the later P20 scale, workload, store, or thread count.  Those P20 inputs remain
independently frozen by the correctness and pristine-store manifests.  Legacy
flat `p20-clean-window-sentinel` artifacts are rejected.

The generated `p02b-admission.json` joins the P02B receipt to the current P20
task/run/repeat, all current input hashes, CPU isolation, and P31 collection
settings.  The P31 collector wrapper is pinned to a housekeeping cpuset while
the benchmark tail is pinned to a disjoint benchmark cpuset.  Paper-use runs
require auxiliary collectors and at least ten P31 samples.

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
P02B result/PASS/provenance chain as its latency half so its CPU/op values are
eligible for the diagnostic panel.  A6 has no CPU-phase half because its automatic
lifecycle CPU is reported by the closed-loop resource experiment.

A3 and A4 replay the identical training trace from the same fresh-clone/cache
prestate.  A3 disables feedback and applies no feedback compaction; A4 enables
feedback and applies its one selected compaction.  The admissible claim is the
overall effect of **feedback-driven adaptation, including its triggered
compaction**.  It is not a pure-feedback-priority or isolated compaction-
algorithm comparison.

A4 and A5 use one frozen training replay, so their profiles explicitly pass
`--ra-min-score 0` for the pre-measurement feedback compaction.  The production
default score floor of 10 can reject this intentionally short trace (the SF10
diagnostic maximum was 4.397).  The existing minimum-query and minimum-L0-
segment eligibility gates remain enabled, so this setting does not manufacture
a candidate when the trace has insufficient observations or eligible segments.
