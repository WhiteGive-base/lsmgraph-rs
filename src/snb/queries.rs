use std::cell::RefCell;
use std::cmp::Reverse;
use std::collections::BinaryHeap;
use std::collections::{BTreeSet, HashMap, HashSet, VecDeque};
use std::path::Path;
use std::sync::Arc;
use std::time::Instant;

use serde::Serialize;
use serde_json::{json, Value};

use crate::base_graph::IoConfig;
use crate::config::IoBackendKind;
use crate::error::Result;
use crate::graph::Engine;
use crate::snb::props::{
    encode_vid, CommentProps, EdgeProp, ForumProps, MessageRef, PersonProps, PostProps, SnbGraph,
    VertexData,
};
use crate::types::{EdgeLabel, VertexId, VertexLabel};

const DAY_MS: i64 = 86_400_000;

#[derive(Debug, Serialize)]
pub struct ValidationReport {
    pub validation_params: String,
    pub max_lines: usize,
    pub checked: usize,
    pub passed: usize,
    pub skipped: usize,
    pub failed: usize,
    pub elapsed_ms: u64,
    pub query_latency_us: QueryLatencyReport,
    pub storage_metrics: Value,
    pub supported_queries: Vec<&'static str>,
    pub first_failure: Option<Value>,
}

#[derive(Debug, Default, Serialize)]
pub struct QueryLatencyReport {
    pub count: usize,
    pub sum_us: u64,
    pub avg_us: u64,
    pub min_us: u64,
    pub p50_us: u64,
    pub p90_us: u64,
    pub p99_us: u64,
    pub max_us: u64,
}

fn summarize_query_latencies(mut values: Vec<u64>) -> QueryLatencyReport {
    if values.is_empty() {
        return QueryLatencyReport::default();
    }
    values.sort_unstable();
    let count = values.len();
    let sum_us = values.iter().sum();
    let percentile = |pct: usize| -> u64 {
        let idx = ((count * pct).div_ceil(100)).saturating_sub(1);
        values[idx.min(count - 1)]
    };
    QueryLatencyReport {
        count,
        sum_us,
        avg_us: sum_us / count as u64,
        min_us: values[0],
        p50_us: percentile(50),
        p90_us: percentile(90),
        p99_us: percentile(99),
        max_us: values[count - 1],
    }
}

#[derive(Debug, Serialize)]
pub struct BatchValidationReport {
    pub validation_dir: String,
    pub max_lines_per_query: usize,
    pub reports: Vec<ValidationReport>,
    pub checked: usize,
    pub passed: usize,
    pub skipped: usize,
    pub failed: usize,
}

