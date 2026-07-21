import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const OUT_DIR = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(OUT_DIR, "../../..");

const rel = (p) => p.replaceAll("\\", "/");
const abs = (p) => path.join(ROOT, p.replaceAll("/", path.sep));
const read = (p) => fs.readFileSync(abs(p), "utf8");

function clean(value) {
  return String(value ?? "")
    .replaceAll("`", "")
    .replaceAll("**", "")
    .trim();
}

function cell(value) {
  return String(value ?? "")
    .replaceAll("\t", " ")
    .replaceAll("\r", " ")
    .replaceAll("\n", " ")
    .trim();
}

function parseNumber(value) {
  const text = clean(value).replaceAll(",", "");
  const match = text.match(/[-+]?(?:\d+(?:\.\d*)?|\.\d+)/);
  if (!match) return null;
  const number = Number(match[0]);
  return Number.isFinite(number) ? number : null;
}

function parseMeanStd(value) {
  const parts = clean(value).replace("+/-", "±").split("±");
  return { mean: parseNumber(parts[0]), std: parseNumber(parts[1]) };
}

function parsePercentileTriple(value) {
  const parts = clean(value).replaceAll("+/-", "±").split("/");
  const parsed = parts.map(parseMeanStd);
  while (parsed.length < 3) parsed.push({ mean: null, std: null });
  return { p50: parsed[0], p90: parsed[1], p99: parsed[2] };
}

function parseMarkdownTable(text, heading) {
  const start = text.indexOf(heading);
  if (start < 0) throw new Error(`heading not found: ${heading}`);
  const lines = text.slice(start + heading.length).split(/\r?\n/);
  const table = [];
  let started = false;
  for (const line of lines) {
    if (line.trim().startsWith("|")) {
      table.push(line.trim());
      started = true;
    } else if (started) {
      break;
    }
  }
  if (table.length < 3) throw new Error(`table not found after: ${heading}`);
  const split = (line) => line.slice(1, -1).split("|").map((x) => clean(x));
  const headers = split(table[0]);
  return table.slice(2).map((line) => {
    const values = split(line);
    if (values.length !== headers.length) {
      throw new Error(`malformed markdown row after ${heading}: ${line}`);
    }
    return Object.fromEntries(headers.map((header, i) => [header, values[i]]));
  });
}

function parseTsv(text) {
  const lines = text.replace(/^\uFEFF/, "").trimEnd().split(/\r?\n/);
  const headers = lines[0].split("\t");
  return lines.slice(1).filter(Boolean).map((line) => {
    const values = line.split("\t");
    while (values.length < headers.length) values.push("");
    return Object.fromEntries(headers.map((header, i) => [header, values[i] ?? ""]));
  });
}

function sha256(relPath) {
  const filename = abs(relPath);
  if (!fs.existsSync(filename) || !fs.statSync(filename).isFile()) return "";
  return crypto.createHash("sha256").update(fs.readFileSync(filename)).digest("hex");
}

function writeTsv(filename, headers, rows) {
  const body = [headers, ...rows.map((row) => headers.map((header) => row[header] ?? ""))]
    .map((row) => row.map(cell).join("\t"))
    .join("\n") + "\n";
  fs.writeFileSync(path.join(OUT_DIR, filename), body, "utf8");
}

const CANDIDATE_HEADERS = [
  "figure_id",
  "panel_id",
  "source_id",
  "record_id",
  "system",
  "variant",
  "dataset",
  "workload",
  "scope",
  "x_name",
  "x_value",
  "x_unit",
  "metric",
  "value",
  "unit",
  "aggregation",
  "protocol_id",
  "correctness_status",
  "plot_eligibility",
  "verdict",
  "value_provenance",
  "protocol_provenance",
  "caveat",
];

function addMetric(rows, base, metric, value, unit, aggregation, provenance, extra = {}) {
  if (value === null || value === undefined || value === "") return;
  if (typeof value === "number" && !Number.isFinite(value)) {
    throw new Error(`non-finite metric ${metric}`);
  }
  rows.push({
    ...base,
    ...extra,
    metric,
    value: String(value),
    unit,
    aggregation,
    value_provenance: provenance,
    record_id: `${base.record_prefix}:${metric}${extra.record_suffix ? `:${extra.record_suffix}` : ""}`,
  });
}

const PATHS = {
  w6sf10: "cidr-experiments/data/source-cache/w6-sf10-priority-20260709-summary.md",
  w6sf100: "cidr-experiments/data/source-cache/sf100-matrix-20260613-cn.md",
  w6runner: "lsmgraph-rs/baseline/run_w6_sf100_matrix_20260613.sh",
  w6sf10raw: "cidr-experiments/data/source-cache/w6-sf10-priority-20260709",
  w6manifest: "cidr-experiments/data/source-cache/w6-sf10-priority-20260709/manifest.tsv",
  w6oldResource: "cidr-experiments/data/source-cache/priority-resource-overhead-20260709.tsv",
  w6progress: "lsmgraph-rs/baseline/w6-progress-20260613-cn.md",
  w8summary: "cidr-experiments/data/source-cache/w8-property-2hop-summary-20260615-cn.md",
  w8raw: "tmp-w8-inspect/w8-property-2hop-20260614-2025",
  w9summary: "cidr-experiments/data/source-cache/w9-steady-state-summary-20260615-cn.md",
  w9runner: "lsmgraph-rs/baseline/run_w9_steady_state_20260612.sh",
  w9progress: "lsmgraph-rs/baseline/w9-steady-state-progress-20260613-cn.md",
  c2table: "baseline/seml0-lifecycle-paper/tables/table-c2-retention.md",
  c2summary: "cidr-experiments/data/source-cache/stage7-sf30-readamp-proxy-summary-20260622-cn.md",
  w13summary: "cidr-experiments/data/source-cache/w13-schema-evolution-summary-20260614.md",
  w13runner: "lsmgraph-rs/baseline/run_w13_schema_evolution_20260613.sh",
  w13inventory: "lsmgraph-rs/baseline/w10-evidence-inventory-20260615-cn.md",
  externalMetrics: "baseline/external-baselines-20260626/3plus3-baselines/metrics.tsv",
  externalCorrectness: "baseline/external-baselines-20260626/3plus3-baselines/correctness.tsv",
  externalConfig: "baseline/external-baselines-20260626/3plus3-baselines/run-config.json",
  externalProgress: "baseline/external-baselines-20260626/3plus3-baselines/progress-table.md",
  livegraphConfig: "baseline/external-baselines-20260624/livegraph/run-config.json",
  livegraphMetrics: "baseline/external-baselines-20260624/livegraph/metrics.tsv",
  asterResult: "baseline/external-baselines-20260626/3plus3-baselines/systems/aster/sf10-main-r1/result.json",
  nebulaResult: "baseline/external-baselines-20260626/3plus3-baselines/systems/nebulagraph/sf10-main-r1/result.json",
};

function loadW6(summaryPath, scale, samplesPerType) {
  const text = read(summaryPath);
  const aggregate = parseMarkdownTable(text, "## Aggregate Matrix");
  const correctness = parseMarkdownTable(text, "## Correctness Compares");
  const rss = parseMarkdownTable(text, "## Import RSS");
  if (aggregate.length !== 9) throw new Error(`${summaryPath}: expected 9 aggregate rows`);
  return {
    summaryPath,
    scale,
    samplesPerType,
    aggregate,
    correctness: new Map(correctness.map((row) => [clean(row.variant), row])),
    rss: new Map(rss.map((row) => [clean(row.variant), row])),
  };
}

const w6sf10 = loadW6(PATHS.w6sf10, "SF10", 1000);
const w6sf100 = loadW6(PATHS.w6sf100, "SF100", 5000);

