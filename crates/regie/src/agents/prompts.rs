//! Prompt templates (the repo's `prompts/` folder, compiled in) and how Régie fills them.

use std::collections::BTreeMap;

use anyhow::Context;
use regie_core::{EvidenceType, Finding, FindingKind, Level, ModelChoice, ProjectFacts, PromptInput, Task};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Template {
    BuilderInitial,
    BuilderFix,
    BuilderOpenPr,
    BuilderCiFix,
    BuilderConflictFix,
    BuilderResume,
    Reviewer,
    Verify,
}

impl Template {
    pub fn text(self) -> &'static str {
        match self {
            Template::BuilderInitial => include_str!("../../../../prompts/builder_initial.md"),
            Template::BuilderFix => include_str!("../../../../prompts/builder_fix.md"),
            Template::BuilderOpenPr => include_str!("../../../../prompts/builder_open_pr.md"),
            Template::BuilderCiFix => include_str!("../../../../prompts/builder_ci_fix.md"),
            Template::BuilderConflictFix => include_str!("../../../../prompts/builder_conflict_fix.md"),
            Template::BuilderResume => include_str!("../../../../prompts/builder_resume.md"),
            Template::Reviewer => include_str!("../../../../prompts/reviewer.md"),
            Template::Verify => include_str!("../../../../prompts/verify.md"),
        }
    }
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct PromptVars(pub BTreeMap<&'static str, String>);

impl PromptVars {
    pub fn set(&mut self, name: &'static str, value: impl Into<String>) -> &mut Self {
        self.0.insert(name, value.into());
        self
    }
}

/// Replaces every `{{name}}` in one pass (values are not scanned again);
/// fails if the template names a variable that is not set.
pub fn render(t: Template, vars: &PromptVars) -> anyhow::Result<String> {
    let mut rest = t.text();
    let mut out = String::with_capacity(rest.len() * 2);
    while let Some(open) = rest.find("{{") {
        out.push_str(&rest[..open]);
        let after = &rest[open + 2..];
        let close = after
            .find("}}")
            .with_context(|| format!("{t:?} template has an unclosed `{{{{`"))?;
        let name = after[..close].trim();
        let value = vars
            .0
            .get(name)
            .with_context(|| format!("{t:?} template needs `{name}`, which is not set"))?;
        out.push_str(value);
        rest = &after[close + 2..];
    }
    out.push_str(rest);
    Ok(out)
}

/// The prompt for a run. A message from Frédéric goes to the agent as is.
pub fn prompt_for(
    task: &Task,
    facts: &ProjectFacts,
    input: &PromptInput,
    model: &ModelChoice,
    port_base: u16,
) -> anyhow::Result<String> {
    let mut v = PromptVars::default();
    v.set("title", task.title.as_str())
        .set("instruction", task.instruction.as_str())
        .set("tickets", bullets(&task.tickets, "None."))
        .set("checks", checks(facts))
        .set("definition_of_done", or(&facts.definition_of_done, "Nothing beyond the task itself."))
        .set("port_base", port_base.to_string())
        .set("model", model.model.as_str())
        .set("level", level_name(task.level));
    let template = match input {
        PromptInput::Message { text } => return Ok(text.clone()),
        PromptInput::Initial => Template::BuilderInitial,
        PromptInput::Fix { findings } => {
            v.set("findings", findings_list(findings));
            Template::BuilderFix
        }
        PromptInput::OpenPr => Template::BuilderOpenPr,
        PromptInput::CiFix { failed } => {
            v.set("failed_checks", bullets(failed, "(no names given; see `gh pr checks`)"));
            Template::BuilderCiFix
        }
        PromptInput::ConflictFix => Template::BuilderConflictFix,
        PromptInput::Resume { note } => {
            v.set("note", note.as_str());
            Template::BuilderResume
        }
        PromptInput::Review { round } => {
            v.set("round", round.to_string());
            Template::Reviewer
        }
        PromptInput::Verify { findings } => {
            v.set("findings", findings_list(findings));
            Template::Verify
        }
    };
    render(template, &v)
}

fn or(s: &str, empty: &str) -> String {
    if s.trim().is_empty() { empty.to_string() } else { s.to_string() }
}

fn bullets(items: &[String], empty: &str) -> String {
    if items.is_empty() {
        return empty.to_string();
    }
    items.iter().map(|i| format!("- {i}")).collect::<Vec<_>>().join("\n")
}

fn checks(f: &ProjectFacts) -> String {
    let lines: Vec<String> = [("Build", &f.build), ("Test", &f.test), ("Lint", &f.lint)]
        .into_iter()
        .filter_map(|(label, cmd)| cmd.as_deref().map(|c| format!("- {label}: `{c}`")))
        .collect();
    if lines.is_empty() {
        "No checks are set for this project; build and test it the usual way for its language.".into()
    } else {
        lines.join("\n")
    }
}

fn level_name(l: Level) -> &'static str {
    match l {
        Level::Light => "light",
        Level::Standard => "standard",
        Level::Hard => "hard",
    }
}