#[derive(Debug, Serialize)]
pub struct MixedValidationReport {
    pub validation_params: String,
    pub max_lines: usize,
    pub processed: usize,
    pub reads_checked: usize,
    pub reads_passed: usize,
    pub updates_applied: usize,
    pub skipped: usize,
    pub failed: usize,
    pub supported_queries: Vec<&'static str>,
    pub first_failure: Option<Value>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct OrgJson {
    #[serde(rename = "organizationName")]
    organization_name: String,
    year: i32,
    #[serde(rename = "placeName")]
    place_name: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct Ic1Row {
    #[serde(rename = "friendId")]
    friend_id: i64,
    #[serde(rename = "friendLastName")]
    friend_last_name: String,
    distance_from_person: i32,
    #[serde(rename = "friendBirthday")]
    friend_birthday: i64,
    #[serde(rename = "friendCreationDate")]
    friend_creation_date: i64,
    #[serde(rename = "friendGender")]
    friend_gender: String,
    #[serde(rename = "friendBrowserUsed")]
    friend_browser_used: String,
    #[serde(rename = "friendLocationIp")]
    friend_location_ip: String,
    #[serde(rename = "friendEmails")]
    friend_emails: Vec<String>,
    #[serde(rename = "friendLanguages")]
    friend_languages: Vec<String>,
    #[serde(rename = "friendCityName")]
    friend_city_name: String,
    #[serde(rename = "friendUniversities")]
    friend_universities: Vec<OrgJson>,
    #[serde(rename = "friendCompanies")]
    friend_companies: Vec<OrgJson>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct Ic2Row {
    person_id: i64,
    person_first_name: String,
    person_last_name: String,
    message_id: i64,
    message_content: String,
    message_creation_date: i64,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct Ic3Row {
    person_id: i64,
    person_first_name: String,
    person_last_name: String,
    x_count: i64,
    y_count: i64,
    count: i64,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct Ic4Row {
    tag_name: String,
    post_count: i64,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct Ic5Row {
    forum_title: String,
    post_count: i64,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct Ic6Row {
    tag_name: String,
    post_count: i64,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct Ic7Row {
    person_id: i64,
    person_first_name: String,
    person_last_name: String,
    like_creation_date: i64,
    message_id: i64,
    message_content: String,
    minutes_latency: i64,
    is_new: bool,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct Ic8Row {
    person_id: i64,
    person_first_name: String,
    person_last_name: String,
    comment_creation_date: i64,
    comment_id: i64,
    comment_content: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct Ic9Row {
    person_id: i64,
    person_first_name: String,
    person_last_name: String,
    message_id: i64,
    message_content: String,
    message_creation_date: i64,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct Ic10Row {
    person_id: i64,
    person_first_name: String,
    person_last_name: String,
    common_interest_score: i64,
    person_gender: String,
    person_city_name: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct Ic11Row {
    person_id: i64,
    person_first_name: String,
    person_last_name: String,
    organization_name: String,
    organization_work_from_year: i32,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct Ic12Row {
    person_id: i64,
    person_first_name: String,
    person_last_name: String,
    tag_names: Vec<String>,
    reply_count: i32,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct Ic13Row {
    shortest_path_length: i32,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct Ic14Row {
    person_ids_in_path: Vec<i64>,
    path_weight: f64,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct Is1Row {
    first_name: String,
    last_name: String,
    birthday: i64,
    location_ip: String,
    browser_used: String,
    city_id: i64,
    gender: String,
    creation_date: i64,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct Is2Row {
    message_id: i64,
    message_content: String,
    message_creation_date: i64,
    original_post_id: i64,
    original_post_author_id: i64,
    original_post_author_first_name: String,
    original_post_author_last_name: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct Is3Row {
    person_id: i64,
    first_name: String,
    last_name: String,
    friendship_creation_date: i64,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct Is4Row {
    message_creation_date: i64,
    message_content: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct Is5Row {
    person_id: i64,
    first_name: String,
    last_name: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct Is6Row {
    forum_id: i64,
    forum_title: String,
    moderator_id: i64,
    moderator_first_name: String,
    moderator_last_name: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct Is7Row {
    comment_id: i64,
    comment_content: String,
    comment_creation_date: i64,
    reply_author_id: i64,
    reply_author_first_name: String,
    reply_author_last_name: String,
    #[serde(rename = "isReplyAuthorKnowsOriginalMessageAuthor")]
    reply_author_knows_original_message_author: bool,
}

pub async fn validate_ic1_ic14(
    engine: Arc<Engine>,
    store_dir: &Path,
    validation_params: &Path,
    max_lines: usize,
) -> Result<ValidationReport> {
    let snb = SnbGraph::open(engine, store_dir).await?;
    validate_file_with_snb(&snb, validation_params, max_lines, None)
}

pub async fn validate_ic1_ic14_dynamic(
    store_dir: &Path,
    base_io: IoConfig,
    io_backend: IoBackendKind,
    validation_params: &Path,
    max_lines: usize,
) -> Result<ValidationReport> {
    let snb = SnbGraph::open_dynamic(store_dir, base_io, io_backend).await?;
    validate_file_with_snb(&snb, validation_params, max_lines, None)
}

pub async fn validate_ic_batch(
    engine: Arc<Engine>,
    store_dir: &Path,
    validation_dir: &Path,
    queries: &[String],
    max_lines_per_query: usize,
) -> Result<BatchValidationReport> {
    let snb = SnbGraph::open(engine, store_dir).await?;
    let mut reports = Vec::new();
    for query in queries {
        let query = query.trim().to_ascii_lowercase();
        if query.is_empty() {
            continue;
        }
        let path = validation_dir.join(format!("validation_params_{query}.csv"));
        if !path.exists() {
            reports.push(ValidationReport {
                validation_params: path.display().to_string(),
                max_lines: max_lines_per_query,
                checked: 0,
                passed: 0,
                skipped: 0,
                failed: 1,
                elapsed_ms: 0,
                query_latency_us: QueryLatencyReport::default(),
                storage_metrics: Value::Null,
                supported_queries: supported_query_names(),
                first_failure: Some(json!({
                    "error": "validation split file not found",
                    "query": query,
                    "path": path,
                })),
            });
            continue;
        }
        reports.push(validate_file_with_snb(
            &snb,
            &path,
            max_lines_per_query,
            Some(&query),
        )?);
    }

    let checked = reports.iter().map(|r| r.checked).sum();
    let passed = reports.iter().map(|r| r.passed).sum();
    let skipped = reports.iter().map(|r| r.skipped).sum();
    let failed = reports.iter().map(|r| r.failed).sum();
    Ok(BatchValidationReport {
        validation_dir: validation_dir.display().to_string(),
        max_lines_per_query,
        reports,
        checked,
        passed,
        skipped,
        failed,
    })
}

pub async fn validate_ic_batch_dynamic(
    store_dir: &Path,
    base_io: IoConfig,
    io_backend: IoBackendKind,
    validation_dir: &Path,
    queries: &[String],
    max_lines_per_query: usize,
) -> Result<BatchValidationReport> {
    let snb = SnbGraph::open_dynamic(store_dir, base_io, io_backend).await?;
    let mut reports = Vec::new();
    for query in queries {
        let query = query.trim().to_ascii_lowercase();
        if query.is_empty() {
            continue;
        }
        let path = validation_dir.join(format!("validation_params_{query}.csv"));
        if !path.exists() {
            reports.push(ValidationReport {
                validation_params: path.display().to_string(),
                max_lines: max_lines_per_query,
                checked: 0,
                passed: 0,
                skipped: 0,
                failed: 1,
                elapsed_ms: 0,
                query_latency_us: QueryLatencyReport::default(),
                storage_metrics: Value::Null,
                supported_queries: supported_query_names(),
                first_failure: Some(json!({
                    "error": "validation split file not found",
                    "query": query,
                    "path": path,
                })),
            });
            continue;
        }
        reports.push(validate_file_with_snb(
            &snb,
            &path,
            max_lines_per_query,
            Some(&query),
        )?);
    }

    let checked = reports.iter().map(|r| r.checked).sum();
    let passed = reports.iter().map(|r| r.passed).sum();
    let skipped = reports.iter().map(|r| r.skipped).sum();
    let failed = reports.iter().map(|r| r.failed).sum();
    Ok(BatchValidationReport {
        validation_dir: validation_dir.display().to_string(),
        max_lines_per_query,
        reports,
        checked,
        passed,
        skipped,
        failed,
    })
}

pub async fn validate_mixed_tugraph(
    engine: Arc<Engine>,
    store_dir: &Path,
    validation_params: &Path,
    max_lines: usize,
) -> Result<MixedValidationReport> {
    let mut snb = SnbGraph::open(engine, store_dir).await?;
    validate_mixed_file_with_snb(&mut snb, validation_params, max_lines)
}

pub async fn validate_mixed_tugraph_dynamic(
    store_dir: &Path,
    base_io: IoConfig,
    io_backend: IoBackendKind,
    validation_params: &Path,
    max_lines: usize,
) -> Result<MixedValidationReport> {
    let mut snb = SnbGraph::open_dynamic(store_dir, base_io, io_backend).await?;
    validate_mixed_file_with_snb(&mut snb, validation_params, max_lines)
}

fn validate_mixed_file_with_snb(
    snb: &mut SnbGraph,
    validation_params: &Path,
    max_lines: usize,
) -> Result<MixedValidationReport> {
    let text = std::fs::read_to_string(validation_params)?;
    let mut processed = 0usize;
    let mut reads_checked = 0usize;
    let mut reads_passed = 0usize;
    let mut updates_applied = 0usize;
    let mut skipped = 0usize;
    let mut failed = 0usize;
    let mut first_failure = None;

    for (line_no, line) in text.lines().enumerate() {
        if max_lines != 0 && processed >= max_lines {
            break;
        }
        let Some((params_raw, expected_raw)) = line.split_once('|') else {
            continue;
        };
        processed += 1;
        let params: Value = serde_json::from_str(params_raw)?;
        let expected: Value = serde_json::from_str(expected_raw)?;
        if expected.as_str() == Some("-1") {
            match update_endpoint_for_params(&params) {
                Some(endpoint) => {
                    run_dgs_http_update(snb, endpoint, &params)?;
                    updates_applied += 1;
                }
                None => {
                    skipped += 1;
                }
            }
            continue;
        }

        let (query_name, actual) = match dispatch_query(snb, &params, None)? {
            Some((query_name, value)) => {
                reads_checked += 1;
                (query_name, value)
            }
            None => {
                skipped += 1;
                continue;
            }
        };

        if values_equal(query_name, &actual, &expected) {
            reads_passed += 1;
        } else {
            failed += 1;
            if first_failure.is_none() {
                first_failure = Some(json!({
                    "line": line_no + 1,
                    "params": params,
                    "expected": expected,
                    "actual": actual,
                }));
            }
        }

        if processed % 1000 == 0 {
            eprintln!(
                "[snb-validate-mixed] processed={processed} reads_checked={reads_checked} reads_passed={reads_passed} updates_applied={updates_applied} skipped={skipped} failed={failed}"
            );
        }
    }

    Ok(MixedValidationReport {
        validation_params: validation_params.display().to_string(),
        max_lines,
        processed,
        reads_checked,
        reads_passed,
        updates_applied,
        skipped,
        failed,
        supported_queries: supported_query_names(),
        first_failure,
    })
}

fn validate_file_with_snb(
    snb: &SnbGraph,
    validation_params: &Path,
    max_lines: usize,
    query_hint: Option<&str>,
) -> Result<ValidationReport> {
    let text = std::fs::read_to_string(validation_params)?;
    snb.reset_storage_metrics();
    let validation_started = Instant::now();
    let mut query_latencies = Vec::new();
    let mut checked = 0usize;
    let mut passed = 0usize;
    let mut skipped = 0usize;
    let mut failed = 0usize;
    let mut first_failure = None;

    for (line_no, line) in text.lines().enumerate() {
        if max_lines != 0 && checked >= max_lines {
            break;
        }
        let Some((params_raw, expected_raw)) = line.split_once('|') else {
            continue;
        };
        let params: Value = serde_json::from_str(params_raw)?;
        let expected: Value = serde_json::from_str(expected_raw)?;
        let query_started = Instant::now();
        let (query_name, actual) = match dispatch_query(&snb, &params, query_hint)? {
            Some((query_name, value)) => {
                query_latencies.push(query_started.elapsed().as_micros() as u64);
                checked += 1;
                (query_name, value)
            }
            None => {
                skipped += 1;
                continue;
            }
        };

        if values_equal(query_name, &actual, &expected) {
            passed += 1;
        } else {
            failed += 1;
            if first_failure.is_none() {
                first_failure = Some(json!({
                    "line": line_no + 1,
                    "params": params,
                    "expected": expected,
                    "actual": actual,
                }));
            }
        }
    }
    let elapsed_ms = validation_started.elapsed().as_millis() as u64;
    let storage_metrics = snb.storage_metrics_snapshot_json();

    Ok(ValidationReport {
        validation_params: validation_params.display().to_string(),
        max_lines,
        checked,
        passed,
        skipped,
        failed,
        elapsed_ms,
        query_latency_us: summarize_query_latencies(query_latencies),
        storage_metrics,
        supported_queries: supported_query_names(),
        first_failure,
    })
}

pub fn supported_query_names() -> Vec<&'static str> {
    vec![
        "IC1", "IC2", "IC3", "IC4", "IC5", "IC6", "IC7", "IC8", "IC9", "IC10", "IC11", "IC12",
        "IC13", "IC14", "IS1", "IS2", "IS3", "IS4", "IS5", "IS6", "IS7", "IU1", "IU2", "IU3",
        "IU4", "IU5", "IU6", "IU7", "IU8",
    ]
}

fn update_endpoint_for_params(params: &Value) -> Option<&'static str> {
    if (params.get("firstName").is_some() || params.get("personFirstName").is_some())
        && (params.get("lastName").is_some() || params.get("personLastName").is_some())
        && params.get("birthday").is_some()
    {
        Some("/query/interactive_update_1")
    } else if params.get("postId").is_some() && params.get("personId").is_some() {
        Some("/query/interactive_update_2")
    } else if params.get("commentId").is_some()
        && params.get("personId").is_some()
        && params.get("creationDate").is_some()
    {
        Some("/query/interactive_update_3")
    } else if params.get("forumTitle").is_some() {
        Some("/query/interactive_update_4")
    } else if params.get("forumId").is_some()
        && params.get("personId").is_some()
        && params.get("joinDate").is_some()
    {
        Some("/query/interactive_update_5")
    } else if params.get("postId").is_some() && params.get("authorPersonId").is_some() {
        Some("/query/interactive_update_6")
    } else if params.get("commentId").is_some() && params.get("authorPersonId").is_some() {
        Some("/query/interactive_update_7")
    } else if params.get("person1Id").is_some() && params.get("person2Id").is_some() {
        Some("/query/interactive_update_8")
    } else {
        None
    }
}

fn dispatch_query(
    snb: &SnbGraph,
    params: &Value,
    query_hint: Option<&str>,
) -> Result<Option<(&'static str, Value)>> {
    let limit = || params["limit"].as_u64().unwrap_or(20) as usize;
    let hint = query_hint.unwrap_or_default().to_ascii_lowercase();
    let item = if hint == "is1" || params.get("personIdSQ1").is_some() {
        (
            "IS1",
            serde_json::to_value(is1(
                snb,
                req_i64_any(params, &["personIdSQ1", "personIdQ1", "personId"]),
            ))?,
        )
    } else if hint == "is2" || params.get("personIdSQ2").is_some() {
        (
            "IS2",
            serde_json::to_value(is2(
                snb,
                req_i64_any(params, &["personIdSQ2", "personIdQ2", "personId"]),
                limit().min(10),
            ))?,
        )
    } else if hint == "is3" || params.get("personIdSQ3").is_some() {
        (
            "IS3",
            serde_json::to_value(is3(
                snb,
                req_i64_any(params, &["personIdSQ3", "personIdQ3", "personId"]),
            ))?,
        )
    } else if hint == "is4" || params.get("messageIdContent").is_some() {
        (
            "IS4",
            serde_json::to_value(is4(
                snb,
                req_i64_any(params, &["messageIdContent", "messageIdQ4", "messageId"]),
            ))?,
        )
    } else if hint == "is5" || params.get("messageIdCreator").is_some() {
        (
            "IS5",
            serde_json::to_value(is5(
                snb,
                req_i64_any(params, &["messageIdCreator", "messageIdQ5", "messageId"]),
            ))?,
        )
    } else if hint == "is6" || params.get("messageForumId").is_some() {
        (
            "IS6",
            serde_json::to_value(is6(
                snb,
                req_i64_any(params, &["messageForumId", "messageIdQ6", "messageId"]),
            ))?,
        )
    } else if hint == "is7" || params.get("messageRepliesId").is_some() {
        (
            "IS7",
            serde_json::to_value(is7(
                snb,
                req_i64_any(params, &["messageRepliesId", "messageIdQ7", "messageId"]),
            ))?,
        )
    } else if params.get("personIdQ1").is_some() {
        (
            "IC1",
            serde_json::to_value(ic1(
                snb,
                req_i64(params, "personIdQ1"),
                req_str(params, "firstName"),
                limit(),
            ))?,
        )
    } else if params.get("personIdQ2").is_some() {
        (
            "IC2",
            serde_json::to_value(ic2(
                snb,
                req_i64(params, "personIdQ2"),
                req_i64(params, "maxDate"),
                limit(),
            ))?,
        )
    } else if params.get("personIdQ3").is_some() {
        (
            "IC3",
            serde_json::to_value(ic3(
                snb,
                req_i64(params, "personIdQ3"),
                req_str(params, "countryXName"),
                req_str(params, "countryYName"),
                req_i64(params, "startDate"),
                req_i64(params, "durationDays"),
                limit(),
            ))?,
        )
    } else if params.get("personIdQ4").is_some() {
        (
            "IC4",
            serde_json::to_value(ic4(
                snb,
                req_i64(params, "personIdQ4"),
                req_i64(params, "startDate"),
                req_i64(params, "durationDays"),
                limit(),
            ))?,
        )
    } else if params.get("personIdQ5").is_some() {
        (
            "IC5",
            serde_json::to_value(ic5(
                snb,
                req_i64(params, "personIdQ5"),
                req_i64(params, "minDate"),
                limit(),
            ))?,
        )
    } else if params.get("personIdQ6").is_some() {
        (
            "IC6",
            serde_json::to_value(ic6(
                snb,
                req_i64(params, "personIdQ6"),
                req_str(params, "tagName"),
                limit(),
            ))?,
        )
    } else if params.get("personIdQ7").is_some() {
        (
            "IC7",
            serde_json::to_value(ic7(snb, req_i64(params, "personIdQ7"), limit()))?,
        )
    } else if params.get("personIdQ8").is_some() {
        (
            "IC8",
            serde_json::to_value(ic8(snb, req_i64(params, "personIdQ8"), limit()))?,
        )
    } else if params.get("personIdQ9").is_some() {
        (
            "IC9",
            serde_json::to_value(ic9(
                snb,
                req_i64(params, "personIdQ9"),
                req_i64(params, "maxDate"),
                limit(),
            ))?,
        )
    } else if params.get("personIdQ10").is_some() {
        (
            "IC10",
            serde_json::to_value(ic10(
                snb,
                req_i64(params, "personIdQ10"),
                req_i64(params, "month") as u32,
                limit(),
            ))?,
        )
    } else if params.get("personIdQ11").is_some() {
        (
            "IC11",
            serde_json::to_value(ic11(
                snb,
                req_i64(params, "personIdQ11"),
                req_str(params, "countryName"),
                req_i64(params, "workFromYear") as i32,
                limit(),
            ))?,
        )
    } else if params.get("personIdQ12").is_some() {
        (
            "IC12",
            serde_json::to_value(ic12(
                snb,
                req_i64(params, "personIdQ12"),
                req_str(params, "tagClassName"),
                limit(),
            ))?,
        )
    } else if params.get("person1IdQ13StartNode").is_some() {
        (
            "IC13",
            serde_json::to_value(ic13(
                snb,
                req_i64(params, "person1IdQ13StartNode"),
                req_i64(params, "person2IdQ13EndNode"),
            ))?,
        )
    } else if params.get("person1IdQ14StartNode").is_some() {
        (
            "IC14",
            serde_json::to_value(ic14(
                snb,
                req_i64(params, "person1IdQ14StartNode"),
                req_i64(params, "person2IdQ14EndNode"),
            ))?,
        )
    } else {
        return Ok(None);
    };
    Ok(Some(item))
}

pub fn run_dgs_http_query(snb: &SnbGraph, endpoint: &str, params: &Value) -> Result<Value> {
    let value = match endpoint {
        "/query/interactive_complex_read_1" => {
            let rows = ic1(
                snb,
                req_i64(params, "personIdQ1"),
                req_str(params, "firstName"),
                20,
            );
            Value::Array(
                rows.into_iter()
                    .map(|r| {
                        json!({
                            "personId": r.friend_id,
                            "lastName": r.friend_last_name,
                            "distance": r.distance_from_person,
                            "birthday": r.friend_birthday,
                            "creationDate": r.friend_creation_date,
                            "gender": r.friend_gender,
                            "browserUsed": r.friend_browser_used,
                            "locationIp": r.friend_location_ip,
                            "email": r.friend_emails.join(";"),
                            "language": r.friend_languages.join(";"),
                            "cityName": r.friend_city_name,
                            "universities": org_tuples(r.friend_universities),
                            "companies": org_tuples(r.friend_companies),
                        })
                    })
                    .collect(),
            )
        }
        "/query/interactive_complex_read_2" => {
            let rows = ic2(
                snb,
                req_i64(params, "personIdQ2"),
                req_i64(params, "maxDate"),
                20,
            );
            Value::Array(
                rows.into_iter()
                    .map(|r| {
                        json!({
                            "friendId": r.person_id,
                            "firstName": r.person_first_name,
                            "lastName": r.person_last_name,
                            "messageId": r.message_id,
                            "content": r.message_content,
                            "creationDate": r.message_creation_date,
                        })
                    })
                    .collect(),
            )
        }
        "/query/interactive_complex_read_3" => {
            let rows = ic3(
                snb,
                req_i64(params, "personIdQ3"),
                req_str(params, "countryXName"),
                req_str(params, "countryYName"),
                req_i64(params, "startDate"),
                req_i64(params, "durationDays"),
                20,
            );
            Value::Array(
                rows.into_iter()
                    .map(|r| {
                        json!({
                            "personId": r.person_id,
                            "firstName": r.person_first_name,
                            "lastName": r.person_last_name,
                            "xCount": r.x_count,
                            "yCount": r.y_count,
                            "count": r.count,
                        })
                    })
                    .collect(),
            )
        }
        "/query/interactive_complex_read_4" => serde_json::to_value(ic4(
            snb,
            req_i64(params, "personIdQ4"),
            req_i64(params, "startDate"),
            req_i64(params, "durationDays"),
            10,
        ))?,
        "/query/interactive_complex_read_5" => serde_json::to_value(ic5(
            snb,
            req_i64(params, "personIdQ5"),
            req_i64(params, "minDate"),
            20,
        ))?,
        "/query/interactive_complex_read_6" => serde_json::to_value(ic6(
            snb,
            req_i64(params, "personIdQ6"),
            req_str(params, "tagName"),
            10,
        ))?,
        "/query/interactive_complex_read_7" => {
            let rows = ic7(snb, req_i64(params, "personIdQ7"), 20);
            Value::Array(
                rows.into_iter()
                    .map(|r| {
                        json!({
                            "personId": r.person_id,
                            "firstName": r.person_first_name,
                            "lastName": r.person_last_name,
                            "likeTime": r.like_creation_date,
                            "messageId": r.message_id,
                            "messageContent": r.message_content,
                            "minutesLatency": r.minutes_latency,
                            "isNew": r.is_new,
                        })
                    })
                    .collect(),
            )
        }
        "/query/interactive_complex_read_8" => {
            let rows = ic8(snb, req_i64(params, "personIdQ8"), 20);
            Value::Array(
                rows.into_iter()
                    .map(|r| {
                        json!({
                            "personId": r.person_id,
                            "firstName": r.person_first_name,
                            "lastName": r.person_last_name,
                            "commentCreationDate": r.comment_creation_date,
                            "commentId": r.comment_id,
                            "commentContent": r.comment_content,
                        })
                    })
                    .collect(),
            )
        }
        "/query/interactive_complex_read_9" => {
            let rows = ic9(
                snb,
                req_i64(params, "personIdQ9"),
                req_i64(params, "maxDate"),
                20,
            );
            Value::Array(
                rows.into_iter()
                    .map(|r| {
                        json!({
                            "friendId": r.person_id,
                            "firstName": r.person_first_name,
                            "lastName": r.person_last_name,
                            "messageId": r.message_id,
                            "content": r.message_content,
                            "creationDate": r.message_creation_date,
                        })
                    })
                    .collect(),
            )
        }
        "/query/interactive_complex_read_10" => {
            let rows = ic10(
                snb,
                req_i64(params, "personIdQ10"),
                req_i64(params, "month") as u32,
                10,
            );
            Value::Array(
                rows.into_iter()
                    .map(|r| {
                        json!({
                            "personId": r.person_id,
                            "firstName": r.person_first_name,
                            "lastName": r.person_last_name,
                            "score": r.common_interest_score,
                            "gender": r.person_gender,
                            "cityName": r.person_city_name,
                        })
                    })
                    .collect(),
            )
        }
        "/query/interactive_complex_read_11" => {
            let rows = ic11(
                snb,
                req_i64(params, "personIdQ11"),
                req_str(params, "countryName"),
                req_i64(params, "workFromYear") as i32,
                10,
            );
            Value::Array(
                rows.into_iter()
                    .map(|r| {
                        json!({
                            "personId": r.person_id,
                            "firstName": r.person_first_name,
                            "lastName": r.person_last_name,
                            "organizationName": r.organization_name,
                            "workFromYear": r.organization_work_from_year,
                        })
                    })
                    .collect(),
            )
        }
        "/query/interactive_complex_read_12" => {
            let rows = ic12(
                snb,
                req_i64(params, "personIdQ12"),
                req_str(params, "tagClassName"),
                20,
            );
            Value::Array(
                rows.into_iter()
                    .map(|r| {
                        json!({
                            "personId": r.person_id,
                            "firstName": r.person_first_name,
                            "lastName": r.person_last_name,
                            "tagNames": r.tag_names,
                            "count": r.reply_count,
                        })
                    })
                    .collect(),
            )
        }
        "/query/interactive_complex_read_13" => serde_json::to_value(ic13(
            snb,
            req_i64(params, "person1IdQ13StartNode"),
            req_i64(params, "person2IdQ13EndNode"),
        ))?,
        "/query/interactive_complex_read_14" => serde_json::to_value(ic14(
            snb,
            req_i64(params, "person1IdQ14StartNode"),
            req_i64(params, "person2IdQ14EndNode"),
        ))?,
        "/query/interactive_short_read_1" => serde_json::to_value(is1(
            snb,
            req_i64_any(params, &["personIdSQ1", "personIdQ1", "personId"]),
        ))?,
        "/query/interactive_short_read_2" => Value::Array(
            is2(
                snb,
                req_i64_any(params, &["personIdSQ2", "personIdQ2", "personId"]),
                10,
            )
            .into_iter()
            .map(|r| {
                json!({
                    "messageId": r.message_id,
                    "content": r.message_content,
                    "creationDate": r.message_creation_date,
                    "originalPostId": r.original_post_id,
                    "originalPostAuthorId": r.original_post_author_id,
                    "firstName": r.original_post_author_first_name,
                    "lastName": r.original_post_author_last_name,
                })
            })
            .collect(),
        ),
        "/query/interactive_short_read_3" => serde_json::to_value(is3(
            snb,
            req_i64_any(params, &["personIdSQ3", "personIdQ3", "personId"]),
        ))?,
        "/query/interactive_short_read_4" => match is4(
            snb,
            req_i64_any(params, &["messageIdContent", "messageIdQ4", "messageId"]),
        ) {
            Some(r) => json!({
                "content": r.message_content,
                "creationDate": r.message_creation_date,
            }),
            None => Value::Null,
        },
        "/query/interactive_short_read_5" => serde_json::to_value(is5(
            snb,
            req_i64_any(params, &["messageIdCreator", "messageIdQ5", "messageId"]),
        ))?,
        "/query/interactive_short_read_6" => match is6(
            snb,
            req_i64_any(params, &["messageForumId", "messageIdQ6", "messageId"]),
        ) {
            Some(r) => json!({
                "forumId": r.forum_id,
                "forumTitle": r.forum_title,
                "moderatorPersonId": r.moderator_id,
                "moderatorFirstName": r.moderator_first_name,
                "moderatorLastName": r.moderator_last_name,
            }),
            None => Value::Null,
        },
        "/query/interactive_short_read_7" => Value::Array(
            is7(
                snb,
                req_i64_any(params, &["messageRepliesId", "messageIdQ7", "messageId"]),
            )
            .into_iter()
            .map(|r| {
                json!({
                    "commentId": r.comment_id,
                    "content": r.comment_content,
                    "creationDate": r.comment_creation_date,
                    "replyAuthorId": r.reply_author_id,
                    "firstName": r.reply_author_first_name,
                    "lastName": r.reply_author_last_name,
                    "isKnown": r.reply_author_knows_original_message_author,
                })
            })
            .collect(),
        ),
        _ => anyhow::bail!("unsupported DGS HTTP endpoint: {endpoint}"),
    };
    Ok(value)
}

pub fn run_dgs_http_update(snb: &mut SnbGraph, endpoint: &str, params: &Value) -> Result<Value> {
    match endpoint {
        "/query/interactive_update_1" => {
            let person_id = req_i64(params, "personId");
            let person_vid = encode_vid(VertexLabel::Person, person_id);
            snb.insert_vertex(VertexData::Person(PersonProps {
                id: person_id,
                first_name: req_str_any(params, &["firstName", "personFirstName"]).to_string(),
                last_name: req_str_any(params, &["lastName", "personLastName"]).to_string(),
                gender: req_str(params, "gender").to_string(),
                birthday: req_i64(params, "birthday"),
                creation_date: req_i64(params, "creationDate"),
                location_ip: req_str(params, "locationIp").to_string(),
                browser_used: req_str(params, "browserUsed").to_string(),
                place: req_i64(params, "cityId"),
                language: req_string_or_array_join(params, "language", "languages"),
                email: req_string_or_array_join(params, "email", "emails"),
            }));
            snb.insert_edge_cached(
                person_vid,
                encode_vid(VertexLabel::Place, req_i64(params, "cityId")),
                EdgeLabel::IsLocatedIn,
                EdgeProp::Empty,
                true,
            );
            for tag_id in req_i64_array(params, "tagIds") {
                snb.insert_edge_cached(
                    person_vid,
                    encode_vid(VertexLabel::Tag, tag_id),
                    EdgeLabel::HasInterest,
                    EdgeProp::Empty,
                    true,
                );
            }
            for org in req_object_array(params, "studyAt") {
                snb.insert_edge_cached(
                    person_vid,
                    encode_vid(VertexLabel::Organisation, req_i64(org, "organizationId")),
                    EdgeLabel::StudyAt,
                    EdgeProp::I32(req_i64_any(org, &["classYear", "year"]) as i32),
                    true,
                );
            }
            for org in req_object_array(params, "workAt") {
                snb.insert_edge_cached(
                    person_vid,
                    encode_vid(VertexLabel::Organisation, req_i64(org, "organizationId")),
                    EdgeLabel::WorkAt,
                    EdgeProp::I32(req_i64_any(org, &["workFromYear", "year"]) as i32),
                    true,
                );
            }
        }
        "/query/interactive_update_2" => {
            insert_update_edge(
                snb,
                encode_vid(VertexLabel::Person, req_i64(params, "personId")),
                encode_vid(VertexLabel::Post, req_i64(params, "postId")),
                EdgeLabel::LikesPost,
                EdgeProp::I64(req_i64(params, "creationDate")),
                false,
            );
        }
        "/query/interactive_update_3" => {
            insert_update_edge(
                snb,
                encode_vid(VertexLabel::Person, req_i64(params, "personId")),
                encode_vid(VertexLabel::Comment, req_i64(params, "commentId")),
                EdgeLabel::LikesComment,
                EdgeProp::I64(req_i64(params, "creationDate")),
                false,
            );
        }
        "/query/interactive_update_4" => {
            let forum_id = req_i64(params, "forumId");
            let forum_vid = encode_vid(VertexLabel::Forum, forum_id);
            snb.insert_vertex(VertexData::Forum(ForumProps {
                id: forum_id,
                title: req_str(params, "forumTitle").to_string(),
                creation_date: req_i64(params, "creationDate"),
                moderator: req_i64(params, "moderatorPersonId"),
            }));
            snb.insert_edge_cached(
                forum_vid,
                encode_vid(VertexLabel::Person, req_i64(params, "moderatorPersonId")),
                EdgeLabel::HasModerator,
                EdgeProp::Empty,
                true,
            );
            for tag_id in req_i64_array(params, "tagIds") {
                snb.insert_edge_cached(
                    forum_vid,
                    encode_vid(VertexLabel::Tag, tag_id),
                    EdgeLabel::HasTag,
                    EdgeProp::Empty,
                    true,
                );
            }
        }
        "/query/interactive_update_5" => {
            insert_update_edge(
                snb,
                encode_vid(VertexLabel::Forum, req_i64(params, "forumId")),
                encode_vid(VertexLabel::Person, req_i64(params, "personId")),
                EdgeLabel::HasMember,
                EdgeProp::I64(req_i64(params, "joinDate")),
                false,
            );
        }
        "/query/interactive_update_6" => {
            let post_id = req_i64(params, "postId");
            let post_vid = encode_vid(VertexLabel::Post, post_id);
            snb.insert_vertex(VertexData::Post(PostProps {
                id: post_id,
                image_file: req_str(params, "imageFile").to_string(),
                creation_date: req_i64(params, "creationDate"),
                location_ip: req_str(params, "locationIp").to_string(),
                browser_used: req_str(params, "browserUsed").to_string(),
                language: req_str(params, "language").to_string(),
                content: req_str(params, "content").to_string(),
                length: req_i64(params, "length"),
                creator: req_i64(params, "authorPersonId"),
                forum_id: req_i64(params, "forumId"),
                place: req_i64(params, "countryId"),
            }));
            snb.insert_edge_cached(
                post_vid,
                encode_vid(VertexLabel::Person, req_i64(params, "authorPersonId")),
                EdgeLabel::HasCreator,
                EdgeProp::Empty,
                true,
            );
            snb.insert_edge_cached(
                encode_vid(VertexLabel::Forum, req_i64(params, "forumId")),
                post_vid,
                EdgeLabel::ContainerOf,
                EdgeProp::Empty,
                true,
            );
            snb.insert_edge_cached(
                post_vid,
                encode_vid(VertexLabel::Place, req_i64(params, "countryId")),
                EdgeLabel::IsLocatedIn,
                EdgeProp::Empty,
                true,
            );
            for tag_id in req_i64_array(params, "tagIds") {
                snb.insert_edge_cached(
                    post_vid,
                    encode_vid(VertexLabel::Tag, tag_id),
                    EdgeLabel::HasTag,
                    EdgeProp::Empty,
                    true,
                );
            }
        }
        "/query/interactive_update_7" => {
            let comment_id = req_i64(params, "commentId");
            let comment_vid = encode_vid(VertexLabel::Comment, comment_id);
            let reply_to_post = optional_positive_i64(params, "replyToPostId");
            let reply_to_comment = optional_positive_i64(params, "replyToCommentId");
            snb.insert_vertex(VertexData::Comment(CommentProps {
                id: comment_id,
                creation_date: req_i64(params, "creationDate"),
                location_ip: req_str(params, "locationIp").to_string(),
                browser_used: req_str(params, "browserUsed").to_string(),
                content: req_str(params, "content").to_string(),
                length: req_i64(params, "length"),
                creator: req_i64(params, "authorPersonId"),
                place: req_i64(params, "countryId"),
                reply_of_post: reply_to_post,
                reply_of_comment: reply_to_comment,
            }));
            snb.insert_edge_cached(
                comment_vid,
                encode_vid(VertexLabel::Person, req_i64(params, "authorPersonId")),
                EdgeLabel::HasCreator,
                EdgeProp::Empty,
                true,
            );
            snb.insert_edge_cached(
                comment_vid,
                encode_vid(VertexLabel::Place, req_i64(params, "countryId")),
                EdgeLabel::IsLocatedIn,
                EdgeProp::Empty,
                true,
            );
            if let Some(post_id) = reply_to_post {
                snb.insert_edge_cached(
                    comment_vid,
                    encode_vid(VertexLabel::Post, post_id),
                    EdgeLabel::ReplyOfPost,
                    EdgeProp::Empty,
                    true,
                );
            }
            if let Some(parent_id) = reply_to_comment {
                snb.insert_edge_cached(
                    comment_vid,
                    encode_vid(VertexLabel::Comment, parent_id),
                    EdgeLabel::ReplyOfComment,
                    EdgeProp::Empty,
                    true,
                );
            }
            for tag_id in req_i64_array(params, "tagIds") {
                snb.insert_edge_cached(
                    comment_vid,
                    encode_vid(VertexLabel::Tag, tag_id),
                    EdgeLabel::HasTag,
                    EdgeProp::Empty,
                    true,
                );
            }
        }
        "/query/interactive_update_8" => {
            let person1 = encode_vid(VertexLabel::Person, req_i64(params, "person1Id"));
            let person2 = encode_vid(VertexLabel::Person, req_i64(params, "person2Id"));
            let prop = EdgeProp::I64(req_i64(params, "creationDate"));
            insert_update_edge(snb, person1, person2, EdgeLabel::Knows, prop, true);
        }
        _ => anyhow::bail!("unsupported DGS HTTP endpoint: {endpoint}"),
    }
    Ok(json!({}))
}

fn org_tuples(rows: Vec<OrgJson>) -> Vec<Value> {
    rows.into_iter()
        .map(|r| json!([r.organization_name, r.year, r.place_name]))
        .collect()
}

fn values_equal(query_name: &str, actual: &Value, expected: &Value) -> bool {
    if query_name != "IC14" {
        return actual == expected;
    }
    normalize_ic14(actual) == normalize_ic14(expected)
}

fn normalize_ic14(value: &Value) -> Value {
    let Value::Array(rows) = value else {
        return value.clone();
    };
    let mut rows = rows.clone();
    rows.sort_by(|a, b| {
        let aw = a["pathWeight"].as_f64().unwrap_or_default();
        let bw = b["pathWeight"].as_f64().unwrap_or_default();
        bw.partial_cmp(&aw)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| path_ids(a).cmp(&path_ids(b)))
    });
    Value::Array(rows)
}

fn path_ids(value: &Value) -> Vec<i64> {
    value["personIdsInPath"]
        .as_array()
        .map(|items| items.iter().filter_map(Value::as_i64).collect())
        .unwrap_or_default()
}

fn ic1(snb: &SnbGraph, person_id: i64, first_name: &str, limit: usize) -> Vec<Ic1Row> {
    let start = encode_vid(VertexLabel::Person, person_id);
    if snb.person(start).is_none() {
        return Vec::new();
    }

    let mut visited: HashSet<VertexId> = HashSet::from_iter([start]);
    let mut frontier = vec![start];
    let mut candidates: Vec<(i32, String, i64, VertexId)> = Vec::new();

    for distance in 1..=3 {
        let mut next = Vec::new();
        for vid in frontier {
            for nbr in knows_neighbors(snb, vid) {
                if visited.insert(nbr) {
                    next.push(nbr);
                }
            }
        }
        for &vid in &next {
            let Some(person) = snb.person(vid) else {
                continue;
            };
            if person.first_name == first_name {
                candidates.push((distance, person.last_name.clone(), person.id, vid));
            }
        }
        if candidates.len() >= limit {
            break;
        }
        frontier = next;
        if frontier.is_empty() {
            break;
        }
    }

    candidates.sort_by(|a, b| {
        a.0.cmp(&b.0)
            .then_with(|| a.1.cmp(&b.1))
            .then_with(|| a.2.cmp(&b.2))
    });
    candidates.truncate(limit);

    candidates
        .into_iter()
        .map(|(distance, _last, _id, vid)| {
            let person = snb.person(vid).unwrap();
            Ic1Row {
                friend_id: person.id,
                friend_last_name: person.last_name.clone(),
                distance_from_person: distance,
                friend_birthday: person.birthday,
                friend_creation_date: person.creation_date,
                friend_gender: person.gender.clone(),
                friend_browser_used: person.browser_used.clone(),
                friend_location_ip: person.location_ip.clone(),
                friend_emails: split_list(&person.email),
                friend_languages: split_list(&person.language),
                friend_city_name: person_city_name(snb, vid),
                friend_universities: org_entries(snb, vid, EdgeLabel::StudyAt),
                friend_companies: org_entries(snb, vid, EdgeLabel::WorkAt),
            }
        })
        .collect()
}

fn ic2(snb: &SnbGraph, person_id: i64, max_date: i64, limit: usize) -> Vec<Ic2Row> {
    let start = encode_vid(VertexLabel::Person, person_id);
    if snb.person(start).is_none() {
        return Vec::new();
    }
    let friends: HashSet<VertexId> = knows_neighbors(snb, start).into_iter().collect();
    let mut rows: Vec<(i64, i64, VertexId, VertexId)> = Vec::new();
    for friend in friends {
        for msg in message_refs_by_creator(snb, friend) {
            if msg.creation_date <= max_date {
                rows.push((msg.creation_date, msg.id, msg.vid, friend));
            }
        }
    }
    rows.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| a.1.cmp(&b.1)));
    rows.truncate(limit);

    rows.into_iter()
        .filter_map(|(creation_date, msg_id, msg, friend)| {
            let person = snb.person(friend)?;
            Some(Ic2Row {
                person_id: person.id,
                person_first_name: person.first_name.clone(),
                person_last_name: person.last_name.clone(),
                message_id: msg_id,
                message_content: message_content(snb, msg),
                message_creation_date: creation_date,
            })
        })
        .collect()
}

fn ic3(
    snb: &SnbGraph,
    person_id: i64,
    country_x_name: &str,
    country_y_name: &str,
    start_date: i64,
    duration_days: i64,
    limit: usize,
) -> Vec<Ic3Row> {
    let start_vid = encode_vid(VertexLabel::Person, person_id);
    if snb.person(start_vid).is_none() {
        return Vec::new();
    }
    let end_date = start_date.saturating_add(duration_days.saturating_mul(DAY_MS));
    let country_x = find_country_vid(snb, country_x_name);
    let country_y = find_country_vid(snb, country_y_name);
    let xy_persons = collect_xy_persons(snb, country_x, country_y);
    let (non_xy_persons, mut counts) = bfs_non_xy_friends(snb, start_vid, &xy_persons);
    if non_xy_persons.is_empty() {
        return Vec::new();
    }
    let country_x_places = country_x
        .map(|country| place_descendants_set(snb, country))
        .unwrap_or_default();
    let country_y_places = country_y
        .map(|country| place_descendants_set(snb, country))
        .unwrap_or_default();

    for &creator in &non_xy_persons {
        snb.for_each_message_ref_by_creator_date_range(creator, start_date, end_date, |msg| {
            let Some(place) = message_place_vid(snb, msg.vid) else {
                return;
            };
            let entry = counts.entry(creator).or_insert((0, 0));
            if country_x_places.contains(&place) {
                entry.0 += 1;
            }
            if country_y_places.contains(&place) {
                entry.1 += 1;
            }
        });
    }

    let mut rows: Vec<(i64, i64, VertexId, i64, i64)> = counts
        .into_iter()
        .filter_map(|(vid, (x, y))| {
            if x > 0 && y > 0 {
                let p = snb.person(vid)?;
                Some((x + y, p.id, vid, x, y))
            } else {
                None
            }
        })
        .collect();
    rows.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| a.1.cmp(&b.1)));
    rows.truncate(limit);
    rows.into_iter()
        .map(|(count, _pid, vid, x_count, y_count)| {
            let p = snb.person(vid).unwrap();
            Ic3Row {
                person_id: p.id,
                person_first_name: p.first_name.clone(),
                person_last_name: p.last_name.clone(),
                x_count,
                y_count,
                count,
            }
        })
        .collect()
}

fn ic4(
    snb: &SnbGraph,
    person_id: i64,
    start_date: i64,
    duration_days: i64,
    limit: usize,
) -> Vec<Ic4Row> {
    #[derive(Default)]
    struct TagAgg {
        in_window_count: i64,
        existed_before: bool,
    }

    let start_vid = encode_vid(VertexLabel::Person, person_id);
    if snb.person(start_vid).is_none() {
        return Vec::new();
    }
    let end_date = start_date.saturating_add(duration_days.saturating_mul(DAY_MS));
    let friends: HashSet<VertexId> = knows_neighbors(snb, start_vid).into_iter().collect();
    let mut by_tag: HashMap<VertexId, TagAgg> = HashMap::new();

    for friend in friends {
        for post_vid in in_posts_creators(snb, friend) {
            let Some(post) = snb.post(post_vid) else {
                continue;
            };
            if post.creation_date < start_date || post.creation_date < end_date {
                for tag_vid in has_tag_neighbors(snb, post_vid) {
                    let entry = by_tag.entry(tag_vid).or_default();
                    if post.creation_date < start_date {
                        entry.existed_before = true;
                    } else {
                        entry.in_window_count += 1;
                    }
                }
            }
        }
    }

    let mut rows: Vec<(i64, String, VertexId)> = by_tag
        .into_iter()
        .filter_map(|(tag_vid, agg)| {
            if agg.existed_before {
                return None;
            }
            let tag = snb.tag(tag_vid)?;
            Some((agg.in_window_count, tag.name.clone(), tag_vid))
        })
        .collect();
    rows.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| a.1.cmp(&b.1)));
    rows.truncate(limit);
    rows.into_iter()
        .map(|(post_count, tag_name, _)| Ic4Row {
            tag_name,
            post_count,
        })
        .collect()
}

fn ic5(snb: &SnbGraph, person_id: i64, min_date: i64, limit: usize) -> Vec<Ic5Row> {
    let start_vid = encode_vid(VertexLabel::Person, person_id);
    if snb.person(start_vid).is_none() {
        return Vec::new();
    }
    let friends = knows_bfs_2hop(snb, start_vid);
    let mut forum_posts: HashMap<VertexId, i64> = HashMap::new();

    for friend in friends {
        let mut qual_forums = HashSet::new();
        for (forum_vid, prop) in snb.in_edges_with_prop(friend, EdgeLabel::HasMember) {
            if prop.as_i64() >= min_date {
                qual_forums.insert(forum_vid);
                forum_posts.entry(forum_vid).or_insert(0);
            }
        }
        if qual_forums.is_empty() {
            continue;
        }
        for post_vid in in_posts_creators(snb, friend) {
            if let Some(post) = snb.post(post_vid) {
                let forum_vid = encode_vid(VertexLabel::Forum, post.forum_id);
                if qual_forums.contains(&forum_vid) {
                    *forum_posts.get_mut(&forum_vid).unwrap() += 1;
                }
            } else {
                for forum_vid in snb.in_neighbors_cached(post_vid, EdgeLabel::ContainerOf) {
                    if qual_forums.contains(&forum_vid) {
                        *forum_posts.get_mut(&forum_vid).unwrap() += 1;
                    }
                }
            }
        }
    }

    let mut rows: Vec<(i64, i64, VertexId)> = forum_posts
        .into_iter()
        .map(|(forum_vid, count)| {
            let forum_id = snb.forum(forum_vid).map(|f| f.id).unwrap_or_default();
            (count, forum_id, forum_vid)
        })
        .collect();
    rows.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| a.1.cmp(&b.1)));
    rows.truncate(limit);
    rows.into_iter()
        .map(|(post_count, _forum_id, forum_vid)| Ic5Row {
            forum_title: snb
                .forum(forum_vid)
                .map(|f| f.title.clone())
                .unwrap_or_default(),
            post_count,
        })
        .collect()
}

fn ic6(snb: &SnbGraph, person_id: i64, tag_name: &str, limit: usize) -> Vec<Ic6Row> {
    let start_vid = encode_vid(VertexLabel::Person, person_id);
    if snb.person(start_vid).is_none() {
        return Vec::new();
    }
    let Some(tag_vid) = find_tag_vid(snb, tag_name) else {
        return Vec::new();
    };
    let friends_2hop = knows_bfs_2hop(snb, start_vid);
    let mut counts: HashMap<VertexId, i64> = HashMap::new();

    for msg_vid in snb.in_neighbors_cached(tag_vid, EdgeLabel::HasTag) {
        if snb.vertex_label(msg_vid) != Some(VertexLabel::Post) {
            continue;
        }
        let Some(creator) = message_creator_vid(snb, msg_vid) else {
            continue;
        };
        if !friends_2hop.contains(&creator) {
            continue;
        }
        for other in has_tag_neighbors(snb, msg_vid) {
            if other != tag_vid {
                *counts.entry(other).or_insert(0) += 1;
            }
        }
    }

    let mut rows: Vec<(i64, String, VertexId)> = counts
        .into_iter()
        .filter_map(|(tag_vid, count)| {
            let tag = snb.tag(tag_vid)?;
            Some((count, tag.name.clone(), tag_vid))
        })
        .collect();
    rows.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| a.1.cmp(&b.1)));
    rows.truncate(limit);
    rows.into_iter()
        .map(|(post_count, tag_name, _)| Ic6Row {
            tag_name,
            post_count,
        })
        .collect()
}

fn ic7(snb: &SnbGraph, person_id: i64, limit: usize) -> Vec<Ic7Row> {
    let person_vid = encode_vid(VertexLabel::Person, person_id);
    if snb.person(person_vid).is_none() {
        return Vec::new();
    }
    let knows_set: HashSet<VertexId> = knows_neighbors(snb, person_vid).into_iter().collect();
    let mut liker_best: HashMap<VertexId, (i64, i64, i64, VertexId)> = HashMap::new();

    for msg in message_refs_by_creator(snb, person_vid) {
        for (liker, prop) in snb.in_edges_with_prop(msg.vid, EdgeLabel::LikesPost) {
            keep_best_like(
                &mut liker_best,
                liker,
                prop.as_i64(),
                msg.id,
                msg.creation_date,
                msg.vid,
            );
        }
        for (liker, prop) in snb.in_edges_with_prop(msg.vid, EdgeLabel::LikesComment) {
            keep_best_like(
                &mut liker_best,
                liker,
                prop.as_i64(),
                msg.id,
                msg.creation_date,
                msg.vid,
            );
        }
    }

    let mut rows: Vec<(i64, i64, VertexId)> = liker_best
        .iter()
        .filter_map(|(&liker, &(like_time, _, _, _))| {
            let p = snb.person(liker)?;
            Some((like_time, p.id, liker))
        })
        .collect();
    rows.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| a.1.cmp(&b.1)));
    rows.truncate(limit);
    rows.into_iter()
        .map(|(_like_time, _pid, liker)| {
            let (like_time, msg_id, msg_creation_date, msg_vid) = liker_best[&liker];
            let p = snb.person(liker).unwrap();
            Ic7Row {
                person_id: p.id,
                person_first_name: p.first_name.clone(),
                person_last_name: p.last_name.clone(),
                like_creation_date: like_time,
                message_id: msg_id,
                message_content: message_content(snb, msg_vid),
                minutes_latency: (like_time - msg_creation_date) / 60_000,
                is_new: !knows_set.contains(&liker),
            }
        })
        .collect()
}

fn ic8(snb: &SnbGraph, person_id: i64, limit: usize) -> Vec<Ic8Row> {
    let person_vid = encode_vid(VertexLabel::Person, person_id);
    if limit == 0 || snb.person(person_vid).is_none() {
        return Vec::new();
    }
    let mut rows = Vec::with_capacity(limit);
    snb.for_each_top_reply_ref_by_parent_creator_date(person_vid, limit, |reply| {
        rows.push((reply.creation_date, reply.comment_id, reply.reply_vid));
    });
    rows.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| a.1.cmp(&b.1)));
    rows.into_iter()
        .filter_map(|(creation_date, comment_id, reply_vid)| {
            let creator = message_creator_vid(snb, reply_vid)?;
            let person = snb.person(creator)?;
            Some(Ic8Row {
                person_id: person.id,
                person_first_name: person.first_name.clone(),
                person_last_name: person.last_name.clone(),
                comment_creation_date: creation_date,
                comment_id,
                comment_content: snb.comment_content(reply_vid).unwrap_or_default(),
            })
        })
        .collect()
}

fn ic9(snb: &SnbGraph, person_id: i64, max_date: i64, limit: usize) -> Vec<Ic9Row> {
    let start = encode_vid(VertexLabel::Person, person_id);
    if snb.person(start).is_none() {
        return Vec::new();
    }
    let friends = knows_bfs_2hop(snb, start);
    let mut friend_of_msg: HashMap<VertexId, VertexId> = HashMap::new();
    let mut rows = Vec::new();
    for friend in friends {
        for msg in message_refs_by_creator(snb, friend) {
            if msg.creation_date < max_date {
                rows.push((msg.creation_date, msg.id, msg.vid));
                friend_of_msg.insert(msg.vid, friend);
            }
        }
    }
    rows.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| a.1.cmp(&b.1)));
    rows.truncate(limit);
    rows.into_iter()
        .filter_map(|(creation_date, msg_id, msg)| {
            let friend = friend_of_msg[&msg];
            let person = snb.person(friend)?;
            Some(Ic9Row {
                person_id: person.id,
                person_first_name: person.first_name.clone(),
                person_last_name: person.last_name.clone(),
                message_id: msg_id,
                message_content: message_content(snb, msg),
                message_creation_date: creation_date,
            })
        })
        .collect()
}

fn ic10(snb: &SnbGraph, person_id: i64, month: u32, limit: usize) -> Vec<Ic10Row> {
    let start = encode_vid(VertexLabel::Person, person_id);
    if snb.person(start).is_none() {
        return Vec::new();
    }
    let person_tags: HashSet<VertexId> = snb
        .out_neighbors_cached(start, EdgeLabel::HasInterest)
        .into_iter()
        .collect();
    if person_tags.is_empty() {
        return Vec::new();
    }

    let mut rows = Vec::new();
    for fof in knows_fof(snb, start) {
        let Some(person) = snb.person(fof) else {
            continue;
        };
        if !birthday_in_month_day_range(person.birthday, month) {
            continue;
        }
        let posts = in_posts_creators(snb, fof);
        let total = posts.len() as i64;
        let common = posts
            .iter()
            .filter(|&&post| {
                has_tag_neighbors(snb, post)
                    .iter()
                    .any(|t| person_tags.contains(t))
            })
            .count() as i64;
        let score = 2 * common - total;
        rows.push((score, person.id, fof));
    }
    rows.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| a.1.cmp(&b.1)));
    rows.truncate(limit);
    rows.into_iter()
        .map(|(score, _pid, vid)| {
            let person = snb.person(vid).unwrap();
            Ic10Row {
                person_id: person.id,
                person_first_name: person.first_name.clone(),
                person_last_name: person.last_name.clone(),
                common_interest_score: score,
                person_gender: person.gender.clone(),
                person_city_name: person_city_name(snb, vid),
            }
        })
        .collect()
}

fn ic11(
    snb: &SnbGraph,
    person_id: i64,
    country_name: &str,
    work_from_year: i32,
    limit: usize,
) -> Vec<Ic11Row> {
    let start = encode_vid(VertexLabel::Person, person_id);
    if snb.person(start).is_none() {
        return Vec::new();
    }
    let friends = knows_bfs_2hop(snb, start);
    let mut rows = Vec::new();
    for friend in friends {
        let Some(person) = snb.person(friend) else {
            continue;
        };
        for (org_vid, prop) in snb.out_edges_with_prop(friend, EdgeLabel::WorkAt) {
            let work_from = prop.as_i32();
            if work_from >= work_from_year {
                continue;
            }
            let country_ok = snb
                .out_neighbors_cached(org_vid, EdgeLabel::IsLocatedIn)
                .into_iter()
                .any(|place| snb.place_name(place) == country_name);
            if !country_ok {
                continue;
            }
            let org_name = snb
                .organisation(org_vid)
                .map(|o| o.name.clone())
                .unwrap_or_default();
            rows.push((work_from, person.id, org_name, friend));
        }
    }
    rows.sort_by(|a, b| {
        a.0.cmp(&b.0)
            .then_with(|| a.1.cmp(&b.1))
            .then_with(|| b.2.cmp(&a.2))
    });
    rows.truncate(limit);
    rows.into_iter()
        .map(|(work_from, _pid, org_name, friend)| {
            let person = snb.person(friend).unwrap();
            Ic11Row {
                person_id: person.id,
                person_first_name: person.first_name.clone(),
                person_last_name: person.last_name.clone(),
                organization_name: org_name,
                organization_work_from_year: work_from,
            }
        })
        .collect()
}

fn ic12(snb: &SnbGraph, person_id: i64, tag_class_name: &str, limit: usize) -> Vec<Ic12Row> {
    let start = encode_vid(VertexLabel::Person, person_id);
    if snb.person(start).is_none() {
        return Vec::new();
    }
    let Some(root_tc) = find_tag_class_vid(snb, tag_class_name) else {
        return Vec::new();
    };
    let ancestors = tag_class_ancestors_like_dgs(snb, root_tc);
    let mut tag_set = HashSet::new();
    for tc_vid in &ancestors {
        snb.for_each_tag_with_type(*tc_vid, |tag_vid| {
            tag_set.insert(tag_vid);
        });
    }
    if tag_set.is_empty() {
        return Vec::new();
    }

    let mut rows = Vec::new();
    snb.for_each_out_neighbor(start, EdgeLabel::Knows, |friend| {
        let Some(person) = snb.person(friend) else {
            return;
        };
        let mut count = 0i32;
        let mut matched_tags = BTreeSet::new();
        snb.for_each_message_ref_by_creator_date(friend, |message| {
            let Some(comment) = snb.comment(message.vid) else {
                return;
            };
            let post_vid = match comment.reply_of_post {
                Some(post_id) => encode_vid(VertexLabel::Post, post_id),
                None => return,
            };
            let mut matched = false;
            snb.for_each_out_neighbor(post_vid, EdgeLabel::HasTag, |tag_vid| {
                if tag_set.contains(&tag_vid) {
                    matched = true;
                    matched_tags.insert(tag_vid);
                }
            });
            if matched {
                count += 1;
            }
        });
        if count > 0 {
            rows.push((count, person.id, friend, matched_tags));
        }
    });
    rows.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| a.1.cmp(&b.1)));
    rows.truncate(limit);
    rows.into_iter()
        .map(|(count, _pid, friend, tag_vids)| {
            let person = snb.person(friend).unwrap();
            let mut tag_names = BTreeSet::new();
            for tag_vid in tag_vids {
                if let Some(tag) = snb.tag(tag_vid) {
                    tag_names.insert(tag.name.clone());
                }
            }
            Ic12Row {
                person_id: person.id,
                person_first_name: person.first_name.clone(),
                person_last_name: person.last_name.clone(),
                tag_names: tag_names.into_iter().rev().collect(),
                reply_count: count,
            }
        })
        .collect()
}