function buildFig2() {
  const rows = [];
  for (const source of [w6sf10, w6sf100]) {
    for (const row of source.aggregate) {
      const variant = clean(row.variant);
      const ops = parseNumber(row["ops/repeat"]);
      const candidates = parseMeanStd(row["candidate L0 mean+/-std"]);
      const readBytes = parseMeanStd(row["read bytes mean+/-std"]);
      const avg = parseMeanStd(row["avg us mean+/-std"]);
      const pct = parsePercentileTriple(row["p50/p90/p99 edge-mean us"]);
      const correct = source.correctness.get(variant);
      const rss = source.rss.get(variant);
      const verdict = source.scale === "SF100" ? "REUSE" : "FIX";
      const sourceId = source.scale === "SF100" ? "w6_sf100_fig2" : "w6_sf10_fig2";
      const base = {
        figure_id: "fig2-component-ablation",
        panel_id: "w6-layout-matrix",
        source_id: sourceId,
        record_prefix: `${source.scale}:${variant}`,
        system: "SemL0",
        variant,
        dataset: source.scale,
        workload: "outgoing_typed_neighbor",
        scope: `edge_types=1,2,3,7,8,9,10,11,12; samples_per_type=${source.samplesPerType}; repeats=3; same_store_repeats`,
        x_name: "",
        x_value: "",
        x_unit: "",
        protocol_id: `w6_${source.scale.toLowerCase()}_core_typed`,
        correctness_status: variant === "naive" ? "ANCHOR" : clean(row["compare vs naive"]),
        plot_eligibility: "PLOT_WITH_CAVEAT",
        verdict,
        protocol_provenance: `${source.summaryPath}#Aggregate Matrix; ${PATHS.w6runner}: EDGE_TYPES/SAMPLES/REPEATS and sample-plan reuse`,
        caveat: "W6 variants are precomposed layouts rather than one-switch component isolation; repeats reuse one imported store and are not independent process trials. Read-byte metrics retain the reader-overread caveat.",
      };
      const prov = (column) => `${source.summaryPath}#Aggregate Matrix row=${variant} column=${column}`;
      addMetric(rows, base, "ops_per_repeat", ops, "operations", "protocol_count", prov("ops/repeat"));
      addMetric(rows, base, "store_size_gib_reported", parseNumber(row["store GiB"]), "GiB_reported", "single_import", prov("store GiB"));
      addMetric(rows, base, "l0_files", parseNumber(row["L0 files"]), "files", "single_import", prov("L0 files"));
      addMetric(rows, base, "candidate_l0_total_mean", candidates.mean, "segments_per_repeat", "mean_across_same_store_repeats", prov("candidate L0 mean+/-std"));
      addMetric(rows, base, "candidate_l0_total_std", candidates.std, "segments_per_repeat", "stddev_across_same_store_repeats", prov("candidate L0 mean+/-std"));
      addMetric(rows, base, "candidate_l0_per_op_mean", candidates.mean / ops, "segments_per_operation", "derived_from_reported_means", `${prov("candidate L0 mean+/-std")} / ${prov("ops/repeat")}`);
      addMetric(rows, base, "candidate_l0_per_op_std", candidates.std / ops, "segments_per_operation", "derived_from_reported_stddev", `${prov("candidate L0 mean+/-std")} / ${prov("ops/repeat")}`);
      addMetric(rows, base, "read_bytes_total_mean", readBytes.mean, "bytes_per_repeat", "mean_across_same_store_repeats", prov("read bytes mean+/-std"), { plot_eligibility: "SECONDARY_ONLY_READER_OVERREAD" });
      addMetric(rows, base, "read_bytes_total_std", readBytes.std, "bytes_per_repeat", "stddev_across_same_store_repeats", prov("read bytes mean+/-std"), { plot_eligibility: "SECONDARY_ONLY_READER_OVERREAD" });
      addMetric(rows, base, "read_bytes_per_op_mean", readBytes.mean / ops, "bytes_per_operation", "derived_from_reported_means", `${prov("read bytes mean+/-std")} / ${prov("ops/repeat")}`, { plot_eligibility: "SECONDARY_ONLY_READER_OVERREAD" });
      addMetric(rows, base, "avg_latency_us_mean", avg.mean, "us_per_operation", "mean_across_same_store_repeats", prov("avg us mean+/-std"));
      addMetric(rows, base, "avg_latency_us_std", avg.std, "us_per_operation", "stddev_across_same_store_repeats", prov("avg us mean+/-std"));
      for (const [name, stats] of Object.entries(pct)) {
        addMetric(rows, base, `${name}_edge_mean_us`, stats.mean, "us", "mean_of_per_edge_type_percentiles_across_repeats", prov("p50/p90/p99 edge-mean us"));
        addMetric(rows, base, `${name}_edge_mean_std_us`, stats.std, "us", "stddev_of_per_edge_type_percentiles_across_repeats", prov("p50/p90/p99 edge-mean us"));
      }
      if (correct) {
        addMetric(rows, base, "correctness_checked", parseNumber(correct.checked), "queries", "naive_anchor_compare", `${source.summaryPath}#Correctness Compares row=${variant} column=checked`);
        addMetric(rows, base, "correctness_mismatches", parseNumber(correct.mismatches), "queries", "naive_anchor_compare", `${source.summaryPath}#Correctness Compares row=${variant} column=mismatches`);
      }
      if (rss) {
        addMetric(rows, base, "import_max_rss_gib_reported", parseNumber(rss["max RSS GiB"]), "GiB_reported", "single_import_time_v", `${source.summaryPath}#Import RSS row=${variant} column=max RSS GiB`);
      }
    }
  }
  return rows;
}

function parseTimeV(relPath) {
  const text = read(relPath);
  const pick = (regex) => text.match(regex)?.[1] ?? "";
  const elapsedRaw = pick(/^\s*Elapsed \(wall clock\) time \(h:mm:ss or m:ss\):\s*([0-9:.]+)\s*$/m);
  if (!elapsedRaw.includes(":")) throw new Error(`elapsed clock missing colon: ${relPath}`);
  const parts = elapsedRaw.split(":").map(Number);
  let elapsedSeconds;
  if (parts.length === 2) elapsedSeconds = parts[0] * 60 + parts[1];
  else if (parts.length === 3) elapsedSeconds = parts[0] * 3600 + parts[1] * 60 + parts[2];
  else throw new Error(`unexpected elapsed clock: ${elapsedRaw}`);
  return {
    elapsedRaw,
    elapsedSeconds,
    userCpuS: Number(pick(/^\s*User time \(seconds\):\s*([0-9.]+)\s*$/m)),
    sysCpuS: Number(pick(/^\s*System time \(seconds\):\s*([0-9.]+)\s*$/m)),
    cpuPercent: Number(pick(/^\s*Percent of CPU this job got:\s*([0-9.]+)%\s*$/m)),
    maxRssKb: Number(pick(/^\s*Maximum resident set size \(kbytes\):\s*(\d+)\s*$/m)),
    fsInputs: Number(pick(/^\s*File system inputs:\s*(\d+)\s*$/m)),
    fsOutputs: Number(pick(/^\s*File system outputs:\s*(\d+)\s*$/m)),
    exitStatus: Number(pick(/^\s*Exit status:\s*(\d+)\s*$/m)),
  };
}

const manifestRows = parseTsv(read(PATHS.w6manifest));
const manifestImports = new Map(manifestRows.filter((row) => row.kind === "import").map((row) => [row.variant, row]));
const sf10ByVariant = new Map(w6sf10.aggregate.map((row) => [clean(row.variant), row]));

