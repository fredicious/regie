//! Keeps the Mac awake (`caffeinate -i`) while any agent runs.

use std::process::{Child, Command, Stdio};

pub struct Caffeinate {
    enabled: bool,
    child: Option<Child>,
}

impl Caffeinate {
    /// A disabled one never starts anything (tests).
    pub fn new(enabled: bool) -> Caffeinate {
        Caffeinate { enabled, child: None }
    }

    /// Starts `caffeinate -i -w <own pid>` when `n` goes above 0 and stops it at 0.
    /// `-w` makes it exit with Régie even if Régie never gets to stop it.
    pub fn set_running(&mut self, n: usize) {
        if !self.enabled {
            return;
        }
        if n > 0 && self.child.is_none() {
            self.child = Command::new("caffeinate")
                .args(["-i", "-w", &std::process::id().to_string()])
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .ok();
        } else if n == 0 {
            self.stop();
        }
    }

    pub fn is_held(&self) -> bool {
        self.child.is_some()
    }

    fn stop(&mut self) {
        if let Some(mut c) = self.child.take() {
            let _ = c.kill();
            let _ = c.wait();
        }
    }
}

impl Drop for Caffeinate {
    fn drop(&mut self) {
        self.stop();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn disabled_never_holds() {
        let mut c = Caffeinate::new(false);
        c.set_running(2);
        assert!(!c.is_held());
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn held_while_running() {
        let mut c = Caffeinate::new(true);
        c.set_running(1);
        assert!(c.is_held());
        c.set_running(3);
        assert!(c.is_held());
        c.set_running(0);
        assert!(!c.is_held());
    }
}