fn ic13(snb: &SnbGraph, person1_id: i64, person2_id: i64) -> Ic13Row {
    if person1_id == person2_id {
        return Ic13Row {
            shortest_path_length: 0,
        };
    }
    let start = encode_vid(VertexLabel::Person, person1_id);
    let goal = encode_vid(VertexLabel::Person, person2_id);
    if snb.person(start).is_none() || snb.person(goal).is_none() {
        return Ic13Row {
            shortest_path_length: -1,
        };
    }
    let mut seen = HashSet::new();
    seen.insert(start);
    let mut queue = VecDeque::from([(start, 0i32)]);
    while let Some((vid, depth)) = queue.pop_front() {
        for nbr in knows_neighbors(snb, vid) {
            if !seen.insert(nbr) {
                continue;
            }
            if nbr == goal {
                return Ic13Row {
                    shortest_path_length: depth + 1,
                };
            }
            queue.push_back((nbr, depth + 1));
        }
    }
    Ic13Row {
        shortest_path_length: -1,
    }
}

fn ic14(snb: &SnbGraph, person1_id: i64, person2_id: i64) -> Vec<Ic14Row> {
    let start = encode_vid(VertexLabel::Person, person1_id);
    let goal = encode_vid(VertexLabel::Person, person2_id);
    if snb.person(start).is_none() || snb.person(goal).is_none() {
        return Vec::new();
    }
    let cache = Ic14Cache::new(snb);
    let paths = ic14_topk_order(all_shortest_paths(start, goal, &cache));
    paths
        .into_iter()
        .map(|(path, weight)| Ic14Row {
            person_ids_in_path: person_path_ids(snb, &path),
            path_weight: weight,
        })
        .collect()
}