function buildFig3() {
  const rows = [];
  for (const [variant, manifest] of manifestImports) {
    const timePath = `${PATHS.w6sf10raw}/${variant}-import.stderr`;
    const time = parseTimeV(timePath);
    const summary = sf10ByVariant.get(variant);
    const base = {
      figure_id: "fig3-resource-tradeoff",
      panel_id: "sf10-import-resource",
      source_id: "w6_sf10_fig3",
      record_prefix: `SF10:${variant}`,
      system: "SemL0",
      variant,
      dataset: "SF10",
      workload: "import_snb_full",
      scope: "single import; /usr/bin/time -v; SNB_SKIP_ADJ_CACHE=1; memgraph_bytes=67108864",
      x_name: "semantic_budget",
      x_value: variant.match(/^budg-b(\d+)$/)?.[1] ?? "",
      x_unit: variant.startsWith("budg-b") ? "max_extra_l0_files" : "",
      protocol_id: "w6_sf10_import_time_v",
      correctness_status: time.exitStatus === 0 ? "EXIT_0" : `EXIT_${time.exitStatus}`,
      plot_eligibility: "IMPORT_RESOURCE_ONLY",
      verdict: "FIX",
      protocol_provenance: `${PATHS.w6runner}#import_variant; ${PATHS.w6manifest} row=${variant}/import`,
      caveat: "Import-side resource evidence only; no read-phase CPU/op, PSS, perf counters, compaction stall, or independent import repeats. Git SHA, binary hash, and run-specific hardware manifest are absent.",
    };
    const rawProv = (field) => `${timePath}#/usr/bin/time -v field=${field}`;
    addMetric(rows, base, "store_size_bytes", Number(manifest.store_size_bytes), "bytes", "du_-sb_after_import", `${PATHS.w6manifest} row=${variant}/import column=store_size_bytes`);
    addMetric(rows, base, "store_size_gib_derived", Number(manifest.store_size_bytes) / 2 ** 30, "GiB", "derived", `${PATHS.w6manifest} row=${variant}/import store_size_bytes / 2^30`);
    addMetric(rows, base, "l0_files", parseNumber(summary?.["L0 files"]), "files", "single_import", `${PATHS.w6sf10}#Aggregate Matrix row=${variant} column=L0 files`);
    addMetric(rows, base, "import_elapsed_s", time.elapsedSeconds, "seconds", "single_import_wall_clock", `${rawProv("Elapsed")} raw=${time.elapsedRaw}; parsed as minutes:seconds`);
    addMetric(rows, base, "import_user_cpu_s", time.userCpuS, "seconds", "single_import", rawProv("User time (seconds)"));
    addMetric(rows, base, "import_system_cpu_s", time.sysCpuS, "seconds", "single_import", rawProv("System time (seconds)"));
    addMetric(rows, base, "import_cpu_percent", time.cpuPercent, "percent", "single_import", rawProv("Percent of CPU this job got"));
    addMetric(rows, base, "import_max_rss_kb", time.maxRssKb, "kbytes_time_v", "single_import", rawProv("Maximum resident set size (kbytes)"));
    addMetric(rows, base, "filesystem_inputs", time.fsInputs, "time_v_reported_units", "single_import", rawProv("File system inputs"));
    addMetric(rows, base, "filesystem_outputs", time.fsOutputs, "time_v_reported_units", "single_import", rawProv("File system outputs"));
    addMetric(rows, base, "exit_status", time.exitStatus, "code", "single_import", rawProv("Exit status"));
  }

  for (const row of w6sf100.aggregate) {
    const variant = clean(row.variant);
    const rss = w6sf100.rss.get(variant);
    const base = {
      figure_id: "fig3-resource-tradeoff",
      panel_id: "sf100-summary-resource",
      source_id: "w6_sf100_fig3",
      record_prefix: `SF100:${variant}`,
      system: "SemL0",
      variant,
      dataset: "SF100",
      workload: "import_snb_full",
      scope: "single import summary",
      x_name: "semantic_budget",
      x_value: variant.match(/^budg-b(\d+)$/)?.[1] ?? "",
      x_unit: variant.startsWith("budg-b") ? "max_extra_l0_files" : "",
      protocol_id: "w6_sf100_import_summary",
      correctness_status: clean(row["compare vs naive"]),
      plot_eligibility: "PARTIAL_RESOURCE_ONLY",
      verdict: "FIX",
      protocol_provenance: `${PATHS.w6sf100}#Aggregate Matrix and Import RSS`,
      caveat: "Only rounded store GiB, L0 file count, and import max RSS are locally summarized; CPU, exact disk bytes, and raw time-v logs are absent locally.",
    };
    addMetric(rows, base, "store_size_gib_reported", parseNumber(row["store GiB"]), "GiB_reported", "single_import", `${PATHS.w6sf100}#Aggregate Matrix row=${variant} column=store GiB`);
    addMetric(rows, base, "l0_files", parseNumber(row["L0 files"]), "files", "single_import", `${PATHS.w6sf100}#Aggregate Matrix row=${variant} column=L0 files`);
    addMetric(rows, base, "import_max_rss_gib_reported", parseNumber(rss?.["max RSS GiB"]), "GiB_reported", "single_import_time_v", `${PATHS.w6sf100}#Import RSS row=${variant} column=max RSS GiB`);
  }
  return rows;
}

function w6Fig1Rows() {
  const rows = [];
  for (const variant of ["schema", "budg-b64", "semantic", "naive", "kv-lsm"]) {
    const row = sf10ByVariant.get(variant);
    const avg = parseMeanStd(row["avg us mean+/-std"]);
    const pct = parsePercentileTriple(row["p50/p90/p99 edge-mean us"]);
    const time = parseTimeV(`${PATHS.w6sf10raw}/${variant}-import.stderr`);
    const manifest = manifestImports.get(variant);
    const base = {
      figure_id: "fig1-end-to-end",
      panel_id: "sf10-scope-limited",
      source_id: "w6_sf10_fig1",
      record_prefix: `SemL0-${variant}:core9`,
      system: "SemL0",
      variant,
      dataset: "SF10",
      workload: "outgoing_typed_neighbor",
      scope: "9 core edge types; 1000 samples/type; 9000 ops/repeat; 3 same-store repeats",
      x_name: "",
      x_value: "",
      x_unit: "",
      protocol_id: "w6_sf10_core_typed",
      correctness_status: variant === "naive" ? "ANCHOR" : clean(row["compare vs naive"]),
      plot_eligibility: "CONTEXT_ONLY_NOT_MATCHED",
      verdict: "RERUN",
      protocol_provenance: `${PATHS.w6sf10}#Aggregate Matrix; ${PATHS.w6runner}#bench_variant`,
      caveat: "Not directly comparable with external rows: SemL0 uses 9 core types and 9000 ops/repeat; external protocols use different type sets, operation counts, APIs, and timing boundaries.",
    };
    const prov = (column) => `${PATHS.w6sf10}#Aggregate Matrix row=${variant} column=${column}`;
    addMetric(rows, base, "query_ops_per_repeat", parseNumber(row["ops/repeat"]), "operations", "protocol_count", prov("ops/repeat"));
    addMetric(rows, base, "avg_latency_us", avg.mean, "us_per_operation", "mean_across_same_store_repeats", prov("avg us mean+/-std"));
    addMetric(rows, base, "p50_us_edge_mean", pct.p50.mean, "us", "mean_of_per_edge_type_percentiles", prov("p50/p90/p99 edge-mean us"));
    addMetric(rows, base, "p90_us_edge_mean", pct.p90.mean, "us", "mean_of_per_edge_type_percentiles", prov("p50/p90/p99 edge-mean us"));
    addMetric(rows, base, "p99_us_edge_mean", pct.p99.mean, "us", "mean_of_per_edge_type_percentiles", prov("p50/p90/p99 edge-mean us"));
    addMetric(rows, base, "load_import_s", time.elapsedSeconds, "seconds", "single_import_wall_clock", `${PATHS.w6sf10raw}/${variant}-import.stderr#/usr/bin/time Elapsed raw=${time.elapsedRaw}`);
    addMetric(rows, base, "peak_import_rss_kb", time.maxRssKb, "kbytes_time_v", "single_import", `${PATHS.w6sf10raw}/${variant}-import.stderr#/usr/bin/time Maximum resident set size`);
    addMetric(rows, base, "disk_total_bytes", Number(manifest.store_size_bytes), "bytes", "du_-sb_after_import", `${PATHS.w6manifest} row=${variant}/import column=store_size_bytes`);
  }
  return rows;
}

