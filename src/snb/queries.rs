use std::collections::HashSet;
use std::path::Path;
use std::sync::Arc;

use serde::Serialize;
use serde_json::{json, Value};

use crate::error::Result;
use crate::graph::Engine;
use crate::snb::props::{encode_vid, SnbGraph};
use crate::types::{EdgeLabel, VertexId, VertexLabel};

#[derive(Debug, Serialize)]
pub struct ValidationReport {
    pub validation_params: String,
    pub max_lines: usize,
    pub checked: usize,
    pub passed: usize,
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

pub async fn validate_ic1_ic2(
    engine: Arc<Engine>,
    store_dir: &Path,
    validation_params: &Path,
    max_lines: usize,
) -> Result<ValidationReport> {
    let snb = SnbGraph::open(engine, store_dir).await?;
    let text = std::fs::read_to_string(validation_params)?;
    let mut checked = 0usize;
    let mut passed = 0usize;
    let mut skipped = 0usize;
    let mut failed = 0usize;
    let mut first_failure = None;

    for (line_no, line) in text.lines().enumerate() {
        if checked >= max_lines {
            break;
        }
        let Some((params_raw, expected_raw)) = line.split_once('|') else {
            continue;
        };
        let params: Value = serde_json::from_str(params_raw)?;
        let expected: Value = serde_json::from_str(expected_raw)?;
        let actual = if params.get("personIdQ1").is_some() {
            checked += 1;
            let person_id = params["personIdQ1"].as_i64().unwrap_or_default();
            let first_name = params["firstName"].as_str().unwrap_or_default();
            let limit = params["limit"].as_u64().unwrap_or(20) as usize;
            serde_json::to_value(ic1(&snb, person_id, first_name, limit).await?)?
        } else if params.get("personIdQ2").is_some() {
            checked += 1;
            let person_id = params["personIdQ2"].as_i64().unwrap_or_default();
            let max_date = params["maxDate"].as_i64().unwrap_or_default();
            let limit = params["limit"].as_u64().unwrap_or(20) as usize;
            serde_json::to_value(ic2(&snb, person_id, max_date, limit).await?)?
        } else {
            skipped += 1;
            continue;
        };

        if actual == expected {
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

    Ok(ValidationReport {
        validation_params: validation_params.display().to_string(),
        max_lines,
        checked,
        passed,
        skipped,
        failed,
        supported_queries: vec!["IC1", "IC2"],
        first_failure,
    })
}

async fn ic1(
    snb: &SnbGraph,
    person_id: i64,
    first_name: &str,
    limit: usize,
) -> Result<Vec<Ic1Row>> {
    let start = encode_vid(VertexLabel::Person, person_id);
    if snb.person(start).is_none() {
        return Ok(Vec::new());
    }

    let mut visited: HashSet<VertexId> = HashSet::new();
    visited.insert(start);
    let mut frontier = vec![start];
    let mut candidates: Vec<(i32, String, i64, VertexId)> = Vec::new();

    for distance in 1..=3 {
        let mut next = Vec::new();
        for vid in frontier {
            for nbr in snb.knows_neighbors(vid).await? {
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
    let mut out = Vec::with_capacity(candidates.len());
    for (distance, _last, _id, vid) in candidates {
        let person = snb.person(vid).unwrap();
        out.push(Ic1Row {
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
            friend_city_name: person_city_name(snb, vid).await?,
            friend_universities: org_entries(snb, vid, EdgeLabel::StudyAt).await?,
            friend_companies: org_entries(snb, vid, EdgeLabel::WorkAt).await?,
        });
    }
    Ok(out)
}

async fn ic2(snb: &SnbGraph, person_id: i64, max_date: i64, limit: usize) -> Result<Vec<Ic2Row>> {
    let start = encode_vid(VertexLabel::Person, person_id);
    if snb.person(start).is_none() {
        return Ok(Vec::new());
    }
    let friends: HashSet<VertexId> = snb.knows_neighbors(start).await?.into_iter().collect();
    let mut rows: Vec<(i64, i64, VertexId, VertexId)> = Vec::new();
    for friend in friends {
        for msg in snb.in_neighbors(friend, EdgeLabel::HasCreator).await? {
            let (msg_id, creation_date) = if let Some(c) = snb.comment(msg) {
                (c.id, c.creation_date)
            } else if let Some(p) = snb.post(msg) {
                (p.id, p.creation_date)
            } else {
                continue;
            };
            if creation_date <= max_date {
                rows.push((creation_date, msg_id, msg, friend));
            }
        }
    }
    rows.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| a.1.cmp(&b.1)));
    rows.truncate(limit);

    let mut out = Vec::with_capacity(rows.len());
    for (creation_date, msg_id, msg, friend) in rows {
        let Some(person) = snb.person(friend) else {
            continue;
        };
        let message_content = if let Some(c) = snb.comment(msg) {
            c.content.clone()
        } else if let Some(p) = snb.post(msg) {
            p.content_or_image().to_string()
        } else {
            String::new()
        };
        out.push(Ic2Row {
            person_id: person.id,
            person_first_name: person.first_name.clone(),
            person_last_name: person.last_name.clone(),
            message_id: msg_id,
            message_content,
            message_creation_date: creation_date,
        });
    }
    Ok(out)
}

async fn person_city_name(snb: &SnbGraph, person_vid: VertexId) -> Result<String> {
    Ok(snb
        .out_neighbors(person_vid, EdgeLabel::IsLocatedIn)
        .await?
        .first()
        .map(|&place| snb.place_name(place))
        .unwrap_or_default())
}

async fn org_entries(
    snb: &SnbGraph,
    person_vid: VertexId,
    edge_label: EdgeLabel,
) -> Result<Vec<OrgJson>> {
    let mut rows = Vec::new();
    for org_vid in snb.out_neighbors(person_vid, edge_label).await? {
        let year = snb
            .edge_prop(person_vid, edge_label, org_vid)
            .await
            .as_i32();
        let org_name = snb
            .organisation(org_vid)
            .map(|o| o.name.clone())
            .unwrap_or_default();
        let place_name = snb
            .out_neighbors(org_vid, EdgeLabel::IsLocatedIn)
            .await?
            .first()
            .map(|&place| snb.place_name(place))
            .unwrap_or_default();
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
    Ok(rows)
}

fn split_list(s: &str) -> Vec<String> {
    if s.is_empty() {
        Vec::new()
    } else {
        s.split(';').map(String::from).collect()
    }
}