fn ic14_topk_order(paths: Vec<(Vec<VertexId>, f64)>) -> Vec<(Vec<VertexId>, f64)> {
    #[derive(Clone)]
    struct Item {
        path: Vec<VertexId>,
        weight: f64,
        start: VertexId,
    }

    fn cmp(a: &Item, b: &Item) -> std::cmp::Ordering {
        b.weight
            .partial_cmp(&a.weight)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.start.cmp(&b.start))
    }

    let mut heap: Vec<Item> = Vec::with_capacity(paths.len());
    for (path, weight) in paths {
        let start = path.first().copied().unwrap_or(0);
        heap.push(Item {
            path,
            weight,
            start,
        });
        let mut i = heap.len() - 1;
        while i > 0 {
            let parent = (i - 1) / 2;
            if cmp(&heap[i], &heap[parent]) == std::cmp::Ordering::Greater {
                heap.swap(i, parent);
                i = parent;
            } else {
                break;
            }
        }
    }
    heap.sort_by(cmp);
    heap.into_iter()
        .map(|item| (item.path, item.weight))
        .collect()
}

fn is1(snb: &SnbGraph, person_id: i64) -> Option<Is1Row> {
    let vid = encode_vid(VertexLabel::Person, person_id);
    let person = snb.person(vid)?;
    Some(Is1Row {
        first_name: person.first_name.clone(),
        last_name: person.last_name.clone(),
        birthday: person.birthday,
        location_ip: person.location_ip.clone(),
        browser_used: person.browser_used.clone(),
        city_id: person.place,
        gender: person.gender.clone(),
        creation_date: person.creation_date,
    })
}