function buildFig1() {
  const rows = w6Fig1Rows();
  const external = parseTsv(read(PATHS.externalMetrics)).filter((row) =>
    row.scale === "SF10" &&
    row.system !== "LSMGraph-style" &&
    ["all-types-summary", "positive-edge-types-summary", "negative-edge-types-summary"].includes(row.scope)
  );
  const protocolId = (system) => system === "LiveGraph" ? "external_livegraph_sf10_s1000" : "external_truth_s50_seed42";
  for (const row of external) {
    const base = {
      figure_id: "fig1-end-to-end",
      panel_id: "sf10-scope-limited",
      source_id: `external_${row.system.toLowerCase().replaceAll(/[^a-z0-9]+/g, "_")}_fig1`,
      record_prefix: `${row.system}:${row.scope}`,
      system: row.system,
      variant: "",
      dataset: row.scale,
      workload: "outgoing_typed_neighbor",
      scope: row.scope,
      x_name: "",
      x_value: "",
      x_unit: "",
      protocol_id: protocolId(row.system),
      correctness_status: row.correctness,
      plot_eligibility: "CONTEXT_ONLY_NOT_MATCHED",
      verdict: "RERUN",
      protocol_provenance: `${PATHS.externalConfig}#workload plus ${PATHS.externalMetrics} row=${row.system}/${row.scale}/${row.scope}`,
      caveat: "Scope-limited external row. Operation counts, edge-type coverage, API layer, client/server boundary, and percentile aggregation differ from SemL0 W6; do not use for a ranked matched-system figure.",
    };
    const mapping = [
      ["ops", "query_ops", "operations"],
      ["avg_us", "avg_latency_us", "us_per_operation"],
      ["p50_us", "p50_us", "us"],
      ["p90_us", "p90_us", "us"],
      ["p99_us", "p99_us", "us"],
      ["load_s", "load_s", "seconds"],
      ["peak_rss_kb", "peak_rss_kb", "kbytes_reported"],
      ["disk_total_bytes", "disk_total_bytes", "bytes"],
    ];
    for (const [column, metric, unit] of mapping) {
      const value = row[column] === "" ? null : Number(row[column]);
      addMetric(rows, base, metric, value, unit, "reported_system_summary", `${PATHS.externalMetrics} row=${row.system}/${row.scale}/${row.scope} column=${column}`);
    }
  }
  return rows;
}

function parseReportedQuantity(text) {
  const value = parseNumber(text);
  const unit = clean(text).replace(/[-+]?(?:\d+(?:\.\d*)?|\.\d+)/, "").trim();
  return { value, unit: unit || "count" };
}

