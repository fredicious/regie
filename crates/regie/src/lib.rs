//! Régie server and CLI library. Modules are added as they are built.

pub mod agents;
pub mod bus;
pub mod caffeinate;
pub mod engine;
pub mod git;
pub mod github;
pub mod paths;
pub mod server;
pub mod store;
pub mod usage;

/// The crate version, as printed by `regie --version`.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