fn is2(snb: &SnbGraph, person_id: i64, limit: usize) -> Vec<Is2Row> {
    let person_vid = encode_vid(VertexLabel::Person, person_id);
    if limit == 0 || snb.person(person_vid).is_none() {
        return Vec::new();
    }
    let mut top: BinaryHeap<Reverse<(i64, Reverse<i64>, VertexId)>> = BinaryHeap::new();
    for msg in message_refs_by_creator(snb, person_vid) {
        let key = (msg.creation_date, Reverse(msg.id), msg.vid);
        if top.len() < limit {
            top.push(Reverse(key));
        } else if let Some(worst) = top.peek() {
            if key > worst.0 {
                top.pop();
                top.push(Reverse(key));
            }
        }
    }
    let mut rows: Vec<_> = top
        .into_iter()
        .map(|Reverse((creation_date, Reverse(message_id), msg_vid))| {
            (creation_date, message_id, msg_vid)
        })
        .collect();
    rows.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| a.1.cmp(&b.1)));
    rows.into_iter()
        .filter_map(|(creation_date, message_id, msg_vid)| {
            let post_vid = root_post_vid(snb, msg_vid)?;
            let post = snb.post(post_vid)?;
            let author_vid = message_creator_vid(snb, post_vid)?;
            let author = snb.person(author_vid)?;
            Some(Is2Row {
                message_id,
                message_content: message_content(snb, msg_vid),
                message_creation_date: creation_date,
                original_post_id: post.id,
                original_post_author_id: author.id,
                original_post_author_first_name: author.first_name.clone(),
                original_post_author_last_name: author.last_name.clone(),
            })
        })
        .collect()
}