function buildFig4() {
  const rows = [];
  const c2 = read(PATHS.c2table);
  const layerA = parseMarkdownTable(c2, "## Layer A — Controlled read-amplification (synthetic; full read workload + correctness)");
  const layerB = parseMarkdownTable(c2, "## Layer B — Real LDBC data: retention + write cost (open-mode: surface + cost only)");
  const layerC = parseMarkdownTable(c2, "## Layer C — Real SF30 metadata-level read-amplification proxy");
  if (layerA.length !== 4 || layerB.length !== 4 || layerC.length !== 2) throw new Error("unexpected C2 table row count");
  for (const row of layerA) {
    const scale = clean(row.scale);
    const policy = clean(row.policy);
    const base = {
      figure_id: "fig4-dynamic-compaction",
      panel_id: "c2-layer-a-controlled",
      source_id: "c2_layer_a_fig4",
      record_prefix: `C2A:${scale}:${policy}`,
      system: "SemL0",
      variant: policy,
      dataset: scale,
      workload: "controlled_typed_neighbor_full_read",
      scope: "controlled synthetic merge; full read consequence and correctness",
      x_name: "",
      x_value: "",
      x_unit: "",
      protocol_id: "c2_controlled_full_read",
      correctness_status: `mismatches=${parseNumber(row.mismatches)}`,
      plot_eligibility: "PLOT_CANDIDATE_CONTROLLED",
      verdict: "REUSE",
      protocol_provenance: `${PATHS.c2table}#Layer A source note`,
      caveat: "Intentionally controlled synthetic partition structure; do not label as real LDBC end-to-end.",
    };
    const prov = (column) => `${PATHS.c2table}#Layer A row=${scale}/${policy} column=${column}`;
    addMetric(rows, base, "pruning_retention", parseNumber(row.pruning_retention), "ratio", "single_controlled_run", prov("pruning_retention"));
    addMetric(rows, base, "output_segments", parseNumber(row["output segs"]), "segments", "single_controlled_run", prov("output segs"));
    addMetric(rows, base, "write_amplification", parseNumber(row.write_amp), "ratio", "single_controlled_run", prov("write_amp"));
    addMetric(rows, base, "relative_full_read_cost_after_vs_before", clean(row["read after vs before"]).startsWith("flat") ? 1 : parseNumber(row["read after vs before"]), "ratio", "single_controlled_run", prov("read after vs before"));
    addMetric(rows, base, "correctness_mismatches", parseNumber(row.mismatches), "queries", "digest_compare", prov("mismatches"));
  }
  for (const row of layerB) {
    const scale = clean(row.scale);
    const policy = clean(row.policy);
    const base = {
      figure_id: "fig4-dynamic-compaction",
      panel_id: "c2-layer-b-real-retention-cost",
      source_id: "c2_layer_b_fig4",
      record_prefix: `C2B:${scale}:${policy}`,
      system: "SemL0",
      variant: policy,
      dataset: scale,
      workload: "open_mode_retention_and_write_cost",
      scope: "real LDBC open-mode; no per-run full read workload",
      x_name: "",
      x_value: "",
      x_unit: "",
      protocol_id: "c2_real_open_mode",
      correctness_status: "LOSSLESSNESS_COVERED_BY_ENGINE_TESTS_NOT_PER_RUN_DIGEST",
      plot_eligibility: "PLOT_CANDIDATE_REAL_RETENTION_COST",
      verdict: "REUSE",
      protocol_provenance: `${PATHS.c2table}#Layer B source note`,
      caveat: "Real-data retention and write-cost evidence only; no full body-read latency in this layer.",
    };
    const prov = (column) => `${PATHS.c2table}#Layer B row=${scale}/${policy} column=${column}`;
    addMetric(rows, base, "pruning_retention", parseNumber(row.pruning_retention), "ratio", "single_open_mode_run", prov("pruning_retention"));
    addMetric(rows, base, "exact_surface_after", parseNumber(row["exact_surface after"]), "ratio", "single_open_mode_run", prov("exact_surface after"));
    addMetric(rows, base, "output_segments", parseNumber(row["output segs"]), "segments", "single_open_mode_run", prov("output segs"));
    const outBytes = parseReportedQuantity(row.output_bytes);
    addMetric(rows, base, "output_bytes_reported", outBytes.value, outBytes.unit, "single_open_mode_run", prov("output_bytes"));
    const logical = parseReportedQuantity(row.logical_bytes);
    addMetric(rows, base, "logical_bytes_reported", logical.value, logical.unit, "single_open_mode_run", prov("logical_bytes"));
    addMetric(rows, base, "write_amplification", parseNumber(row.write_amp), "ratio", "single_open_mode_run", prov("write_amp"));
  }
  for (const row of layerC) {
    const scale = clean(row.scale);
    const policy = clean(row.policy);
    const base = {
      figure_id: "fig4-dynamic-compaction",
      panel_id: "c2-layer-c-real-metadata-proxy",
      source_id: "c2_layer_c_fig4",
      record_prefix: `C2C:${scale}:${policy}`,
      system: "SemL0",
      variant: policy,
      dataset: scale,
      workload: "metadata_typed_neighbor_partition_replay",
      scope: "40 active typed partitions; metadata candidates only; no body decode",
      x_name: "",
      x_value: "",
      x_unit: "",
      protocol_id: "c2_sf30_metadata_proxy",
      correctness_status: "NOT_A_QUERY_DIGEST_RUN",
      plot_eligibility: "PLOT_CANDIDATE_METADATA_PROXY_ONLY",
      verdict: "REUSE",
      protocol_provenance: `${PATHS.c2summary}#方法; ${PATHS.c2table}#Layer C`,
      caveat: "Metadata-level proxy only. Never label 6.52x as end-to-end latency or full SF30 body-read speedup.",
    };
    const prov = (column) => `${PATHS.c2table}#Layer C row=${scale}/${policy} column=${column}`;
    addMetric(rows, base, "query_partitions", parseNumber(row["query partitions"]), "partitions", "single_metadata_replay", prov("query partitions"));
    addMetric(rows, base, "avg_candidate_segments_per_query", parseNumber(row["avg candidate segs/query"]), "segments_per_partition_query", "single_metadata_replay", prov("avg candidate segs/query"));
    const candBytes = parseReportedQuantity(row["candidate bytes total"]);
    addMetric(rows, base, "candidate_bytes_total_reported", candBytes.value, candBytes.unit, "single_metadata_replay", prov("candidate bytes total"));
    const exactBytes = parseReportedQuantity(row["exact bytes before"]);
    addMetric(rows, base, "exact_bytes_before_reported", exactBytes.value, exactBytes.unit, "single_metadata_replay", prov("exact bytes before"));
    addMetric(rows, base, "weighted_read_amplification_proxy", parseNumber(row["weighted read-amp proxy"]), "ratio", "single_metadata_replay", prov("weighted read-amp proxy"));
    addMetric(rows, base, "mixed_candidate_segments", parseNumber(row["mixed candidate segs"]), "segments", "single_metadata_replay", prov("mixed candidate segs"));
  }

  const w9 = parseMarkdownTable(read(PATHS.w9summary), "## Per-checkpoint trace");
  if (w9.length !== 18) throw new Error(`expected 18 W9 checkpoints, got ${w9.length}`);
  for (const row of w9) {
    const variant = clean(row.variant);
    const elapsed = parseNumber(row["elapsed s"]);
    const base = {
      figure_id: "fig4-dynamic-compaction",
      panel_id: "w9-no-compaction-timeline",
      source_id: "w9_fig4",
      record_prefix: `W9:${variant}:${elapsed}`,
      system: "SemL0",
      variant,
      dataset: "SF30",
      workload: "mixed_read_write_append_only_trace",
      scope: "duration=1800s; checkpoint=300s; target query_rate=200/s; target write_rate=200/s; flush_every=1024; compact_every=0",
      x_name: "elapsed",
      x_value: elapsed,
      x_unit: "seconds",
      protocol_id: "w9_sf30_no_compaction",
      correctness_status: `writer_errors=${parseNumber(row["writer errors"])}; no_query_digest`,
      plot_eligibility: "SUPPORTING_NO_COMPACTION_ONLY",
      verdict: "RERUN",
      protocol_provenance: `${PATHS.w9progress}#Planned command; ${PATHS.w9runner}: QUERY_RATE/WRITE_RATE/FLUSH_EVERY_WRITES/COMPACT_EVERY_SECS`,
      caveat: "COMPACT_EVERY_SECS=0 and one sequential run per variant; checkpoints are correlated time points, not independent repeats. Formal JSONL raw is remote-only.",
    };
    const prov = (column) => `${PATHS.w9summary}#Per-checkpoint trace row=${variant}/${elapsed}s column=${column}`;
    addMetric(rows, base, "queries", parseNumber(row.queries), "queries_per_checkpoint", "checkpoint", prov("queries"));
    addMetric(rows, base, "query_rate", parseNumber(row["query rate/s"]), "queries_per_second", "checkpoint", prov("query rate/s"));
    addMetric(rows, base, "p50_us", parseNumber(row["p50 us"]), "us", "checkpoint_percentile", prov("p50 us"));
    addMetric(rows, base, "p99_us", parseNumber(row["p99 us"]), "us", "checkpoint_percentile", prov("p99 us"));
    addMetric(rows, base, "candidate_l0_mean", parseNumber(row["candidate L0 mean"]), "segments_per_query", "checkpoint_mean", prov("candidate L0 mean"));
    addMetric(rows, base, "l0_files", parseNumber(row["L0 files"]), "files", "checkpoint", prov("L0 files"));
    const rewrite = parseReportedQuantity(row["rewrite bytes"]);
    addMetric(rows, base, "rewrite_bytes_reported", rewrite.value, rewrite.unit, "checkpoint_delta", prov("rewrite bytes"));
    const ioWrite = parseReportedQuantity(row["io write bytes"]);
    addMetric(rows, base, "io_write_bytes_reported", ioWrite.value, ioWrite.unit, "checkpoint_delta", prov("io write bytes"));
    addMetric(rows, base, "writer_errors", parseNumber(row["writer errors"]), "operations", "checkpoint", prov("writer errors"));
    addMetric(rows, base, "writer_slow_ops", parseNumber(row["writer slow ops"]), "operations", "checkpoint", prov("writer slow ops"));
  }
  return rows;
}

