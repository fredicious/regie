fn main() {
    // `web/dist` is embedded into the binary; tell cargo it is an input so `make build` re-embeds it.
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed=../../web/dist");
}
