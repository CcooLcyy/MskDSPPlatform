mod download;
pub mod grpc;
pub mod http;
pub mod models;
pub mod store;

const TESTBENCH_PRODUCT: &str = "mskdsp-testbench";

#[cfg(test)]
mod integration_tests;

#[cfg(test)]
mod download_integration_tests;
