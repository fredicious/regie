//! Régie server and CLI library. Modules are added as they are built.

pub mod git;
pub mod paths;
pub mod store;

/// The crate version, as printed by `regie --version`.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
