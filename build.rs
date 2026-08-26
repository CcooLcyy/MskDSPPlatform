fn main() -> Result<(), Box<dyn std::error::Error>> {
    println!("cargo:rerun-if-changed=proto/platform/v1/agent.proto");

    tonic_build::configure()
        .build_server(true)
        .build_client(true)
        .compile_protos(&["proto/platform/v1/agent.proto"], &["proto"])?;

    Ok(())
}
