//! Git worktrees for tasks: create, set up, remove. Never touches the user's
//! working copy, and never writes to the remote.
mod locks;
mod worktree;

pub use locks::RepoLocks;
pub use worktree::{
    branch_name, create_worktree, default_branch, github_remote, remove_worktree, run_setup, slug, task_env,
};
