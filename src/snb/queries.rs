use std::cell::RefCell;
use std::collections::{BTreeSet, HashMap, HashSet, VecDeque};
use std::path::Path;
use std::sync::Arc;

use serde::Serialize;
use serde_json::{json, Value};

use crate::error::Result;
use crate::graph::Engine;
use crate::snb::props::{encode_vid, SnbGraph};
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

pub async fn validate_ic1_ic14(
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
        let (query_name, actual) = match dispatch_query(&snb, &params)? {
            Some((query_name, value)) => {
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

    Ok(ValidationReport {
        validation_params: validation_params.display().to_string(),
        max_lines,
        checked,
        passed,
        skipped,
        failed,
        supported_queries: vec![
            "IC1", "IC2", "IC3", "IC4", "IC5", "IC6", "IC7", "IC8", "IC9", "IC10", "IC11", "IC12",
            "IC13", "IC14",
        ],
        first_failure,
    })
}

fn dispatch_query(snb: &SnbGraph, params: &Value) -> Result<Option<(&'static str, Value)>> {
    let limit = || params["limit"].as_u64().unwrap_or(20) as usize;
    let item = if params.get("personIdQ1").is_some() {
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
        for msg in in_messages_creators(snb, friend) {
            let Some((msg_id, creation_date, _content)) = message_summary(snb, msg) else {
                continue;
            };
            if creation_date <= max_date {
                rows.push((creation_date, msg_id, msg, friend));
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

    if let Some(country) = country_x {
        for (_msg, _kind, _date, creator) in
            place_incoming_messages(snb, country, start_date, end_date)
        {
            if non_xy_persons.contains(&creator) {
                counts.entry(creator).or_insert((0, 0)).0 += 1;
            }
        }
    }
    if let Some(country) = country_y {
        for (_msg, _kind, _date, creator) in
            place_incoming_messages(snb, country, start_date, end_date)
        {
            if non_xy_persons.contains(&creator) {
                counts.entry(creator).or_insert((0, 0)).1 += 1;
            }
        }
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
            for forum_vid in snb.in_neighbors_cached(post_vid, EdgeLabel::ContainerOf) {
                if qual_forums.contains(&forum_vid) {
                    *forum_posts.get_mut(&forum_vid).unwrap() += 1;
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

    for msg_vid in in_messages_creators(snb, person_vid) {
        let Some((msg_id, msg_creation_date, _content)) = message_summary(snb, msg_vid) else {
            continue;
        };
        for (liker, prop) in snb.in_edges_with_prop(msg_vid, EdgeLabel::LikesPost) {
            keep_best_like(
                &mut liker_best,
                liker,
                prop.as_i64(),
                msg_id,
                msg_creation_date,
                msg_vid,
            );
        }
        for (liker, prop) in snb.in_edges_with_prop(msg_vid, EdgeLabel::LikesComment) {
            keep_best_like(
                &mut liker_best,
                liker,
                prop.as_i64(),
                msg_id,
                msg_creation_date,
                msg_vid,
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
    if snb.person(person_vid).is_none() {
        return Vec::new();
    }
    let mut rows = Vec::new();
    for msg_vid in snb.in_neighbors_cached(person_vid, EdgeLabel::HasCreator) {
        for reply_vid in snb
            .in_neighbors_cached(msg_vid, EdgeLabel::ReplyOfPost)
            .into_iter()
            .chain(snb.in_neighbors_cached(msg_vid, EdgeLabel::ReplyOfComment))
        {
            let Some(comment) = snb.comment(reply_vid) else {
                continue;
            };
            rows.push((comment.creation_date, comment.id, reply_vid));
        }
    }
    rows.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| a.1.cmp(&b.1)));
    rows.truncate(limit);
    rows.into_iter()
        .filter_map(|(creation_date, comment_id, reply_vid)| {
            let comment = snb.comment(reply_vid)?;
            let creator = message_creator_vid(snb, reply_vid)?;
            let person = snb.person(creator)?;
            Some(Ic8Row {
                person_id: person.id,
                person_first_name: person.first_name.clone(),
                person_last_name: person.last_name.clone(),
                comment_creation_date: creation_date,
                comment_id,
                comment_content: comment.content.clone(),
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
        for msg in in_messages_creators(snb, friend) {
            let Some((msg_id, creation_date, _content)) = message_summary(snb, msg) else {
                continue;
            };
            if creation_date < max_date {
                rows.push((creation_date, msg_id, msg));
                friend_of_msg.insert(msg, friend);
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
    for tag_vid in snb.vertices_by_label(VertexLabel::Tag) {
        for tc_vid in snb.out_neighbors_cached(tag_vid, EdgeLabel::HasType) {
            if ancestors.contains(&tc_vid) {
                tag_set.insert(tag_vid);
            }
        }
    }
    if tag_set.is_empty() {
        return Vec::new();
    }

    let mut rows = Vec::new();
    for friend in knows_neighbors(snb, start) {
        let Some(person) = snb.person(friend) else {
            continue;
        };
        let mut count = 0i32;
        let mut names = BTreeSet::new();
        for msg in in_messages_creators(snb, friend) {
            if snb.comment(msg).is_none() {
                continue;
            }
            let Some(post_vid) = snb
                .out_neighbors_cached(msg, EdgeLabel::ReplyOfPost)
                .first()
                .copied()
            else {
                continue;
            };
            let mut matched = false;
            for tag_vid in has_tag_neighbors(snb, post_vid) {
                if tag_set.contains(&tag_vid) {
                    matched = true;
                    if let Some(tag) = snb.tag(tag_vid) {
                        names.insert(tag.name.clone());
                    }
                }
            }
            if matched {
                count += 1;
            }
        }
        if count > 0 {
            rows.push((
                count,
                person.id,
                friend,
                names.into_iter().rev().collect::<Vec<_>>(),
            ));
        }
    }
    rows.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| a.1.cmp(&b.1)));
    rows.truncate(limit);
    rows.into_iter()
        .map(|(count, _pid, friend, tag_names)| {
            let person = snb.person(friend).unwrap();
            Ic12Row {
                person_id: person.id,
                person_first_name: person.first_name.clone(),
                person_last_name: person.last_name.clone(),
                tag_names,
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

fn req_i64(params: &Value, key: &str) -> i64 {
    params[key].as_i64().unwrap_or_default()
}

fn req_str<'a>(params: &'a Value, key: &str) -> &'a str {
    params[key].as_str().unwrap_or_default()
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

fn message_creator_vid(snb: &SnbGraph, msg_vid: VertexId) -> Option<VertexId> {
    snb.out_neighbors_cached(msg_vid, EdgeLabel::HasCreator)
        .first()
        .copied()
}

fn message_summary(snb: &SnbGraph, msg_vid: VertexId) -> Option<(i64, i64, String)> {
    if let Some(comment) = snb.comment(msg_vid) {
        Some((comment.id, comment.creation_date, comment.content.clone()))
    } else {
        let post = snb.post(msg_vid)?;
        Some((
            post.id,
            post.creation_date,
            post.content_or_image().to_string(),
        ))
    }
}

fn message_content(snb: &SnbGraph, msg_vid: VertexId) -> String {
    message_summary(snb, msg_vid)
        .map(|(_, _, content)| content)
        .unwrap_or_default()
}

fn person_city_name(snb: &SnbGraph, person_vid: VertexId) -> String {
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
        let org_name = snb
            .organisation(org_vid)
            .map(|o| o.name.clone())
            .unwrap_or_default();
        let place_name = snb
            .out_neighbors_cached(org_vid, EdgeLabel::IsLocatedIn)
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
    rows
}

fn find_country_vid(snb: &SnbGraph, country_name: &str) -> Option<VertexId> {
    snb.vertices_by_label(VertexLabel::Place).find(|&vid| {
        snb.place(vid)
            .map(|p| p.name == country_name && p.place_type == "country")
            .unwrap_or(false)
    })
}

fn find_tag_vid(snb: &SnbGraph, tag_name: &str) -> Option<VertexId> {
    snb.vertices_by_label(VertexLabel::Tag)
        .find(|&vid| snb.tag(vid).map(|t| t.name == tag_name).unwrap_or(false))
}

fn find_tag_class_vid(snb: &SnbGraph, tag_class_name: &str) -> Option<VertexId> {
    snb.vertices_by_label(VertexLabel::TagClass).find(|&vid| {
        snb.tag_class(vid)
            .map(|t| t.name == tag_class_name)
            .unwrap_or(false)
    })
}

fn collect_xy_persons(
    snb: &SnbGraph,
    country_x: Option<VertexId>,
    country_y: Option<VertexId>,
) -> HashSet<VertexId> {
    let mut out = HashSet::new();
    for country in country_x.into_iter().chain(country_y) {
        for city in snb.in_neighbors_cached(country, EdgeLabel::IsPartOf) {
            for person in snb.in_neighbors_cached(city, EdgeLabel::IsLocatedIn) {
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

fn place_incoming_messages(
    snb: &SnbGraph,
    place_vid: VertexId,
    start_date: i64,
    end_date: i64,
) -> Vec<(VertexId, u8, i64, VertexId)> {
    let mut out = Vec::new();
    for msg_vid in snb.in_neighbors_cached(place_vid, EdgeLabel::IsLocatedIn) {
        match snb.vertex_label(msg_vid) {
            Some(VertexLabel::Post) => {
                if let Some(post) = snb.post(msg_vid) {
                    if post.creation_date >= start_date && post.creation_date < end_date {
                        if let Some(creator) = message_creator_vid(snb, msg_vid) {
                            out.push((msg_vid, 1, post.creation_date, creator));
                        }
                    }
                }
            }
            Some(VertexLabel::Comment) => {
                if let Some(comment) = snb.comment(msg_vid) {
                    if comment.creation_date >= start_date && comment.creation_date < end_date {
                        if let Some(creator) = message_creator_vid(snb, msg_vid) {
                            out.push((msg_vid, 2, comment.creation_date, creator));
                        }
                    }
                }
            }
            _ => {}
        }
    }
    out
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
