//! Port blocks: each task gets its own block of ports.

use std::collections::BTreeSet;

use crate::settings::Settings;

/// Lowest `port_base + 10 * k`, `k < port_blocks`, that is not in use.
pub fn allocate_port_base(in_use: &BTreeSet<u16>, s: &Settings) -> Option<u16> {
    (0..s.port_blocks)
        .filter_map(|k| s.port_base.checked_add(k.checked_mul(10)?))
        .find(|base| !in_use.contains(base))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ports_lowest_free_block() {
        let s = Settings::default();
        assert_eq!(allocate_port_base(&BTreeSet::new(), &s), Some(41000));
        let used: BTreeSet<u16> = [41000, 41010].into();
        assert_eq!(allocate_port_base(&used, &s), Some(41020));
        let gap: BTreeSet<u16> = [41000, 41020].into();
        assert_eq!(allocate_port_base(&gap, &s), Some(41010));
        let all: BTreeSet<u16> = (0..200).map(|k| 41000 + 10 * k).collect();
        assert_eq!(allocate_port_base(&all, &s), None);
    }
}