fn is3(snb: &SnbGraph, person_id: i64) -> Vec<Is3Row> {
    let person_vid = encode_vid(VertexLabel::Person, person_id);
    if snb.person(person_vid).is_none() {
        return Vec::new();
    }
    let mut rows = Vec::new();
    for (friend_vid, prop) in snb.out_edges_with_prop(person_vid, EdgeLabel::Knows) {
        let Some(friend) = snb.person(friend_vid) else {
            continue;
        };
        rows.push(Is3Row {
            person_id: friend.id,
            first_name: friend.first_name.clone(),
            last_name: friend.last_name.clone(),
            friendship_creation_date: prop.as_i64(),
        });
    }
    rows.sort_by(|a, b| {
        b.friendship_creation_date
            .cmp(&a.friendship_creation_date)
            .then_with(|| a.person_id.cmp(&b.person_id))
    });
    rows
}

fn is4(snb: &SnbGraph, message_id: i64) -> Option<Is4Row> {
    let msg_vid = message_vid(snb, message_id)?;
    let (_, creation_date, content) = message_summary(snb, msg_vid)?;
    Some(Is4Row {
        message_creation_date: creation_date,
        message_content: content,
    })
}

fn is5(snb: &SnbGraph, message_id: i64) -> Option<Is5Row> {
    let msg_vid = message_vid(snb, message_id)?;
    let creator_vid = message_creator_vid(snb, msg_vid)?;
    let person = snb.person(creator_vid)?;
    Some(Is5Row {
        person_id: person.id,
        first_name: person.first_name.clone(),
        last_name: person.last_name.clone(),
    })
}