function buildFig5() {
  const rows = [];
  const w8text = read(PATHS.w8summary);
  const property = parseMarkdownTable(w8text, "## Property predicate results");
  const twoHop = parseMarkdownTable(w8text, "## 2-hop results");
  if (property.length !== 12 || twoHop.length !== 3) throw new Error("unexpected W8 table row count");
  for (const row of property) {
    const variant = clean(row.variant);
    const predicate = clean(row.predicate);
    const base = {
      figure_id: "fig5-workload-coverage",
      panel_id: "w8-property",
      source_id: "w8_fig5",
      record_prefix: `W8:property:${variant}:${predicate}`,
      system: "SemL0",
      variant,
      dataset: "SF30",
      workload: `property_${predicate}`,
      scope: "edge_type=1; sampled_srcs=5000; repeats=3; warmup_runs=1; same imported store",
      x_name: "",
      x_value: "",
      x_unit: "",
      protocol_id: "w8_sf30_property",
      correctness_status: "NO_QUERY_LEVEL_DIGEST",
      plot_eligibility: predicate === "equality" ? "PROTOTYPE_ONLY_FIX" : "FIX_BEFORE_PAPER",
      verdict: "FIX",
      protocol_provenance: `${PATHS.w8raw}/${variant}/property-${predicate === "required_property" ? "required-property" : predicate.replaceAll("_", "-")}.json top-level repeats/warmup/workload/property fields`,
      caveat: predicate === "equality" ? "Equality uses a separate prototype path; no query-level digest is present." : "No query-level result digest is present; repeats reuse one store.",
    };
    const prov = (column) => `${PATHS.w8summary}#Property predicate results row=${variant}/${predicate} column=${column}`;
    addMetric(rows, base, "candidate_l0_mean", parseNumber(row["candidate L0 mean"]), "segments_per_repeat_reported", "summary_mean", prov("candidate L0 mean"));
    addMetric(rows, base, "body_reads_mean", parseNumber(row["body reads mean"]), "records_per_repeat_reported", "summary_mean", prov("body reads mean"));
    const rb = parseReportedQuantity(row["read bytes mean"]);
    addMetric(rows, base, "read_bytes_mean_reported", rb.value, rb.unit, "summary_mean", prov("read bytes mean"), { plot_eligibility: "SECONDARY_ONLY_READER_OVERREAD" });
    const elapsed = parseReportedQuantity(row["elapsed mean"]);
    addMetric(rows, base, "elapsed_mean_reported", elapsed.value, elapsed.unit, "summary_mean", prov("elapsed mean"));
    addMetric(rows, base, "p50_us", parseNumber(row["p50 us"]), "us", "summary_percentile", prov("p50 us"));
    addMetric(rows, base, "p90_us", parseNumber(row["p90 us"]), "us", "summary_percentile", prov("p90 us"));
    addMetric(rows, base, "p99_us", parseNumber(row["p99 us"]), "us", "summary_percentile", prov("p99 us"));
    addMetric(rows, base, "one_hop_edges_mean", parseNumber(row["one-hop edges mean"]), "edges_per_repeat_reported", "summary_mean", prov("one-hop edges mean"));
  }
  for (const row of twoHop) {
    const variant = clean(row.variant);
    const base = {
      figure_id: "fig5-workload-coverage",
      panel_id: "w8-two-hop",
      source_id: "w8_fig5",
      record_prefix: `W8:twohop:${variant}`,
      system: "SemL0",
      variant,
      dataset: "SF30",
      workload: "typed_two_hop",
      scope: "edge_type=1; sampled_srcs=5000; repeats=3; warmup_runs=1; two_hop_fanout=64",
      x_name: "",
      x_value: "",
      x_unit: "",
      protocol_id: "w8_sf30_two_hop",
      correctness_status: "NO_QUERY_LEVEL_DIGEST",
      plot_eligibility: "FIX_BEFORE_PAPER",
      verdict: "FIX",
      protocol_provenance: `${PATHS.w8raw}/${variant}/2hop-typed.json top-level and benchmark fields`,
      caveat: "No query-level digest; candidates increase for budgeted/semantic versus schema even though body reads and elapsed decrease.",
    };
    const prov = (column) => `${PATHS.w8summary}#2-hop results row=${variant} column=${column}`;
    for (const [column, metric, unit] of [
      ["candidate L0 mean", "candidate_l0_mean", "segments_per_repeat_reported"],
      ["body reads mean", "body_reads_mean", "records_per_repeat_reported"],
      ["p50 us", "p50_us", "us"],
      ["p90 us", "p90_us", "us"],
      ["p99 us", "p99_us", "us"],
      ["one-hop edges mean", "one_hop_edges_mean", "edges_per_repeat_reported"],
      ["two-hop edges mean", "two_hop_edges_mean", "edges_per_repeat_reported"],
      ["two-hop sources mean", "two_hop_sources_mean", "sources_per_repeat_reported"],
    ]) addMetric(rows, base, metric, parseNumber(row[column]), unit, "summary_mean_or_percentile", prov(column));
    const rb = parseReportedQuantity(row["read bytes mean"]);
    addMetric(rows, base, "read_bytes_mean_reported", rb.value, rb.unit, "summary_mean", prov("read bytes mean"), { plot_eligibility: "SECONDARY_ONLY_READER_OVERREAD" });
    const elapsed = parseReportedQuantity(row["elapsed mean"]);
    addMetric(rows, base, "elapsed_mean_reported", elapsed.value, elapsed.unit, "summary_mean", prov("elapsed mean"));
  }

  const w13lines = read(PATHS.w13summary).split(/\r?\n/);
  const tests = w13lines.filter((line) => line.includes("\tpass\t")).map((line) => {
    const [test, status, logDir] = line.split("\t");
    return { test, status, logDir };
  });
  if (tests.length !== 10) throw new Error(`expected 10 W13 tests, got ${tests.length}`);
  for (const test of tests) {
    const base = {
      figure_id: "fig5-workload-coverage",
      panel_id: "w13-bounded-safety-coverage",
      source_id: "w13_fig5",
      record_prefix: `W13:${test.test}`,
      system: "SemL0",
      variant: "conservative_fallback",
      dataset: "engine_test_fixture",
      workload: test.test,
      scope: "bounded schema/snapshot/compaction/reopen correctness tests",
      x_name: "",
      x_value: "",
      x_unit: "",
      protocol_id: "w13_primary_10_tests",
      correctness_status: "PASS",
      plot_eligibility: "TABLE_OR_COVERAGE_MATRIX_ONLY",
      verdict: "REUSE",
      protocol_provenance: `${PATHS.w13runner}#TESTS; ${PATHS.w13summary}#Tests`,
      caveat: "Bounded deterministic engine tests only; no full online migration/reclamation, randomized differential stress, or performance claim.",
    };
    addMetric(rows, base, "test_pass", 1, "boolean", "single_test_execution", `${PATHS.w13summary}#Tests row=${test.test} status=pass; remote_log_dir=${test.logDir}`);
  }
  const aggBase = {
    figure_id: "fig5-workload-coverage",
    panel_id: "w13-bounded-safety-coverage",
    source_id: "w13_fig5",
    record_prefix: "W13:aggregate",
    system: "SemL0",
    variant: "conservative_fallback",
    dataset: "engine_test_fixture",
    workload: "schema_evolution_bounded_suite",
    scope: "10 selected tests",
    x_name: "",
    x_value: "",
    x_unit: "",
    protocol_id: "w13_primary_10_tests",
    correctness_status: "PASS",
    plot_eligibility: "TABLE_OR_COVERAGE_MATRIX_ONLY",
    verdict: "REUSE",
    protocol_provenance: `${PATHS.w13runner}#TESTS; ${PATHS.w13summary}#Tests`,
    caveat: "Aggregate count is derived from the ten explicit pass rows; raw logs are remote-only.",
  };
  addMetric(rows, aggBase, "tests_passed", tests.length, "tests", "derived_count", `${PATHS.w13summary}#Tests count(status=pass)`);
  addMetric(rows, aggBase, "tests_total", tests.length, "tests", "derived_count", `${PATHS.w13summary}#Tests row count`);
  return rows;
}

function buildFig6() {
  const rows = [];
  const selected = new Set(["schema", "budg-b64", "semantic", "naive", "kv-lsm"]);
  for (const source of [w6sf10, w6sf100]) {
    const sfValue = source.scale === "SF10" ? 10 : 100;
    for (const row of source.aggregate) {
      const variant = clean(row.variant);
      if (!selected.has(variant)) continue;
      const ops = parseNumber(row["ops/repeat"]);
      const candidates = parseMeanStd(row["candidate L0 mean+/-std"]);
      const readBytes = parseMeanStd(row["read bytes mean+/-std"]);
      const avg = parseMeanStd(row["avg us mean+/-std"]);
      const p99 = parsePercentileTriple(row["p50/p90/p99 edge-mean us"]).p99;
      const rss = source.rss.get(variant);
      const base = {
        figure_id: "fig6-scalability",
        panel_id: "w6-two-point-scale-diagnostic",
        source_id: source.scale === "SF10" ? "w6_sf10_fig6" : "w6_sf100_fig6",
        record_prefix: `${source.scale}:${variant}`,
        system: "SemL0",
        variant,
        dataset: source.scale,
        workload: "outgoing_typed_neighbor",
        scope: `9 core edge types; samples_per_type=${source.samplesPerType}; repeats=3; same-store repeats`,
        x_name: "ldbc_scale_factor",
        x_value: sfValue,
        x_unit: "SF",
        protocol_id: `w6_${source.scale.toLowerCase()}_core_typed`,
        correctness_status: variant === "naive" ? "ANCHOR" : clean(row["compare vs naive"]),
        plot_eligibility: "DIAGNOSTIC_TWO_POINT_ONLY",
        verdict: "RERUN",
        protocol_provenance: `${source.summaryPath}#Aggregate Matrix; ${PATHS.w6runner}#EDGE_TYPES/SAMPLES/REPEATS`,
        caveat: "Only SF10 and SF100; sample counts differ (1000 vs 5000 per type), run commits/binary hashes are not tied, and no four-point or strict-doubling curve exists.",
      };
      const prov = (column) => `${source.summaryPath}#Aggregate Matrix row=${variant} column=${column}`;
      addMetric(rows, base, "ops_per_repeat", ops, "operations", "protocol_count", prov("ops/repeat"));
      addMetric(rows, base, "candidate_l0_per_op_mean", candidates.mean / ops, "segments_per_operation", "derived_from_reported_means", `${prov("candidate L0 mean+/-std")} / ${prov("ops/repeat")}`);
      addMetric(rows, base, "read_bytes_per_op_mean", readBytes.mean / ops, "bytes_per_operation", "derived_from_reported_means", `${prov("read bytes mean+/-std")} / ${prov("ops/repeat")}`, { plot_eligibility: "DIAGNOSTIC_SECONDARY_READER_OVERREAD" });
      addMetric(rows, base, "avg_latency_us_mean", avg.mean, "us_per_operation", "mean_across_same_store_repeats", prov("avg us mean+/-std"));
      addMetric(rows, base, "p99_edge_mean_us", p99.mean, "us", "mean_of_per_edge_type_percentiles", prov("p50/p90/p99 edge-mean us"));
      addMetric(rows, base, "store_size_gib_reported", parseNumber(row["store GiB"]), "GiB_reported", "single_import", prov("store GiB"));
      addMetric(rows, base, "l0_files", parseNumber(row["L0 files"]), "files", "single_import", prov("L0 files"));
      addMetric(rows, base, "import_max_rss_gib_reported", parseNumber(rss?.["max RSS GiB"]), "GiB_reported", "single_import_time_v", `${source.summaryPath}#Import RSS row=${variant} column=max RSS GiB`);
    }
  }
  return rows;
}