fn findings_list(findings: &[Finding]) -> String {
    findings
        .iter()
        .map(|f| {
            let kind = match f.kind {
                FindingKind::Bug => "bug",
                FindingKind::Security => "security",
                FindingKind::MissingTest => "missing test",
                FindingKind::UnmetCriterion => "unmet criterion",
                FindingKind::Design => "design",
                FindingKind::Other => "other",
            };
            let weight = if f.blocking { "blocking" } else { "advisory" };
            let mut s = format!("{}. {} ({kind}, {weight})", f.id, f.title);
            if let Some(e) = &f.evidence {
                let ty = match e.r#type {
                    EvidenceType::Command => "command",
                    EvidenceType::Test => "test",
                    EvidenceType::Criterion => "criterion",
                };
                s.push_str(&format!("\n   Evidence ({ty}): {}", e.detail));
                if let Some(file) = &e.file {
                    s.push_str(&format!(" at {file}"));
                    if let Some(line) = e.line {
                        s.push_str(&format!(":{line}"));
                    }
                }
            }
            s
        })
        .collect::<Vec<_>>()
        .join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;
    use regie_core::{Effort, Evidence, Millis, SessionId, TaskId};

    const ALL: [Template; 8] = [
        Template::BuilderInitial,
        Template::BuilderFix,
        Template::BuilderOpenPr,
        Template::BuilderCiFix,
        Template::BuilderConflictFix,
        Template::BuilderResume,
        Template::Reviewer,
        Template::Verify,
    ];

    fn full_vars() -> PromptVars {
        let names = [
            "title",
            "instruction",
            "tickets",
            "checks",
            "definition_of_done",
            "port_base",
            "model",
            "level",
            "findings",
            "failed_checks",
            "note",
            "round",
        ];
        PromptVars(names.into_iter().map(|n| (n, format!("<{n}>"))).collect())
    }

    fn task() -> Task {
        Task {
            id: TaskId(7),
            session: SessionId(1),
            repo: "/r".into(),
            title: "Add a health endpoint".into(),
            instruction: "Add GET /health returning 200.".into(),
            tickets: vec!["ABC-12".into()],
            blocked_by: vec![],
            level: Level::Standard,
            paths: vec![],
            created_at: Millis(0),
            worktree: None,
            setup_ok: None,
            setup_output: String::new(),
            builder_native_session: None,
            last_builder_status: None,
            pending_messages: vec![],
            reviews: vec![],
            verify: None,
            open_pr_requested: false,
            pr: None,
            pr_missing: false,
            comments: vec![],
            loops: Default::default(),
            fixed_shas: Default::default(),
            cancelled: None,
            pr_closed_by_regie: false,
            cleaned: false,
            last_run_outcome: None,
        }
    }

    fn facts() -> ProjectFacts {
        ProjectFacts {
            setup: Some("make setup".into()),
            build: Some("cargo build".into()),
            test: Some("cargo test".into()),
            lint: None,
            copy_files: vec![],
            definition_of_done: "Tests pass and the endpoint is documented.".into(),
        }
    }

    fn opus() -> ModelChoice {
        ModelChoice { model: "claude-opus-5-5".into(), effort: Effort::Medium }
    }

    fn prompt(input: PromptInput) -> String {
        prompt_for(&task(), &facts(), &input, &opus(), 41010).unwrap()
    }

    fn finding() -> Finding {
        Finding {
            id: 1,
            title: "Panics on empty body".into(),
            kind: FindingKind::Bug,
            blocking: true,
            evidence: Some(Evidence {
                r#type: EvidenceType::Command,
                detail: "curl -X POST localhost/health fails".into(),
                file: Some("src/health.rs".into()),
                line: Some(12),
            }),
        }
    }

    #[test]
    fn templates_render_without_leftovers() {
        for t in ALL {
            let out = render(t, &full_vars()).unwrap_or_else(|e| panic!("{t:?}: {e}"));
            assert!(!out.contains("{{"), "{t:?} left a placeholder:\n{out}");
            assert!(out.len() > 100, "{t:?} is suspiciously short");
        }
    }

    #[test]
    fn missing_var_is_an_error() {
        let mut vars = full_vars();
        vars.0.remove("instruction");
        let err = render(Template::BuilderInitial, &vars).unwrap_err().to_string();
        assert!(err.contains("instruction"), "{err}");
    }

    #[test]
    fn builder_prompt_has_contract_lines() {
        let p = prompt(PromptInput::Initial);
        for needle in [
            "Add GET /health returning 200.",
            "ABC-12",
            "cargo test",
            "Tests pass and the endpoint is documented.",
            "41010",
            "Run the project's checks before you finish.",
            "Regie-Agent: builder, claude, claude-opus-5-5, standard",
            "Do not push and do not open a PR yet: review comes first.",
            "Never merge.",
            "Status: done",
            "Status: question",
        ] {
            assert!(p.contains(needle), "missing {needle:?} in:\n{p}");
        }
    }

    #[test]
    fn every_builder_prompt_has_the_status_contract() {
        for input in [
            PromptInput::Initial,
            PromptInput::Fix { findings: vec![finding()] },
            PromptInput::OpenPr,
            PromptInput::CiFix { failed: vec!["test (ubuntu)".into()] },
            PromptInput::ConflictFix,
            PromptInput::Resume { note: regie_core::RESUME_NOTE.into() },
        ] {
            let p = prompt(input.clone());
            assert!(p.contains("End your final message with one line: `Status: done`"), "{input:?}");
            assert!(p.contains("Never merge") || p.contains("never merge"), "{input:?}");
        }
    }

    #[test]
    fn follow_up_prompts_say_what_to_fix() {
        let p = prompt(PromptInput::Fix { findings: vec![finding()] });
        assert!(p.contains("Panics on empty body") && p.contains("src/health.rs:12"), "{p}");
        assert!(p.contains("run the checks") && p.contains("commit"), "{p}");
        assert!(!p.contains("push this branch"), "{p}");

        let p = prompt(PromptInput::CiFix { failed: vec!["test (ubuntu)".into()] });
        assert!(p.contains("test (ubuntu)") && p.contains("push"), "{p}");

        let p = prompt(PromptInput::ConflictFix);
        assert!(
            p.contains("your branch conflicts with the repo's default branch: fetch, then merge or rebase it"),
            "{p}"
        );
        assert!(p.contains("push"), "{p}");

        let p = prompt(PromptInput::OpenPr);
        assert!(p.contains("Review is closed. Push this branch and open a pull request with `gh pr create`"));
        assert!(p.contains("Add a health endpoint") && p.contains("ABC-12"), "{p}");

        let p = prompt(PromptInput::Resume { note: regie_core::RESUME_NOTE.into() });
        assert!(p.contains(regie_core::RESUME_NOTE), "{p}");
    }

    #[test]
    fn reviewer_prompt_forbids_edits() {
        let p = prompt(PromptInput::Review { round: 2 });
        assert!(p.contains("Do not edit files or commit."), "{p}");
        assert!(p.contains("Add GET /health returning 200."), "{p}");
        assert!(p.contains("Tests pass and the endpoint is documented."), "{p}");
        assert!(p.contains("```json") && p.contains(r#""findings""#), "{p}");
        // The example in the prompt parses with the real parser.
        assert!(matches!(
            regie_core::parse_review(&p),
            regie_core::ReviewOutcome::Findings(f) if f.len() == 1 && f[0].blocking
        ));
        assert!(!p.contains("Status: done"), "{p}");
    }

    #[test]
    fn verify_prompt_lists_findings_and_format() {
        let p = prompt(PromptInput::Verify { findings: vec![finding()] });
        assert!(p.contains("Panics on empty body"), "{p}");
        assert!(p.contains(r#""results""#) && p.contains("not_fixed"), "{p}");
        assert!(matches!(regie_core::parse_verify(&p), regie_core::VerifyOutcome::Results(r) if r.len() == 1));
    }

    #[test]
    fn message_is_returned_as_is() {
        assert_eq!(prompt(PromptInput::Message { text: "Use {{braces}} too.".into() }), "Use {{braces}} too.");
    }
}
