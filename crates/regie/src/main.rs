use clap::Parser;

#[tokio::main]
async fn main() {
    let cli = regie::cli::Cli::parse();
    if let Err(e) = regie::cli::run(cli).await {
        eprintln!("regie: {e:#}");
        std::process::exit(1);
    }
}