function registryRows() {
  const w6sf10Protocol = "SF10 outgoing typed-neighbor; edge types 1,2,3,7,8,9,10,11,12; 1000 sampled sources/type; 3 same-store repeats; 9000 ops/repeat";
  const w6sf100Protocol = "SF100 outgoing typed-neighbor; edge types 1,2,3,7,8,9,10,11,12; 5000 sampled sources/type; 3 same-store repeats; 45000 ops/repeat";
  const rows = [
    ["w6_sf10_fig1", "E01", "fig1-end-to-end", "summary+partial_raw", PATHS.w6sf10, "/data/WorkSpace/lsmgraph-rs/remote-logs/w6-sf10-priority-20260709", "", "", "", w6sf10Protocol, "LOCAL summary + manifest + DONE + 9 import time-v logs; bench/compare JSON remote-only", "PASS 9000 per non-anchor variant", "RERUN", "External rows do not share SemL0 query list/type set/API/timing boundary."],
    ["w6_sf10_fig2", "E03", "fig2-component-ablation", "summary+partial_raw", PATHS.w6sf10, "/data/WorkSpace/lsmgraph-rs/remote-logs/w6-sf10-priority-20260709", "", "", "", w6sf10Protocol, "LOCAL summary + import raw; bench/compare JSON remote-only", "PASS 9000 per non-anchor variant", "FIX", "Precomposed layout variants are not a full one-switch staircase; git SHA/binary hash/hardware manifest absent."],
    ["w6_sf10_fig3", "E04", "fig3-resource-tradeoff", "raw_time_v+manifest", PATHS.w6manifest, "/data/WorkSpace/lsmgraph-rs/remote-logs/w6-sf10-priority-20260709", "", "", "", "single SF10 import per variant under /usr/bin/time -v", "LOCAL manifest + DONE + 9 import time-v logs", "exit_status=0 for 9 imports", "FIX", "Import resource only; no read CPU/op/PSS/perf/compaction. Elapsed must parse full m:ss, not suffix after colon."],
    ["w6_sf10_old_resource_derived", "E04", "fig3-resource-tradeoff", "derived_tsv_with_parser_bug", PATHS.w6oldResource, "", "", "", "", "derived from W6 SF10 time-v logs", "LOCAL derived TSV", "not a correctness artifact", "FIX", "Known parser defect: raw 3:19.49 was truncated to 19.49. Excluded from normalized candidates."],
    ["w6_sf10_fig6", "E07", "fig6-scalability", "summary", PATHS.w6sf10, "/data/WorkSpace/lsmgraph-rs/remote-logs/w6-sf10-priority-20260709", "", "", "", w6sf10Protocol, "PARTIAL local raw", "PASS", "RERUN", "Only one of two scale points; no common commit/binary manifest."],
    ["w6_sf100_fig2", "E03", "fig2-component-ablation", "summary", PATHS.w6sf100, "/data/WorkSpace/lsmgraph-rs/remote-logs/w6-sf100-matrix-20260613-132325", "", "", "", w6sf100Protocol, "LOCAL summary; raw JSON/TSV remote-only", "PASS 45000 per non-anchor variant", "REUSE", "Reuse only for layout-matrix facts; same-store repeats, Gate-1 latency fallback, read-byte overread caveat."],
    ["w6_sf100_fig3", "E04", "fig3-resource-tradeoff", "summary", PATHS.w6sf100, "/data/WorkSpace/lsmgraph-rs/remote-logs/w6-sf100-matrix-20260613-132325", "", "", "", "single SF100 import per variant", "LOCAL summary; raw time-v remote-only", "PASS", "FIX", "Only rounded store/L0/import RSS locally summarized; structured CPU and exact disk breakdown absent."],
    ["w6_sf100_fig6", "E07", "fig6-scalability", "summary", PATHS.w6sf100, "/data/WorkSpace/lsmgraph-rs/remote-logs/w6-sf100-matrix-20260613-132325", "", "", "", w6sf100Protocol, "LOCAL summary; raw remote-only", "PASS", "RERUN", "SF10->SF100 is a two-point diagnostic, not a scaling curve; commit identity not tied."],
    ["c2_layer_a_fig4", "E05", "fig4-dynamic-compaction", "paper_table_from_remote_raw", PATHS.c2table, "/data/WorkSpace/lsmgraph-rs/baseline/path-b-c2/stage4-smoke-sf1-20260618.json; /data/WorkSpace/lsmgraph-rs/baseline/path-b-c2/stage5-scale-sf10class-20260618.json", "", "", "", "controlled SF1/SF10-class typed-neighbor full-read before/after merge", "LOCAL paper table; raw JSON remote-only", "mismatches=0", "REUSE", "Controlled synthetic evidence; do not relabel as real LDBC."],
    ["c2_layer_b_fig4", "E05", "fig4-dynamic-compaction", "paper_table_from_remote_raw", PATHS.c2table, "/data/WorkSpace/lsmgraph-rs/remote-logs/c2-sf30-real-20260619/{naive,semantic}.json", "", "", "", "real SF30 open-mode merge; retention and write cost", "LOCAL paper table; raw JSON remote-only", "engine losslessness tests; no per-run query digest", "REUSE", "No full read workload in Layer B."],
    ["c2_layer_c_fig4", "E05", "fig4-dynamic-compaction", "summary_from_remote_raw", PATHS.c2summary, "/data/WorkSpace/lsmgraph-rs/remote-logs/c2-sf30-readamp-proxy-20260621/{naive,semantic}-proxy.json", "", "", "", "real SF30 metadata replay over 40 typed-neighbor partitions", "LOCAL summary; raw proxy JSON remote-only", "not a full query digest run", "REUSE", "6.52x is metadata candidate-byte proxy, not latency or body-read speedup."],
    ["w9_fig4", "E05", "fig4-dynamic-compaction", "summary", PATHS.w9summary, "/data/WorkSpace/lsmgraph-rs/remote-logs/w9-steady-state-formal-20260615-1200", "", "", "", "SF30 W8 stores; 1800s/variant; 300s checkpoints; target 200 query/s + 200 write/s; flush every 1024; compaction interval 0", "LOCAL summary; formal JSONL remote-only; local smoke JSONL is not the formal run", "writer_errors=0; no query digest", "RERUN", "COMPACT_EVERY_SECS=0; one sequential run; cannot support closed-loop compaction claim."],
    ["w8_fig5", "E06", "fig5-workload-coverage", "summary+complete_local_raw", PATHS.w8summary, "/data/WorkSpace/lsmgraph-rs/remote-logs/w8-property-2hop-20260614-2025", "", "", "", "SF30 edge_type=1; 5000 sampled sources; 3 same-store repeats; warmup=1; property predicates and two-hop fanout=64", "LOCAL 15 wrapped benchmark JSON + 3 import JSON + DONE + launcher log", "no query-level digest", "FIX", "Equality is prototype; two-hop candidate count worsens; add digest/current-version confirmation."],
    ["w13_fig5", "E09", "fig5-workload-coverage", "summary", PATHS.w13summary, "/data/WorkSpace/lsmgraph-rs/remote-logs/w13-schema-evolution-20260614-0004", "", "", "", "10 selected deterministic engine tests; per-test timeout 1800s", "LOCAL summary; tests.tsv/run.meta/per-test logs remote-only", "10/10 pass", "REUSE", "Bounded additive/fixed-width/schema-epoch correctness only; no migration/reclamation/random stress."],
    ["w13_supplemental", "E09", "fig5-workload-coverage", "inventory_note", PATHS.w13inventory, "/data/WorkSpace/lsmgraph-rs/remote-logs/w13-schema-evolution-20260615-143649", "", "", "", "same 10-test runner, accidentally triggered by --help", "LOCAL inventory note; raw run remote-only", "10/10 pass", "FIX", "Supplemental sanity only; primary W13 remains canonical."],
    ["external_livegraph_fig1", "E01", "fig1-end-to-end", "local_raw+aggregate", PATHS.livegraphMetrics, "/data/WorkSpace/lsmgraph-rs/baseline/external-baselines-20260624/livegraph", "eea5a40ce6ee443f901e44323c1119f59113eaf3", "driver_md5=40d8bd8d23dd2638eea8ef8bda5cce39; digest_driver_md5=f27799e3635701a7e18e52bccc7733f2", "finbench; Xeon Platinum 8163; 64 vCPU; 503GiB; /dev/vdb1 /data", "SF10 34 signed edge types; nominal 1000 samples/type; 32140 checked; seed=42", "LOCAL raw + config + DONE + digest", "PASS mismatches=0", "RERUN", "Scope and operation stream do not match SemL0 W6; SemL0 repo was dirty at c2aa16c6 during external-driver record."],
    ["external_aster_rocksgraph_fig1", "E01", "fig1-end-to-end", "local_result+aggregate", PATHS.asterResult, "/data/WorkSpace/lsmgraph-rs/baseline/external-baselines-20260626/3plus3-baselines/systems/aster/sf10-main-r1", "6abb258e577c479325092a8ac0e7691fdfd154c2", "", "", "truth-s50-seed42; 34 types x 50 = 1700; compact logical vertex bridge", "LOCAL result.json + DONE; build log local", "PASS mismatches=0", "RERUN", "Typed-neighbor bridge, not full AsterDB/Gremlin; hardware and exact matched SemL0 stream absent."],
    ["external_neo4j_community_fig1", "E01", "fig1-end-to-end", "aggregate_only_local", PATHS.externalMetrics, "/data/WorkSpace/lsmgraph-rs/baseline/external-baselines-20260626/3plus3-baselines/systems/neo4j/sf10-main", "neo4j:5.26.24", "", "", "truth-s50-seed42; 34 relationship types x 50 = 1700; Cypher/client path", "LOCAL aggregate metrics/correctness only; raw run remote-only", "PASS mismatches=0", "RERUN", "Client/API/timing differs; local raw missing; peak RSS missing."],
    ["external_tugraph_fig1", "E01", "fig1-end-to-end", "aggregate_only_local", PATHS.externalMetrics, "/data/WorkSpace/lsmgraph-rs/baseline/external-baselines-20260626/3plus3-baselines/systems/tugraph/sf10-main-r1", "", "", "", "truth-s50-seed42; 1700; embedded C++ API", "LOCAL aggregate metrics/correctness only; raw run remote-only", "PASS mismatches=0", "RERUN", "Version/hardware not recorded locally; API/timing differs from SemL0."],
    ["external_nebulagraph_fig1", "E01", "fig1-end-to-end", "local_result+aggregate", PATHS.nebulaResult, "/data/WorkSpace/lsmgraph-rs/baseline/external-baselines-20260626/3plus3-baselines/systems/nebulagraph/sf10-main-r1", "v3.8.0", "graphd=sha256:1040573cc684ea6cc5e673b667422e9abab607c9d553c2c17c7bac6ad8a74e05; metad=sha256:ab687bd32d3e441d436842b41427ba0961a5e169c46997ef03fa4dcfd5388b35; storaged=sha256:7142642ee69001a5b5c50520196538d58bb2ec3930707c067cb4c4e2be3890f9", "", "truth-s50-seed42; 1700; nGQL edge-type model; batch=500", "LOCAL result.json + DONE", "PASS mismatches=0", "RERUN", "nGQL/client/server boundary differs; hardware not attached to result."],
  ];
  return rows.map(([source_id, experiment_id, figure_id, source_kind, local_path, remote_path, git_sha_or_version, binary_hash, hardware, query_protocol, raw_status, correctness_gate, verdict, caveat]) => ({
    source_id,
    experiment_id,
    figure_id,
    source_kind,
    local_path: rel(local_path),
    remote_path,
    local_sha256: sha256(local_path),
    git_sha_or_version,
    binary_hash,
    hardware,
    query_protocol,
    raw_status,
    correctness_gate,
    verdict,
    caveat,
  }));
}