fn is6(snb: &SnbGraph, message_id: i64) -> Option<Is6Row> {
    let msg_vid = message_vid(snb, message_id)?;
    let post_vid = root_post_vid(snb, msg_vid)?;
    let forum_vid = snb
        .post(post_vid)
        .map(|post| encode_vid(VertexLabel::Forum, post.forum_id))
        .or_else(|| {
            snb.in_neighbors_cached(post_vid, EdgeLabel::ContainerOf)
                .first()
                .copied()
        })?;
    let forum = snb.forum(forum_vid)?;
    let moderator_vid = Some(encode_vid(VertexLabel::Person, forum.moderator)).or_else(|| {
        snb.out_neighbors_cached(forum_vid, EdgeLabel::HasModerator)
            .first()
            .copied()
    })?;
    let moderator = snb.person(moderator_vid)?;
    Some(Is6Row {
        forum_id: forum.id,
        forum_title: forum.title.clone(),
        moderator_id: moderator.id,
        moderator_first_name: moderator.first_name.clone(),
        moderator_last_name: moderator.last_name.clone(),
    })
}

fn is7(snb: &SnbGraph, message_id: i64) -> Vec<Is7Row> {
    let Some(msg_vid) = message_vid(snb, message_id) else {
        return Vec::new();
    };
    let Some(original_author) = message_creator_vid(snb, msg_vid) else {
        return Vec::new();
    };
    let direct_friends: HashSet<_> = knows_neighbors(snb, original_author).into_iter().collect();
    let mut replies = Vec::new();
    for reply_vid in snb.in_neighbors_cached(msg_vid, EdgeLabel::ReplyOfPost) {
        collect_is7_reply(snb, reply_vid, &direct_friends, &mut replies);
    }
    for reply_vid in snb.in_neighbors_cached(msg_vid, EdgeLabel::ReplyOfComment) {
        collect_is7_reply(snb, reply_vid, &direct_friends, &mut replies);
    }
    replies.sort_by(|a, b| {
        b.comment_creation_date
            .cmp(&a.comment_creation_date)
            .then_with(|| a.reply_author_id.cmp(&b.reply_author_id))
    });
    replies
}

fn req_i64(params: &Value, key: &str) -> i64 {
    params[key].as_i64().unwrap_or_default()
}

fn optional_positive_i64(params: &Value, key: &str) -> Option<i64> {
    let value = req_i64(params, key);
    (value >= 0).then_some(value)
}

fn req_i64_any(params: &Value, keys: &[&str]) -> i64 {
    keys.iter()
        .find_map(|key| params.get(*key).and_then(Value::as_i64))
        .unwrap_or_default()
}

fn req_str<'a>(params: &'a Value, key: &str) -> &'a str {
    params[key].as_str().unwrap_or_default()
}

fn req_str_any<'a>(params: &'a Value, keys: &[&str]) -> &'a str {
    keys.iter()
        .find_map(|key| params.get(*key).and_then(Value::as_str))
        .unwrap_or_default()
}

fn req_string_or_array_join(params: &Value, string_key: &str, array_key: &str) -> String {
    if let Some(value) = params.get(string_key).and_then(Value::as_str) {
        return value.to_string();
    }
    params
        .get(array_key)
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join(";")
        })
        .unwrap_or_default()
}

fn req_i64_array(params: &Value, key: &str) -> Vec<i64> {
    params
        .get(key)
        .and_then(Value::as_array)
        .map(|arr| arr.iter().filter_map(Value::as_i64).collect())
        .unwrap_or_default()
}

fn req_object_array<'a>(params: &'a Value, key: &str) -> Vec<&'a Value> {
    params
        .get(key)
        .and_then(Value::as_array)
        .map(|arr| arr.iter().collect())
        .unwrap_or_default()
}

fn insert_update_edge(
    snb: &mut SnbGraph,
    src: VertexId,
    dst: VertexId,
    edge_label: EdgeLabel,
    prop: EdgeProp,
    bidirectional_positive: bool,
) {
    snb.insert_edge_cached(src, dst, edge_label, prop, true);
    if bidirectional_positive {
        snb.insert_edge_cached(dst, src, edge_label, prop, false);
    }
}

fn split_list(s: &str) -> Vec<String> {
    if s.is_empty() {
        Vec::new()
    } else {
        s.split(';').map(String::from).collect()
    }
}

fn knows_neighbors(snb: &SnbGraph, vid: VertexId) -> Vec<VertexId> {
    snb.out_neighbors_cached(vid, EdgeLabel::Knows)
}

fn knows_bfs_2hop(snb: &SnbGraph, start: VertexId) -> HashSet<VertexId> {
    let mut visited = HashSet::new();
    let frontier = knows_neighbors(snb, start);
    for &vid in &frontier {
        if vid != start {
            visited.insert(vid);
        }
    }
    for &vid in &frontier {
        for nbr in knows_neighbors(snb, vid) {
            if nbr != start {
                visited.insert(nbr);
            }
        }
    }
    visited
}

fn knows_fof(snb: &SnbGraph, start: VertexId) -> HashSet<VertexId> {
    let hop1 = knows_neighbors(snb, start);
    let direct: HashSet<VertexId> = hop1.iter().copied().collect();
    let mut out = HashSet::new();
    for friend in hop1 {
        for nbr in knows_neighbors(snb, friend) {
            if nbr != start && !direct.contains(&nbr) {
                out.insert(nbr);
            }
        }
    }
    out
}

fn has_tag_neighbors(snb: &SnbGraph, vid: VertexId) -> Vec<VertexId> {
    snb.out_neighbors_cached(vid, EdgeLabel::HasTag)
}

fn in_posts_creators(snb: &SnbGraph, person_vid: VertexId) -> Vec<VertexId> {
    snb.in_neighbors_cached(person_vid, EdgeLabel::HasCreator)
        .into_iter()
        .filter(|&vid| snb.vertex_label(vid) == Some(VertexLabel::Post))
        .collect()
}

fn in_messages_creators(snb: &SnbGraph, person_vid: VertexId) -> Vec<VertexId> {
    if let Some(messages) = snb.message_refs_by_creator_date(person_vid) {
        return messages.into_iter().map(|msg| msg.vid).collect();
    }
    snb.in_neighbors_cached(person_vid, EdgeLabel::HasCreator)
        .into_iter()
        .filter(|&vid| {
            matches!(
                snb.vertex_label(vid),
                Some(VertexLabel::Post | VertexLabel::Comment)
            )
        })
        .collect()
}

fn message_refs_by_creator(snb: &SnbGraph, person_vid: VertexId) -> Vec<MessageRef> {
    if let Some(messages) = snb.message_refs_by_creator_date(person_vid) {
        return messages;
    }
    in_messages_creators(snb, person_vid)
        .into_iter()
        .filter_map(|vid| {
            let (id, creation_date) = message_identity(snb, vid)?;
            Some(MessageRef {
                vid,
                id,
                creation_date,
            })
        })
        .collect()
}

fn message_creator_vid(snb: &SnbGraph, msg_vid: VertexId) -> Option<VertexId> {
    if let Some(post) = snb.post(msg_vid) {
        return Some(encode_vid(VertexLabel::Person, post.creator));
    }
    if let Some(comment) = snb.comment(msg_vid) {
        return Some(encode_vid(VertexLabel::Person, comment.creator));
    }
    snb.out_neighbors_cached(msg_vid, EdgeLabel::HasCreator)
        .first()
        .copied()
}

fn message_place_vid(snb: &SnbGraph, msg_vid: VertexId) -> Option<VertexId> {
    if let Some(post) = snb.post(msg_vid) {
        return Some(encode_vid(VertexLabel::Place, post.place));
    }
    if let Some(comment) = snb.comment(msg_vid) {
        return Some(encode_vid(VertexLabel::Place, comment.place));
    }
    snb.out_neighbors_cached(msg_vid, EdgeLabel::IsLocatedIn)
        .first()
        .copied()
}

fn message_vid(snb: &SnbGraph, message_id: i64) -> Option<VertexId> {
    let post_vid = encode_vid(VertexLabel::Post, message_id);
    if snb.post(post_vid).is_some() {
        return Some(post_vid);
    }
    let comment_vid = encode_vid(VertexLabel::Comment, message_id);
    if snb.comment(comment_vid).is_some() {
        return Some(comment_vid);
    }
    None
}

fn message_summary(snb: &SnbGraph, msg_vid: VertexId) -> Option<(i64, i64, String)> {
    let (id, creation_date) = message_identity(snb, msg_vid)?;
    Some((id, creation_date, message_content(snb, msg_vid)))
}

fn message_identity(snb: &SnbGraph, msg_vid: VertexId) -> Option<(i64, i64)> {
    if let Some(comment) = snb.comment(msg_vid) {
        Some((comment.id, comment.creation_date))
    } else {
        let post = snb.post(msg_vid)?;
        Some((post.id, post.creation_date))
    }
}

fn root_post_vid(snb: &SnbGraph, msg_vid: VertexId) -> Option<VertexId> {
    if snb.post(msg_vid).is_some() {
        return Some(msg_vid);
    }
    let mut current = msg_vid;
    let mut seen = HashSet::new();
    while seen.insert(current) {
        if let Some(comment) = snb.comment(current) {
            if let Some(post_id) = comment.reply_of_post {
                return Some(encode_vid(VertexLabel::Post, post_id));
            }
            if let Some(comment_id) = comment.reply_of_comment {
                current = encode_vid(VertexLabel::Comment, comment_id);
                continue;
            }
        }
        if let Some(post_vid) = snb
            .out_neighbors_cached(current, EdgeLabel::ReplyOfPost)
            .first()
            .copied()
        {
            return Some(post_vid);
        }
        current = snb
            .out_neighbors_cached(current, EdgeLabel::ReplyOfComment)
            .first()
            .copied()?;
    }
    None
}

