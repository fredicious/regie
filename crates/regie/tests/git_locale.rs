//! Git runs with `LC_ALL=C` whatever the user's locale (a French git translates
//! `HEAD branch:`), and finds the default branch without `origin/HEAD`.
//! The only test in this binary: it changes the process environment.
mod support;

use std::os::unix::fs::PermissionsExt;

use regie::git::default_branch;
use support::repo::{git, TempRepo};

#[tokio::test]
async fn default_branch_fallback_in_a_french_locale() {
    let r = TempRepo::new();
    git(
        &r.work,
        &["symbolic-ref", "--delete", "refs/remotes/origin/HEAD"],
    );
    // A `git` first on PATH that refuses to run unless the locale is C.
    let real = std::process::Command::new("sh")
        .args(["-c", "command -v git"])
        .output()
        .unwrap();
    let real = String::from_utf8(real.stdout).unwrap().trim().to_string();
    let shim_dir = tempfile::tempdir().unwrap();
    let shim = shim_dir.path().join("git");
    std::fs::write(
        &shim,
        format!(
            "#!/bin/sh\nif [ \"$LC_ALL\" != C ]; then echo \"git run with LC_ALL=$LC_ALL\" >&2; exit 97; fi\nexec '{real}' \"$@\"\n"
        ),
    )
    .unwrap();
    std::fs::set_permissions(&shim, std::fs::Permissions::from_mode(0o755)).unwrap();
    let path = std::env::var("PATH").unwrap_or_default();
    std::env::set_var("PATH", format!("{}:{path}", shim_dir.path().display()));
    std::env::set_var("LC_ALL", "fr_FR.UTF-8");
    std::env::set_var("LANG", "fr_FR.UTF-8");
    std::env::set_var("LANGUAGE", "fr");
    assert_eq!(default_branch(&r.work).await.unwrap(), "main");
}