const outputs = {
  "normalized-candidate-fig1-end-to-end.tsv": buildFig1(),
  "normalized-candidate-fig2-component-ablation.tsv": buildFig2(),
  "normalized-candidate-fig3-resource-tradeoff.tsv": buildFig3(),
  "normalized-candidate-fig4-dynamic-compaction.tsv": buildFig4(),
  "normalized-candidate-fig5-workload-coverage.tsv": buildFig5(),
  "normalized-candidate-fig6-scalability.tsv": buildFig6(),
};

const registry = registryRows();
const registeredSourceIds = new Set(registry.map((row) => row.source_id));
for (const [filename, rows] of Object.entries(outputs)) {
  const recordIds = new Set();
  for (const row of rows) {
    if (!registeredSourceIds.has(row.source_id)) throw new Error(`${filename}: unregistered source_id=${row.source_id}`);
    if (!Number.isFinite(Number(row.value))) throw new Error(`${filename}: non-numeric value record_id=${row.record_id}`);
    if (!row.value_provenance || !row.protocol_provenance) throw new Error(`${filename}: missing provenance record_id=${row.record_id}`);
    if (!new Set(["REUSE", "FIX", "RERUN"]).has(row.verdict)) throw new Error(`${filename}: bad verdict record_id=${row.record_id}`);
    if (recordIds.has(row.record_id)) throw new Error(`${filename}: duplicate record_id=${row.record_id}`);
    recordIds.add(row.record_id);
  }
}

writeTsv("EVIDENCE-REGISTRY.tsv", [
  "source_id",
  "experiment_id",
  "figure_id",
  "source_kind",
  "local_path",
  "remote_path",
  "local_sha256",
  "git_sha_or_version",
  "binary_hash",
  "hardware",
  "query_protocol",
  "raw_status",
  "correctness_gate",
  "verdict",
  "caveat",
], registry);

for (const [filename, rows] of Object.entries(outputs)) {
  writeTsv(filename, CANDIDATE_HEADERS, rows);
}

const counts = Object.fromEntries(Object.entries(outputs).map(([name, rows]) => [name, rows.length]));
fs.writeFileSync(path.join(OUT_DIR, "build-summary.json"), JSON.stringify({ generated_at: new Date().toISOString(), registry_rows: registry.length, candidate_rows: counts }, null, 2) + "\n", "utf8");
console.log(JSON.stringify({ registry_rows: registry.length, candidate_rows: counts }, null, 2));