fn collect_is7_reply(
    snb: &SnbGraph,
    reply_vid: VertexId,
    original_author_friends: &HashSet<VertexId>,
    out: &mut Vec<Is7Row>,
) {
    let Some(comment) = snb.comment(reply_vid) else {
        return;
    };
    let Some(author_vid) = message_creator_vid(snb, reply_vid) else {
        return;
    };
    let Some(author) = snb.person(author_vid) else {
        return;
    };
    out.push(Is7Row {
        comment_id: comment.id,
        comment_content: snb.comment_content(reply_vid).unwrap_or_default(),
        comment_creation_date: comment.creation_date,
        reply_author_id: author.id,
        reply_author_first_name: author.first_name.clone(),
        reply_author_last_name: author.last_name.clone(),
        reply_author_knows_original_message_author: original_author_friends.contains(&author_vid),
    });
}

fn message_content(snb: &SnbGraph, msg_vid: VertexId) -> String {
    snb.message_content(msg_vid).unwrap_or_default()
}

fn person_city_name(snb: &SnbGraph, person_vid: VertexId) -> String {
    if let Some(person) = snb.person(person_vid) {
        return snb.place_name(encode_vid(VertexLabel::Place, person.place));
    }
    snb.out_neighbors_cached(person_vid, EdgeLabel::IsLocatedIn)
        .first()
        .map(|&place| snb.place_name(place))
        .unwrap_or_default()
}

fn org_entries(snb: &SnbGraph, person_vid: VertexId, edge_label: EdgeLabel) -> Vec<OrgJson> {
    let mut rows = Vec::new();
    for org_vid in snb.out_neighbors_cached(person_vid, edge_label) {
        let year = snb
            .edge_prop_by_type(person_vid, edge_label.as_i32(), org_vid)
            .as_i32();
        let (org_name, place_name) = if let Some(org) = snb.organisation(org_vid) {
            (
                org.name.clone(),
                snb.place_name(encode_vid(VertexLabel::Place, org.place)),
            )
        } else {
            let place_name = snb
                .out_neighbors_cached(org_vid, EdgeLabel::IsLocatedIn)
                .first()
                .map(|&place| snb.place_name(place))
                .unwrap_or_default();
            (String::new(), place_name)
        };
        rows.push(OrgJson {
            organization_name: org_name,
            year,
            place_name,
        });
    }
    if rows.is_empty() {
        rows.push(OrgJson {
            organization_name: String::new(),
            year: 0,
            place_name: String::new(),
        });
    }
    rows
}

fn find_country_vid(snb: &SnbGraph, country_name: &str) -> Option<VertexId> {
    snb.country_by_name(country_name)
}

fn find_tag_vid(snb: &SnbGraph, tag_name: &str) -> Option<VertexId> {
    snb.tag_by_name(tag_name)
}

fn find_tag_class_vid(snb: &SnbGraph, tag_class_name: &str) -> Option<VertexId> {
    snb.tag_class_by_name(tag_class_name)
}

fn collect_xy_persons(
    snb: &SnbGraph,
    country_x: Option<VertexId>,
    country_y: Option<VertexId>,
) -> HashSet<VertexId> {
    let mut out = HashSet::new();
    for country in country_x.into_iter().chain(country_y) {
        for place in place_descendants_inclusive(snb, country) {
            for person in snb.in_neighbors_cached(place, EdgeLabel::IsLocatedIn) {
                if snb.vertex_label(person) == Some(VertexLabel::Person) {
                    out.insert(person);
                }
            }
        }
    }
    out
}

fn bfs_non_xy_friends(
    snb: &SnbGraph,
    start: VertexId,
    xy_persons: &HashSet<VertexId>,
) -> (HashSet<VertexId>, HashMap<VertexId, (i64, i64)>) {
    let mut visited = HashSet::new();
    visited.insert(start);
    let mut non_xy = HashSet::new();
    let mut counts = HashMap::new();

    let frontier = knows_neighbors(snb, start);
    for &vid in &frontier {
        visited.insert(vid);
        if !xy_persons.contains(&vid) {
            non_xy.insert(vid);
            counts.insert(vid, (0, 0));
        }
    }
    for &vid in &frontier {
        for nbr in knows_neighbors(snb, vid) {
            if visited.insert(nbr) && !xy_persons.contains(&nbr) {
                non_xy.insert(nbr);
                counts.insert(nbr, (0, 0));
            }
        }
    }
    (non_xy, counts)
}

fn place_descendants_inclusive(snb: &SnbGraph, root: VertexId) -> Vec<VertexId> {
    let mut out = Vec::new();
    let mut seen = HashSet::new();
    let mut queue = VecDeque::from([root]);
    while let Some(place) = queue.pop_front() {
        if !seen.insert(place) {
            continue;
        }
        out.push(place);
        for child in snb.in_neighbors_cached(place, EdgeLabel::IsPartOf) {
            if snb.vertex_label(child) == Some(VertexLabel::Place) {
                queue.push_back(child);
            }
        }
    }
    out
}

fn place_descendants_set(snb: &SnbGraph, root: VertexId) -> HashSet<VertexId> {
    place_descendants_inclusive(snb, root).into_iter().collect()
}

fn keep_best_like(
    best: &mut HashMap<VertexId, (i64, i64, i64, VertexId)>,
    liker: VertexId,
    like_time: i64,
    msg_id: i64,
    msg_creation_date: i64,
    msg_vid: VertexId,
) {
    let keep = best
        .get(&liker)
        .map(|(old, _, _, _)| like_time > *old)
        .unwrap_or(true);
    if keep {
        best.insert(liker, (like_time, msg_id, msg_creation_date, msg_vid));
    }
}

fn tag_class_ancestors_like_dgs(snb: &SnbGraph, root: VertexId) -> HashSet<VertexId> {
    let mut out = HashSet::new();
    let mut queue = VecDeque::from([root]);
    while let Some(tc) = queue.pop_front() {
        if !out.insert(tc) {
            continue;
        }
        for parent in snb.in_neighbors_cached(tc, EdgeLabel::IsSubclassOf) {
            queue.push_back(parent);
        }
    }
    out
}

fn birthday_in_month_day_range(ts_ms: i64, month: u32) -> bool {
    let (_, m, d) = civil_from_unix_ms(ts_ms);
    let next_m = if month == 12 { 1 } else { month + 1 };
    (m == month && d >= 21) || (m == next_m && d < 22)
}

fn civil_from_unix_ms(ts_ms: i64) -> (i32, u32, u32) {
    let days = ts_ms.div_euclid(DAY_MS);
    civil_from_days(days)
}

fn civil_from_days(days_since_epoch: i64) -> (i32, u32, u32) {
    let z = days_since_epoch + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = mp + if mp < 10 { 3 } else { -9 };
    let year = y + if month <= 2 { 1 } else { 0 };
    (year as i32, month as u32, day as u32)
}

struct Ic14Cache<'a> {
    snb: &'a SnbGraph,
    comment_creator: RefCell<HashMap<VertexId, VertexId>>,
    knows_weight: RefCell<HashMap<(VertexId, VertexId), f64>>,
}

impl<'a> Ic14Cache<'a> {
    fn new(snb: &'a SnbGraph) -> Self {
        Self {
            snb,
            comment_creator: RefCell::new(HashMap::new()),
            knows_weight: RefCell::new(HashMap::new()),
        }
    }

    fn comment_creator(&self, msg: VertexId) -> Option<VertexId> {
        if let Some(&creator) = self.comment_creator.borrow().get(&msg) {
            return Some(creator);
        }
        let creator = message_creator_vid(self.snb, msg)?;
        self.comment_creator.borrow_mut().insert(msg, creator);
        Some(creator)
    }

    fn knows_weight(&self, a: VertexId, b: VertexId) -> f64 {
        let key = if a <= b { (a, b) } else { (b, a) };
        if let Some(&weight) = self.knows_weight.borrow().get(&key) {
            return weight;
        }
        let weight = self.reply_weight(a, b) + self.reply_weight(b, a);
        self.knows_weight.borrow_mut().insert(key, weight);
        weight
    }

    fn reply_weight(&self, author: VertexId, target: VertexId) -> f64 {
        let mut weight = 0.0;
        for msg in self.snb.in_neighbors_cached(author, EdgeLabel::HasCreator) {
            if self.comment_creator(msg) != Some(author) {
                continue;
            }
            for post in self.snb.out_neighbors_cached(msg, EdgeLabel::ReplyOfPost) {
                if self.comment_creator(post) == Some(target) {
                    weight += 1.0;
                }
            }
            for comment in self
                .snb
                .out_neighbors_cached(msg, EdgeLabel::ReplyOfComment)
            {
                if self.comment_creator(comment) == Some(target) {
                    weight += 0.5;
                }
            }
        }
        weight
    }
}

fn all_shortest_paths(
    start: VertexId,
    goal: VertexId,
    cache: &Ic14Cache<'_>,
) -> Vec<(Vec<VertexId>, f64)> {
    if start == goal {
        return vec![(vec![start], 0.0)];
    }
    let Some((vids, parents, goal_idx, goal_depth)) =
        shortest_path_parent_dag(start, goal, cache.snb)
    else {
        return Vec::new();
    };
    let mut paths = Vec::new();
    let mut current = Vec::new();
    enumerate_paths(
        &vids,
        &parents,
        goal_idx,
        goal_depth,
        cache,
        &mut current,
        &mut paths,
    );
    paths
}

fn shortest_path_parent_dag(
    start: VertexId,
    goal: VertexId,
    snb: &SnbGraph,
) -> Option<(Vec<VertexId>, Vec<Vec<usize>>, usize, u32)> {
    let mut vids = vec![start];
    let mut depths = vec![0u32];
    let mut parents: Vec<Vec<usize>> = vec![Vec::new()];
    let mut index = HashMap::new();
    index.insert(start, 0usize);
    let mut frontier = vec![0usize];
    let mut goal_idx = None;
    let mut goal_depth = None;

    while !frontier.is_empty() && goal_idx.is_none() {
        let next_depth = depths[frontier[0]] + 1;
        let mut next = Vec::new();
        for &idx in &frontier {
            for nbr in knows_neighbors(snb, vids[idx]) {
                if let Some(&nidx) = index.get(&nbr) {
                    if depths[nidx] == next_depth {
                        parents[nidx].push(idx);
                    }
                    continue;
                }
                let nidx = vids.len();
                vids.push(nbr);
                depths.push(next_depth);
                parents.push(vec![idx]);
                index.insert(nbr, nidx);
                next.push(nidx);
                if nbr == goal {
                    goal_idx = Some(nidx);
                    goal_depth = Some(next_depth);
                }
            }
        }
        frontier = next;
    }
    Some((vids, parents, goal_idx?, goal_depth?))
}

fn enumerate_paths(
    vids: &[VertexId],
    parents: &[Vec<usize>],
    cur: usize,
    depth: u32,
    cache: &Ic14Cache<'_>,
    current_rev: &mut Vec<VertexId>,
    out: &mut Vec<(Vec<VertexId>, f64)>,
) {
    current_rev.push(vids[cur]);
    if depth == 0 {
        let mut path = current_rev.clone();
        path.reverse();
        let weight = path
            .windows(2)
            .map(|pair| cache.knows_weight(pair[0], pair[1]))
            .sum();
        out.push((path, weight));
    } else {
        for &parent in &parents[cur] {
            enumerate_paths(vids, parents, parent, depth - 1, cache, current_rev, out);
        }
    }
    current_rev.pop();
}

fn person_path_ids(snb: &SnbGraph, path: &[VertexId]) -> Vec<i64> {
    path.iter()
        .filter_map(|&vid| snb.person(vid).map(|p| p.id))
        .collect()
}
