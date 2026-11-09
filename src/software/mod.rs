mod download;
pub mod grpc;
pub mod http;
pub mod models;
pub mod store;

const TESTBENCH_PRODUCT: &str = "mskdsp-testbench";

fn testbench_channel(channel: &str) -> Option<&'static str> {
    match channel {
        "ci" => Some("ci"),
        "" | "stable" => Some("stable"),
        _ => None,
    }
}

#[cfg(test)]
mod integration_tests;

#[cfg(test)]
mod download_integration_tests;
