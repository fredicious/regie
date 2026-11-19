//! Claude Code's `--output-format stream-json` lines, reduced to what Régie uses.

use regie_core::{Millis, Reading, Window};
use serde_json::Value;

#[derive(Clone, Debug, PartialEq)]
pub enum StreamItem {
    Init {
        session_id: String,
    },
    Text(String),
    ToolUse {
        name: String,
        summary: String,
    },
    Reading(Reading),
    Rejected {
        resets_at: Millis,
    },
    /// `usage` is (input, output, cache read) tokens.
    Result {
        text: Option<String>,
        is_error: bool,
        usage: (u64, u64, u64),
        duration_ms: u64,
    },
}

/// Longest tool summary kept, in characters.
const SUMMARY_MAX: usize = 200;

/// The items in one stdout line; lines Régie does not use (hooks, thinking,
/// tool results, malformed JSON) yield nothing.
pub fn parse_line(line: &str, now: Millis) -> Vec<StreamItem> {
    let Ok(v) = serde_json::from_str::<Value>(line) else {
        return Vec::new();
    };
    match v["type"].as_str() {
        Some("system") if v["subtype"] == "init" => v["session_id"]
            .as_str()
            .map(|s| StreamItem::Init {
                session_id: s.to_string(),
            })
            .into_iter()
            .collect(),
        Some("assistant") => v["message"]["content"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(content_item)
            .collect(),
        Some("rate_limit_event") => rate_limit(&v["rate_limit_info"], now),
        Some("result") => {
            let usage = &v["usage"];
            let n = |x: &Value| x.as_u64().unwrap_or(0);
            vec![StreamItem::Result {
                text: v["result"].as_str().map(str::to_string),
                is_error: v["is_error"].as_bool().unwrap_or(false),
                usage: (
                    n(&usage["input_tokens"]),
                    n(&usage["output_tokens"]),
                    n(&usage["cache_read_input_tokens"]),
                ),
                duration_ms: n(&v["duration_ms"]),
            }]
        }
        _ => Vec::new(),
    }
}

fn content_item(block: &Value) -> Option<StreamItem> {
    match block["type"].as_str()? {
        "text" => {
            let text = block["text"].as_str()?;
            (!text.trim().is_empty()).then(|| StreamItem::Text(text.to_string()))
        }
        "tool_use" => Some(StreamItem::ToolUse {
            name: block["name"].as_str().unwrap_or("tool").to_string(),
            summary: tool_summary(&block["input"]),
        }),
        _ => None,
    }
}

/// The input field that best says what a tool call does, on one line.
fn tool_summary(input: &Value) -> String {
    const KEYS: [&str; 8] = [
        "command",
        "file_path",
        "path",
        "pattern",
        "url",
        "query",
        "description",
        "prompt",
    ];
    let Some(s) = KEYS.iter().find_map(|k| input[*k].as_str()) else {
        return String::new();
    };
    let first = s.lines().next().unwrap_or("").trim();
    if first.chars().count() > SUMMARY_MAX || s.trim().lines().count() > 1 {
        let cut: String = first.chars().take(SUMMARY_MAX).collect();
        format!("{cut}…")
    } else {
        first.to_string()
    }
}

fn rate_limit(info: &Value, now: Millis) -> Vec<StreamItem> {
    let secs_to_millis = |v: &Value| v.as_i64().map(|s| Millis(s * 1000));
    let window = |name: &str| {
        let w = &info["unifiedWindows"][name];
        Some(Window {
            used: w["utilization"].as_f64()?,
            resets_at: secs_to_millis(&w["resetsAt"])?,
        })
    };
    let mut items = Vec::new();
    if let (Some(five_hour), Some(seven_day)) = (window("five_hour"), window("seven_day")) {
        items.push(StreamItem::Reading(Reading {
            five_hour,
            seven_day,
            read_at: now,
        }));
    }
    if info["status"] == "rejected" {
        if let Some(resets_at) = secs_to_millis(&info["resetsAt"]) {
            items.push(StreamItem::Rejected { resets_at });
        }
    }
    items
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture(name: &str) -> String {
        let path = format!(
            "{}/tests/fixtures/claude/{name}",
            env!("CARGO_MANIFEST_DIR")
        );
        std::fs::read_to_string(path).unwrap()
    }

    fn parse_all(text: &str, now: Millis) -> Vec<StreamItem> {
        text.lines().flat_map(|l| parse_line(l, now)).collect()
    }

    #[test]
    fn basic_stream_items() {
        let items = parse_all(&fixture("basic.jsonl"), Millis(42));
        assert_eq!(
            items.first(),
            Some(&StreamItem::Init {
                session_id: "2d7d8177-a3f8-4406-a208-e98cbe2a6f7d".into()
            })
        );
        let readings: Vec<_> = items
            .iter()
            .filter_map(|i| {
                if let StreamItem::Reading(r) = i {
                    Some(r)
                } else {
                    None
                }
            })
            .collect();
        assert_eq!(
            readings,
            [&Reading {
                five_hour: Window {
                    used: 0.12,
                    resets_at: Millis(1_795_129_200_000)
                },
                seven_day: Window {
                    used: 0.79,
                    resets_at: Millis(1_795_273_200_000)
                },
                read_at: Millis(42),
            }]
        );
        assert!(items.contains(&StreamItem::Text("ok".into())));
        match items.last() {
            Some(StreamItem::Result {
                text: Some(t),
                is_error: false,
                usage,
                duration_ms,
            }) => {
                assert!(t.eq_ignore_ascii_case("ok"), "{t}");
                assert_eq!(*usage, (2, 116, 25185));
                assert_eq!(*duration_ms, 2855);
            }
            other => panic!("expected a result last, got {other:?}"),
        }
        assert!(!items
            .iter()
            .any(|i| matches!(i, StreamItem::Rejected { .. })));
    }

    #[test]
    fn rejected_status_is_limit() {
        let items = parse_all(&fixture("limit_rejected.jsonl"), Millis(0));
        assert!(items.contains(&StreamItem::Rejected {
            resets_at: Millis(1_795_129_200_000)
        }));
        assert!(items
            .iter()
            .any(|i| matches!(i, StreamItem::Reading(r) if r.five_hour.used == 1.0)));
    }

    #[test]
    fn tool_use_summarised() {
        let items = parse_all(&fixture("error_no_result.jsonl"), Millis(0));
        match items.as_slice() {
            [StreamItem::Init { .. }, StreamItem::ToolUse { name, summary }] => {
                assert_eq!(name, "Bash");
                assert!(summary.starts_with("cargo test --workspace"), "{summary}");
            }
            other => panic!("unexpected {other:?}"),
        }
    }

    #[test]
    fn other_tools_and_junk() {
        let line =
            r#"{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"hm"},
            {"type":"text","text":"Reading it."},
            {"type":"tool_use","name":"Read","input":{"file_path":"src/lib.rs"}}]}}"#
                .replace('\n', "");
        assert_eq!(
            parse_line(&line, Millis(0)),
            [
                StreamItem::Text("Reading it.".into()),
                StreamItem::ToolUse {
                    name: "Read".into(),
                    summary: "src/lib.rs".into()
                }
            ]
        );
        assert!(parse_line("not json", Millis(0)).is_empty());
        assert!(parse_line(r#"{"type":"system","subtype":"hook_started"}"#, Millis(0)).is_empty());
        let long = format!(
            r#"{{"type":"assistant","message":{{"content":[{{"type":"tool_use","name":"Bash","input":{{"command":"{}\nsecond line"}}}}]}}}}"#,
            "x".repeat(500)
        );
        match parse_line(&long, Millis(0)).as_slice() {
            [StreamItem::ToolUse { summary, .. }] => {
                assert!(
                    summary.chars().count() <= 201 && !summary.contains('\n'),
                    "{summary}"
                );
            }
            other => panic!("unexpected {other:?}"),
        }
    }
}
