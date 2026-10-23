fn main() -> Result<(), Box<dyn std::error::Error>> {
    println!("cargo:rerun-if-changed=proto/platform/v1/agent.proto");
    println!("cargo:rerun-if-changed=platform-proto/platform/v1/remote.proto");
    println!("cargo:rerun-if-changed=platform-proto/platform/v1/software.proto");

    println!("cargo:rerun-if-changed=platform-proto/platform/v1/templates.proto");

    tonic_build::configure()
        .build_server(true)
        .build_client(true)
        .compile_protos(
            &[
                "proto/platform/v1/agent.proto",
                "platform-proto/platform/v1/remote.proto",
                "platform-proto/platform/v1/software.proto",
                "platform-proto/platform/v1/templates.proto",
            ],
            &["proto", "platform-proto"],
        )?;

    Ok(())
}
