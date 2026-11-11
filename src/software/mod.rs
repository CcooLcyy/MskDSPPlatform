mod download;
pub mod grpc;
pub mod http;
pub mod models;
pub mod store;

const TESTBENCH_PRODUCT: &str = "mskdsp-testbench";
const TESTBENCH_CASES_PRODUCT: &str = "mskdsp-testbench-cases";
const TESTBENCH_CASES_PLATFORM: &str = "python-zip";

fn uses_scoped_publisher(product: &str) -> bool {
    matches!(product, TESTBENCH_PRODUCT | TESTBENCH_CASES_PRODUCT)
}

fn scoped_package_channel(product: &str, channel: &str) -> Option<&'static str> {
    match (product, channel) {
        (TESTBENCH_PRODUCT | TESTBENCH_CASES_PRODUCT, "ci") => Some("ci"),
        (TESTBENCH_PRODUCT | TESTBENCH_CASES_PRODUCT, "stable") | (TESTBENCH_PRODUCT, "") => {
            Some("stable")
        }
        _ => None,
    }
}

#[cfg(test)]
mod integration_tests;

#[cfg(test)]
mod download_integration_tests;
